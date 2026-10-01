import re

def load_code_dict(conn):
    """啟動時載入一次警報代碼表"""
    rows = conn.execute(
        "SELECT code, name_zh, description, likely_causes, recommended_checks "
        "FROM alarm_codes"
    ).fetchall()
    return {r[0]: r[1:] for r in rows}

def annotate_codes(sql: str, rows: list, code_dict: dict) -> str:
    """找出 SQL 與結果中出現的警報代碼，回傳說明文字"""
    found = {c for c in code_dict if c in sql}               # 掃 SQL
    for row in rows:                                         # 掃結果
        for v in row:
            if isinstance(v, str) and v in code_dict:
                found.add(v)
    if not found:
        return ""
    lines = ["\n[警報代碼說明]"]
    for c in sorted(found):
        name_zh, desc, causes, checks = code_dict[c]
        lines.append(f"{c}（{name_zh}）：{desc}\n  可能原因：{causes}\n  建議檢查：{checks}")
    return "\n".join(lines)