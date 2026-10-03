"""Cell validator v2: registered structure/arithmetic, never prose or judgment."""
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
from .contracts import Report, ResultSpec, FactV2, canonical

ROUNDINGS={'ROUND_HALF_UP':ROUND_HALF_UP,'ROUND_HALF_EVEN':ROUND_HALF_EVEN,'ROUND_DOWN':ROUND_DOWN}
SCOPE='结构化事实的引用、实体、时间、单位、数值和登记计算；文字未校验'

class InvalidFact(ValueError):pass

def same(a,b):return canonical(a)==canonical(b)
def number(value,which):
    if value is None:raise InvalidFact(which+'_null')
    if isinstance(value,bool):raise InvalidFact(which+'_boolean')
    try:value=Decimal(str(value))
    except (InvalidOperation,ValueError):raise InvalidFact(which+'_non_numeric') from None
    if not value.is_finite():raise InvalidFact(which+'_non_finite')
    return value

def row_time(item,row,catalog):
    config=catalog.get('reference_time',{})
    if config.get('date_column'):
        return {'date':str(row[config['date_column']])}
    if config.get('selector'):
        window=config['windows'][row[config['selector']]]
        return {k:item['time_range'][v] for k,v in window.items()}
    return item['time_range']

def arithmetic(operation,values,units,catalog):
    policy=catalog.get('derivation_policy',{})
    rule=policy.get('operations',{}).get(operation)
    if not rule:raise InvalidFact('unregistered_operation')
    if len(set(units))!=1 or units[0] not in rule.get('input_units',[]):raise InvalidFact('incompatible_units')
    if operation=='subtract' and units[0] in {'ratio','percent'}:raise InvalidFact('ratio_difference_requires_pp_change')
    unit=units[0] if operation=='subtract' else 'ratio'
    if operation=='pp_change':
        if units[0] not in {'ratio','percent'}:raise InvalidFact('incompatible_units')
        unit={'ratio':'ratio_point','percent':'percent_point'}[units[0]]
    a=number(values[0],'input'); b=number(values[1],'denominator' if operation in {'ratio','pct_change'} else 'input')
    if operation in {'ratio','pct_change'} and b==0:raise InvalidFact('zero_denominator')
    rounding=rule.get('rounding'); places=rule.get('decimal_places'); tolerance=number(rule.get('tolerance'),'tolerance')
    if rounding not in ROUNDINGS or type(places)is not int or not 0<=places<=18 or tolerance<0:raise InvalidFact('invalid_derivation_policy')
    with localcontext() as ctx:
        ctx.prec=50
        value={'subtract':lambda:a-b,'ratio':lambda:a/b,'pct_change':lambda:(a-b)/b,'pp_change':lambda:a-b}[operation]()
        value=value.quantize(Decimal(1).scaleb(-places),rounding=ROUNDINGS[rounding])
    return value,unit,tolerance

def validate_report(report: Report,get_evidence,required_columns=None,*,allow_empty_insufficient=False,result_spec=None,catalog=None):
    catalog=catalog or {}; errors=[];warnings=[];checked=[];observations=[];labels=[];normalized=[]
    for index,fact in enumerate(report.facts):
        refs=fact.inputs if isinstance(fact,FactV2) else fact.refs
        note=fact.note if isinstance(fact,FactV2) else fact.label
        if note:warnings.append({'fact':index,'code':'unverified_text','message':'文字未校验'})
        values=[];units=[];details=[]
        try:
            for ref in refs:
                try:e=get_evidence(ref.evidence_id)
                except Exception:raise InvalidFact('invalid_evidence_reference') from None
                try:row=e['rows'][ref.row]
                except (KeyError,IndexError):raise InvalidFact('invalid_evidence_reference') from None
                if ref.column not in row or ref.column not in e['units']:raise InvalidFact('unknown_column_or_unit')
                entity=ref.entity if isinstance(fact,FactV2) else fact.entity
                time=ref.time_range if isinstance(fact,FactV2) else fact.time_range
                expected_entity={k:row[k] for k in e['entity_keys']}
                expected_time=row_time(e,row,catalog) if isinstance(fact,FactV2) else e['time_range']
                if not same(entity,expected_entity):raise InvalidFact('wrong_entity')
                if not same(time,expected_time):raise InvalidFact('wrong_time_range')
                values.append(row[ref.column]); units.append(e['units'][ref.column])
                details.append({'column':ref.column,'entity':entity,'time':row_time(e,row,catalog),
                    'legacy_time':time,'unit':e['units'][ref.column],
                    'bounded':bool(e.get('truncated')) or e.get('completeness')!='complete_for_query'})
            if fact.operation=='cell':
                if len(values)!=1 or fact.kind!='observation':raise InvalidFact('cell_requires_one_observation')
                value=values[0];unit=units[0]
                if type(value)is not type(fact.value) or not same(value,fact.value):raise InvalidFact('value_mismatch')
            else:
                if len(values)!=2 or fact.kind!='derived':raise InvalidFact('derivation_requires_two_inputs')
                value,unit,tolerance=arithmetic(fact.operation,values,units,catalog)
                reported=number(fact.value,'result')
                if abs(reported-value)>tolerance:raise InvalidFact('value_mismatch')
            if fact.unit!=unit:raise InvalidFact('unit_mismatch')
            checked.append(index)
            # A derived number does not itself answer the underlying raw input cells.
            if fact.operation=='cell':observations.extend({**d,'fact':index} for d in details)
            label=' | '.join(f"{d['entity']} · {d['column']} [{d['unit']}] · {d['time']}" for d in details)
            if fact.operation!='cell':label=f"{fact.operation}({label}) → [{unit}]"
            labels.append({'fact':index,'label':label})
            normalized.append({'fact':index,'label':label,'value':fact.value,'unit':fact.unit,'operation':fact.operation,'inputs':details,'note':note})
        except (InvalidFact,KeyError,TypeError,ValueError,InvalidOperation,OverflowError) as exc:
            code=str(exc) if isinstance(exc,InvalidFact) else 'invalid_fact_structure'
            errors.append({'fact':index,'code':code})
    spec=ResultSpec.model_validate(result_spec) if isinstance(result_spec,dict) else result_spec
    requirements=spec.requirements if spec else []
    coverage=[]
    for index,req in enumerate(requirements):
        def matches(o):
            if o['column']!=req.column or any(k not in o['entity'] or not same(o['entity'][k],v) for k,v in req.entity.items()):return False
            if req.time is None:return True
            if isinstance(req.time,str):return o['time']=={'date':req.time} or o['time'].get('start')==o['time'].get('end')==req.time
            return same(o['time'],req.time)
        candidates=[o for o in observations if matches(o)]
        accepted=[o for o in candidates if req.allow_bounded or not o['bounded']]
        coverage.append({'requirement':index,'spec':req.model_dump(),'status':'covered' if accepted else 'covered_by_bounded_only' if candidates else 'uncovered',
                         'fact_indices':sorted({o['fact'] for o in accepted})})
    missing=[x['requirement'] for x in coverage if x['status']!='covered']
    waived=allow_empty_insufficient and report.result_status=='insufficient_evidence' and not report.facts
    # Legacy-only compatibility requirements cannot establish entity/time completeness.
    covered_columns={o['column'] for o in observations}; legacy_missing=sorted(set(required_columns or [])-covered_columns)
    if report.result_status=='complete' and (missing or legacy_missing):
        errors.append({'code':'complete_requirement_uncovered','requirements':missing,'legacy_columns':legacy_missing})
    elif (missing or legacy_missing) and not waived:
        warnings.append({'code':'requirement_uncovered','requirements':missing,'legacy_columns':legacy_missing})
    if report.result_status=='complete' and not report.facts:errors.append({'code':'complete_report_needs_facts'})
    if report.result_status=='complete':
        allowed={f for c in coverage if c['status']=='covered' and c['spec']['allow_bounded'] for f in c['fact_indices']}
        if any(n['fact'] not in allowed and any(d['bounded'] for d in n['inputs']) for n in normalized):
            errors.append({'code':'complete_uses_bounded_evidence'})
    elif any(any(d['bounded'] for d in n['inputs']) for n in normalized):warnings.append({'code':'bounded_evidence'})
    if report.result_status=='partial':warnings.append({'code':'partial_result'})
    if any((report.hypotheses,report.limitations,report.next_checks)):warnings.append({'code':'unverified_text','message':'文字未校验'})
    legacy_coverage={'required':required_columns or [],'covered':sorted(covered_columns)}
    if waived:legacy_coverage['exception']='zero_tool_insufficient_evidence'
    return {'schema_version':'cell_validator@2','status':'blocked' if errors else 'warning' if warnings else 'valid',
            'verified_fact_indices':checked,'errors':errors,'warnings':warnings,'scope':SCOPE,
            'required_fact_coverage':legacy_coverage,'requirement_coverage':coverage,'generated_labels':labels,'normalized_facts':normalized}
