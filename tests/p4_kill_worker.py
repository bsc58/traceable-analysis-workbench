"""Subprocess-only synthetic SIGKILL target; never opens a model credential."""
import json,os,signal,sys
from pathlib import Path
from kill_scenario import build_scenario
from analysis_agent.execution import Executor
from analysis_agent.providers import FixtureProvider
from test_p4_executor import responder
config=json.loads(Path(sys.argv[1]).read_text())
def stop(point,identifiers):
    if point!=config['point']:return
    with open(config['marker'],'w') as f:
        json.dump({'point':point,'ids':identifiers,'fsynced':True},f);f.flush();os.fsync(f.fileno())
    os.kill(os.getpid(),signal.SIGKILL)
scenario=build_scenario(config,fault_hook=stop)
executor=Executor(scenario.workbench,FixtureProvider(responder))
executor.run('kill-worker',config['run_id'])
raise SystemExit('Expected SIGKILL point was not reached')
