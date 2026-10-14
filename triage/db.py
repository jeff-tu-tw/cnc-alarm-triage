"""Read-only SQL access for the agent, with guardrails.

Defence in depth, because the query text comes from an LLM:
  1. Statement validation: one statement, must start with SELECT/WITH,
     no write/DDL/PRAGMA/ATTACH keywords (checked outside string literals).
  2. The connection itself is opened read-only (sqlite `mode=ro`), so a
     write that slips past validation still fails.
  3. A wall-clock timeout and a row cap keep a bad query from hanging the
     agent or flooding its context window.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import config

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|analyze|load_extension)\b",
    re.IGNORECASE,
)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


class SQLGuardError(ValueError):
    """Raised when a query is rejected before it reaches the database."""


def validate_sql(query: str) -> str:
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        raise SQLGuardError("Empty query.")
    code_only = _STRING_LITERAL.sub("''", q)  # ignore keywords that appear inside quoted text
    if ";" in code_only:
        raise SQLGuardError("Only a single statement is allowed.")
    if not re.match(r"^(select|with)\b", code_only, re.IGNORECASE):
        raise SQLGuardError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    bad = _FORBIDDEN.search(code_only)
    if bad:
        raise SQLGuardError(f"Keyword not allowed in read-only queries: {bad.group(0).upper()}")
    return q


class ReadOnlyDB:
    def __init__(self, path: Path | str = config.DB_PATH, max_rows: int = config.SQL_MAX_ROWS,
                 timeout_s: float = config.SQL_TIMEOUT_S):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"{self.path} not found. Run `python -m triage.generate_data` first.")
        self.max_rows = max_rows
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)

    def query(self, sql: str, params: tuple = ()) -> dict:
        q = validate_sql(sql)
        con = self._connect()
        deadline = time.monotonic() + self.timeout_s
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = con.execute(q, params)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(self.max_rows + 1)
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise SQLGuardError(f"Query exceeded {self.timeout_s:.0f}s timeout.") from e
            raise
        finally:
            con.close()
        truncated = len(rows) > self.max_rows
        rows = [list(r) for r in rows[: self.max_rows]]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}

    def scalar(self, sql: str):
        res = self.query(sql)
        return res["rows"][0][0] if res["rows"] else None

    def fetch_all(self, sql: str) -> list[dict]:
        res = self.query(sql)
        return [dict(zip(res["columns"], r)) for r in res["rows"]]
