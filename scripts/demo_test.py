"""Run public regression checks on a fresh, guarded R instance; always clean it."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from demo_runtime import LocalDemo, ROOT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    demo=LocalDemo(test_instance=True)
    try:
        demo.start()
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'src'),str(ROOT/'tests')]),
             'WORKBENCH_TEST_DB_URL_FILE':str(demo.pg.url_file),
             'P7_REFERENCE_DIR':str(out/'no-reference-answers'),
             'B2_REFERENCE_DIR':str(out/'no-reference-answers'),
             'WORKBENCH_KILL_EVIDENCE_DIR':str(out/'kill'),
             'WORKBENCH_P4_KILL_EVIDENCE_DIR':str(out/'p4-kill')}
        if Path(env['P7_REFERENCE_DIR']).exists():
            raise RuntimeError('Public clean-install test must not have hidden answer files')
        # Rollback probes require an empty instance. Run them before any task fixture.
        phases=[('migration',['tests/test_p3_revalidation.py']),
                ('public',['tests','--ignore=tests/test_p3_revalidation.py'])]
        records=[]
        for name,selection in phases:
            command=[sys.executable,'-m','pytest',*selection,'-q','-ra','--junitxml',str(out/(name+'-tests.xml'))]
            with (out/(name+'-tests.txt')).open('w') as handle:
                result=subprocess.run(command,cwd=ROOT,env=env,stdout=handle,stderr=subprocess.STDOUT)
            records.append({'phase':name,'command':command,'exit_code':result.returncode})
            if result.returncode:break
        (out/'test-command.json').write_text(json.dumps({'phases':records,
            'database':'workbench_test_b2r','fixture_provider':'no live demo during migration tests',
            'hidden_answers':'absent','exit_code':result.returncode},indent=2))
        return result.returncode
    finally:
        demo.close()
        (out/'cleanup.json').write_text(json.dumps({'status':'PASSED','instance_removed':not demo.state.exists(),
            'socket_removed':not demo.pg.socket.exists()},indent=2))

if __name__=='__main__':raise SystemExit(main())
