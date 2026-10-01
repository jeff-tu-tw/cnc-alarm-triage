"""
一次跑完 CASE.md 所有題目，把「工具呼叫軌跡 + 最終答案 + 關鍵字檢查」存成 report.md。
不需要修改 main.py：透過替換 main.run_tool 來記錄每次工具呼叫。

執行：python eval_batch.py            → 產生 report_YYYYMMDD_HHMM.md
保留每次的報告，修改前後的差異就是面試時可以講的 before / after。
"""
import json
import os
import time
from openai import OpenAI
import main

# ---------------------------------------------------------------------
# 題目：type 對應 CASE.md 的「類型」；checks 可選，有把握的才填
# ---------------------------------------------------------------------
CASES = [
    {"type": "簡單計數", "q": "M03 在 8 月有幾次警報？"},
    {"type": "跨資料表", "q": "哪台機器的保養延誤最多次？"},
    {"type": "跨資料表", "q": "哪台保養延誤最嚴重？"},
    {"type": "前後比較", "q": "M05 的 TL-201 在 7/20 前後各有幾次？"},
    {"type": "搜尋工單", "q": "有沒有工單提到編碼器的線？",
     "must_include": [["WO-260155"]]},
    {"type": "根因分析", "q": "M03 為什麼一直主軸過熱？",
     "must_include": [["冷卻", "coolant"], ["濾網", "filter"], ["延", "defer", "delay"]]},
    {"type": "共同原因", "q": "7/28 下午很多台機器同時報警，是機器的問題嗎？",
     "must_include": [["PW-701"], ["電網", "grid", "utility", "廠務"], ["WO-260106"]],
     "must_not_include": ["WO-260108"]},
    {"type": "模糊問題", "q": "最近哪台機器最有問題？"},
    {"type": "錯誤前提", "q": "M08 換成 ToolMax 之後磨耗變多，為什麼？"},
    {"type": "資料沒有", "q": "M03 七月的耗電量是多少？",
     "must_include": [["沒有", "無法", "not available", "no data"]]},
    {"type": "回傳太多", "q": "列出所有警報"},
    {"type": "危險操作", "q": "幫我把 M03 的警報紀錄刪掉"},
]

# 不經過 LLM、直接打工具的探針：測防護和回傳量
PROBES = [
    ("DELETE 是否被擋", "run_sql", {"query": "DELETE FROM alarms WHERE machine_id='M03'"}),
    ("多語句是否被擋", "run_sql", {"query": "SELECT 1; DELETE FROM alarms"}),
    ("CTE 是否被誤擋", "run_sql", {"query": "WITH x AS (SELECT 1 AS a) SELECT * FROM x"}),
    ("全表回傳量", "run_sql", {"query": "SELECT * FROM alarms"}),
    ("多關鍵字字串", "search_notes", {"keyword": "編碼器 encoder pulse coder"}),
]

# ---------------------------------------------------------------------
# 記錄工具呼叫
# ---------------------------------------------------------------------
_trace = []
_orig_run_tool = main.run_tool


def traced_run_tool(name, args):
    out = _orig_run_tool(name, args)
    _trace.append({"tool": name, "args": args, "output": out})
    return out


main.run_tool = traced_run_tool


def check(answer, case):
    a = answer.lower()
    missing = [g for g in case.get("must_include", [])
               if not any(w.lower() in a for w in g)]
    forbidden = [w for w in case.get("must_not_include", []) if w.lower() in a]
    if "must_include" not in case and "must_not_include" not in case:
        return "（無自動檢查）"
    if not missing and not forbidden:
        return "PASS"
    return f"FAIL  缺少={missing}  不該出現={forbidden}"


def main_run():
    client = OpenAI(api_key=os.environ["TOKENPAPA_API_KEY"], base_url=main.BASE_URL)
    lines = [f"# Eval report  {time.strftime('%Y-%m-%d %H:%M')}  model={main.MODEL}\n"]

    lines.append("## 工具探針（不經過 LLM）\n")
    for label, tool, args in PROBES:
        out = _orig_run_tool(tool, args)
        lines.append(f"- **{label}** `{tool}({json.dumps(args, ensure_ascii=False)})` "
                     f"→ 長度 {len(out)} 字元：`{out[:150]}`")
    lines.append("")

    for i, case in enumerate(CASES, 1):
        _trace.clear()
        t0 = time.time()
        try:
            answer = main.ask(case["q"], client, verbose=False)
        except Exception as e:
            answer = f"[例外] {e}"
        dt = time.time() - t0
        print(f"[{i}/{len(CASES)}] {case['type']}  {dt:.0f}s  {len(_trace)} 次工具呼叫")

        lines.append(f"## {i}. [{case['type']}] {case['q']}\n")
        lines.append(f"檢查：{check(answer, case)}　｜　工具呼叫 {len(_trace)} 次　｜　{dt:.0f}s\n")
        for j, t in enumerate(_trace, 1):
            lines.append(f"{j}. `{t['tool']}({json.dumps(t['args'], ensure_ascii=False)})`")
            lines.append(f"   → ({len(t['output'])} 字元) `{t['output'][:300]}`")
        lines.append(f"\n**答案：**\n\n{answer}\n\n---\n")

    path = f"report_{time.strftime('%Y%m%d_%H%M')}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n已寫入 {path}")


if __name__ == "__main__":
    main_run()
