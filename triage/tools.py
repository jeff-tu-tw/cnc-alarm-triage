"""Tools the agent can call, plus the code that executes them.

Tool errors are returned to the model as text (is_error=True) rather than
raised, so the agent can read the message and fix its own query.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .db import ReadOnlyDB, SQLGuardError
from .retrieval import BM25Index, load_documents

SCHEMA_DOC = """Tables (SQLite). Timestamps are local plant time as 'YYYY-MM-DD HH:MM:SS' text; use date(ts), substr(ts,1,7), or string comparison.
Data covers 2026-06-01 to 2026-08-31.

machines(machine_id, model, site, line, install_year)
  8 CNC machines M01-M08. Site A: lines L1, L2. Site B: line L3.
alarm_codes(code, name, name_zh, severity, description, likely_causes, recommended_checks)
  Reference for each alarm code (also searchable in the manual).
alarms(alarm_id, machine_id, ts, alarm_code, severity, axis, duration_min, reset_by, shift)
  One row per alarm event. axis is set only for AX-* codes. reset_by: auto | operator | technician.
  shift: day (08-20) | night (20-08).
work_orders(wo_id, machine_id, created_ts, closed_ts, wo_type, component, technician, notes)
  wo_type: PM (preventive) | CM (corrective) | NOTE (observation, no repair). notes are free text,
  mixed English/Chinese. Use search_documents for free-text search; use SQL for filtering/counting.
pm_plan(plan_id, machine_id, component, interval_days, planned_date, done_date, status)
  Preventive maintenance schedule. status: done | done_late (>3 days after plan) | scheduled.
  The next planned date is based on the previous done_date.
tool_changes(change_id, machine_id, ts, tool_type, supplier, batch)
  Cutting insert changes, with supplier and batch.

Not recorded anywhere in this data: energy consumption, vibration/sensor time series, production counts,
operator identity for alarms."""

TOOL_SPECS = [
    {
        "name": "get_schema",
        "description": "Return the database schema, what each table and column means, and what data is NOT available. "
                       "Call this first if you are unsure which table holds what.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_sql",
        "description": "Run one read-only SQLite SELECT query against the plant database and get rows back as JSON "
                       f"(max {config.SQL_MAX_ROWS} rows). Use for counting, filtering, grouping and comparing alarms, "
                       "maintenance plans, work orders and tool changes. Aggregate in SQL rather than pulling raw rows.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A single SELECT or WITH ... SELECT statement."}},
            "required": ["query"],
        },
    },
    {
        "name": "search_documents",
        "description": "Keyword search over maintenance work order notes (mixed English/Chinese) and the maintenance "
                       "manual (alarm code reference, troubleshooting guides). Use for 'what did technicians find', "
                       "'what does the manual say', or when you need the text of notes. Include specific terms "
                       "(alarm codes, components, part numbers) in the query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {"type": "string", "enum": ["all", "work_orders", "manual"], "default": "all"},
                "machine_id": {"type": "string", "description": "Filter work orders to one machine, e.g. M03."},
                "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive."},
                "top_k": {"type": "integer", "default": 6, "minimum": 1, "maximum": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_alarm_code",
        "description": "Get the reference entry for one alarm code: meaning, severity, likely causes and recommended checks.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "e.g. SP-101"}},
            "required": ["code"],
        },
    },
]


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


class ToolRunner:
    def __init__(self, db_path: Path | str = config.DB_PATH, manual_dir: Path | str = config.MANUAL_DIR):
        self.db = ReadOnlyDB(db_path)
        self.index = BM25Index(load_documents(Path(db_path), Path(manual_dir)))

    def execute(self, name: str, args: dict) -> ToolResult:
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return ToolResult(f"Unknown tool: {name}", is_error=True)
            out = handler(**(args or {}))
        except SQLGuardError as e:
            return ToolResult(f"Query rejected: {e}", is_error=True)
        except TypeError as e:
            return ToolResult(f"Bad arguments for {name}: {e}", is_error=True)
        except Exception as e:  # surface DB errors (bad column name etc.) so the model can fix its query
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        if len(out) > config.TOOL_OUTPUT_MAX_CHARS:
            out = out[: config.TOOL_OUTPUT_MAX_CHARS] + "\n...[truncated: narrow the query or aggregate in SQL]"
        return ToolResult(out)

    def _tool_get_schema(self) -> str:
        return SCHEMA_DOC

    def _tool_run_sql(self, query: str) -> str:
        return json.dumps(self.db.query(query), ensure_ascii=False, default=str)

    def _tool_search_documents(self, query: str, source: str = "all", machine_id: str | None = None,
                               date_from: str | None = None, date_to: str | None = None, top_k: int = 6) -> str:
        top_k = max(1, min(int(top_k), 15))
        hits = self.index.search(query, top_k=top_k, source=source, machine_id=machine_id,
                                 date_from=date_from, date_to=date_to)
        if not hits:
            return "No matching documents."
        return json.dumps([
            {"id": d.doc_id, "source": d.source, "machine_id": d.machine_id, "date": d.date,
             "title": d.title, "text": d.text[:600], "score": round(s, 2)}
            for d, s in hits
        ], ensure_ascii=False)

    def _tool_lookup_alarm_code(self, code: str) -> str:
        code = re.sub(r"\s+", "", code.upper())
        res = self.db.query("SELECT code, name, name_zh, severity, description, likely_causes, recommended_checks "
                            "FROM alarm_codes WHERE code = ?", (code,))
        rows = [dict(zip(res["columns"], r)) for r in res["rows"]]
        if not rows:
            known = [r["code"] for r in self.db.fetch_all("SELECT code FROM alarm_codes ORDER BY code")]
            return f"No alarm code '{code}'. Known codes: {', '.join(known)}"
        return json.dumps(rows[0], ensure_ascii=False)
