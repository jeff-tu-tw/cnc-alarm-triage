"""Compare two eval result files: what got fixed, what regressed.

    python -m evals.compare evals/results/<before>.json evals/results/<after>.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[1:])
    before = {r["id"]: r for r in a["results"]}
    after = {r["id"]: r for r in b["results"]}
    print(f"{a['tag']} -> {b['tag']}")
    for key in ("correct",):
        sa, sb = a["summary"]["overall"], b["summary"]["overall"]
        print(f"overall correct: {sa[key]}/{sa['n']} -> {sb[key]}/{sb['n']}")
    for qid in sorted(set(before) & set(after)):
        x, y = before[qid]["correct"], after[qid]["correct"]
        if x != y:
            print(f"  {'FIXED    ' if y else 'REGRESSED'} {qid}  {after[qid]['question']}")
    for cat in b["summary"]:
        if cat == "overall" or cat not in a["summary"]:
            continue
        sa, sb = a["summary"][cat], b["summary"][cat]
        print(f"  {cat:<14} {sa['correct']}/{sa['n']} -> {sb['correct']}/{sb['n']}   "
              f"tools/q {sa['avg_tool_calls']} -> {sb['avg_tool_calls']}")


if __name__ == "__main__":
    main()
