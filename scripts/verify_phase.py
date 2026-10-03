#!/usr/bin/env python3
"""Fail-closed phase evidence verifier. Never connects to a database or a provider."""
from pathlib import Path
import argparse
import hashlib
import json


def checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def inventory(roots):
    result={}
    excluded={'.git','__pycache__','.pytest_cache','.venv','node_modules','dist','objects','.test-tmp','snapshots','playwright-report','test-results'}
    allowed={'.py','.json','.toml','.lock','.ts','.tsx','.js','.mjs','.css','.html','.md'}
    for root in map(Path,roots):
        for file in root.rglob('*'):
            if not file.is_file() or any(part in excluded for part in file.relative_to(root).parts):continue
            rel=file.relative_to(root)
            if rel.parts[0]=='docs' or file.name.startswith('.env') or file.suffix=='.url':continue
            if file.suffix in allowed or file.name in {'requirements.lock'}:
                result[str(file.resolve())]=checksum(file)
    return result


def verify(manifest,records,protected):
    errors=[]
    actual=inventory(manifest['source_roots'])
    if actual!=manifest['files']:errors.append('sealed_tree_changed')
    if canonical_hash(manifest['files'])!=manifest['source_sha256']:errors.append('manifest_digest_mismatch')
    for filename,value in protected['files'].items():
        if not Path(filename).is_file() or checksum(filename)!=value:errors.append('protected_file_changed:'+filename)
    for folder,files in protected.get('directory_files',{}).items():
        present=sorted(str(p.resolve()) for p in Path(folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
        if present!=files:errors.append('protected_directory_changed:'+folder)
    acceptance=Path(manifest['acceptance_file'])
    cases=json.loads(acceptance.read_text())['cases']
    if len(cases)!=58 or any(x['status']!='NOT RUN' for x in cases):errors.append('acceptance_status_changed')
    groups={r['group']:r for r in records}
    if len(groups)!=len(records) or set(groups)!=set(manifest['required_groups']):errors.append('test_group_set_mismatch')
    for group,record in groups.items():
        if record.get('status')!='PASSED' or record.get('exit_code')!=0:errors.append('test_failed:'+group)
        if record.get('source_sha256')!=manifest['source_sha256']:errors.append('test_source_mismatch:'+group)
        if record.get('started_at','')<manifest['sealed_at']:errors.append('test_precedes_seal:'+group)
        output=Path(record['output_file'])
        if not output.is_file() or checksum(output)!=record['output_sha256']:errors.append('output_hash_mismatch:'+group)
        if record.get('skipped',0):errors.append('test_skipped:'+group)
    for folder in manifest['test_instance_directories']:
        if Path(folder).exists():errors.append('test_instance_not_cleaned:'+folder)
    return {'status':'FAILED' if errors else 'PASSED','phase':manifest['phase'],
            'source_sha256':manifest['source_sha256'],'errors':errors,
            'groups_checked':sorted(groups),'full_acceptance_changed':False,
            'scope':'Implementer self-check, not independent acceptance'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest',required=True);parser.add_argument('--records-dir',required=True)
    parser.add_argument('--protected-manifest',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    manifest=json.loads(Path(args.manifest).read_text())
    protected=json.loads(Path(args.protected_manifest).read_text())
    records=[json.loads(p.read_text()) for p in sorted(Path(args.records_dir).glob('*-command.json'))]
    result=verify(manifest,records,protected)
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result));return int(result['status']!='PASSED')

if __name__=='__main__':raise SystemExit(main())
