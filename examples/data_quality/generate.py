"""Independent deterministic batch/check/dependency generator; inputs only."""
import json


def generate(case=0):
    if type(case) is not int or case not in range(5):
        raise ValueError("Unknown fixture selection")
    stamp = "2026-01-02T00:30:00Z" if case != 1 else "2026-01-01T00:30:00Z"
    version = "v2" if case in (2, 3) else "v1"
    batches = [
        {"batch_id": "b01", "dataset": "orders", "version": "v1", "imported_at": "2026-01-01T00:00:00Z",
         "available_at": "2026-01-01T00:00:00Z", "rows": 4, "row_unit": "records"},
        {"batch_id": "b02", "dataset": "orders", "version": version, "imported_at": stamp,
         "available_at": stamp, "rows": 4, "row_unit": "records"},
        {"batch_id": "b03", "dataset": "raw", "version": "v1", "imported_at": "2026-01-02T00:15:00Z",
         "available_at": "2026-01-02T00:15:00Z", "rows": 4, "row_unit": "records"},
        {"batch_id": "b04", "dataset": "orders", "version": "v3", "imported_at": "2026-01-02T01:00:00Z",
         "available_at": "2026-01-02T01:00:00Z", "rows": 99, "row_unit": "records"},
    ]
    if case == 4:
        batches = [row for row in batches if row["dataset"] != "raw"]
    checks = []
    for index in range(4):
        for rule in ("not_null", "unique"):
            row = {"check_id": f"k{index}{rule}", "batch_id": "b02", "record_id": f"r{index}",
                   "rule": rule, "version": version, "passed": not (case == 2 and index < 2),
                   "checked_at": "2026-01-02T00:40:00Z", "available_at": "2026-01-02T00:40:00Z"}
            checks.extend([row, dict(row)])
    return {"batches": batches, "checks": checks,
            "dependencies": [{"edge_id": "d01", "dataset": "orders", "upstream": "raw",
                              "required_version": "v1"}]}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
