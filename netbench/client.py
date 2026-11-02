"""NetBench measurement client.

Runs the three test cases from profiles.py against a NetBench server and
prints each metric with PASS/FAIL.

  python -m netbench.client --network home-wifi --test cyclic_udp
  python -m netbench.client --network uni-net --host <server-ip>

Every run is saved as a JSON record in ./results/ and, if NETBENCH_API_URL
and NETBENCH_API_KEY are set, uploaded to the backend (see upload.py).
"""
import argparse
import asyncio
import os
import statistics
import time

from netbench import protocol as p
from netbench import results
from netbench.profiles import PROFILES, evaluate
from netbench.upload import upload_pending


# ---------- statistics helpers ----------

def percentile(values, pct):
    """pct-th percentile (0-100) with linear interpolation."""
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mean_abs_diff(values):
    """Jitter: mean absolute change between consecutive values (IP delay variation)."""
    if len(values) < 2:
        return 0.0
    return statistics.mean(abs(b - a) for a, b in zip(values, values[1:]))


# ---------- test 1: cyclic UDP ----------

class _CyclicProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.rtts_ms = {}  # seq -> RTT

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        seq, sent_ns = p.UDP_HEADER.unpack_from(data)
        self.rtts_ms.setdefault(seq, (time.perf_counter_ns() - sent_ns) / 1e6)


async def run_cyclic_udp(host, port, interval_ms, duration_s, size):
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_CyclicProtocol, remote_addr=(host, port))

    n_sent = int(duration_s * 1000 / interval_ms)
    send_times = []
    start = loop.time()
    for seq in range(n_sent):
        # Absolute schedule: packet k is due at start + k * interval, so late wake-ups don't add up.
        delay = start + seq * interval_ms / 1000 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        now_ns = time.perf_counter_ns()
        send_times.append(now_ns)
        transport.sendto(p.UDP_HEADER.pack(seq, now_ns).ljust(size, b"\0"))

    await asyncio.sleep(1.0)  # grace period for the last echoes
    transport.close()

    rtts = [proto.rtts_ms[s] for s in sorted(proto.rtts_ms)]
    gaps_ms = [(b - a) / 1e6 for a, b in zip(send_times, send_times[1:])]
    if not rtts:
        raise RuntimeError("no UDP echoes received; is the server running and the port open?")
    return {
        "sent": n_sent,
        "received": len(rtts),
        "loss_pct": 100 * (n_sent - len(rtts)) / n_sent,
        "rtt_median_ms": statistics.median(rtts),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "jitter_ms": mean_abs_diff(rtts),
        "send_gap_p99_ms": percentile(gaps_ms, 99),  # is the CLIENT keeping time?
        # One entry per packet SENT, in send order; None = lost. Keeping the
        # gaps lets the analysis see whether losses come alone or in bursts.
        "samples_ms": [proto.rtts_ms.get(seq) for seq in range(n_sent)],
    }


# ---------- test 2: TCP request/response ----------

async def run_reqresp_tcp(host, port, n_requests, interval_ms, resp_size, timeout_s=2.0):
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(p.MODE_REQRESP)
    request = p.REQ_HEADER.pack(resp_size).ljust(p.REQ_SIZE, b"\0")

    rtts, errors = [], 0
    loop = asyncio.get_running_loop()
    start = loop.time()
    for i in range(n_requests):
        delay = start + i * interval_ms / 1000 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        t0 = time.perf_counter_ns()
        try:
            writer.write(request)
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(resp_size), timeout_s)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            # After a timeout the byte stream is out of sync, so stop and
            # count every remaining request as failed.
            errors = n_requests - i
            break
        rtts.append((time.perf_counter_ns() - t0) / 1e6)

    writer.close()
    if not rtts:
        raise RuntimeError("no TCP responses received")
    return {
        "requests": n_requests,
        "errors": errors,
        "rtt_median_ms": statistics.median(rtts),
        "rtt_p95_ms": percentile(rtts, 95),
        "rtt_p99_ms": percentile(rtts, 99),
        "jitter_ms": mean_abs_diff(rtts),
        "samples_ms": rtts,
    }


# ---------- test 3: TCP bulk download ----------

async def run_bulk_tcp(host, port, n_bytes, timeout_s=60.0):
    reader, writer = await asyncio.open_connection(host, port)
    t0 = time.perf_counter_ns()
    writer.write(p.MODE_BULK + p.BULK_HEADER.pack(n_bytes))
    await writer.drain()

    received = 0

    async def receive_all():
        nonlocal received
        while received < n_bytes:
            chunk = await reader.read(256 * 1024)
            if not chunk:  # server closed early
                break
            received += len(chunk)

    try:
        await asyncio.wait_for(receive_all(), timeout_s)
    except asyncio.TimeoutError:
        pass
    elapsed_s = (time.perf_counter_ns() - t0) / 1e9
    writer.close()
    return {
        "bytes": received,
        "complete": received >= n_bytes,
        "duration_s": elapsed_s,
        "throughput_mbps": received * 8 / elapsed_s / 1e6,
    }


RUNNERS = {"cyclic_udp": run_cyclic_udp, "reqresp_tcp": run_reqresp_tcp, "bulk_tcp": run_bulk_tcp}


async def run_benchmark(host, port, network, tests, duration=None, on_test_done=None):
    """Run the given tests and return the run record. Used by the CLI and the web UI.

    on_test_done(entry) is called after each test, so a caller can show
    progress while the run is still going.
    """
    record = results.new_record(network, host, port)
    for name in tests:
        profile = PROFILES[name]
        params = dict(profile["params"])
        if duration and "duration_s" in params:
            params["duration_s"] = duration
        try:
            metrics = await RUNNERS[name](host, port, **params)
        except (OSError, RuntimeError) as e:
            # A test that can't even run is a result too: record it as a failure.
            record["tests"].append({"test": name, "params": params, "error": str(e), "passed": False})
        else:
            samples = metrics.pop("samples_ms", None)
            passed, checks = evaluate(name, metrics)
            results.add_test(record, name, params, profile["thresholds"], metrics, samples, passed, checks)
        if on_test_done:
            on_test_done(record["tests"][-1])
    return record


def nothing_reached_server(record):
    """True if every test errored: almost always a setup problem, not a network result."""
    return all("error" in t for t in record["tests"])


def print_test(entry):
    if "error" in entry:
        print(f"\n=== {entry['test']}: ERROR === {entry['error']}")
        return
    print(f"\n=== {entry['test']}: {'PASS' if entry['passed'] else 'FAIL'} ===")
    for key, value in entry["metrics"].items():
        line = f"{key:>16}: {value:.3f}" if isinstance(value, float) else f"{key:>16}: {value}"
        if key in entry["thresholds"]:
            op, limit = entry["thresholds"][key]
            line += f"   (needs {op} {limit})  {'FAIL' if key in entry['failed_checks'] else 'ok'}"
        print(line)


async def main(host, port, tests, duration, network, save_unreachable):
    record = await run_benchmark(host, port, network, tests, duration, on_test_done=print_test)

    if nothing_reached_server(record) and not save_unreachable:
        print("\nNo test reached the server, so this run was NOT saved. Is the server running?"
              "\n(If the network really blocks everything, re-run with --save-unreachable to keep it as a result.)")
        return
    path = results.save(record)
    print(f"\nSaved run {record['run_id']} -> {path}")
    upload_pending()  # uploads this run plus any earlier ones that failed to upload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("NETBENCH_SERVER", "127.0.0.1"),
                        help="measurement server (default: $NETBENCH_SERVER or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--test", default="all", choices=["all", *PROFILES])
    parser.add_argument("--duration", type=float, help="override cyclic test length in seconds")
    parser.add_argument("--network", required=True, help='label for the network under test, e.g. "home-wifi"')
    parser.add_argument("--save-unreachable", action="store_true",
                        help="save the run even if no test reached the server")
    args = parser.parse_args()
    tests = list(PROFILES) if args.test == "all" else [args.test]
    asyncio.run(main(args.host, args.port, tests, args.duration, args.network, args.save_unreachable))
