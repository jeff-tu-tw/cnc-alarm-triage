"""Tests that run without an API key. `python -m unittest` or `pytest`."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from triage import generate_data
from triage.agent import TriageAgent
from triage.db import ReadOnlyDB, SQLGuardError, validate_sql
from triage.retrieval import tokenize
from triage.tools import ToolRunner

from evals.checks import evidence_cited, run_check
from evals.run_eval import load_questions

TMP = Path(tempfile.mkdtemp())
generate_data.write_all(generate_data.generate(), TMP)
DB_PATH, MANUAL = TMP / "plant.db", TMP / "manual"


class TestPlantedScenarios(unittest.TestCase):
    """If these fail, the ground truth the eval relies on is broken."""

    def setUp(self):
        self.db = ReadOnlyDB(DB_PATH)

    def count(self, sql):
        return self.db.scalar(sql)

    def test_s1_spindle_overheat_rises_then_stops_after_fix(self):
        before = self.count("SELECT count(*) FROM alarms WHERE machine_id='M03' AND alarm_code='SP-101' AND ts < '2026-07-15'")
        during = self.count("SELECT count(*) FROM alarms WHERE machine_id='M03' AND alarm_code='SP-101' AND ts BETWEEN '2026-07-25' AND '2026-08-12'")
        after = self.count("SELECT count(*) FROM alarms WHERE machine_id='M03' AND alarm_code='SP-101' AND ts >= '2026-08-12 09:00'")
        self.assertGreater(during, before + 5)
        self.assertLessEqual(after, 1)

    def test_s1_pm_deferred(self):
        row = self.db.fetch_all("SELECT * FROM pm_plan WHERE machine_id='M03' AND component='coolant pump filter' AND planned_date='2026-07-10'")
        self.assertEqual(row[0]["done_date"], "2026-08-12")
        self.assertEqual(row[0]["status"], "done_late")

    def test_s2_toolmax_raises_wear_but_not_on_control(self):
        def rate(mid, cmp_):
            days = 50 if cmp_ == "<" else 43
            n = self.count(f"SELECT count(*) FROM alarms WHERE machine_id='{mid}' AND alarm_code='TL-201' AND ts {cmp_} '2026-07-20'")
            return n / days
        for mid in ("M05", "M06"):
            self.assertGreater(rate(mid, ">="), 2 * rate(mid, "<"))
        self.assertLess(rate("M08", ">="), 1.5 * rate("M08", "<") + 0.1)

    def test_s3_following_error_stops_after_cable_fix(self):
        self.assertEqual(self.count("SELECT count(*) FROM alarms WHERE machine_id='M01' AND alarm_code='AX-402' AND ts >= '2026-08-19'"), 0)

    def test_s4_power_event_site_a_only(self):
        rows = self.db.fetch_all("SELECT DISTINCT machine_id FROM alarms WHERE alarm_code='PW-701' AND ts LIKE '2026-07-28 14:%'")
        self.assertEqual(sorted(r["machine_id"] for r in rows), ["M01", "M02", "M03", "M04", "M05", "M06"])


class TestSQLGuard(unittest.TestCase):
    def test_rejects_writes_and_multiple_statements(self):
        for q in ["DELETE FROM alarms", "select 1; drop table alarms", "PRAGMA table_info(alarms)",
                  "ATTACH 'x.db' AS x", "WITH x AS (SELECT 1) DELETE FROM alarms"]:
            with self.assertRaises(SQLGuardError, msg=q):
                validate_sql(q)

    def test_allows_keywords_inside_strings(self):
        validate_sql("SELECT * FROM work_orders WHERE notes LIKE '%update; delete%'")

    def test_connection_is_read_only_even_if_guard_bypassed(self):
        import sqlite3
        con = ReadOnlyDB(DB_PATH)._connect()
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM alarms")

    def test_row_cap(self):
        res = ReadOnlyDB(DB_PATH, max_rows=10).query("SELECT * FROM alarms")
        self.assertEqual(res["row_count"], 10)
        self.assertTrue(res["truncated"])


class TestRetrieval(unittest.TestCase):
    runner = ToolRunner(DB_PATH, MANUAL)

    def test_cjk_tokenizer_makes_bigrams(self):
        self.assertIn("濾網", tokenize("濾網嚴重堵塞"))
        self.assertIn("cl-302", tokenize("CL-302 alarm"))
        self.assertIn("302", tokenize("CL-302 alarm"))

    def test_finds_encoder_cable_root_cause(self):
        hits = json.loads(self.runner.execute("search_documents",
                                              {"query": "X axis following error encoder cable", "machine_id": "M01",
                                               "source": "work_orders"}).content)
        self.assertTrue(any("encoder cable" in h["text"] for h in hits[:3]))

    def test_chinese_query_finds_mixed_language_note(self):
        hits = json.loads(self.runner.execute("search_documents", {"query": "濾網堵塞", "machine_id": "M03"}).content)
        self.assertTrue(any("clogged" in h["text"] for h in hits[:3]))

    def test_tool_errors_are_returned_not_raised(self):
        self.assertTrue(self.runner.execute("run_sql", {"query": "SELECT nope FROM alarms"}).is_error)
        self.assertTrue(self.runner.execute("no_such_tool", {}).is_error)
        self.assertTrue(self.runner.execute("run_sql", {}).is_error)


def _resp(message, finish_reason="stop"):
    return NS(choices=[NS(message=message, finish_reason=finish_reason)],
              usage=NS(prompt_tokens=100, completion_tokens=20))


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=True):
        result = {"role": "assistant", "tool_calls": [
            {"id": call.id, "type": "function", "function": {
                "name": call.function.name,
                "arguments": call.function.arguments,
            }}
            for call in self.tool_calls
        ]}
        if self.content is not None:
            result["content"] = self.content
        return result


class FakeClient:
    """Replays a scripted conversation so the agent loop can be tested offline."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs)))
        return self.responses.pop(0)


class TestAgentLoop(unittest.TestCase):
    runner = ToolRunner(DB_PATH, MANUAL)

    def test_tool_call_then_answer(self):
        client = FakeClient([
            _resp(
                FakeMessage(tool_calls=[
                    NS(
                        id="t1",
                        function=NS(
                            name="run_sql",
                            arguments=json.dumps({
                                "query": "SELECT count(*) FROM machines WHERE site='Site B'",
                            }),
                        ),
                    )
                ]),
                "tool_calls",
            ),
            _resp(FakeMessage(content="Site B has 2 machines.")),
        ])
        res = TriageAgent(self.runner, client=client, trace_path=None).ask("How many machines at Site B?")
        self.assertEqual(res.answer, "Site B has 2 machines.")
        self.assertEqual([s.tool for s in res.steps], ["run_sql"])
        self.assertIn("[[2]]", res.steps[0].output)
        second_call_messages = client.calls[1]["messages"]
        self.assertEqual(second_call_messages[-1]["role"], "tool")
        self.assertEqual(second_call_messages[-1]["tool_call_id"], "t1")
        self.assertEqual(client.calls[1]["model"], "deepseek-v4-flash")
        self.assertEqual(res.input_tokens, 200)

    def test_step_limit_forces_answer(self):
        loop = _resp(
            FakeMessage(tool_calls=[NS(
                id="t", function=NS(name="get_schema", arguments="{}"),
            )]),
            "tool_calls",
        )
        client = FakeClient([loop, loop, _resp(FakeMessage(content="Partial answer."))])
        res = TriageAgent(self.runner, client=client, max_steps=2, trace_path=None).ask("q")
        self.assertTrue(res.hit_step_limit)
        self.assertEqual(client.calls[-1]["tool_choice"], "none")
        self.assertEqual(res.answer, "Partial answer.")

    def test_truncated_answer_is_rewritten(self):
        client = FakeClient([
            _resp(FakeMessage(content="## Summary\nThe cause was (M05 Jun 15, M03 Jul"), "length"),
            _resp(FakeMessage(content="Short complete answer.")),
        ])
        res = TriageAgent(self.runner, client=client, trace_path=None).ask("q")
        self.assertEqual(res.answer, "Short complete answer.")
        self.assertFalse(res.truncated)
        self.assertEqual(res.finish_reasons, ["length", "stop"])
        self.assertEqual(client.calls[1]["tool_choice"], "none")
        self.assertIn("cut off", client.calls[1]["messages"][-1]["content"])

    def test_lookalike_hyphens_in_ids_become_ascii(self):
        client = FakeClient([_resp(FakeMessage(content="See WO‑260140 and PM‐0026; 2–8 alarms — resolved."))])
        res = TriageAgent(self.runner, client=client, trace_path=None).ask("q")
        self.assertEqual(res.answer, "See WO-260140 and PM-0026; 2–8 alarms — resolved.")

    def test_still_truncated_after_rewrite_is_flagged(self):
        cut = _resp(FakeMessage(content="Half an answer"), "length")
        client = FakeClient([cut, cut])
        res = TriageAgent(self.runner, client=client, trace_path=None).ask("q")
        self.assertTrue(res.truncated)
        self.assertTrue(res.answer.startswith("Half an answer"))
        self.assertIn("truncated", res.answer)
        self.assertEqual(len(client.calls), 2)


class TestEvalChecks(unittest.TestCase):
    db = ReadOnlyDB(DB_PATH)

    def test_question_file_is_valid(self):
        qs = load_questions()
        self.assertEqual(len({q["id"] for q in qs}), len(qs))
        for q in qs:  # every SQL in the eval must run against the current data
            for c in q["checks"]:
                if c["type"].startswith("sql_"):
                    self.db.query(c["sql"])
            if q.get("evidence_sql"):
                self.assertTrue(self.db.query(q["evidence_sql"])["rows"], q["id"])

    def test_checks(self):
        self.assertTrue(run_check({"type": "sql_number", "sql": "SELECT 2"}, "There are 2 machines.", self.db)[0])
        self.assertFalse(run_check({"type": "sql_number", "sql": "SELECT 3"}, "There are 2 machines.", self.db)[0])
        self.assertTrue(run_check({"type": "keywords", "all_of": [["coolant"], ["late", "延誤"]]},
                                  "Coolant PM 延誤", self.db)[0])
        self.assertTrue(run_check({"type": "refusal"}, "Energy consumption is not recorded in this data.", self.db)[0])
        self.assertTrue(evidence_cited("SELECT 'WO-1' UNION SELECT 'WO-2'", "see wo-2", self.db))


if __name__ == "__main__":
    unittest.main()
