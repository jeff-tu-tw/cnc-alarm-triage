"""
驗證「哪台機器的保養延誤最多次？」
直接查 plant.db 的 pm_plan 表，用來對照 AI 的回答。

執行：python check_pm_delay.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(r"D:\\pythonproject\\cnc-alarm-triage\\data\\plant.db")

# 若實際欄位名稱不同，只要改這裡
TABLE = "pm_plan"
COL_MACHINE = "machine_id"
COL_PLANNED = "planned_date"
COL_ACTUAL = "done_date"

# QUERY = f"""
# SELECT {COL_MACHINE},
#        SUM(CASE WHEN {COL_ACTUAL} > {COL_PLANNED} THEN 1 ELSE 0 END) AS late_done,
#        SUM(CASE WHEN {COL_ACTUAL} IS NULL THEN 1 ELSE 0 END)         AS not_done
# FROM {TABLE}
# GROUP BY {COL_MACHINE}
# ORDER BY late_done DESC;
# """
QUERY = f"""
SELECT * FROM sqlite_master WHERE type='table' AND name='{TABLE}';
""" 


def print_table(headers, rows):
    """簡單的對齊輸出，不需要額外套件。"""
    widths = [len(str(h)) for h in headers]
    for row in rows:
        widths = [max(w, len(str(v))) for w, v in zip(widths, row)]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def main():
    if not DB_PATH.exists():
        sys.exit(f"找不到資料庫：{DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1. 先列出欄位，確認名稱對不對
    cur.execute(f"PRAGMA table_info({TABLE});")
    cols = [r[1] for r in cur.fetchall()]
    if not cols:
        conn.close()
        sys.exit(f"資料庫裡沒有 {TABLE} 這張表")
    print(f"[{TABLE} 欄位] {cols}\n")

    # 2. 看前幾筆資料，確認日期格式（ISO 格式字串比較才正確）
    cur.execute(f"SELECT * FROM {TABLE} LIMIT 3;")
    # print("[前 3 筆資料]")
    # print_table(cols, cur.fetchall())
    print()

    # 3. 執行驗證查詢
    try:
        cur.execute(QUERY)
    except sqlite3.OperationalError as e:
        conn.close()
        sys.exit(f"查詢失敗：{e}\n請依照上方欄位名稱，修改檔案開頭的 COL_* 設定。")

    headers = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print("[延誤統計]")
    print_table(headers, rows)

    conn.close()


if __name__ == "__main__":
    main()