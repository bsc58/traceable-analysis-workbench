"""Offline only: reads completed exports and answer files; never imported by runtime."""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


def grade(export, answers, spec):
    if export.get('run',{}).get('state')!='SUCCEEDED' or not export.get('report'):
        return [{'question':key,'status':'FAILED','error':'incomplete_export'} for key in answers]
    facts=export['report']['validation'].get('normalized_facts',[])
    results=[]
    for key, answer in answers.items():
        matches=[]
        for fact in facts:
            inputs=fact.get('inputs',[])
            selectors=[{'column':v['column'],'entity':v['entity'],'time':v['time']} for v in inputs]
            if fact.get('operation')==answer['operation'] and selectors==answer['inputs']:matches.append(fact)
        code=None
        if len(matches)!=1:code='missing_answer' if not matches else 'ambiguous_answer'
        elif matches[0]['unit']!=answer['unit']:code='wrong_unit'
        else:
            try:
                raw=matches[0]['value']
                value=Decimal(str(raw))
                if isinstance(raw,bool) or not value.is_finite():raise ValueError()
                if abs(value-Decimal(answer['value']))>Decimal(spec['tolerance']):code='wrong_value'
            except (ValueError,InvalidOperation):code='invalid_number'
        results.append({'question':key,'status':'FAILED' if code else 'PASSED','error':code})
    return results


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--export',required=True);parser.add_argument('--answers',required=True)
    args=parser.parse_args()
    result=grade(json.loads(Path(args.export).read_text()),json.loads(Path(args.answers).read_text()),
                 json.loads(Path(__file__).with_name('grader_spec.json').read_text()))
    print(json.dumps({'operation':'offline_fixture_grading','items':result,'model_quality_score':None},indent=2))

if __name__=='__main__':main()
