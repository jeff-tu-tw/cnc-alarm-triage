"""Streamlit front end.  streamlit run app.py"""
from __future__ import annotations

import json
import os

import streamlit as st

from triage import config
from triage.agent import TriageAgent
from triage.tools import ToolRunner

st.set_page_config(page_title="CNC alarm triage", page_icon="🛠️", layout="wide")

SAMPLES = [
    "Why did M03 keep throwing spindle overheat alarms from late July?",
    "TL-201 alarms went up on M05 and M06 from late July. What changed?",
    "M01 最近一直出現 X 軸跟隨誤差，根本原因找到了嗎？",
    "On July 28 many machines alarmed at once. Was it a machine problem?",
    "Which machine had the most alarms in August, and what kind?",
]


@st.cache_resource
def get_runner() -> ToolRunner:
    if not config.DB_PATH.exists():
        from triage import generate_data
        generate_data.write_all(generate_data.generate())
    return ToolRunner()


runner = get_runner()


def plant_overview() -> list[dict]:
    return runner.db.fetch_all("""
        WITH last30 AS (SELECT * FROM alarms WHERE ts >= '2026-08-02'),
        top AS (SELECT machine_id, alarm_code, count(*) n,
                       row_number() OVER (PARTITION BY machine_id ORDER BY count(*) DESC) rk
                FROM last30 GROUP BY machine_id, alarm_code)
        SELECT m.machine_id AS machine, m.site || ' ' || m.line AS line,
               (SELECT count(*) FROM last30 a WHERE a.machine_id = m.machine_id) AS alarms,
               (SELECT alarm_code || ' (' || n || ')' FROM top t WHERE t.machine_id = m.machine_id AND rk = 1) AS most_common
        FROM machines m ORDER BY m.machine_id""")


with st.sidebar:
    st.subheader("Plant, last 30 days of data")
    st.caption("Aug 2 – Aug 31, 2026 · synthetic data")
    st.dataframe(plant_overview(), hide_index=True)
    st.subheader("Try asking")
    clicked = None
    for i, q in enumerate(SAMPLES):
        if st.button(q, key=f"sample_{i}"):
            clicked = q
    if st.button("Clear conversation", type="secondary"):
        st.session_state.messages = []
        st.rerun()
    st.caption(f"Model: {config.MODEL}")

st.title("Alarm triage")
st.write("Ask about CNC alarms, maintenance history and cutting tools across Site A and Site B. "
         "Answers cite the work orders and records they are based on.")

if not config.API_KEY:
    st.error("TRIAGE_API_KEY is not set. Add your API provider key as an environment variable (or a Space secret) and reload.")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []


def render_trace(trace: dict) -> None:
    steps = trace.get("steps", [])
    label = f"How this was answered: {len(steps)} tool calls, {trace['latency_ms'] / 1000:.1f}s, " \
            f"{trace['input_tokens'] + trace['output_tokens']:,} tokens"
    with st.expander(label):
        for i, s in enumerate(steps, 1):
            st.markdown(f"**{i}. {s['tool']}**" + (" · error" if s["is_error"] else ""))
            if s["tool"] == "run_sql":
                st.code(s["input"].get("query", ""), language="sql")
            elif s["input"]:
                st.code(json.dumps(s["input"], ensure_ascii=False, indent=1), language="json")
            out = s["output"]
            st.text(out if len(out) < 1500 else out[:1500] + "\n…")
        if trace.get("hit_step_limit"):
            st.warning("Stopped at the step limit; parts of this answer may be unverified.")


for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("trace"):
            render_trace(m["trace"])

prompt = st.chat_input("Ask about a machine, an alarm code or a time period") or clicked
if prompt:
    history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.messages[-6:]]
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Checking alarms and maintenance records…"):
            try:
                res = TriageAgent(runner).ask(prompt, history=history)
                answer, trace = res.answer or "_No answer returned._", res.to_dict()
            except Exception as e:
                answer, trace = f"The request failed: `{type(e).__name__}: {e}`", None
        st.markdown(answer)
        if trace:
            render_trace(trace)
    st.session_state.messages.append({"role": "assistant", "content": answer, "trace": trace})
