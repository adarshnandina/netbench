"""NetBench web UI: start benchmark runs and look at stored results.

Run from the project folder, in a terminal where NETBENCH_API_URL and
NETBENCH_API_KEY are exported:

    streamlit run dashboard.py

How Streamlit works: this whole file is re-run from top to bottom every time
you click or change anything on the page. Widgets return their current value,
so the code reads like a normal script. Anything slow (like API calls) is
cached with @st.cache_data so it doesn't run again on every click.
"""
import asyncio
import os

import altair as alt
import pandas as pd
import streamlit as st

from netbench import analysis, results
from netbench.client import nothing_reached_server, run_benchmark
from netbench.profiles import PROFILES
from netbench.upload import api_config, api_get, upload_pending

st.set_page_config(page_title="NetBench", layout="wide")

DARK = st.context.theme.type == "dark"
# Fixed colour order. Each network keeps its colour even when others are filtered out.
SERIES = (["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"] if DARK
          else ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"])
CRITICAL = "#d03b3b"   # reserved for "lost packet"; never used as a series colour
MUTED = "#a3a29c" if DARK else "#76756f"
LOCAL_TZ = "Europe/Berlin"

TEST_LABELS = {
    "cyclic_udp": "Cyclic UDP (10 ms)",
    "reqresp_tcp": "Request/response TCP",
    "bulk_tcp": "Bulk TCP download",
}
# (test, metric) pairs that can be plotted over time, with a readable label.
PLOTTABLE = {
    "Cyclic UDP: RTT p99 (ms)": ("cyclic_udp", "rtt_p99_ms"),
    "Cyclic UDP: jitter (ms)": ("cyclic_udp", "jitter_ms"),
    "Cyclic UDP: packet loss (%)": ("cyclic_udp", "loss_pct"),
    "Request/response: RTT p95 (ms)": ("reqresp_tcp", "rtt_p95_ms"),
    "Request/response: RTT p99 (ms)": ("reqresp_tcp", "rtt_p99_ms"),
    "Bulk: throughput (Mbit/s)": ("bulk_tcp", "throughput_mbps"),
}

st.title("NetBench")
st.caption("Internet benchmark for automation-style traffic")

if api_config() is None:
    st.error("Backend not configured. In the terminal, export NETBENCH_API_URL and "
             "NETBENCH_API_KEY, then start Streamlit again from that same terminal.")
    st.stop()


# ---------- data loading ----------

@st.cache_data(ttl=30)
def load_results():
    """All (run, test) rows as a flat table, times in local time. Cached for 30 s."""
    df = analysis.load_results()
    if not df.empty:
        df["started_at"] = df["started_at"].dt.tz_convert(LOCAL_TZ)
    return df


@st.cache_data(ttl=300)
def load_run(run_id):
    return api_get(f"/runs/{run_id}")


def network_colours(all_runs, shown):
    """Fixed colour per network, assigned in order of each network's FIRST run.

    A new network gets the next free colour at the end, so existing networks
    never change colour (alphabetical order would shift them). Only networks
    actually shown go into the legend.
    """
    first_seen = all_runs.groupby("network")["started_at"].min().sort_values().index
    slot = {name: SERIES[i % len(SERIES)] for i, name in enumerate(first_seen)}
    names = [n for n in first_seen if n in set(shown)]
    return alt.Scale(domain=names, range=[slot[n] for n in names])


# JSONB stores object keys in its own order, so put metrics back in a readable order.
METRIC_ORDER = ["sent", "received", "requests", "errors", "bytes", "complete", "loss_pct",
                "rtt_median_ms", "rtt_p95_ms", "rtt_p99_ms", "jitter_ms", "send_gap_p99_ms",
                "duration_s", "throughput_mbps"]


def test_table(entry):
    """One test's metrics as a small table: value, limit, ok."""
    rows = []
    ordered = sorted(entry["metrics"].items(),
                     key=lambda kv: METRIC_ORDER.index(kv[0]) if kv[0] in METRIC_ORDER else 99)
    for metric, value in ordered:
        op, limit = entry["thresholds"].get(metric, (None, None))
        status = "" if op is None else ("❌ fail" if metric in entry["failed_checks"] else "✅ ok")
        # Format as text: one column can't mix numbers and booleans (complete: True).
        shown = f"{value:.3f}" if isinstance(value, float) else str(value)
        rows.append({"metric": metric, "value": shown, "limit": "" if op is None else f"{op} {limit}",
                     "check": status})
    return pd.DataFrame(rows)


run_tab, results_tab = st.tabs(["Run a test", "Results"])

# =====================================================================
# Tab 1: run a benchmark
# =====================================================================
with run_tab:
    try:
        known = sorted(load_results()["network"].unique()) if not load_results().empty else []
    except OSError:
        known = []

    c1, c2, c3 = st.columns([2, 2, 1])
    network = c1.text_input("Network under test", placeholder="e.g. home-wifi",
                            help="Use the same label every time you test this network. "
                                 f"Known: {', '.join(known) or 'none yet'}")
    host = c2.text_input("Measurement server", value=os.environ.get("NETBENCH_SERVER", "127.0.0.1"))
    port = c3.number_input("Port", value=9000, step=1)

    c4, c5 = st.columns([3, 2])
    tests = c4.multiselect("Tests", list(PROFILES), default=list(PROFILES),
                           format_func=TEST_LABELS.get)
    duration = c5.slider("Cyclic test length (s)", 5, 60, PROFILES["cyclic_udp"]["params"]["duration_s"])

    if st.button("Run benchmark", type="primary", disabled=not (network.strip() and tests)):
        with st.status(f"Running {len(tests)} test(s) on **{network}** …", expanded=True) as status:

            def show(entry):
                label = TEST_LABELS[entry["test"]]
                if "error" in entry:
                    st.write(f"⚠️ **{label}: ERROR** – {entry['error']}")
                else:
                    st.write(f"{'✅' if entry['passed'] else '❌'} **{label}: "
                             f"{'PASS' if entry['passed'] else 'FAIL'}**")
                    st.dataframe(test_table(entry), hide_index=True)

            record = asyncio.run(run_benchmark(host, int(port), network.strip(), tests, duration, show))

            if nothing_reached_server(record):
                status.update(label="No test reached the server. Run not saved.", state="error", expanded=True)
                st.error("Is the measurement server running at that address and port?")
            else:
                path = results.save(record)
                messages = []
                upload_pending(log=messages.append)
                load_results.clear()  # so the Results tab shows the new run
                all_ok = all(t["passed"] for t in record["tests"])
                status.update(label=f"Done: {'all tests passed' if all_ok else 'some tests failed'}",
                              state="complete", expanded=True)
                st.caption(f"Saved to `{path}`")
                for m in messages:
                    st.caption(m)

# =====================================================================
# Tab 2: results from the backend
# =====================================================================
with results_tab:
    try:
        df = load_results()
    except OSError as e:
        st.error(f"Could not reach the backend: {e}")
        st.stop()
    if df.empty:
        st.info("No runs stored yet. Start one in the first tab.")
        st.stop()

    f1, f2, f3 = st.columns([3, 1, 1])
    chosen = f1.multiselect("Networks", sorted(df["network"].unique()), default=sorted(df["network"].unique()))
    hide_local = f2.toggle("Hide localhost runs", value=True,
                           help="Runs against 127.0.0.1 never leave your laptop, so they say nothing about a network.")
    if f3.button("Refresh"):
        load_results.clear()
        st.rerun()

    view = df[df["network"].isin(chosen)]
    if hide_local:
        view = view[~view["is_localhost"]]
    if view.empty:
        st.info("Nothing to show with these filters. (All runs so far may be localhost runs: "
                "switch off 'Hide localhost runs' to see them.)")
        st.stop()

    colours = network_colours(df, view["network"].unique())

    # --- assessment: the same analysis as `python -m netbench.analysis` ---
    st.subheader("Assessment")
    st.markdown(analysis.method_note())
    data, reasons = analysis.clean(view, drop_localhost=False)  # localhost already handled by the toggle
    excluded = [f"{n} {why}" for why, n in reasons.items() if n]
    if excluded:
        st.caption(f"Excluded: {', '.join(excluded)}.")
    summary = analysis.summarize(data) if not data.empty else pd.DataFrame()
    for test in PROFILES:
        if summary.empty or test not in set(summary["test"]):
            continue
        st.markdown(f"**{TEST_LABELS[test]}**")
        st.dataframe(analysis.display_table(summary, test), hide_index=True, use_container_width=True)

    # --- one metric over time ---
    st.subheader("Over time")
    choice = st.selectbox("Metric", list(PLOTTABLE))
    test, metric = PLOTTABLE[choice]
    series = view[(view["test"] == test) & view[metric].notna()] if metric in view else view.iloc[0:0]

    if series.empty:
        st.info("No data for this metric yet.")
    else:
        op, limit = PROFILES[test]["thresholds"].get(metric, (None, None))
        base = alt.Chart(series).encode(
            x=alt.X("started_at:T", title=f"Time ({LOCAL_TZ})"),
            y=alt.Y(f"{metric}:Q", title=choice.split(": ")[1]),
            color=alt.Color("network:N", scale=colours, legend=alt.Legend(title="Network", orient="top")),
            tooltip=[alt.Tooltip("network:N"),
                     alt.Tooltip("started_at:T", title="time", format="%d %b %H:%M"),
                     alt.Tooltip(f"{metric}:Q", title=metric, format=".3f"),
                     alt.Tooltip("passed:N")],
        )
        chart = base.mark_line(strokeWidth=2) + base.mark_point(filled=True, size=70)
        if limit is not None:
            rule = alt.Chart(pd.DataFrame({"limit": [limit]}))
            chart += rule.mark_rule(color=MUTED, strokeDash=[5, 4]).encode(y="limit:Q")
            chart += rule.mark_text(color=MUTED, align="left", dx=4, dy=-7, x=0).encode(
                y="limit:Q", text=alt.value(f"threshold {op} {limit}"))
        st.altair_chart(chart.properties(height=320), use_container_width=True, theme="streamlit")

    # --- single run in detail ---
    st.subheader("Single run")
    runs = (view.drop_duplicates("run_id").sort_values("started_at", ascending=False))
    labels = {r.run_id: f"{r.started_at:%d %b %H:%M} · {r.network}" for r in runs.itertuples()}
    run_id = st.selectbox("Run", list(labels), format_func=labels.get)
    run = load_run(run_id)

    for entry in sorted(run["tests"], key=lambda t: list(PROFILES).index(t["test"])):
        label = TEST_LABELS[entry["test"]]
        if entry["error"]:
            st.write(f"⚠️ **{label}: ERROR** – {entry['error']}")
            continue
        st.write(f"{'✅' if entry['passed'] else '❌'} **{label}: {'PASS' if entry['passed'] else 'FAIL'}**")
        left, right = st.columns([2, 3])
        left.dataframe(test_table(entry), hide_index=True, use_container_width=True)

        if entry["samples_ms"]:
            # Per-packet / per-request RTT in send order. None = lost packet.
            s = pd.DataFrame({"n": range(len(entry["samples_ms"])), "rtt_ms": entry["samples_ms"]})
            lost = s[s["rtt_ms"].isna()]
            unit = "packet" if entry["test"] == "cyclic_udp" else "request"
            plot = alt.Chart(s.dropna()).mark_line(strokeWidth=1, color=SERIES[0]).encode(
                x=alt.X("n:Q", title=f"{unit} number"),
                y=alt.Y("rtt_ms:Q", title="RTT (ms)"),
                tooltip=[alt.Tooltip("n:Q", title=unit), alt.Tooltip("rtt_ms:Q", title="RTT (ms)", format=".3f")],
            )
            if not lost.empty:
                plot += alt.Chart(lost).mark_rule(color=CRITICAL, strokeWidth=1).encode(
                    x="n:Q", tooltip=[alt.Tooltip("n:Q", title=f"lost {unit}")])
            right.altair_chart(plot.properties(height=220), use_container_width=True, theme="streamlit")
            right.caption(f"RTT of every {unit} in send order."
                          + (f" Red lines: {len(lost)} lost {unit}s." if not lost.empty else " No losses."))
