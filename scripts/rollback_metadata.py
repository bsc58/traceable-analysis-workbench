#!/usr/bin/env python3
"""Explicit schema 2→1 rollback; refuses to discard post-migration activity."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_agent.contracts import WorkbenchError
from analysis_agent.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url-file", required=True)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--from", dest="from_version", type=int, choices=[2, 3], default=2)
    parser.add_argument("--to", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    store = None
    try:
        # The connection file is loaded internally and never echoed.
        store = Store(Path(args.db_url_file).read_text(encoding="utf-8").strip())
        result = store.rollback(expected_database=args.expected_database, from_version=args.from_version, to_version=args.to)
        print(json.dumps(result))
    except WorkbenchError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 2
    except Exception:
        print(json.dumps({"error": {"code": "rollback_failed", "message": "Rollback failed; connection details suppressed"}}))
        return 2
    finally:
        if store is not None:
            store.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
