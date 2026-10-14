"""Deterministic answer checks.

Expected values are computed from the database at eval time (sql_* checks),
so the eval stays correct if the data generator changes.

Known blind spots, on purpose (cheap and reproducible beats clever for v1):
  * keyword checks can pass a wrong answer that mentions the right words,
    and fail a right answer phrased unusually;
  * sql_number passes if the expected number appears anywhere in the answer.
Read the failures by hand; add an LLM-as-judge pass only when these checks
stop being informative.
"""
from __future__ import annotations

import re

from triage.db import ReadOnlyDB

REFUSAL_MARKERS = [
    "not available", "no data", "not recorded", "not in the data", "isn't recorded", "is not recorded",
    "doesn't exist", "does not exist", "no record", "don't have", "do not have", "isn't available",
    "not tracked", "not captured", "no such machine", "not included", "cannot determine", "can't determine",
    "沒有", "無法", "不存在", "未記錄", "查無", "並未", "不包含",
]
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        try:
            out.append(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def run_check(check: dict, answer: str, db: ReadOnlyDB) -> tuple[bool, str]:
    a = answer.lower()
    kind = check["type"]
    if kind == "sql_number":
        expected = db.scalar(check["sql"])
        tol = check.get("tolerance", 0)
        ok = expected is not None and any(abs(n - float(expected)) <= tol for n in _numbers(answer))
        return ok, f"expected {expected}"
    if kind == "sql_contains":
        values = [str(r[0]) for r in db.query(check["sql"])["rows"]]
        missing = [v for v in values if v.lower() not in a]
        return not missing and bool(values), f"expected all of {values}" + (f", missing {missing}" if missing else "")
    if kind == "keywords":
        missing = [g for g in check["all_of"] if not any(k.lower() in a for k in g)]
        return not missing, "missing one of " + " | ".join("/".join(g) for g in missing) if missing else "ok"
    if kind == "refusal":
        ok = any(m in a for m in REFUSAL_MARKERS)
        return ok, "expected the agent to say the data is not available"
    if kind == "lang_zh":
        ok = len(re.findall(r"[\u4e00-\u9fff]", answer)) >= 10
        return ok, "expected a Chinese answer"
    raise ValueError(f"Unknown check type: {kind}")


def evidence_cited(evidence_sql: str | None, answer: str, db: ReadOnlyDB) -> bool | None:
    """True if the answer cites at least one expected record ID. None if the question has no evidence check."""
    if not evidence_sql:
        return None
    ids = [str(r[0]) for r in db.query(evidence_sql)["rows"]]
    return any(i.lower() in answer.lower() for i in ids)
