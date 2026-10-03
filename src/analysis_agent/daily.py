"""Generic chronological decisions; no domain selection predicates or private schema."""
from typing import Literal
from pydantic import Field
from sqlalchemy import insert, select, update
from .contracts import Contract, WorkbenchError, now, digest
from .storage import daily_sessions, decisions, decision_reviews, attempts


class ReviewItem(Contract):
    entity: str = Field(min_length=1)
    reason: str = Field(min_length=1,max_length=6000)
    evidence_ids: list[str] = Field(default_factory=list)


class Selection(Contract):
    entity: str = Field(min_length=1)
    state: Literal['new_entry','continue','exit','unknown','right_censored']
    reason: str = Field(min_length=1,max_length=6000)


class Decision(Contract):
    schema_version: Literal['decision@1']='decision@1'
    day: str
    reviewed: list[ReviewItem]
    selections: list[Selection]
    stopping_reason: str = Field(min_length=1,max_length=6000)


def admit(request,adapter):
    config=request.daily_protocol
    if not adapter.capabilities().get('daily_protocol'):
        raise WorkbenchError('unsupported','This adapter does not implement daily cutoff semantics',422)
    if request.parameters.get(config.cutoff_parameter)!=config.dates[0]:
        raise WorkbenchError('invalid_parameters','Initial task cutoff must equal first protocol date',422)
    for day in config.dates:adapter.validate_task({**request.parameters,config.cutoff_parameter:day})


def session(c,run):
    return c.execute(select(daily_sessions).where(daily_sessions.c.run_id==run['id'])).mappings().one()


def effective_parameters(w,run):
    parameters=run['manifest']['request']['parameters']
    if not run['manifest']['request'].get('daily_protocol'):return parameters
    with w.store.engine.connect() as c:s=session(c,run)
    config=s['config'];index=min(s['position'],len(config['dates'])-1)
    return {**parameters,config['cutoff_parameter']:config['dates'][index]}


def require_complete(c,run):
    s=session(c,run)
    if s['position']!=len(s['config']['dates']):raise WorkbenchError('daily_decision_required','Complete each daily decision before publishing report',409)


def record(w,run_id):
    with w.store.engine.connect() as c:
        run=w._run(c,run_id);s=session(c,run)
        rows=c.execute(select(decisions).where(decisions.c.run_id==run_id).order_by(decisions.c.day)).mappings().all()
        reviews=c.execute(select(decision_reviews).where(decision_reviews.c.run_id==run_id).order_by(decision_reviews.c.created_at)).mappings().all()
    done=s['position']==len(s['config']['dates'])
    return {'current_date':None if done else s['config']['dates'][s['position']], 'complete':done,
        'review_only':False if done else s['config']['dates'][s['position']] in s['config']['review_only'],
        'decisions':[w.objects.get(r['object_hash']) for r in rows],
        'reviews':[{'day':r['day'],**w.objects.get(r['object_hash'])} for r in reviews]}


def submit(w,run_id,value,*,lease=None):
    w._writable();decision=Decision.model_validate(value);body=decision.model_dump(mode='json')
    with w.store.engine.begin() as c:
        run=w._run(c,run_id,lock=True);w._assert_submit(c,run,lease)
        if not run['manifest']['request'].get('daily_protocol'):raise WorkbenchError('unsupported','Run has no daily protocol',422)
        previous=c.execute(select(decisions).where(decisions.c.run_id==run_id,decisions.c.day==decision.day)).mappings().first()
        if previous:
            if digest(w.objects.get(previous['object_hash']))!=digest(body):raise WorkbenchError('immutable_decision','Submitted decisions cannot be replaced',409)
            current=session(c,run);complete=current['position']==len(current['config']['dates'])
            return {'day':decision.day,'accepted':True,'replayed':True,'complete':complete,'current_date':None if complete else current['config']['dates'][current['position']], 'next_action':'report' if complete else 'query_current_date'}
        s=session(c,run);config=s['config'];position=s['position']
        if position==len(config['dates']) or decision.day!=config['dates'][position]:raise WorkbenchError('out_of_scope','Decision must match current visible date',403)
        if c.execute(select(attempts.c.id).where(attempts.c.run_id==run_id,attempts.c.state.in_(['DISPATCHED','UNKNOWN'])).limit(1)).first():
            raise WorkbenchError('unresolved_attempt','Resolve outstanding tool activity before advancing',409)
        if len({r.entity for r in decision.reviewed})!=len(decision.reviewed) or len({r.entity for r in decision.selections})!=len(decision.selections):
            raise WorkbenchError('duplicate_entity','Decision entity lists must be unique',422)
        if not {r.entity for r in decision.selections}<={r.entity for r in decision.reviewed}:raise WorkbenchError('review_required','Every selected entity needs a review reason',422)
        if decision.day in config['review_only'] and any(r.state=='new_entry' for r in decision.selections):raise WorkbenchError('review_only','New entry is not allowed on a review-only date',403)
        from .storage import evidence
        rows=c.execute(select(evidence.c.id,evidence.c.object_hash).where(evidence.c.run_id==run_id)).mappings().all()
        valid={r['id'] for r in rows}
        if not any(w.objects.get(r['object_hash']).get('time_range',{}).get('end')==decision.day for r in rows):
            raise WorkbenchError('daily_evidence_required','Query evidence ending on the current date before submitting its decision; an empty query result may justify unknown',409)
        if any(e not in valid for r in decision.reviewed for e in r.evidence_ids):raise WorkbenchError('foreign_evidence','Decision evidence must belong to this run',403)
        # Generic chronology only: no score/volume/price-based grading here.
        object_hash=w.objects.put(body)
        c.execute(insert(decisions).values(run_id=run_id,day=decision.day,object_hash=object_hash,created_at=now()))
        c.execute(update(daily_sessions).where(daily_sessions.c.run_id==run_id).values(position=position+1))
        w.store.event(c,run_id,'DAILY_DECISION_ACCEPTED',{'day':decision.day,'object_hash':object_hash,'next_date':config['dates'][position+1] if position+1<len(config['dates']) else None})
        w._checkpoint('daily_after_insert_before_commit',run_id=run_id,day=decision.day)
    return {'day':decision.day,'accepted':True,'complete':position+1==len(config['dates']),'current_date':config['dates'][position+1] if position+1<len(config['dates']) else None,'next_action':'report' if position+1==len(config['dates']) else 'query_current_date'}


def review(w,run_id,day,text,*,lease=None):
    from .runtime import new_id
    w._writable()
    if not isinstance(text,str) or not 1<=len(text.strip())<=6000:raise WorkbenchError('invalid_review','A bounded review explanation is required')
    with w.store.engine.begin() as c:
        run=w._run(c,run_id,lock=True);w._assert_submit(c,run,lease)
        if not c.execute(select(decisions).where(decisions.c.run_id==run_id,decisions.c.day==day)).first():raise WorkbenchError('not_found','Decision not found',404)
        key=new_id('review');body={'text':text,'actor':w.actor,'created_at':now()};object_hash=w.objects.put(body)
        c.execute(insert(decision_reviews).values(id=key,run_id=run_id,day=day,object_hash=object_hash,created_at=now()))
        w.store.event(c,run_id,'DECISION_REVIEW_APPENDED',{'day':day,'object_hash':object_hash})
    return {'review_id':key,'day':day}
