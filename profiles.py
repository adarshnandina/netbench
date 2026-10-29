"""The three NetBench test cases, modelled on industrial automation traffic.

Each profile has
  params:     how the test traffic looks
  thresholds: what the network must achieve to PASS, as metric -> (operator, limit)

The thresholds are design decisions, not laws of nature. They assume a remote
connection over the public internet to a cloud server (monitoring, remote
control, updates), NOT a hard-real-time fieldbus on the factory floor, which
needs sub-millisecond cycles that no internet link can guarantee.
"""
import operator

PROFILES = {
    "cyclic_udp": {
        "description": "Small UDP packet every 10 ms, like a controller sending cyclic process data.",
        "params": {"interval_ms": 10, "duration_s": 30, "size": 64},
        "thresholds": {
            "rtt_p99_ms": ("<=", 50.0),   # 99% of cycles answered within 50 ms
            "jitter_ms": ("<=", 5.0),     # delay must be steady, not just low
            "loss_pct": ("<=", 0.1),      # at most 1 lost packet in 1000
        },
    },
    "reqresp_tcp": {
        "description": "Poll a device every 50 ms over TCP (128 B request, 1 KB reply), like Modbus/TCP or OPC UA reads.",
        "params": {"n_requests": 200, "interval_ms": 50, "resp_size": 1024},
        "thresholds": {
            "rtt_p95_ms": ("<=", 100.0),
            "rtt_p99_ms": ("<=", 200.0),
            "errors": ("<=", 0),
        },
    },
    "bulk_tcp": {
        "description": "Download 20 MB, like a firmware update or a log/recipe transfer.",
        "params": {"n_bytes": 20_000_000},
        "thresholds": {
            "throughput_mbps": (">=", 10.0),
            "complete": ("==", True),
        },
    },
}

_OPS = {"<=": operator.le, ">=": operator.ge, "==": operator.eq}


def evaluate(profile_name, metrics):
    """Compare measured metrics against the profile's thresholds.

    Returns (overall_pass, checks) where checks is a list of
    (metric, value, op, limit, passed).
    """
    checks = []
    for metric, (op, limit) in PROFILES[profile_name]["thresholds"].items():
        value = metrics[metric]
        checks.append((metric, value, op, limit, _OPS[op](value, limit)))
    return all(c[4] for c in checks), checks