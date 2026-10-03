"""Child process: persist a hook marker, then kill only its own process."""
import argparse
import json
import os
from pathlib import Path
import signal
import time

from kill_scenario import BEFORE_ACTION, CREATE_KEY, POINTS, build_scenario, valid_report


class LeaveDispatched(BaseException):
    """Reach reconciliation with a committed intent and no classified outcome."""


def durable_marker(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(str(Path(path).parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run(config):
    target = config["point"]
    if target not in POINTS or config["delay_ms"] not in range(10):
        raise ValueError("Invalid synthetic crash case")

    def hook(point, context):
        if target == "unknown_mark_before_commit" and point == "after_intent_commit":
            raise LeaveDispatched()
        if point != target:
            return
        time.sleep(config["delay_ms"] / 1000)
        durable_marker(config["marker"], {
            "point": point, "delay_ms": config["delay_ms"], "pid": os.getpid(),
            "context": context, "signal": "SIGKILL", "marker_fsynced_before_kill": True,
        })
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("SIGKILL unexpectedly returned")

    scenario = build_scenario(config, fault_hook=hook)
    try:
        if target == "failed_mark_before_commit":
            scenario.adapter.fail_execution = True
        run_id = scenario.workbench.create(scenario.request, CREATE_KEY)["run_id"]
        try:
            item = scenario.call(run_id, BEFORE_ACTION)
        except LeaveDispatched:
            if target != "unknown_mark_before_commit":
                raise
            scenario.workbench.reconcile(run_id, older_than_seconds=0)
            raise AssertionError("The reconciliation hook did not terminate the child")
        scenario.workbench.finalize(run_id, valid_report(item))
        raise AssertionError("The requested hook did not terminate the child")
    finally:
        scenario.store.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-file", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.case_file).read_text(encoding="utf-8"))
        run(config)
    except Exception as exc:
        # Unexpected worker failures are evidence, without connection details.
        print(json.dumps({"worker_error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
