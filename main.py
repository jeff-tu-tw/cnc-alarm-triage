"""
迷你版 CNC 警報分析 Agent —— 只保留核心概念，一個檔案讀完。

核心概念只有一個：
    LLM 看不到你的資料 → 你給它「工具」→ 它說要用哪個工具 →
    你的程式執行工具 → 把結果還給它 → 重複，直到它能回答。

整個檔案分四段，由上往下讀：
    第 1 段：準備資料（一個小小的 SQLite 資料庫）
    第 2 段：定義工具（告訴 LLM 它能用什麼）
    第 3 段：Agent 迴圈（整個專案最重要的 30 行）
    第 4 段：迷你 eval（怎麼知道它答得對不對）

這個版本用 OpenAI 相容格式，接 TokenPAPA 平台的 deepseek-v4-flash。

執行方式：
    pip install openai
    export TOKENPAPA_API_KEY=你的key            (Windows: $env:TOKENPAPA_API_KEY="你的key")
    python main.py "M03 為什麼一直主軸過熱？"
    python main.py --eval
"""
import json
import os
import sqlite3
import sys
from check_alarmcode import load_code_dict, annotate_codes

MODEL = "deepseek-v4-flash"
BASE_URL = "https://tokenpapa.ai/v1"

# =====================================================================
# 第 1 段：準備資料
# 用記憶體中的 SQLite，裡面埋了一個「答案」：
#   M03 的冷卻泵濾網保養原本 7/10 要做，因為趕單被延後到 8/12，
#   所以 7 月下旬開始主軸過熱（SP-101）變多，8/12 修好後就停了。
# =====================================================================
'''
db = sqlite3.connect(":memory:")
db.executescript("""
CREATE TABLE alarms (machine_id TEXT, ts TEXT, alarm_code TEXT);
CREATE TABLE work_orders (wo_id TEXT, machine_id TEXT, date TEXT, notes TEXT);

INSERT INTO alarms VALUES
  ('M01','2026-07-05 10:00','TL-201'), ('M01','2026-07-20 14:00','TL-201'),
  ('M02','2026-07-11 09:30','DR-601'), ('M02','2026-08-03 16:10','TL-201'),
  ('M03','2026-06-20 11:00','TL-201'),
  ('M03','2026-07-22 13:05','SP-101'), ('M03','2026-07-27 02:40','SP-101'),
  ('M03','2026-07-30 15:20','SP-101'), ('M03','2026-08-02 08:15','SP-101'),
  ('M03','2026-08-05 21:45','SP-101'), ('M03','2026-08-08 03:30','SP-101'),
  ('M03','2026-08-10 17:00','SP-101');

INSERT INTO work_orders VALUES
  ('WO-1','M03','2026-07-10','Coolant pump filter PM deferred - rush order, line cannot stop.'),
  ('WO-2','M03','2026-08-02','主軸過熱，冷卻後重新啟動正常'),
  ('WO-3','M01','2026-07-20','Tool wear, replaced insert.'),
  ('WO-4','M03','2026-08-12','Coolant pump filter heavily clogged (濾網嚴重堵塞). Replaced filter. Pressure back to normal.');
""")
'''
db = sqlite3.connect("D:\pythonproject\cnc-alarm-triage\data\plant.db")
CODE_DICT = load_code_dict(db)   
# =====================================================================
# 第 2 段：定義工具
# TOOLS 是給 LLM 看的「說明書」：名稱、用途、要什麼參數。
# LLM 不會執行任何東西，它只會回覆「我想用 run_sql，參數是 ...」。
# 真正執行的是下面的 run_tool()，也就是你的程式。
# =====================================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "對警報資料庫執行一個 SELECT 查詢。"
                           "寫 SQL 之前先呼叫 get_schema 確認資料表和欄位。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "用關鍵字搜尋維修工單的文字內容（中英混雜）。"
                         "請一次提供多個關鍵字，包含繁體、簡體、英文及常見同義詞，"
                         "例如 ['編碼器','编码器','encoder','pulse coder']。",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "取得資料庫所有資料表和欄位名稱。寫 SQL 之前如果不確定欄位名稱，先呼叫這個。",
            "parameters": {"type": "object", "properties": {}},
        }
    },
]


def run_tool(name, args):
    """LLM 說要用哪個工具，這裡就真的去執行，回傳文字結果。"""
    try:
        if name == "run_sql":
            query = args["query"]
            # 最基本的防護：只允許讀取。（大專案裡有三層防護）
            if not query.strip().lower().startswith("select"):
                return "錯誤：只允許 SELECT 查詢"
            rows = db.execute(query).fetchall()
            # print(f"LINE 113 query : {args['query']}")
            # print(f"LINE 114 rows : {rows}")
            return json.dumps(rows, ensure_ascii=False) + annotate_codes(query, rows, CODE_DICT)
        if name == "search_notes":
            rows = db.execute(
                "SELECT wo_id, machine_id, notes, created_ts FROM work_orders WHERE notes LIKE ?",
                (f"%{args['keyword']}%",),
            ).fetchall()
            return json.dumps(rows, ensure_ascii=False) if rows else "找不到相關工單"
        if name == "get_schema":
            rows = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            schema = {}
            for (table_name,) in rows:
                cols = db.execute(f"PRAGMA table_info({table_name})").fetchall()
                schema[table_name] = [col[1] for col in cols]
            return json.dumps(schema, ensure_ascii=False)

        return f"沒有這個工具：{name}"
    except Exception as e:
        # 錯誤不要讓程式當掉，而是告訴 LLM，讓它自己修正查詢
        return f"錯誤：{e}"


# =====================================================================
# 第 3 段：Agent 迴圈 —— 整個專案最重要的部分
# =====================================================================
SYSTEM_PROMPT = """你是 CNC 工廠的警報分析助手。
一定要先用工具查資料再回答，不可以猜。
回答時引用證據（工單編號、警報次數）。
如果資料裡沒有答案，就直接說沒有。
用使用者提問的語言回答。
主張兩件事有關聯時，要實際查時間上的重疊；判斷某台機台異常時，要和其他機台比較。
"""


def ask(question, client, verbose=True):
    # OpenAI 格式：system prompt 是對話裡的第一則訊息
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for step in range(8):  # 最多 8 輪，避免無限迴圈
        # (1) 把對話 + 工具說明書送給 LLM
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        msg = response.choices[0].message

       
        # 把 LLM 的回覆加進對話紀錄（下一輪它才記得自己說過什麼）
        assistant_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        # DeepSeek 的思考模式在工具迴圈中需要把 reasoning_content 傳回去。
        # 如果遇到跟 reasoning_content 有關的 400 錯誤，把這兩行刪掉試試。
        if getattr(msg, "reasoning_content", None):
            assistant_msg["reasoning_content"] = msg.reasoning_content
        messages.append(assistant_msg)

        # (2) 如果 LLM 沒有要用工具，代表它準備好回答了 → 結束
        if not msg.tool_calls:
            return msg.content or ""

        # (3) LLM 要用工具 → 我們執行 → 把結果放回對話
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")  # OpenAI 格式的參數是 JSON 字串
                # print(f"LINE 188 : {tc.function.name}({args})")
                output = run_tool(tc.function.name, args)
            except json.JSONDecodeError:
                args, output = {}, "錯誤：參數不是合法的 JSON，請重新呼叫"
            if verbose:
                print(f"  [第{step + 1}輪] {tc.function.name}({args})")
                print(f"           → {output[:120]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,  # 對應是哪一次工具呼叫
                "content": output,
            })
        # (4) 回到迴圈開頭，LLM 會看到工具結果，再決定下一步

    return "超過步數上限，沒有得到答案。"


# =====================================================================
# 第 4 段：迷你 eval
# 每一題寫下「答對的話，答案裡一定要出現哪些詞」。
# 每組詞裡只要出現其中一個就算有提到。
# =====================================================================
EVAL_SET = [
    {   # 單純查詢
        "question": "M03 在 2026 年 8 月有幾次 SP-101 警報？",
        "must_include": [["4"]],
    },
    {   # 根因分析：要同時提到冷卻、濾網、延後
        "question": "Why did M03 keep throwing spindle overheat alarms from late July?",
        "must_include": [["coolant", "冷卻"], ["filter", "濾網"], ["defer", "delay", "late", "postpone", "延"]],
    },
    {   # 資料裡沒有 → 應該說沒有，而不是瞎掰
        "question": "M03 七月的耗電量是多少？",
        "must_include": [["沒有", "無法", "not available", "no data"]],
    },
    {   # 跨語言搜尋：中文提問、英文工單
        "question": "有沒有工單提到編碼器的線？",
        "must_include": [["WO-260155"]],
    },
    {
        "question": "M03 的冷卻泵濾網保養週期（30 天）是否足夠？",
        "why": "6/10 清完濾網，9 天後（6/19）就出現 CL-302 → 30 天對 M03 太長",
        "must_include": [
            ["6/19", "06-19", "6月19", "6 月 19", "jun 19", "june 19"],
            ["6/10", "06-10", "6月10", "6 月 10", "9 天", "9天", "9 days", "nine days", "九天"],
            ["縮短", "太長", "過長", "不夠", "不足以", "shorten", "too long"],
        ],
    },
    {
        "question": "7/28 下午很多台機器同時報警，是機器的問題嗎？",
        "must_include": [["PW-701"], ["電網", "grid", "utility", "廠務"], ["WO-260106"]],
        "must_not_include": ["WO-260108"],   # 慢性 AX-402 問題，與停電無關
    },
]


def run_eval(client):
    passed = 0
    for item in EVAL_SET:
        answer = ask(item["question"], client, verbose=False)
        ok = all(any(word.lower() in answer.lower() for word in group) for group in item["must_include"])
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {item['question']}")
        if not ok:
            print(f"      答案：{answer[:200]}")
    print(f"\n得分：{passed}/{len(EVAL_SET)}")


if __name__ == "__main__":
    from openai import OpenAI


    client = OpenAI(api_key=os.environ["TOKENPAPA_API_KEY"], base_url=BASE_URL)
    if len(sys.argv) > 1 and sys.argv[1] == "--eval":
        run_eval(client)
    else:
        question = sys.argv[1] if len(sys.argv) > 1 else "M03 為什麼一直主軸過熱？"
        print(f"問題：{question}\n")
        print(f"\n答案：\n{ask(question, client)}")