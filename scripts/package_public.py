"""Explicit public allowlist; copy into a new directory without development history."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import zipfile

ROOT_FILES={'README.md','pyproject.toml','requirements.lock','.gitignore','Dockerfile','docker-compose.yml'}
EVAL_FILES={'eval/b2/generate.py','eval/b2/harness.py','eval/b2/grader.py','eval/b2/metrics.py','eval/fixture_config.json','eval/grader_spec.json','eval/offline_grader.py','eval/p3_validator_cases.json',
            'eval/service_ops/task_family.json','eval/service_ops/grader_spec.json',
            'eval/data_quality/task_family.json','eval/data_quality/grader_spec.json'}
SCRIPT_FILES={'scripts/local_postgres.py','scripts/rollback_metadata.py','scripts/verify_phase.py','scripts/package_public.py'}
IGNORED_DIRS={'.git','.venv','.pytest_cache','__pycache__','node_modules','dist','.tools','.cache','test-results','playwright-report'}


def scanner():
    filename=Path(__file__).with_name('release_scan.py')
    spec=importlib.util.spec_from_file_location('release_scanner',filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def allowed(name):
    parts=Path(name).parts
    if name in ROOT_FILES or name in EVAL_FILES or name in SCRIPT_FILES:
        return True
    if parts[0]=='src':
        return Path(name).suffix in {'.py','.md'}
    if parts[0]=='tests':
        return Path(name).suffix=='.py'
    if parts[:2]==('apps','web'):
        return Path(name).suffix in {'.py','.ts','.tsx','.mjs','.css','.json','.html'} or name=='apps/web/.gitignore'
    if parts[:2]==('docs','release'):
        return Path(name).suffix in {'.md','.txt','.json','.svg','.png','.mp4','.gif'}
    if parts[0]=='scripts' and (Path(name).name.startswith('demo') or Path(name).name.startswith('release_scan')):
        return Path(name).suffix in {'.py','.sh','.mjs','.json'}
    if parts[:2] in {('examples','service_ops'),('examples','data_quality')}:
        return Path(name).suffix in {'.py','.json','.md'}
    # Only E's public per-trial results and summary, never question answers/grader internals.
    if parts[:3]==('eval','results','b2'):
        return Path(name).suffix in {'.json','.csv','.svg','.png','.md'}
    return False


def public_files(root):
    root=Path(root); selected=[]; check=scanner()
    for directory, dirs, files in os.walk(root,followlinks=False):
        parent=Path(directory)
        for name in dirs+files:
            p=parent/name
            if p.is_symlink():
                raise ValueError('Symlinks are not allowed: '+str(p.relative_to(root)))
        dirs[:]=[name for name in dirs if name not in IGNORED_DIRS and not name.startswith('.')]
        for name in files:
            path=parent/name; relative=path.relative_to(root).as_posix()
            if not allowed(relative):
                continue
            findings=check.inspect_file(path,relative)
            if findings:
                raise ValueError('Release scan rejected '+relative+': '+','.join(item['rule'] for item in findings))
            selected.append(path)
    return sorted(selected)


def package(root,destination):
    root=Path(root).resolve(); destination=Path(destination).resolve()
    if destination==root or root in destination.parents:
        raise ValueError('Write release outside the source tree')
    if destination.exists():
        raise ValueError('Release destination must be new; never erase an existing repository')
    files=public_files(root)  # All bytes are checked before anything is copied.
    manifest={'schema_version':'public_release@1','classification':'public_code_and_synthetic_examples',
              'license_status':'NOT RUN','evaluation_status':('PASSED' if (root/'eval/results/b2/summary.json').is_file() else 'NOT RUN'),
              'evaluation_scope':'Measured small synthetic holdout; status records execution, not a performance target',
              'files':{path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    destination.mkdir(parents=True)
    for path in files:
        target=destination/path.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
    pending=root/'docs/release/LICENSE.pending.txt'
    if pending.is_file():
        shutil.copy2(pending,destination/'LICENSE')
        manifest['files']['LICENSE']=hashlib.sha256(pending.read_bytes()).hexdigest()
    (destination/'PUBLIC_MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path);parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    if args.output.suffix=='.zip':
        files=public_files(args.source)
        if args.output.exists() or args.output.resolve().is_relative_to(args.source.resolve()):
            raise SystemExit('Archive must be new and outside source tree')
        with zipfile.ZipFile(args.output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for p in files:archive.write(p,p.relative_to(args.source))
        print(json.dumps({'status':'PASSED','file_count':len(files)}))
    else:
        manifest=package(args.source,args.output)
        print(json.dumps({'status':'PASSED','file_count':len(manifest['files'])}))

if __name__=='__main__':main()
