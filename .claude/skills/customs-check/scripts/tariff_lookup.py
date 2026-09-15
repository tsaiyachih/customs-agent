#!/usr/bin/env python3
"""
查詢層：品名關鍵字 → 候選稅則號列

對 build_tariff_db.py 建好的 tariff.db 做關鍵字檢索，回傳候選 CCC 號列
及其輸出入規定代號說明。這是檢索工具，不是歸列引擎：關鍵字比對永遠可能有
多個候選、也可能漏掉真正對的那筆，最終歸列責任在報關人員，跟 checker.py
R13 對 supplier_hs_code 的定位一致。因此本檔輸出刻意不用 PASS/WARN/FAIL/SKIP
這套判定語彙。

用法：
    python3 tariff_lookup.py "<品名關鍵字或英文品名>"
        [--top N] [--db 路徑] [--chapter 章號]
"""
import os
import re
import sqlite3
import sys

# 相對於這支腳本自己的位置算路徑，不寫死 $HOME 或任何固定安裝路徑——
# 這個 skill 資料夾放在哪裡都能動。
DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tariff.db")
DEFAULT_TOP = 5

DISCLAIMER_CANDIDATE = (
    "  ⚠ 以上為關鍵字比對之候選清單，非歸列結果；正確 11 碼 CCC 須由報關人員\n"
    "    依貨品實際構造、材質、功能綜合認定，必要時申請預先審核。"
)
DISCLAIMER_COVERAGE = (
    "  ⚠ 本資料庫僅涵蓋稅則本身之輸出入規定代號（一般許可管制項目）。\n"
    "    「戰略性高科技貨品」「兩用貨品」及禁運國別管制清單由經濟部國際\n"
    "    貿易署另行公告，不在本資料庫涵蓋範圍內，仍須另行查核。"
)


def split_terms(query: str) -> list:
    """把查詢字串切成中文連續片段與英文單字。"""
    return [t for t in re.findall(r"[一-鿿]+|[A-Za-z0-9]+", query) if t]


def get_meta(con: sqlite3.Connection, key: str):
    row = con.execute("SELECT value FROM build_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# 英文欄位裡常見的標點，比對「整個詞」前要先拿掉，不然詞尾黏著逗號/句點
# （如 "punched, for vehicles" 裡的 "punched,"）會讓詞界比對失敗。
_EN_PUNCT = [",", ".", "(", ")", ";", ":", "/", "-", "'", '"']


def _normalize_en_expr(col: str = "name_en") -> str:
    expr = f"ifnull({col}, '')"
    for ch in _EN_PUNCT:
        sql_lit = ch.replace("'", "''")  # SQL 字串字面值裡的單引號要寫成兩個
        expr = f"replace({expr}, '{sql_lit}', ' ')"
    return expr


_EN_NORMALIZED = _normalize_en_expr()


def _term_condition(term: str) -> tuple:
    """回傳 (SQL 條件字串, params)。

    短詞（≤4 字元，如 CAR、FOR、PAD）子字串比對很容易在不相干的字裡
    巧合命中——例如查 "BRAKE PAD FOR PASSENGER CAR" 時，"CAR" 會命中
    "carapax"（玳瑁殼的「甲殼」）、"carbonaceous"（含碳的）這些完全無關
    的字，稀釋掉真正相關的候選（已用實測驗證過這個坑：資料庫裡明明有
    23 筆煞車相關資料，卻被這些巧合命中的雜訊擠到候選之外）。
    因此短詞要求整個詞相符（用空白包住比對，比對前先把常見標點換成
    空白）。長詞（5 字元以上）維持子字串比對，碰撞機率低很多，還能
    順便涵蓋複數／字尾變化（screw 命中 screws）。"""
    if term.isascii() and len(term) <= 4:
        cond = f"((' ' || {_EN_NORMALIZED} || ' ') LIKE ? OR name_zh LIKE ?)"
        params = [f"% {term} %", f"%{term}%"]
    else:
        cond = "(name_en LIKE ? OR name_zh LIKE ?)"
        params = [f"%{term}%", f"%{term}%"]
    return cond, params


# 這些字在稅則品名裡到處都是（品名慣用語、狀態描述、連接詞），單獨命中
# 完全不代表品名相關——已用實際案例驗證過這個坑：「NATURAL COCONUT FLAVOR
# EXTRACT」在 AND 全比對失敗後退成 OR 比對，結果因為 "NATURAL"／"EXTRACT"
# 這兩個字剛好也出現在「豬脂肪…未以其他方法萃取（extracted）」「其他天然
# （natural）乳品」這類完全無關的品名裡，候選變成豬脂肪、家禽脂肪、乳品。
# OR 退讓時要求至少命中一個「有意義」的字，不能只靠這些通用字撐起一個
# 看起來像候選的結果。
_GENERIC_TERMS = {
    "OF", "FOR", "AND", "OR", "THE", "A", "AN", "TO", "IN", "ON", "BY",
    "WITH", "WITHOUT", "FROM", "NOT", "OTHER", "OTHERS", "NATURAL",
    "FRESH", "FROZEN", "CHILLED", "DRIED", "SMOKED", "SALTED", "COOKED",
    "RAW", "PURE", "PLAIN", "EXTRACT", "EXTRACTED", "EXTRACTS",
    "WHETHER", "INCLUDED", "ELSEWHERE", "SPECIFIED", "PRODUCT",
    "PRODUCTS", "PREPARED", "PREPARATION", "PREPARATIONS", "MIXTURE",
    "MIXTURES", "CONTAINING", "MATTER", "USE", "EDIBLE", "NON", "KIND",
    "TYPE", "TYPES", "PART", "PARTS", "GENERAL", "ETC", "ALSO", "SUCH",
    "ONLY", "MORE", "LESS", "EACH", "SAME", "MADE", "FORM", "FORMS",
    "NEW", "USED", "CONCENTRATE", "CONCENTRATED", "CONCENTRATES",
    "POWDER", "POWDERED", "LIQUID", "SOLID",
    # 這幾個字大量出現在稅則書內部交叉引用的附註文字裡（例如「...of Item
    # No. 0507.90.20.00」），是行政/結構用語，不是品名描述，單獨命中
    # 完全不代表品名相關——已用實測驗證過這個坑（一個完全查無關聯的隨機
    # 品名字串，只因為含 "ITEM" 就命中了一堆鹿茸、乳品之類毫不相干的候選）。
    "ITEM", "ITEMS", "NO", "NUMBER",
}


def _is_meaningful(term: str) -> bool:
    return term.upper() not in _GENERIC_TERMS and len(term) >= 3


def like_search(con: sqlite3.Connection, terms: list, limit: int) -> list:
    if not terms:
        return [], True
    pairs = [_term_condition(t) for t in terms]  # [(cond, params), ...] 跟 terms 對齊
    conds = [c for c, _ in pairs]
    params = [p for _, ps in pairs for p in ps]
    where_and = " AND ".join(conds)
    rows = con.execute(
        f"SELECT * FROM tariff_lines WHERE {where_and} LIMIT ?", (*params, limit)
    ).fetchall()
    if rows or len(terms) <= 1:
        return rows, True
    # 全部關鍵字都要符合（AND）查不到，才退成「符合任一關鍵字」（OR）——
    # 這種退讓match品質比較差，呼叫端要標示出來，不能讓使用者誤以為是同樣可信的候選。
    #
    # OR 的「入圍資格」只能從「有意義」的字裡取——通用連接詞/狀態詞（見
    # _GENERIC_TERMS，如 NATURAL／EXTRACT／FOR）不能單獨讓一列入圍，
    # 查詢字全是通用詞時直接回傳空清單，不要退讓出不相關的結果。
    # 但排序用的分數仍然採計「全部」關鍵字（含通用詞）：入圍門檻只管
    # 「有沒有資格出現」，通用詞只要不是唯一命中的字，一樣可以貢獻分數
    # 幫忙排序——已用真實案例驗證過這個坑：如果連通用詞的計分都排除，
    # 「BRAKE PAD FOR PASSENGER CAR」裡真正相關的煞車來令片（同時命中
    # BRAKE 跟 for vehicles 的 FOR）反而會輸給只命中 PASSENGER／CAR 兩個
    # 字、完全不相關的「小客車底盤」，排序倒退。
    meaningful_conds = [c for t, (c, _) in zip(terms, pairs) if _is_meaningful(t)]
    if not meaningful_conds:
        return [], False
    meaningful_params = [p for t, (_, p) in zip(terms, pairs) if _is_meaningful(t)]
    meaningful_params = [p for ps in meaningful_params for p in ps]
    #
    # OR 不能直接 LIMIT：稅則列是按號列（等於按章）排序存的，常見字（如
    # BALL）在前面章節（魚類、球類用品...）隨便都能撈到一堆，真正相關、
    # 同時符合多個關鍵字的那筆（例如第84章「Ball bearings」同時符合 BALL
    # 和 BEARING）可能排在後面，還沒排序就被 LIMIT 砍掉——已用真實查詢
    # （"BALL BEARING, DEEP GROOVE" 查不到滾珠軸承）驗證過這個坑。
    # 做法：按「符合幾個關鍵字」算分數，分數高的排前面，再套用 LIMIT。
    score_expr = " + ".join(f"(CASE WHEN {c} THEN 1 ELSE 0 END)" for c in conds)
    where_or = " OR ".join(meaningful_conds)
    rows = con.execute(
        f"SELECT *, ({score_expr}) AS _match_score FROM tariff_lines "
        f"WHERE {where_or} ORDER BY _match_score DESC LIMIT ?",
        (*params, *meaningful_params, limit),
    ).fetchall()
    return rows, False


def fts_search(con: sqlite3.Connection, long_terms: list, limit: int) -> list:
    if not long_terms:
        return []
    match_expr = " AND ".join(f'"{t}"' for t in long_terms)
    try:
        rows = con.execute(
            """
            SELECT tl.*, bm25(tariff_lines_fts) AS score FROM tariff_lines_fts
            JOIN tariff_lines tl ON tl.rowid = tariff_lines_fts.rowid
            WHERE tariff_lines_fts MATCH ? ORDER BY score LIMIT ?
            """,
            (match_expr, limit),
        ).fetchall()
        return rows
    except sqlite3.OperationalError:
        return []


def search(con: sqlite3.Connection, query: str, top_n: int, chapter: int | None):
    """回傳 (候選列表, full_match: bool)。full_match=False 代表關鍵字沒有同時全部
    命中，退而求其次改成「符合任一關鍵字」，結果的相關度明顯比較弱，報表要標示
    出來，不能讓使用者誤以為每個候選都是同一個信賴等級。"""
    mode = get_meta(con, "search_mode")
    terms = split_terms(query)
    long_terms = [t for t in terms if len(t) >= 3 or t.isascii()]
    short_terms = [t for t in terms if t not in long_terms]

    fts_rows = fts_search(con, long_terms, top_n * 4) if mode == "fts5_trigram" else []
    like_rows, full_match = like_search(con, terms, top_n * 4)

    if fts_rows or (full_match and like_rows):
        rows = fts_rows + like_rows
    else:
        rows = like_rows  # 只剩 OR 退讓結果

    seen = set()
    dedup = []
    for row in rows:
        code = row["ccc_code_digits"]
        if code in seen:
            continue
        seen.add(code)
        dedup.append(row)

    if chapter is not None:
        dedup = [r for r in dedup if r["chapter"] == chapter]

    full_match = full_match or bool(fts_rows)
    return dedup[:top_n], full_match


def breadcrumb(con: sqlite3.Connection, heading_code: str) -> str:
    parts = []
    code = heading_code
    seen = set()
    while code and code not in seen:
        seen.add(code)
        row = con.execute(
            "SELECT code, name_zh, parent_code FROM tariff_headings WHERE code=?", (code,)
        ).fetchone()
        if not row:
            break
        label = row["code"] + (f" {row['name_zh']}" if row["name_zh"] else "")
        parts.append(label)
        code = row["parent_code"]
    parts.reverse()
    return " › ".join(parts) if parts else f"第{heading_code[:2]}章"


def resolve_reg_codes(con: sqlite3.Connection, direction: str, codes_field: str) -> list:
    codes = [c for c in (codes_field or "").split(",") if c]
    out = []
    for c in codes:
        if c == "(空白)":
            out.append({"code": c, "desc_zh": "准許（免除簽發許可證）"})
            continue
        row = con.execute(
            "SELECT desc_zh FROM regulation_codes WHERE direction=? AND code=?", (direction, c)
        ).fetchone()
        out.append({"code": c, "desc_zh": row["desc_zh"] if row else "（資料庫查無此代號說明）"})
    return out


def format_reg_line(entries: list) -> str:
    if not entries:
        return "（空白）免證"
    return "；".join(f"{e['code']} {e['desc_zh']}" for e in entries)


def report(query: str, db_path: str, rows: list, full_match: bool, con: sqlite3.Connection) -> str:
    L = []
    L.append("=" * 70)
    L.append("  稅則候選查詢（僅供參考，非最終歸列）")
    L.append("=" * 70)
    L.append(f"  查詢關鍵字    {query}")
    L.append(f"  資料庫        {os.path.basename(db_path)}（112年版合訂本，上冊 + 下冊）")
    L.append(f"  候選筆數      {len(rows)}（依關鍵字相關度排序，非唯一正確答案）")
    if rows and not full_match:
        L.append("  ⚠ 沒有任何候選同時符合全部關鍵字，以下改列「符合其中至少一個關鍵字」的")
        L.append("    結果——相關度明顯較弱，僅供參考方向，不代表這些是合理候選。")
    L.append("-" * 70)
    if not rows:
        L.append("  查無候選——可換個關鍵字（例如用更概略的材質/功能詞）再試。")
    for i, row in enumerate(rows, 1):
        crumb = breadcrumb(con, row["heading_code"])
        imp = resolve_reg_codes(con, "import", row["import_reg_codes"])
        exp = resolve_reg_codes(con, "export", row["export_reg_codes"])
        L.append(f"  候選 {i}／{len(rows)}   CCC {row['ccc_code_display']}")
        L.append(f"              {crumb}")
        L.append(f"              品名(中) {row['name_zh'] or '（未取得）'}")
        L.append(f"              品名(英) {row['name_en'] or '（未取得）'}")
        L.append(f"              單位 {row['unit'] or '-'}   稅率 第一欄 {row['rate1'] or '-'}"
                  f"／第二欄 {row['rate2'] or '-'}／第三欄 {row['rate3'] or '-'}")
        L.append(f"              輸入規定  {format_reg_line(imp)}")
        L.append(f"              輸出規定  {format_reg_line(exp)}")
        L.append("-" * 70)
    L.append(DISCLAIMER_CANDIDATE)
    L.append(DISCLAIMER_COVERAGE)
    L.append("=" * 70)
    return "\n".join(L)


def main():
    args = sys.argv[1:]
    positional = []
    top_n = DEFAULT_TOP
    db_path = DEFAULT_DB
    chapter = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--top":
            top_n = int(args[i + 1]); i += 2; continue
        if a == "--db":
            db_path = args[i + 1]; i += 2; continue
        if a == "--chapter":
            chapter = int(args[i + 1]); i += 2; continue
        positional.append(a)
        i += 1

    if not positional:
        raise SystemExit(
            '用法：python3 tariff_lookup.py "<品名關鍵字或英文品名>" '
            "[--top N] [--db 路徑] [--chapter 章號]"
        )
    query = " ".join(positional)

    if not os.path.exists(db_path):
        raise SystemExit(
            f"[tariff_lookup] 查無 tariff.db（{db_path}），請先執行 build_tariff_db.py 建立資料庫。"
        )

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows, full_match = search(con, query, top_n, chapter)
    print(report(query, db_path, rows, full_match, con))
    con.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
