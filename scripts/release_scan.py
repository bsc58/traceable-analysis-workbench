"""Fail-closed release scan. Findings report locations, never matched secrets."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import json
from pathlib import Path
import re
import subprocess

MARKERS = ['private'+'_runtime', 'workbench'+'_private', 'aw_app'+'_private',
           'analysis_agent'+'_private', 'stock'+'_', 'pa'+'ng', chr(0x6f58), 'wen'+'cai', chr(0x95ee)+chr(0x8d22)]
RULES = {
    'local_user_path': re.compile(r'(?:/Users/|/home/)[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+\\'),
    'private_marker': re.compile('|'.join(re.escape(value) for value in MARKERS), re.I),
    'security_identifier': re.compile(r'\b(?:\d{6}\.(?:SH|SZ)|(?:SH|SZ)\d{6}|\d{4,5}\.HK)\b', re.I),
    'credential': re.compile(r'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|xai-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'assigned_secret': re.compile(r'(?i)(?:api[_-]?key|password|secret|access[_-]?token)\s*[=:]\s*[\x22\x27][A-Za-z0-9+/=_-]{24,}[\x22\x27]'),
}
TEXT_SUFFIXES = {'.py','.sh','.mjs','.ts','.tsx','.css','.html','.json','.md','.txt','.yml','.yaml','.toml','.lock','.svg','.csv'}
MEDIA_SUFFIXES = {'.png','.gif','.mp4'}
BLOCKED_SUFFIXES = {'.url','.env','.key','.token','.pem','.db','.sqlite','.sqlite3','.parquet','.dump','.backup','.sql','.gz','.zip'}
MAX_BYTES = 5 * 1024 * 1024


def inspect_file(path, relative):
    findings = []
    name = path.name
    if path.is_symlink():
        return [{'file': relative, 'rule': 'symlink'}]
    if name.startswith('.env') or path.suffix.lower() in BLOCKED_SUFFIXES or name in {'PG_VERSION','postmaster.pid'}:
        findings.append({'file':relative,'rule':'forbidden_file'})
    if path.stat().st_size > MAX_BYTES:
        findings.append({'file':relative,'rule':'large_file'})
    raw = path.read_bytes()
    if raw.startswith((b'SQLite format 3', b'PAR1', b'PGDMP')):
        findings.append({'file':relative,'rule':'database_binary'})
    is_text = path.suffix.lower() in TEXT_SUFFIXES or name in {'README','LICENSE','Dockerfile','.gitignore','.gitattributes'}
    if not is_text and path.suffix.lower() not in MEDIA_SUFFIXES:
        findings.append({'file':relative,'rule':'unknown_binary_type'})
    if path.suffix == '.png' and not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        findings.append({'file':relative,'rule':'invalid_media'})
    if path.suffix == '.mp4' and raw[4:8] != b'ftyp':
        findings.append({'file':relative,'rule':'invalid_media'})
    if path.suffix == '.gif' and not raw.startswith((b'GIF87a',b'GIF89a')):
        findings.append({'file':relative,'rule':'invalid_media'})
    try:
        content = raw.decode('utf-8') if is_text else raw.decode('utf-8', errors='ignore')
    except UnicodeDecodeError:
        return findings + [{'file':relative,'rule':'invalid_text_encoding'}]
    username = getpass.getuser()
    rules = dict(RULES)
    if len(username) >= 4:
        rules['local_username'] = re.compile(re.escape(username),re.I)
    for rule, pattern in rules.items():
        for value, location in [(relative,0),(content,1)]:
            match = pattern.search(value)
            if match:
                findings.append({'file':relative,'rule':rule,'line':0 if not location else value.count('\n',0,match.start())+1})
    return findings


def scan(root, *, check_git=False):
    root = root.resolve()
    findings=[]; files={}; total=0
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if relative.parts[0]=='.git':
            continue
        if path.is_symlink():
            findings.append({'file':relative.as_posix(),'rule':'symlink'});continue
        if not path.is_file():
            continue
        name=relative.as_posix(); findings.extend(inspect_file(path,name))
        data=path.read_bytes(); total+=len(data); files[name]=hashlib.sha256(data).hexdigest()
    repository={'status':'NOT RUN'}
    if check_git:
        def git(*args): return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
        count=int(git('rev-list','--count','HEAD'))
        repository={'status':'PASSED','commit_count':count,'remote_count':len(git('remote').splitlines()),
                    'author':git('log','-1','--format=%an <%ae>'),'clean':not git('status','--porcelain')}
        if count!=1 or repository['remote_count'] or not repository['clean']:
            findings.append({'file':'.git','rule':'release_history_or_state'})
        if repository['author']!='Release Author <release@example.invalid>':
            findings.append({'file':'.git','rule':'nonplaceholder_author'})
        repository['git_bytes']=sum(p.stat().st_size for p in (root/'.git').rglob('*') if p.is_file())
    return {'status':'FAILED' if findings else 'PASSED','file_count':len(files),'content_bytes':total,
            'files':files,'findings':findings,'repository':repository,
            'scope':'Filename/content/size/signature scan; not a guarantee that every secret format is recognized.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root',type=Path);parser.add_argument('--git',action='store_true');parser.add_argument('--report',type=Path)
    args=parser.parse_args();result=scan(args.root,check_git=args.git)
    if args.report:
        if args.report.resolve().is_relative_to(args.root.resolve()):
            raise SystemExit('Keep scan output outside the scanned release')
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!='files'},ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['status']=='PASSED' else 1)

if __name__=='__main__': main()
