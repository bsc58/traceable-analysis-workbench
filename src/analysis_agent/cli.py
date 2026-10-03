"""File-based CLI for an attached agent; public default loads synthetic data only."""
import argparse
import json
import os
from pathlib import Path

from .access import check_metadata_access
from .path_policy import runtime_path
from .contracts import Registry, Report, RunRequest, WorkbenchError
from .presentation import render_record
from .runtime import Workbench
from .storage import Objects, Store, SCHEMA_VERSION


def demo_workbench(args):
    from .demo import CommerceAdapter, demo_skill
    Objects.check_class(Path(args.objects), "public")
    store = Store(runtime_path(args.db_url_file).read_text().strip())
    legacy = getattr(args, "legacy_readonly", False)
    store.check_version(legacy_readonly=legacy)
    check_metadata_access(store, "workbench_public", "aw_app_public", legacy_readonly=legacy)
    scenario = getattr(args, "scenario", "commerce")
    if scenario != "commerce":
        from .scenarios import build_registry
        registered = build_registry(scenario)
        projects = {scenario: list(registered.adapters)}
    else:
        adapters = [CommerceAdapter(), CommerceAdapter(variant="normal")]
        registered = Registry(adapters, [demo_skill()])
        projects = {"demo": [a.ref for a in adapters]}
    return Workbench(store, Objects(Path(args.objects)), registered,
                     "local_owner", projects, readonly=legacy, legacy_sources={"demo": ["synthetic_commerce@1", "synthetic_commerce_normal@1", "src_a71d94f2c806@2", "src_9b38e06d152f@2"]})


def migration_command(args):
    """The sole CLI path that may change the metadata schema."""
    store = Store(runtime_path(args.db_url_file).read_text().strip())
    try:
        return store.migrate(expected_database=args.expected_database,
                             from_version=args.from_version, to_version=args.to)
    finally:
        store.engine.dispose()


def main(factory=demo_workbench, *, default_db=None, default_objects=None):
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = Path(__file__).resolve().parents[4] / "work" / "workbench_runtime"
    parser.add_argument("--db-url-file", default=default_db or str(runtime / "public-app.url"))
    parser.add_argument("--legacy-readonly", action="store_true")
    parser.add_argument("--scenario",choices=["commerce","service_ops","data_quality"],default="commerce")
    parser.add_argument("--objects", default=default_objects or str(runtime / "public-objects"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    c = commands.add_parser("init-objects")
    c.add_argument("--store-class", choices=["public", "private"], required=True)
    c = commands.add_parser("migrate")
    c.add_argument("--to", type=int, choices=[1, 2, 3], required=True)
    c.add_argument("--from", dest="from_version", type=int, choices=[0, 1, 2, 3], default=1)
    c.add_argument("--expected-database", required=True)
    c = commands.add_parser("create")
    c.add_argument("--request", required=True)
    c.add_argument("--key", required=True)
    c = commands.add_parser("call")
    for name in ("run", "tool", "args", "key", "explanation"):
        c.add_argument("--" + name, required=True)
    c = commands.add_parser("finalize")
    c.add_argument("--run", required=True)
    c.add_argument("--report", required=True)
    c = commands.add_parser("revalidate")
    c.add_argument("--run", required=True)
    c.add_argument("--validator", choices=["cell_validator@2"], required=True)
    c.add_argument("--target-db-url-file", required=True)
    c.add_argument("--target-objects", required=True)
    c = commands.add_parser("reconcile")
    c.add_argument("--run", required=True)
    c.add_argument("--older-than-seconds", type=float, required=True)
    c = commands.add_parser("resolutions")
    for name in ("run", "attempt", "actor", "reason"):
        c.add_argument("--" + name, required=True)
    c.add_argument("--disposition", choices=["confirm_no_side_effects_failed"], default="confirm_no_side_effects_failed")
    c = commands.add_parser("replay")
    c.add_argument("--run", required=True)
    for operation in ("rerun_frozen", "run_latest"):
        c = commands.add_parser(operation)
        c.add_argument("--run", required=True);c.add_argument("--key",required=True)
        if operation == "run_latest":c.add_argument("--source-ref",required=True)
    c = commands.add_parser("show")
    c.add_argument("--run", required=True)
    c.add_argument("--format", choices=["json", "html"], default="json")
    c.add_argument("--output")
    c = commands.add_parser("skill")
    c.add_argument("--run", required=True)
    c.add_argument("--attachment")
    c = commands.add_parser("history")
    c.add_argument("--project", required=True)
    commands.add_parser("mcp")
    c = commands.add_parser("serve")
    c.add_argument("--port", type=int, default=8765)
    c.add_argument("--token-file", required=True)
    args = parser.parse_args()
    try:
        for field in ("db_url_file", "objects", "request", "args", "report", "token_file", "target_db_url_file", "target_objects", "output"):
            value = getattr(args, field, None)
            if value: runtime_path(value)
        if args.command == "init-objects":
            print(json.dumps({"store_class": Objects.initialize(Path(args.objects), args.store_class)}))
            return
        if args.legacy_readonly and args.command not in {"show", "replay", "history", "mcp", "serve", "init", "skill", "revalidate"}:
            raise WorkbenchError("legacy_readonly", "Archive mode refuses write operations", 403)
        if args.command == "migrate":
            print(json.dumps(migration_command(args), ensure_ascii=False, indent=2))
            return
        workbench = factory(args)
        load = lambda p: json.loads(runtime_path(p).read_text())
        if args.command == "init":
            result = {"metadata_schema": SCHEMA_VERSION, "source_mutations": 0}
        elif args.command == "create":
            result = workbench.create(RunRequest(**load(args.request)), args.key)
        elif args.command == "replay":
            result = workbench.replay(args.run)
        elif args.command == "rerun_frozen":
            result = workbench.rerun_frozen(args.run,args.key)
        elif args.command == "run_latest":
            result = workbench.run_latest(args.run,args.source_ref,args.key)
        elif args.command == "call":
            result = workbench.call(args.run, args.tool, load(args.args), args.key, args.explanation)
        elif args.command == "finalize":
            result = workbench.finalize(args.run, Report(**load(args.report)))
        elif args.command == "revalidate":
            from .revalidation import revalidate_command
            result = revalidate_command(workbench, args)
        elif args.command == "reconcile":
            result = workbench.reconcile(args.run, older_than_seconds=args.older_than_seconds)
        elif args.command == "resolutions":
            result = workbench.resolve(args.run, args.attempt, actor=args.actor, reason=args.reason, disposition=args.disposition)
        elif args.command == "skill":
            result = workbench.get_run_skill(args.run, args.attachment)
        elif args.command == "history":
            result = workbench.history(args.project)
        elif args.command == "show":
            record = workbench.reopen(args.run)
            result = render_record(record) if args.format == "html" else record
        elif args.command == "mcp":
            from .mcp_server import build_server
            build_server(workbench).run(transport="stdio")
            return
        elif args.command == "serve":
            import uvicorn
            from .api import create_app
            uvicorn.run(create_app(workbench, Path(args.token_file).read_text().strip()), host="127.0.0.1", port=args.port)
            return
        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        if getattr(args, "output", None):
            target = Path(args.output)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(output + "\n")
            print(json.dumps({"written": str(target), "operation": "replay_saved_records"}))
        else:
            print(output)
    except WorkbenchError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
