"""
稅則候選層：逐品項自動跑檢索，產出候選清單。

直接 import tariff_lookup.py 的 search()／resolve_reg_codes()／
breadcrumb()，不重寫比對邏輯——跟 checker.py 對 run_checks() 的重用原則
一致。這支模組只負責：(1) 幫每個品項決定該用哪種檢索策略、
(2) 把查詢結果轉成固定 schema 的候選列並標上信賴等級、
(3) 產生固定字彙的 match_basis 說明。**候選永遠只是候選**：
requires_human_confirmation 恆為 true，沒有任何一列會被自動視為已歸類；
候選裡 rank=1 的號列在 items.csv 裡對應 top_candidate_ccc_code 這個中性
欄位，**只有整體狀態夠可信時才填**，不夠可信寧可留空（詳見
RELIABLE_STATUSES），不要讓一個明顯不相關的候選看起來像已經找到答案。

**「來源 HS6 範圍相符」不等於「品名也相符」，兩件事分開記錄，不要讓其中
一個掩蓋另一個**：

- `source_hs_scope_match`（bool）：這個候選是不是從供應商申報的來源 HS6
  前綴直接查表找到的——單純是「落在同一個號列範圍」，不代表品名合理。
- `description_relevance`（`HIGH`／`LOW`）：這個候選的品名裡有沒有真的
  出現查詢品名裡「有意義」的字詞——單純是字面相關度，跟來源 HS 無關。
- `overall_retrieval_status`：把上面兩個維度合成一個給人看的狀態，見下。

三種檢索策略，依序嘗試：

1. **來源 HS6 前綴直接查表**（`source_hs_prefix` 有值時優先）：直接在
   `tariff_lines` 找 `ccc_code_digits` 以該 6 碼開頭的號列——這是資料庫
   查表，不是關鍵字比對，`source_hs_scope_match=True`。但同一個 6 碼
   標題下常常還分好幾個子目（例如「其他水果汁」底下同時有芒果汁跟椰子
   汁；「其他方式調製或保藏之堅果」底下只有杏仁、開心果、胡桃，沒有
   椰子），範圍相符不代表品名也相符——逐筆再檢查
   `description_relevance`：品名裡有命中查詢字詞的標 `HIGH`，整體狀態
   才是 `STRONG_SOURCE_HS_MATCH`；完全沒命中的標 `LOW`，整體狀態是
   `SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT`——範圍對、品名對不上，
   不能當成強候選，`top_candidate_ccc_code` 不會填這種候選。查無對應
   號列時**不靜默放寬到不相關章節**，改用品名關鍵字檢索當退路，但明確
   標成 `OUTSIDE_SOURCE_HS_SCOPE`，不能讓人誤以為這些候選跟供應商申報的
   HS 一致。
2. **品名關鍵字檢索**（沒有來源 HS 時，或來源 HS 查無對應號列的退路）：
   呼叫 `tariff_lookup.search()`，並要求候選品名裡至少有一個「有意義」
   的查詢詞真的出現（`_passes_relevance()`，跟 `tariff_lookup.py` 的
   `_GENERIC_TERMS` 過濾是同一個防線的第二層，防的是 FTS 那條路徑沒有
   套用到通用詞過濾的漏洞）——這條路徑上的候選 `source_hs_scope_match`
   恆為 `False`，`description_relevance` 恆為 `HIGH`（門檻本身就是相關度
   過濾）。`full_match`（AND 全比對成功）標 `DESCRIPTION_MATCH`，退成 OR
   比對的標 `WEAK_MATCH`。
3. **查無可信候選**：以上都沒有，回傳空清單，`tariff_review_status`
   由呼叫端（`export_erp.py`）標成 `REVIEW_REQUIRED`。

用法（單一品項，人工測試用）：
    python3 tariff_candidates.py "<品名>" [--top N] [--db 路徑]
        [--hs6 6碼HS前綴]
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tariff_lookup  # noqa: E402  （唯一允許重用的地方：search() 等既有函式）

DEFAULT_DB = tariff_lookup.DEFAULT_DB
DEFAULT_TOP = 3

# 整體狀態（回應「不要優化成永遠有三個候選，要優化成相關度跟安全棄權」）：
# 只有 STRONG_SOURCE_HS_MATCH／DESCRIPTION_MATCH 夠可信，可以填
# top_candidate_ccc_code；其餘（含新增的
# SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT）仍會列在
# tariff_candidates.csv 供人工參考（完整揭露，不是藏起來），但不會被當成
# 「找到候選」呈現——「範圍對」跟「品名對」是兩件事，範圍對不代表整體
# 候選可信。
STRONG_SOURCE_HS_MATCH = "STRONG_SOURCE_HS_MATCH"
SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT = "SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT"
DESCRIPTION_MATCH = "DESCRIPTION_MATCH"
WEAK_MATCH = "WEAK_MATCH"
NO_RELIABLE_CANDIDATE = "NO_RELIABLE_CANDIDATE"
OUTSIDE_SOURCE_HS_SCOPE = "OUTSIDE_SOURCE_HS_SCOPE"

RELIABLE_STATUSES = {STRONG_SOURCE_HS_MATCH, DESCRIPTION_MATCH}
# 向後相容既有呼叫端命名
RELIABLE_CONFIDENCES = RELIABLE_STATUSES

CANDIDATE_FIELDS = [
    "case_id", "line_no", "item_description",
    "source_hs_code_normalized", "source_hs_prefix",
    "candidate_rank", "candidate_ccc_code", "candidate_description",
    "source_hs_scope_match", "description_relevance", "overall_retrieval_status",
    "tariff_rate_reference", "tariff_rate_basis",
    "import_regulations", "export_regulations",
    "match_basis", "requires_human_confirmation",
]


def _meaningful_terms(description: str) -> list:
    return [t for t in tariff_lookup.split_terms(description)
            if tariff_lookup._is_meaningful(t)]


def _relevance(description: str, row) -> str:
    """跟 HS6 範圍是否相符是兩個獨立的問題：這裡只看候選品名裡有沒有
    真的出現查詢品名裡「有意義」的字，不能只因為通用詞、字根巧合
    （如 concentrate/concentrated）就算相關。回傳 "HIGH"（有命中）或
    "LOW"（沒有，含品名本身沒有任何有意義字詞可比對的情況）。"""
    terms = _meaningful_terms(description)
    if not terms:
        return "LOW"
    haystack = f"{row['name_zh'] or ''} {row['name_en'] or ''}".upper()
    return "HIGH" if any(t.upper() in haystack for t in terms) else "LOW"


def _passes_relevance(description: str, row) -> bool:
    """第二層防線（用於品名關鍵字檢索路徑，不用於來源 HS6 直查路徑）：
    候選的品名裡至少要真的出現一個查詢裡「有意義」的字。
    `tariff_lookup.search()` 內部已經在 SQL 查詢層面做了同樣的過濾，這裡
    是在拿到結果後再檢查一次——防的是 FTS5 那條路徑（AND 全比對）沒有
    套用同一套通用詞過濾時，仍可能巧合命中不相關品名的情況。"""
    return _relevance(description, row) == "HIGH"


def _hs_prefix_rows(con: sqlite3.Connection, prefix: str, limit: int,
                     description: str | None = None) -> list:
    """來源 HS6 前綴底下常常不只一筆（同一個 6 碼標題下還分好幾個更細的
    8/11 碼子目，例如同樣是「其他水果汁」底下同時有芒果汁跟椰子汁）。
    單純照號列數字排序、直接切前 N 筆，可能把跟品名完全無關的子目排在
    真正對應的前面（芒果汁排到椰子汁前面）。這裡在『同樣屬於來源 HS6
    範圍、因此信賴等級都是 STRONG_SOURCE_HS_MATCH』的前提下，再用品名
    有意義字詞是否出現在候選品名裡做第二層排序——這仍然是確定性的字面
    比對，不是語意判斷，也不會把 HS6 範圍外的候選拉進來。"""
    rows = con.execute(
        "SELECT * FROM tariff_lines WHERE ccc_code_digits LIKE ? ORDER BY ccc_code_digits",
        (prefix + "%",),
    ).fetchall()
    if description:
        terms = _meaningful_terms(description)
        if terms:
            def _score(row):
                haystack = f"{row['name_zh'] or ''} {row['name_en'] or ''}".upper()
                return -sum(1 for t in terms if t.upper() in haystack)
            rows = sorted(rows, key=_score)
    return rows[:limit]


def _candidate_description(row) -> str:
    parts = [p for p in (row["name_zh"], row["name_en"]) if p]
    return " / ".join(parts) if parts else "（未取得）"


def _match_basis(status: str, prefix: str | None) -> str:
    return {
        STRONG_SOURCE_HS_MATCH: f"來源 HS6 前綴（{prefix}）直接對應稅則號列，且品名有命中查詢字詞",
        SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT:
            f"來源 HS6 前綴（{prefix}）範圍相符，但候選品名沒有命中查詢字詞——"
            "範圍對不代表品名對，須人工確認是否真的是這個號列",
        OUTSIDE_SOURCE_HS_SCOPE: f"來源 HS6 前綴（{prefix}）查無對應號列，以下為品名關鍵字候選，"
                                  "已超出供應商申報的 HS 範圍",
        DESCRIPTION_MATCH: "品名關鍵字（全部相符）",
        WEAK_MATCH: "品名關鍵字（部分相符，關鍵字未同時全部命中）",
    }.get(status, status)


def _row_to_candidate(con, case_id, line_no, description, prefix, rank, row,
                       source_hs_scope_match, description_relevance, overall_status):
    imp = tariff_lookup.resolve_reg_codes(con, "import", row["import_reg_codes"])
    exp = tariff_lookup.resolve_reg_codes(con, "export", row["export_reg_codes"])
    return {
        "case_id": case_id,
        "line_no": line_no,
        "item_description": description,
        "source_hs_code_normalized": None,
        "source_hs_prefix": prefix,
        "candidate_rank": rank,
        "candidate_ccc_code": row["ccc_code_display"],
        "candidate_description": _candidate_description(row),
        "source_hs_scope_match": source_hs_scope_match,
        "description_relevance": description_relevance,
        "overall_retrieval_status": overall_status,
        "tariff_rate_reference": row["rate1"],
        "tariff_rate_basis": "rate1",
        "import_regulations": tariff_lookup.format_reg_line(imp),
        "export_regulations": tariff_lookup.format_reg_line(exp),
        "match_basis": _match_basis(overall_status, prefix),
        "requires_human_confirmation": True,
    }


def build_candidates_for_item(con: sqlite3.Connection | None, case_id: str,
                               item: dict, top_n: int = DEFAULT_TOP) -> list:
    """回傳這個品項的候選列清單（可能是空清單——查無可信候選時，寧可
    回傳空清單也不要塞一個不相關的候選進去）。優化目標是相關度跟安全
    棄權，不是「每個品項都要湊滿 top_n 個候選」。"""
    if con is None:
        return []
    description = item.get("description_normalized") or item.get("description")
    prefix = item.get("source_hs_prefix")
    line_no = item.get("line_no")

    if prefix:
        hs_rows = _hs_prefix_rows(con, prefix, top_n, description)
        if hs_rows:
            out = []
            for idx, row in enumerate(hs_rows, 1):
                # 範圍相符（來自 HS6 直查）不等於品名相符——分開判斷，
                # 不要讓「範圍對」偽裝成「整體候選可信」。
                relevance = _relevance(description, row) if description else "LOW"
                status = (STRONG_SOURCE_HS_MATCH if relevance == "HIGH"
                          else SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT)
                c = _row_to_candidate(con, case_id, line_no, description, prefix, idx, row,
                                      True, relevance, status)
                c["source_hs_code_normalized"] = item.get("source_hs_code_normalized")
                out.append(c)
            return out
        # 來源 HS6 前綴查無對應號列——不要靜默放寬到不相關章節；仍可以
        # 品名關鍵字檢索當退路，但要明確標成「超出來源 HS 範圍」。
        if not description or len(description.strip()) < 3:
            return []
        rows, _full_match = tariff_lookup.search(con, description, top_n, None)
        rows = [r for r in rows if _passes_relevance(description, r)]
        out = []
        for idx, row in enumerate(rows, 1):
            c = _row_to_candidate(con, case_id, line_no, description, prefix, idx, row,
                                  False, "HIGH", OUTSIDE_SOURCE_HS_SCOPE)
            c["source_hs_code_normalized"] = item.get("source_hs_code_normalized")
            out.append(c)
        return out

    # 沒有來源 HS：純品名檢索，一樣要求相關度門檻。
    if not description or len(description.strip()) < 3:
        return []
    rows, full_match = tariff_lookup.search(con, description, top_n, None)
    rows = [r for r in rows if _passes_relevance(description, r)]
    if not rows:
        return []
    status = DESCRIPTION_MATCH if full_match else WEAK_MATCH
    return [_row_to_candidate(con, case_id, line_no, description, prefix, idx, row,
                              False, "HIGH", status)
            for idx, row in enumerate(rows, 1)]


if __name__ == "__main__":
    args = sys.argv[1:]
    positional, top_n, db_path, hs6 = [], DEFAULT_TOP, DEFAULT_DB, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--top":
            top_n = int(args[i + 1]); i += 2; continue
        if a == "--db":
            db_path = args[i + 1]; i += 2; continue
        if a == "--hs6":
            hs6 = args[i + 1]; i += 2; continue
        positional.append(a); i += 1
    if not positional:
        raise SystemExit('用法：python3 tariff_candidates.py "<品名>" [--top N] [--db 路徑] [--hs6 6碼HS前綴]')
    description = " ".join(positional)
    if not os.path.exists(db_path):
        raise SystemExit(f"[tariff_candidates] 查無 tariff.db（{db_path}），"
                          "請先執行 build_tariff_db.py 建立資料庫。")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = build_candidates_for_item(
        con, "manual-test",
        {"line_no": 1, "description": description, "source_hs_prefix": hs6}, top_n)
    con.close()
    if not rows:
        print("查無可信候選（NO_RELIABLE_CANDIDATE）——建議直接人工歸列，不要用不相關的候選湊數。")
    for row in rows:
        print(f"候選 {row['candidate_rank']}  [{row['overall_retrieval_status']}]  "
              f"（scope_match={row['source_hs_scope_match']}, "
              f"relevance={row['description_relevance']}）  "
              f"CCC {row['candidate_ccc_code']}  {row['candidate_description']}")
        print(f"  參考稅率（第一欄） {row['tariff_rate_reference']}")
        print(f"  match_basis: {row['match_basis']}")
        print(f"  輸入規定 {row['import_regulations']}")
        print(f"  輸出規定 {row['export_regulations']}")
