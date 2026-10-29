"""Statistical assessment: does each network meet each test case?

Run from the project folder (with NETBENCH_API_URL / NETBENCH_API_KEY set):

    python -m netbench.analysis

It prints a Markdown report and writes analysis/report.md and analysis/summary.csv.

Statistical design, in short:
  * The unit of analysis is a RUN, not a packet. Packets within one run are
    not independent (a congested minute delays many packets in a row), so
    treating 3000 packets as 3000 samples would make the intervals far too
    narrow. Each run contributes one pass/fail and one value per metric.
  * Pass rate: share of runs that met all thresholds, with a Wilson score
    confidence interval (behaves well for small n and rates near 0% or 100%,
    unlike the textbook "p +- 1.96*sqrt(p(1-p)/n)").
  * Verdict: MEETS if even the LOWER end of the interval reaches the required
    pass rate, FAILS if even the UPPER end is below it, otherwise
    INCONCLUSIVE (= collect more runs).
  * Metrics: median over runs with a bootstrap confidence interval. Network
    metrics are skewed (occasional large spikes), so the median is more
    robust than the mean, and the bootstrap needs no normality assumption.
"""
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binomtest, bootstrap

from netbench.profiles import PROFILES
from netbench.upload import api_get

REQUIRED_PASS_RATE = 0.90   # a network must pass a test case in at least 90% of runs
CONFIDENCE = 0.95           # confidence level for all intervals
MAX_SEND_GAP_MS = 15.0      # cyclic runs where the CLIENT sent this unevenly are excluded

# Metrics reported per test case: the ones that have a numeric threshold.
KEY_METRICS = {
    test: [m for m, (op, limit) in p["thresholds"].items() if not isinstance(limit, bool) and m != "errors"]
    for test, p in PROFILES.items()
}
TEST_LABELS = {"cyclic_udp": "Cyclic UDP (10 ms)", "reqresp_tcp": "Request/response TCP",
               "bulk_tcp": "Bulk TCP download"}


# ---------- loading and cleaning ----------

def load_results():
    """All (run, test) rows from the backend as a flat DataFrame."""
    rows = api_get("/results")
    if not rows:
        return pd.DataFrame()
    df = pd.json_normalize(rows)
    df.columns = [c.removeprefix("metrics.") for c in df.columns]
    df["started_at"] = pd.to_datetime(df["started_at"], utc=True)
    df["is_localhost"] = df["server"].str.startswith(("127.0.0.1", "localhost"))
    return df


def clean(df, drop_localhost=True):
    """Remove rows that say nothing about the network. Returns (clean_df, reasons).

    Tests that ERRORED are kept: "could not connect" is a real failure of the network.
    """
    reasons = {}
    keep = pd.Series(True, index=df.index)
    if drop_localhost:
        local = df["is_localhost"]
        reasons["localhost runs"] = int(local.sum())
        keep &= ~local
    if "send_gap_p99_ms" in df:
        bad_client = (df["test"] == "cyclic_udp") & (df["send_gap_p99_ms"] > MAX_SEND_GAP_MS)
        reasons[f"cyclic runs with client send gap p99 > {MAX_SEND_GAP_MS:g} ms"] = int((bad_client & keep).sum())
        keep &= ~bad_client
    return df[keep], reasons


# ---------- statistics ----------

def pass_rate_ci(passed, runs):
    ci = binomtest(passed, runs).proportion_ci(confidence_level=CONFIDENCE, method="wilson")
    return ci.low, ci.high


def verdict(low, high):
    if low >= REQUIRED_PASS_RATE:
        return "MEETS"
    if high < REQUIRED_PASS_RATE:
        return "FAILS"
    return "INCONCLUSIVE"


def median_ci(values):
    """Median with a percentile-bootstrap confidence interval (nan if too few values)."""
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if len(values) == 0:
        return math.nan, math.nan, math.nan
    if len(values) < 5 or np.ptp(values) == 0:
        return float(np.median(values)), math.nan, math.nan
    res = bootstrap((values,), np.median, confidence_level=CONFIDENCE, n_resamples=10_000,
                    method="percentile", rng=np.random.default_rng(0))  # fixed seed = reproducible report
    return float(np.median(values)), float(res.confidence_interval.low), float(res.confidence_interval.high)


def runs_needed_if_all_pass():
    """Fewest runs that can prove REQUIRED_PASS_RATE, even if every single run passes."""
    n = 1
    while pass_rate_ci(n, n)[0] < REQUIRED_PASS_RATE:
        n += 1
    return n


def summarize(df):
    """One row per (network, test): runs, pass rate + CI, verdict, median + CI per key metric."""
    rows = []
    for (network, test), g in df.groupby(["network", "test"]):
        runs, passed = len(g), int(g["passed"].sum())
        low, high = pass_rate_ci(passed, runs)
        row = {"network": network, "test": test, "runs": runs, "passed": passed,
               "pass_rate": passed / runs, "pass_rate_low": low, "pass_rate_high": high,
               "verdict": verdict(low, high)}
        for metric in KEY_METRICS[test]:
            med, lo, hi = median_ci(g[metric]) if metric in g else (math.nan,) * 3
            row[f"{metric}_median"], row[f"{metric}_low"], row[f"{metric}_high"] = med, lo, hi
        rows.append(row)
    order = {t: i for i, t in enumerate(PROFILES)}
    return pd.DataFrame(rows).sort_values(["test", "network"], key=lambda s: s.map(order) if s.name == "test" else s)


# ---------- report ----------

def _fmt(med, lo, hi, digits=1):
    if math.isnan(med):
        return "n/a"
    if math.isnan(lo):
        return f"{med:.{digits}f}"
    return f"{med:.{digits}f} [{lo:.{digits}f}–{hi:.{digits}f}]"


VERDICT_ICON = {"MEETS": "✅", "FAILS": "❌", "INCONCLUSIVE": "❔"}


def display_table(summary, test):
    """The summary rows for one test case, formatted as text: used by the report AND the web UI."""
    part = summary[summary["test"] == test]
    table = pd.DataFrame({
        "Network": part["network"],
        "Runs": part["runs"].astype(str),
        "Pass rate [95% CI]": [f"{r.pass_rate:.0%} [{r.pass_rate_low:.0%}–{r.pass_rate_high:.0%}]"
                               for r in part.itertuples()],
        "Verdict": [f"{VERDICT_ICON[v]} {v}" for v in part["verdict"]],
    })
    for metric in KEY_METRICS[test]:
        op, limit = PROFILES[test]["thresholds"][metric]
        table[f"{metric} (limit {op} {limit})"] = [
            _fmt(getattr(r, f"{metric}_median"), getattr(r, f"{metric}_low"), getattr(r, f"{metric}_high"),
                 digits=2 if metric == "loss_pct" else 1)
            for r in part.itertuples()]
    return table.reset_index(drop=True)


def method_note():
    return (f"A network **meets** a test case if, at {CONFIDENCE:.0%} confidence, it passes all of the test "
            f"case's thresholds in at least **{REQUIRED_PASS_RATE:.0%} of runs** (Wilson interval on the pass "
            f"rate). Metric values are medians over runs with {CONFIDENCE:.0%} bootstrap intervals. "
            f"Proving {REQUIRED_PASS_RATE:.0%} needs at least **{runs_needed_if_all_pass()} runs** "
            f"even if every run passes.")


def markdown_report(summary, reasons):
    lines = [
        "# NetBench assessment",
        "",
        method_note(),
        "",
    ]
    excluded = [f"{n} {why}" for why, n in reasons.items() if n]
    if excluded:
        lines += [f"Excluded: {', '.join(excluded)}.", ""]

    for test in PROFILES:
        table = display_table(summary, test)
        if table.empty:
            continue
        lines += [f"## {TEST_LABELS[test]}", "",
                  "| " + " | ".join(table.columns) + " |",
                  "|" + "---|" * len(table.columns)]
        lines += ["| " + " | ".join(row) + " |" for row in table.itertuples(index=False)]
        lines.append("")
    return "\n".join(lines)


def main():
    try:
        df = load_results()
    except OSError as e:
        print(f"Could not reach the backend at the configured NETBENCH_API_URL: {e}")
        return
    if df.empty:
        print("No results in the backend yet.")
        return
    data, reasons = clean(df)
    if data.empty:
        print("Nothing left after cleaning (only localhost runs so far?).")
        return
    summary = summarize(data)
    report = markdown_report(summary, reasons)
    out = Path("analysis")
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text(report)
    summary.to_csv(out / "summary.csv", index=False)
    print(report)
    print(f"\nWritten: {out / 'report.md'}, {out / 'summary.csv'}")


if __name__ == "__main__":
    main()
