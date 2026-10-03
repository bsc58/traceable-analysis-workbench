"""Named append-only operation. Saved source reports/runs/events are immutable."""
from pathlib import Path
import uuid
from sqlalchemy import insert, text
from .access import check_metadata_access
from .contracts import Report, WorkbenchError, now
from .storage import Objects, Store, revalidations
from .validation import validate_report
from .path_policy import runtime_path


def revalidate(source, target_store, target_objects, run_id, validator):
    if validator != 'cell_validator@2':
        raise WorkbenchError('unsupported_validator', 'Only cell_validator@2 is registered', 422)
    target_store.check_version()
    if target_objects.store_class != source.objects.store_class:
        raise WorkbenchError('store_class_mismatch', 'Revalidation must retain source privacy', 403)
    record=source.reopen(run_id)
    if record['run']['state'] != 'SUCCEEDED' or not record['report']:
        raise WorkbenchError('report_not_final', 'Revalidation requires a published terminal report', 409)
    report=Report.model_validate(record['report']['report'])
    request=record['run']['manifest']['request']
    all_failed=bool(record['attempts']) and all(a['state'] in {'FAILED','REJECTED_AFTER_EXECUTION','RESOLVED_FAILED'} for a in record['attempts'])
    validation=validate_report(report,lambda key:source.read_evidence(run_id,key),
        request.get('required_fact_columns',[]),result_spec=request.get('result_spec'),catalog=record['catalog'],
        allow_empty_insufficient=record['run']['tool_calls']==0 or all_failed)
    if all_failed and report.result_status=='insufficient_evidence' and not report.facts:
        validation['required_fact_coverage']['exception']='all_attempts_failed_insufficient_evidence'
        validation['failed_attempt_ids']=sorted(a['id'] for a in record['attempts'])
    with source.store.engine.connect() as c:
        database=c.execute(text('SELECT current_database()')).scalar_one()
    row={'id':'revalidation_'+uuid.uuid4().hex,'run_id':run_id,'source_database':database,
         'original_report_hash':record['run']['report_hash'],'validator':validator,'created_at':now()}
    result={'schema_version':'revalidation@1',**row,'validation':validation,
            'original_validation_status':record['report']['validation']['status'],
            'source_queries_executed':0,'source_records_changed':False}
    row['result_hash']=target_objects.put(result)
    with target_store.engine.begin() as c:c.execute(insert(revalidations).values(**row))
    return {**row,'validation':validation,'original_validation_status':result['original_validation_status']}


def revalidate_command(source,args):
    privacy=source.objects.store_class
    if privacy not in {'public','private'}:
        raise WorkbenchError('store_class_missing','Classified object stores are required',403)
    Objects.check_class(Path(args.target_objects),privacy)
    target=Store(runtime_path(args.target_db_url_file).read_text().strip())
    try:
        check_metadata_access(target,f'workbench_{privacy}',f'aw_app_{privacy}')
        return revalidate(source,target,Objects(Path(args.target_objects)),args.run,args.validator)
    finally:target.engine.dispose()
