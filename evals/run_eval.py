"""Run the eval set against the agent and write a results file.

    python -m evals.run_eval                 # all questions
    python -m evals.run_eval --ids q13,q14   # a subset
    python -m evals.run_eval --tag baseline  # label the run (used in the file name)

Results go to evals/results/<timestamp>_<tag>.json. Compare runs with
    python -m evals.compare evals/results/A.json evals/results/B.json
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from triage import config
from triage.agent import TriageAgent
from triage.db import ReadOnlyDB

from .checks import evidence_cited, run_check

HERE = Path(__file__).parent


def load_questions(path: Path = HERE / "questions.jsonl") -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def grade(q: dict, answer: str, db: ReadOnlyDB) -> dict:
    results = [(c["type"], *run_check(c, answer, db)) for c in q["checks"]]
    return {
        "correct": all(ok for _, ok, _ in results),
        "check_details": [{"type": t, "pass": ok, "detail": d} for t, ok, d in results],
        "evidence_cited": evidence_cited(q.get("evidence_sql"), answer, db),
    }


def summarize(rows: list[dict]) -> dict:
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    def stats(rs):
        ev = [r["evidence_cited"] for r in rs if r["evidence_cited"] is not None]
        return {
            "n": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "evidence_cited": f"{sum(ev)}/{len(ev)}" if ev else "-",
            "avg_tool_calls": round(sum(r["tool_calls"] for r in rs) / len(rs), 1),
            "avg_latency_s": round(sum(r["latency_ms"] for r in rs) / len(rs) / 1000, 1),
            "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rs),
        }
    return {"overall": stats(rows), **{c: stats(rs) for c, rs in sorted(by_cat.items())}}


def print_summary(summary: dict) -> None:
    print(f"\n{'category':<14}{'correct':>10}{'evidence':>10}{'tools/q':>9}{'sec/q':>7}{'tokens':>9}")
    for cat, s in summary.items():
        print(f"{cat:<14}{s['correct']:>5}/{s['n']:<4}{s['evidence_cited']:>10}{s['avg_tool_calls']:>9}"
              f"{s['avg_latency_s']:>7}{s['tokens']:>9}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated question ids")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--model", default=config.MODEL)
    args = ap.parse_args()

    questions = load_questions()
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    db = ReadOnlyDB()
    agent = TriageAgent(model=args.model, trace_path=config.ROOT / "logs" / "eval_traces.jsonl")
    rows = []
    for q in questions:
        try:
            res = agent.ask(q["question"])
            answer, err = res.answer, None
        except Exception as e:  # one API failure should not kill the whole run
            res, answer, err = None, "", f"{type(e).__name__}: {e}"
        g = grade(q, answer, db)
        row = {
            "id": q["id"], "category": q["category"], "question": q["question"], "answer": answer, "error": err,
            **g,
            "tool_calls": len(res.steps) if res else 0,
            "tool_errors": sum(s.is_error for s in res.steps) if res else 0,
            "tools_used": [s.tool for s in res.steps] if res else [],
            "hit_step_limit": res.hit_step_limit if res else False,
            "truncated": res.truncated if res else False,
            "finish_reasons": res.finish_reasons if res else [],
            "reasoning_tokens": res.reasoning_tokens if res else 0,
            "latency_ms": res.latency_ms if res else 0,
            "input_tokens": res.input_tokens if res else 0,
            "output_tokens": res.output_tokens if res else 0,
        }
        rows.append(row)
        mark = "PASS" if row["correct"] else "FAIL"
        ev = {True: " evidence:yes", False: " evidence:NO", None: ""}[row["evidence_cited"]]
        trunc = "  TRUNCATED" if row["truncated"] else ""
        print(f"[{mark}] {q['id']} ({q['category']}){ev}  {row['tool_calls']} tools  {row['latency_ms']/1000:.1f}s{trunc}")
        if not row["correct"]:
            for c in row["check_details"]:
                if not c["pass"]:
                    print(f"        - {c['type']}: {c['detail']}")
            if err:
                print(f"        - error: {err}")

    summary = summarize(rows)
    print_summary(summary)
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{args.tag}.json"
    out.write_text(json.dumps({"model": args.model, "tag": args.tag, "summary": summary, "results": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
