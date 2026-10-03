"""Independent seeded property oracles and P4.0 contract regression tests."""
import ast
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import random

import pytest
from sqlalchemy import event, select
from analysis_agent.contracts import RequiredFact, ResultSpec, Report, digest
from analysis_agent.demo import CommerceAdapter
from analysis_agent.storage import access_refusals
from analysis_agent.validation import arithmetic, InvalidFact, validate_report
from test_runtime import harness, real_store, assert_error, valid_report


def independent_round(value, places):
    # Exact rational arithmetic + integer tie-away-from-zero; no Decimal quantize.
    scaled = value * 10**places
    sign = -1 if scaled < 0 else 1
    quotient, remainder = divmod(abs(scaled.numerator), scaled.denominator)
    return Decimal(sign * (quotient + (2 * remainder >= scaled.denominator))).scaleb(-places)


@pytest.mark.parametrize('operation',['subtract','ratio','pct_change','pp_change'])
def test_seeded_arithmetic_against_exact_rational_oracle(operation):
    rng=random.Random(402026)
    unit='ratio' if operation=='pp_change' else 'count'
    catalog={'derivation_policy':{'operations':{operation:{'input_units':[unit],
        'decimal_places':8,'rounding':'ROUND_HALF_UP','tolerance':'0'}}}}
    for _ in range(350):
        x,y=(rng.randint(-1000000,1000000) for _ in range(2))
        if not y:y=1
        a,b=Fraction(x,100),Fraction(y,100)
        expected=(a-b if operation in {'subtract','pp_change'} else a/b if operation=='ratio' else (a-b)/b)
        actual,out_unit,_=arithmetic(operation,[str(Decimal(x)/100),str(Decimal(y)/100)],[unit,unit],catalog)
        assert actual==independent_round(expected,8)
        assert out_unit==('ratio_point' if operation=='pp_change' else unit if operation=='subtract' else 'ratio')


@pytest.mark.parametrize('unit',['ratio','percent'])
def test_ratio_subtraction_forbidden_even_when_catalog_registers_it(unit):
    policy={'derivation_policy':{'operations':{'subtract':{'input_units':[unit]}}}}
    with pytest.raises(InvalidFact,match='ratio_difference_requires_pp_change'):
        arithmetic('subtract',[1,2],[unit,unit],policy)


def test_seeded_requirement_coverage_against_set_membership_oracle():
    rng=random.Random(403026)
    universe=[(e,d,c) for e in ['a','b','c'] for d in ['2026-01-01','2026-01-02'] for c in ['requests','errors']]
    for _ in range(180):
        required=set(rng.sample(universe,rng.randint(1,len(universe))))
        observed=set(rng.sample(universe,rng.randint(1,len(universe))))
        rows=[];facts=[]
        for e,d,c in sorted(observed):
            i=len(rows);rows.append({'entity':e,'day':d,c:11})
            facts.append({'schema_version':'fact@2','kind':'observation','operation':'cell',
                'inputs':[{'evidence_id':'ev','row':i,'column':c,'entity':{'entity':e},'time_range':{'date':d}}],
                'value':11,'unit':'count'})
        item={'rows':rows,'units':{'requests':'count','errors':'count'},'entity_keys':['entity'],
              'time_range':{},'completeness':'complete_for_query','truncated':False}
        spec=ResultSpec(requirements=[RequiredFact(column=c,entity={'entity':e},time=d) for e,d,c in sorted(required)])
        report=Report(result_status='complete',title='Synthetic',facts=facts)
        result=validate_report(report,lambda _:item,result_spec=spec,catalog={'reference_time':{'date_column':'day'}})
        assert [r['status']=='covered' for r in result['requirement_coverage']]==[r in observed for r in sorted(required)]
        assert (result['status']=='valid')==(required<=observed)


@pytest.mark.parametrize('spec',[None,ResultSpec(requirements=[])])
def test_new_run_requires_nonempty_contract(harness,spec):
    assert_error('result_contract_required',harness.create,result_spec=spec)


def test_insufficient_with_accepted_evidence_and_missing_requirement_is_warning(harness):
    run=harness.create(result_spec=ResultSpec(requirements=[RequiredFact(column='errors')]))
    item=harness.call(run)
    report=valid_report(item).model_copy(update={'result_status':'insufficient_evidence'})
    result=harness.workbench.finalize(run,report)['validation']
    assert result['status']=='warning'
    assert 'requirement_uncovered' in [w['code'] for w in result['warnings']]


def test_refusals_do_not_mutate_existing_events(harness):
    h=harness;h.adapter.knowledge_cutoff=lambda p:p['last_day'];h.adapter.validate_task=lambda p:None
    h.workbench.project_policies={h.project:{'as_of_guard':True}}
    later=h.create();earlier=h.create(parameters={**h.request.parameters,'last_day':'2026-01-02'})
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement.upper())
    event.listen(h.store.engine,'before_cursor_execute',capture)
    try:
        assert_error('cross_run_read_refused',h.workbench.reopen,later)
        first=h.event_rows(earlier)
        for _ in range(6):assert_error('cross_run_read_refused',h.workbench.reopen,later)
        assert first==h.event_rows(earlier)
        with h.store.engine.connect() as c:
            assert c.execute(select(access_refusals.c.count).where(access_refusals.c.run_id==earlier)).scalar_one()==7
        assert not any(s.startswith(('UPDATE AW_EVENTS','DELETE FROM AW_EVENTS')) for s in statements)
    finally:event.remove(h.store.engine,'before_cursor_execute',capture)


def test_no_production_event_update_or_delete_path():
    for path in (Path(__file__).resolve().parents[1]/'src/analysis_agent').rglob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            if isinstance(node.func,ast.Name) and node.func.id in {'update','delete'}:
                assert not (node.args and isinstance(node.args[0],ast.Name) and node.args[0].id=='events')
            if isinstance(node.func,ast.Attribute) and node.func.attr in {'update','delete'}:
                assert not (isinstance(node.func.value,ast.Name) and node.func.value.id=='events')


def test_catalog_reference_is_full_content_hash_and_detects_changes():
    a=CommerceAdapter();body=dict(a.catalog());body.pop('source_ref')
    assert a.ref=='src_'+digest(body)+'@3'
    old=a.ref;a._orders=(*a._orders,dict(a._orders[0],order_id='new'))
    assert a.ref!=old
    assert a.ref!=CommerceAdapter('normal').ref
