"""Explicit local operator entry point; no API keys in arguments or config files."""
import argparse
import json
from pathlib import Path
from .cli import demo_workbench
from .contracts import RunRequest, WorkbenchError
from .execution import BatchConfig, ExecutionConfig, Executor
from .providers import DeepSeekProvider, GrokProvider


def main(factory=demo_workbench):
    parser=argparse.ArgumentParser(description='Internal worker and recorded recovery operations')
    parser.add_argument('--db-url-file',required=True)
    parser.add_argument('--objects',required=True)
    parser.add_argument('--provider',choices=['deepseek','xai'],required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    submit=commands.add_parser('submit');submit.add_argument('--request',required=True);submit.add_argument('--config',required=True);submit.add_argument('--batch',required=True);submit.add_argument('--key',required=True)
    run=commands.add_parser('work');run.add_argument('--run',required=True);run.add_argument('--owner',required=True)
    for name in ['cancel','resume']:
        command=commands.add_parser(name);command.add_argument('--run',required=True)
    resolve=commands.add_parser('resolve-model');resolve.add_argument('--run',required=True);resolve.add_argument('--invocation',required=True);resolve.add_argument('--reason',required=True)
    args=parser.parse_args();args.legacy_readonly=False
    workbench=None;executor=None
    try:
        workbench=factory(args)
        executor=Executor(workbench,DeepSeekProvider() if args.provider=='deepseek' else GrokProvider())
        if args.command=='submit':
            config=ExecutionConfig.model_validate_json(Path(args.config).read_text())
            batch=BatchConfig.model_validate_json(Path(args.batch).read_text())
            executor.register_batch(config.batch_account,batch)
            result=executor.submit(RunRequest.model_validate_json(Path(args.request).read_text()),args.key,config)
        elif args.command=='work':result=executor.run(args.owner,args.run)
        elif args.command=='cancel':result=executor.cancel(args.run)
        elif args.command=='resume':result=executor.resume(args.run)
        else:
            executor.resolve_unknown(args.run,args.invocation,args.reason);result={'run_id':args.run,'resolved':True,'cost_reservation_retained':True}
        print(json.dumps(result,ensure_ascii=False))
    except WorkbenchError as exc:
        print(json.dumps({'error':{'code':exc.code,'message':exc.message}}));raise SystemExit(1)
    except Exception:
        print(json.dumps({'error':{'code':'operator_command_failed','message':'Failure details suppressed to avoid exposing connection credentials'}}));raise SystemExit(1)
    finally:
        if executor:executor.close()
        if workbench:workbench.store.engine.dispose()

if __name__=='__main__':main()
