import json
import os
import time
from typing import Any, Dict, List

import streamlit as st # type: ignore

LOG_DIR = os.getenv("LOG_DIR", "/tmp/agriconnect-logs")


def _read_jsonl(filename: str) -> List[Dict[str, Any]]:
    path = os.path.join(LOG_DIR, filename)
    if not os.path.exists(path):
        return []
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _read_text(filename: str) -> str:
    path = os.path.join(LOG_DIR, filename)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _compute_metrics(traces: List[Dict[str, Any]], audit: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(traces)
    successes = sum(1 for t in traces if t.get("outcome") == "success")
    errors = sum(1 for t in traces if t.get("outcome") == "error")
    avg_latency = (
        sum(t.get("duration_ms", 0.0) for t in traces) / total if total else 0.0
    )
    security_events = sum(
        1
        for entry in audit
        if entry.get("event")
        in {"INJECTION_DETECTED", "VALIDATION_ERROR", "RATE_LIMIT"}
    )
    return {
        "total": total,
        "success_rate": (successes / total) * 100 if total else 0.0,
        "errors": errors,
        "avg_latency": avg_latency,
        "security_events": security_events,
    }


def main() -> None:
    st.set_page_config(page_title="AgriConnect MCP Dashboard", layout="wide")
    st.title("AgriConnect MCP Observability")

    auto_refresh = st.sidebar.checkbox("Auto refresh", value=True)

    traces = _read_jsonl("traces.jsonl")
    audit = _read_jsonl("audit.jsonl")
    server_log = _read_text("server.log")

    metrics = _compute_metrics(traces, audit)

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Calls", metrics["total"])
    col2.metric("Success Rate", f"{metrics['success_rate']:.1f}%")
    col3.metric("Errors", metrics["errors"])
    col4.metric("Avg Latency (ms)", f"{metrics['avg_latency']:.1f}")
    col5.metric("Security Events", metrics["security_events"])

    tab1, tab2, tab3, tab4 = st.tabs(["Performance", "Traces", "Audit", "Server Log"])

    with tab1:
        st.subheader("Tool Performance")
        if traces:
            st.dataframe(traces)
        else:
            st.write("No trace data yet.")

    with tab2:
        st.subheader("Trace Inspector")
        if traces:
            st.dataframe(traces)
        else:
            st.write("No trace data yet.")

    with tab3:
        st.subheader("Audit Events")
        if audit:
            st.dataframe(audit)
        else:
            st.write("No audit data yet.")

    with tab4:
        st.subheader("Server Log")
        st.text(server_log or "No server log data yet.")

    if auto_refresh:
        time.sleep(5)
        st.experimental_rerun()


if __name__ == "__main__":
    main()
