"""Turn one benchmark run into a self-describing JSON record and save it.

A record must make sense on its own months later, so it stores not just the
numbers but everything needed to interpret them: which network, when, which
client machine, the exact test parameters and thresholds used, and the raw
samples behind every summary statistic.
"""
import json
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def new_record(network, server_host, server_port):
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        # Always store UTC with an explicit offset; convert to local time only for display.
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network": network,
        "server": f"{server_host}:{server_port}",
        "client": {
            "os": platform.system(),
            "os_version": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "tests": [],
    }


def add_test(record, name, params, thresholds, metrics, samples, passed, checks):
    record["tests"].append({
        "test": name,
        "params": params,  # the parameters actually used, including any overrides
        "thresholds": {m: [op, limit] for m, (op, limit) in thresholds.items()},
        "metrics": {k: _r(v) for k, v in metrics.items()},
        "passed": passed,
        "failed_checks": [c[0] for c in checks if not c[4]],
        # Raw per-packet / per-request RTTs (None = lost), rounded to 1 microsecond.
        "samples_ms": [_r(s) for s in samples] if samples else None,
    })


def _r(value):
    """Round floats to 3 decimals (1 µs when in ms); leave everything else alone."""
    return round(value, 3) if isinstance(value, float) else value


def save(record, out_dir=None):
    base_dir = Path(__file__).resolve().parent
    out = base_dir / out_dir if out_dir else base_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = record["started_at"].replace(":", "").replace("+0000", "Z")
    safe_net = "".join(c if c.isalnum() or c in "-_" else "_" for c in record["network"])
    path = out / f"{stamp}_{safe_net}.json"
    path.write_text(json.dumps(record, indent=2))
    return path