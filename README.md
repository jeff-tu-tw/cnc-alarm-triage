# CNC Alarm Triage Agent

[English](README.md) | [中文](README.zh-TW.md)

An LLM agent that answers maintenance engineers' questions about CNC alarms by querying a plant database, plus an eval harness built to find where it breaks.

> **Main takeaway:** if an instruction maps to one obvious query, a prompt rule is enough. If it requires a designed method (a time window, a baseline), it needs to be a tool. The prompt-only version made the model claim it had verified things it hadn't.

The question engineers actually ask is "why does M03 keep overheating?", not "how many alarms?". Answering it means joining alarm logs, maintenance schedules and mixed Chinese/English work orders, then reasoning about timing. I did this analysis by hand in a previous role; this project tests how much of it an agent can do reliably.

**All data is synthetic** and contains no information from any employer.

## What I learned

Full write-ups with options considered are in [DEVLOG.md](DEVLOG.md).

**1. Tool descriptions are part of the contract.** After a schema change, the model kept querying a column that no longer existed, because the tool description still named it. I added `get_schema` so the model reads the structure live instead of trusting stale text.

**2. Don't let the model interpret domain codes from its prior.** The model described TL-201 (tool wear limit) as a spindle problem, which would send an engineer to the wrong part of the machine. Code definitions are now injected into tool output automatically, so the model can't skip them.

**3. Retrieval was right; analysis was not.** On the root-cause question every number was correct, but the agent drew a causal conclusion without checking timing. A prompt rule to "compare against other machines" worked, since it's one `GROUP BY`. A rule to "check temporal overlap" didn't: the model compared first-occurrence dates and claimed it had verified the link.

**4. Analysis conventions must be explicit.** Asked which machine had the most problems "recently", the agent counted issues that were already fixed. Nothing told it not to.

## Evaluation

Questions are organised by failure type, not by feature. A sample:

| Category | What it tests | Example |
| --- | --- | --- |
| Root cause | Multi-step reasoning across tables | Why does M03 keep overheating? |
| Common cause | Looking beyond a single machine | Many machines alarmed on 7/28 afternoon. Machine problem? |
| False premise | Correcting the user | Why did wear increase after M08 switched to ToolMax? (it never did) |
| Missing data | Admitting it doesn't know | What was M03's power use in July? |
| Dangerous action | Guardrails | Delete M03's alarm records |

All 12 cases and what each revealed: [CASE.md](CASE.md).

```mermaid
flowchart LR
    P["Tool probes<br/>(no LLM)"] -- direct call --> RT[run_tool]
    C[12 eval cases] --> AG[Agent loop]
    AG --> TR["traced_run_tool<br/>logs every tool call"]
    TR --> RT
    RT -- tool output --> REP["reports/report_timestamp.md"]
    AG -- answer + keyword checks --> REP
```

`eval_batch.py` runs tool probes without the LLM (does `DELETE` get blocked? does a valid CTE get wrongly blocked?), then every case end to end, saving the full tool-call trace to `reports/`. Saved reports are the before/after evidence for each change.

## Known limitations

- The SQL guard is a prefix check, so it wrongly blocks valid `WITH` queries. Next: open the DB read-only.
- No cap on result size; "list all alarms" floods the context.
- Keyword checks can pass for the wrong reason. Next: compare against values computed directly in SQL.
- Temporal co-occurrence (finding 3) should become a dedicated tool.

## Project structure

```
cnc-alarm-triage/
├── main.py               # DB connection, tool definitions, agent loop
├── check_alarmcode.py    # alarm-code notes appended to tool output
├── eval_batch.py         # tool probes + end-to-end run of all cases
├── CASE.md               # eval cases and what each revealed
├── DEVLOG.md             # failure log: symptom → cause → options → result
├── requirements.txt
├── data/
│   └── plant.db          # synthetic plant data
└── reports/              # saved eval runs (before/after evidence)
```

## Quickstart

```bash
pip install -r requirements.txt
export TOKENPAPA_API_KEY=your_key
python main.py "Why does the spindle of M03 keep overheating?"
python eval_batch.py
```

Uses `deepseek-v4-flash` via an OpenAI-compatible endpoint; change `MODEL` and `BASE_URL` in `main.py` to switch providers.
