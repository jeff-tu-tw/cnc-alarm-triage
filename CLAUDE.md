# Notes for Claude Code

Project: LLM agent for CNC alarm root-cause triage. See README.md for the why.

## Commands
- Build data: `python -m triage.generate_data`
- Tests (offline, no API key): `python -m unittest`
- One question: `python -m triage.agent "..."`
- Eval: `python -m evals.run_eval --tag <name>`; compare runs with `python -m evals.compare A.json B.json`
- UI: `streamlit run app.py`

## Invariants
- The agent is read-only. Never add a tool that writes to the database.
- `tests/test_core.py::TestPlantedScenarios` guards the eval's ground truth. If you change the generator, those tests must still pass.
- Do not edit eval questions or checks to make a failing answer pass. Fix the agent, prompt, tools or retrieval instead, and record before/after runs.
- Keep the core dependency-free (stdlib only); only `agent.py` imports `openai` and only `app.py` imports `streamlit`.
