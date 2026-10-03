"""Independent deterministic input generator; no adapter or answer imports."""
import json
from datetime import datetime, timedelta, timezone


def generate(case=0):
    if type(case) is not int or case not in range(5):
        raise ValueError("Unknown fixture selection")
    requests, errors = [], []
    counts = (4, 4, 1, 1, 0)
    failures = (1, 3, 0, 1, 0)
    for window, day, size, failed in (("b", 1, 4, 1), ("c", 2, counts[case], failures[case])):
        for index in range(size):
            stamp = datetime(2026, 1, day, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
            # Explicit offsets straddle a local date; windows are evaluated in UTC.
            local = stamp.astimezone(timezone(timedelta(hours=-5))).isoformat()
            request_id = f"r{window}{index}"
            requests.append({"request_id": request_id, "service": "edge", "observed_at": local,
                             "available_at": stamp.isoformat(), "duration": 100 + index, "duration_unit": "ms"})
            if index < failed:
                event = {"error_id": f"e{window}{index}", "request_id": request_id,
                         "observed_at": local, "available_at": stamp.isoformat()}
                errors.extend([event, dict(event), {**event, "error_id": f"x{window}{index}"}])
    # Outside each half-open window; included to catch endpoint mistakes.
    for day in (1, 2):
        requests.append({"request_id": f"endpoint{day}", "service": "edge",
                         "observed_at": f"2026-01-0{day}T01:00:00Z",
                         "available_at": f"2026-01-0{day}T01:00:00Z", "duration": 999, "duration_unit": "ms"})
    return {"requests": requests, "errors": errors,
            "context": [{"event_id": "ctx01", "service": "edge", "kind": "release",
                         "observed_at": "2026-01-02T00:00:00Z", "available_at": "2026-01-02T00:00:00Z",
                         "detail": "Release r18 recorded; temporal coincidence alone cannot establish causation."},
                        {"event_id": "ctx02", "service": "edge", "kind": "alert",
                         "observed_at": "2026-01-02T00:30:00Z", "available_at": "2026-01-02T00:30:00Z",
                         "detail": "Synthetic alert; no causal intervention or control group."}]}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
