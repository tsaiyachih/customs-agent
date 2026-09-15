"""
驗證層：JSON → 檢查結果

此檔為系統中唯一有權判定「數值是否相符」的元件。
全部為確定性運算，無模型參與、無隨機性、可重現、可稽核。

用法：
    python3 checker.py <json路徑> [--direction import|export] [--csv 輸出路徑]
"""

import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

TOL = 0.01
FAIL, WARN, PASS, SKIP = "FAIL", "WARN", "PASS", "SKIP"

# 相對於這支腳本自己的位置算路徑（scripts/ 的上一層 data/），不寫死
# $HOME 或任何固定安裝路徑——這個 skill 資料夾放在哪裡都能動。
TARIFF_DB = Path(__file__).resolve().parent.parent / "data" / "tariff.db"


def _r(results, level, rule_id, name, detail):
    results.append({"level": level, "rule": rule_id, "name": name, "detail": detail})


def _eq(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) < tol


def _fnum(value, fmt="{:,.2f}", default="未知"):
    """數值格式化防呆：value 為 None 時回傳 default，絕不對 None 套用
    :,.2f 這類數值格式規格（f-string 對 None 做這種格式化會直接丟
    TypeError——GD-2609-004 這票的 Packing List 完全抽取失敗、
    declared_gross 是 None 時，report() 表頭就是這樣把整支腳本弄崩潰
    的）。也絕不把 None 悄悄當成 0 顯示：0 是「欄位確實是 0」，
    None 是「這個欄位沒抽到」，兩者意義不同，不能混為一談。"""
    return fmt.format(value) if value is not None else default


def _disp(value, default="-"):
    """一般字串欄位的防呆顯示：value 為 None 時顯示 default。這種欄位
    沒有 :,.2f 這類格式規格，None 不會讓程式崩潰，但直接印給人看會
    變成一個原文完全沒有的英文字面值「None」，一樣是『欄位缺』被
    誤讀成『欄位值就是 None』的假象（同一份 GD-2609-004 報表，
    inv.get('currency', '') 曾經因為 currency 存在但值是 None，
    印出「1,900.00 None」——.get(key, default) 的 default 只在
    key 不存在時生效，key 存在但值是 None 時不會用上 default，
    這個坑不是只有數值格式化會踩到，純字串接圖同樣要防。"""
    return value if value is not None else default


def _lookup_confirmed_ccc(ccc_raw: str) -> dict | None:
    """R16 專用：純查表，輸入必須是人工已確認的 CCC 號列，這裡不做任何
    比對或猜測——「哪個號列符合這個品名」是 tariff_lookup.py／模型判斷的事，
    不屬於 checker.py 的確定性保證範圍。查無資料庫或查無該號列都回傳 None，
    由呼叫端決定要 SKIP 還是別的處理，不在這裡下判斷。"""
    if not TARIFF_DB.exists():
        return None
    digits = re.sub(r"\D", "", ccc_raw)
    if len(digits) != 11:
        return None
    con = sqlite3.connect(str(TARIFF_DB))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM tariff_lines WHERE ccc_code_digits=?", (digits,)
    ).fetchone()
    if row is None:
        con.close()
        return None
    result = dict(row)
    for direction in ("import", "export"):
        codes = [c for c in (result.get(f"{direction}_reg_codes") or "").split(",") if c]
        resolved = []
        for c in codes:
            if c == "(空白)":
                resolved.append({"code": c, "desc_zh": "准許（免除簽發許可證）"})
                continue
            r = con.execute(
                "SELECT desc_zh FROM regulation_codes WHERE direction=? AND code=?",
                (direction, c),
            ).fetchone()
            resolved.append({"code": c, "desc_zh": r["desc_zh"] if r else "（資料庫查無此代號說明）"})
        result[f"{direction}_reg_resolved"] = resolved
    con.close()
    return result


CORE_FIELDS = [
    ("invoice.invoice_no", lambda i, p, c: i.get("invoice_no")),
    ("invoice.items", lambda i, p, c: i.get("items")),
    ("invoice.date", lambda i, p, c: i.get("date")),
    ("packing_list.invoice_no", lambda i, p, c: p.get("invoice_no")),
    ("packing_list.containers", lambda i, p, c: c),
    ("packing_list.declared_gross", lambda i, p, c: p.get("declared_gross")),
]


DIRECTION_LABEL = {"import": "進口", "export": "出口"}
DOC_LABEL = {"BL": "B/L", "AWB": "AWB", "SO": "SO／訂艙確認書"}


def run_checks(doc: dict, direction: str | None = None) -> list:
    r = []
    inv = doc.get("invoice") or {}
    pl = doc.get("packing_list") or {}
    # transport_document 為新欄位；bill_of_lading 為舊版相容
    bl = doc.get("transport_document") or doc.get("bill_of_lading") or {}
    cs = pl.get("containers") or []
    direction = direction or doc.get("trade_direction")

    # --- R0 抽取完整性（守門規則） ---
    # 解析器與特定版面耦合。換一家供應商可能整份抽不出來，
    # 此時其餘規則會因缺欄位而 SKIP，少數僅需單一欄位的規則卻可能誤報 PASS。
    # 因此先檢查抽取完整性；不足時，本次結果一律不得出現 PASS。
    missing = [n for n, f in CORE_FIELDS if not f(inv, pl, cs)]
    incomplete = len(missing) > len(CORE_FIELDS) // 2
    if missing:
        _r(r, FAIL if incomplete else WARN, "R0", "抽取完整性",
           f"缺 {len(missing)}/{len(CORE_FIELDS)} 項核心欄位："
           + "、".join(missing)
           + ("。版面可能不受支援，本次結果不足採信，"
              "請改走視覺模型抽取後重跑。" if incomplete else "。"))
    else:
        _r(r, PASS, "R0", "抽取完整性", f"{len(CORE_FIELDS)} 項核心欄位齊備")

    # --- 跨文件一致性 ---
    a, b = inv.get("invoice_no"), pl.get("invoice_no")
    if a and b:
        _r(r, PASS if a == b else FAIL, "R1", "文件編號一致",
           f"Invoice={a}｜Packing List={b}")
    else:
        _r(r, SKIP, "R1", "文件編號一致", "缺欄位")

    a, b = inv.get("date"), pl.get("date")
    if a and b:
        _r(r, PASS if a == b else FAIL, "R2", "文件日期一致", f"{a}｜{b}")
    else:
        _r(r, SKIP, "R2", "文件日期一致", "缺欄位")

    # --- Packing List 內部一致性 ---
    if cs and pl.get("declared_containers") is not None:
        n = len(cs)
        _r(r, PASS if n == pl["declared_containers"] else FAIL, "R3", "櫃數相符",
           f"實列 {n} 櫃｜申報 {pl['declared_containers']} 櫃")
    else:
        _r(r, SKIP, "R3", "櫃數相符", "缺欄位")

    if cs and pl.get("declared_packages") is not None:
        s = sum(c["packages"] for c in cs if c.get("packages"))
        _r(r, PASS if s == pl["declared_packages"] else FAIL, "R4", "件數加總",
           f"逐櫃加總 {s} PKG｜表尾 {pl['declared_packages']} PKG")
    else:
        _r(r, SKIP, "R4", "件數加總", "缺欄位")

    if cs and pl.get("declared_net") is not None and pl.get("declared_gross") is not None:
        sn = sum(c["net_weight"] for c in cs if c.get("net_weight"))
        sg = sum(c["gross_weight"] for c in cs if c.get("gross_weight"))
        ok = _eq(sn, pl["declared_net"]) and _eq(sg, pl["declared_gross"])
        _r(r, PASS if ok else FAIL, "R5", "重量加總",
           f"N.W. {sn:,.2f}／{pl['declared_net']:,.2f}"
           f"｜G.W. {sg:,.2f}／{pl['declared_gross']:,.2f}")
    elif cs:
        # 逐櫃列有，但 Packing List 表尾申報淨重／毛重缺其中之一——不能只
        # 印「缺欄位」三個字了事，也不能把缺的那個當 0 硬算，兩個來源都
        # 齊備才做得了加總比對，缺哪個就明說哪個，讓人知道要去原文找哪
        # 一個數字。
        missing = [n for n, v in (("Packing List 淨重合計 (declared_net)", pl.get("declared_net")),
                                   ("Packing List 毛重合計 (declared_gross)", pl.get("declared_gross")))
                   if v is None]
        _r(r, SKIP, "R5", "重量加總",
           "缺：" + "、".join(missing) + "，無法核對重量加總")
    else:
        _r(r, SKIP, "R5", "重量加總", "缺欄位")

    # --- 物理常識 ---
    bad = [c["cntr_no"] for c in cs
           if c.get("net_weight") is not None and c.get("gross_weight") is not None
           and c["net_weight"] >= c["gross_weight"]]
    if cs:
        _r(r, FAIL if bad else PASS, "R6", "淨重須小於毛重",
           f"{len(bad)}/{len(cs)} 櫃淨重≥毛重，未計包裝重" if bad else "全部通過")
    else:
        _r(r, SKIP, "R6", "淨重須小於毛重", "缺櫃列")

    # --- 跨文件數量 ---
    items = inv.get("items") or []
    # 有些 Packing List 版面（如獨立的 CONTAINER SUMMARY 表）只列件數/淨重/
    # 毛重，逐櫃不記數量——這種情況下 cs 非空但每一櫃的 qty 都是 None，
    # 若不特別處理，加總會得到 0 再拿去跟 Invoice 數量比，變成「明明沒有
    # 這項資料，卻回報成數字對不上」的假 FAIL。改成：完全沒有任何一櫃
    # 帶 qty 時視為『這份文件沒有可比對的逐櫃數量』，SKIP，不是 FAIL；
    # 只要有任何一櫃帶了 qty（即使並非每一櫃都有），才照舊加總比對——
    # 部分缺漏會讓加總偏低，這仍是有意義的訊號，不特別處理。
    if cs and items and any(c.get("qty") for c in cs) and items[-1].get("qty") is not None:
        s = sum(c["qty"] for c in cs if c.get("qty"))
        iq = items[-1]["qty"]
        _r(r, PASS if _eq(s, iq) else FAIL, "R7", "數量跨文件相符",
           f"Packing List {s:,.0f}｜Invoice {iq:,.0f} {items[-1].get('unit','')}")
    elif cs and items and any(c.get("qty") for c in cs):
        _r(r, SKIP, "R7", "數量跨文件相符", "Invoice 缺最後一項品項數量（items[-1].qty），無法比對")
    elif cs and items:
        _r(r, SKIP, "R7", "數量跨文件相符", "Packing List 各櫃未列數量（僅有件數/重量），無法逐櫃比對")
    else:
        _r(r, SKIP, "R7", "數量跨文件相符", "缺欄位")

    # --- Invoice 算術 ---
    if items and all(items[0].get(f) is not None for f in ("qty", "unit_price", "amount")):
        it = items[0]
        calc = it["qty"] * it["unit_price"]
        _r(r, PASS if _eq(calc, it["amount"], 1) else FAIL, "R8", "金額計算",
           f"{it['qty']:,.0f} × {it['unit_price']:,.2f} = {calc:,.2f}"
           f"｜申報 {it['amount']:,.2f}")
    elif items:
        # 用 is not None 逐欄檢查，不是 truthy 檢查——單價 0（贈品／樣品）
        # 是合法值，不該被當成「缺欄位」誤判成 SKIP。
        missing = [f for f in ("qty", "unit_price", "amount") if items[0].get(f) is None]
        _r(r, SKIP, "R8", "金額計算", "品項 1 缺：" + "、".join(missing))
    else:
        _r(r, SKIP, "R8", "金額計算", "缺欄位")

    # --- 申報價格基礎（進口採 CIF、出口採 FOB） ---
    term = inv.get("incoterm")
    if not direction:
        _r(r, WARN, "R9", "申報價格基礎",
           "未能判定進出口方向，價格基礎規則無法適用。"
           "進口以 CIF 為完稅價格、出口以 FOB 申報，兩者調整方向相反，"
           "請以 --direction import|export 指定後重跑。")
    elif not term:
        _r(r, SKIP, "R9", "申報價格基礎", "未辨識貿易條件")
    elif direction == "import":
        if term != "CIF":
            missing = [n for n, k in (("運費", "freight"), ("保險費", "insurance"))
                       if not inv.get(k)]
            _r(r, WARN, "R9", "申報價格基礎",
               f"進口採 CIF 完稅價格；貿易條件為 {term}，需加計"
               + ("、".join(missing) + "（文件未載明）" if missing else "運費與保險費"))
        else:
            _r(r, PASS, "R9", "申報價格基礎", "進口 CIF，無須調整")
    else:  # export
        if term in {"CIF", "CIP", "CFR", "CPT", "DAP", "DDP", "DPU"}:
            _r(r, WARN, "R9", "申報價格基礎",
               f"出口以 FOB 申報；貿易條件為 {term}，價格已含運費"
               f"{'與保險費' if term in {'CIF', 'CIP'} else ''}，"
               "須自成交價格中扣除後申報。")
        else:
            _r(r, PASS, "R9", "申報價格基礎", f"出口 {term}，已為離岸價格基礎")

    # --- 幣別 ---
    fc = (inv.get("freight") or {}).get("currency")
    if inv.get("currency") and fc and inv["currency"] != fc:
        _r(r, WARN, "R10", "幣別一致",
           f"貨價 {inv['currency']}、運費 {fc}，需分別依當旬匯率換算")
    else:
        _r(r, PASS if inv.get("currency") else SKIP, "R10", "幣別一致",
           inv.get("currency") or "未辨識")

    # --- 櫃號格式 ISO 6346 ---
    if cs:
        bad = [c["cntr_no"] for c in cs
               if not re.fullmatch(r"[A-Z]{4}\d{7}", c["cntr_no"] or "")]
        _r(r, FAIL if bad else PASS, "R11", "櫃號格式",
           "、".join(bad) if bad else f"{len(cs)} 個櫃號符合 ISO 6346")
    else:
        _r(r, SKIP, "R11", "櫃號格式", "缺櫃列")

    # --- 原產地與管制規定（進出口查的規定不同） ---
    origin = (inv.get("origin") or "").strip().lower()
    if not direction:
        _r(r, WARN, "R12", "管制規定",
           "未判定方向：進口查輸入規定與輸入許可，出口查輸出規定與輸出許可，兩者不同。")
    elif direction == "import":
        if origin in {"china", "prc", "中國", "中国"}:
            _r(r, WARN, "R12", "輸入規定",
               "原產地為中國大陸：須查核輸入規定代號、是否需輸入許可、"
               "及有無反傾銷或平衡稅措施。許可申請耗時，應於裝船前完成確認。")
        elif origin:
            _r(r, PASS, "R12", "輸入規定", f"原產地 {origin}")
        else:
            _r(r, SKIP, "R12", "輸入規定", "未辨識原產地")
    else:  # export
        _r(r, WARN, "R12", "輸出規定",
           "出口須確認貨品是否屬管制品（化學品、食品、醫療器材、"
           "特定科技等）而需輸出許可證；管制品建議提前 3–5 個工作天確認，"
           "避免臨截關才發現文件不全。")

    # --- 稅則號列 ---
    hs = inv.get("supplier_hs_code")
    if hs:
        _r(r, WARN, "R13", "稅則號列",
           f"供應商標示 HS {hs}（出口國申報，僅為候選值）；"
           "台灣進口須申報 11 碼 CCC，後段仍須自行歸列並負申報責任。")
    else:
        _r(r, WARN, "R13", "稅則號列", "文件未標示 HS Code，須依品名與材質自行歸列。")

    # --- 運輸單據交叉核對（進口為 B/L、出口為 SO／訂艙確認書） ---
    dt = DOC_LABEL.get(bl.get("doc_type"), "運輸單據") if bl else "運輸單據"
    if direction == "export":
        absent = ("出口申報時尚未取得 B/L（裝船後才簽發），"
                  "應以 SO／訂艙確認書核對；本次未提供。")
    elif direction == "import":
        absent = "進口應以 B/L 核對；本次未提供。換單取得 D/O 前無法申報提貨。"
    else:
        absent = "未提供運輸單據。"

    bl_c = bl.get("containers") if bl else None
    if bl_c:
        a, b = {c["cntr_no"] for c in cs}, set(bl_c)
        only_pl, only_bl = sorted(a - b), sorted(b - a)
        if only_pl or only_bl:
            d = []
            if only_pl:
                d.append("僅見於 Packing List：" + "、".join(only_pl))
            if only_bl:
                d.append(f"僅見於 {dt}：" + "、".join(only_bl))
            _r(r, FAIL, "R14", f"櫃號與{dt}相符", "；".join(d))
        else:
            _r(r, PASS, "R14", f"櫃號與{dt}相符", f"{len(a)} 櫃全數相符")
    else:
        _r(r, SKIP, "R14", f"櫃號與{dt}相符", absent)

    # --- 件數與重量比對 ---
    if bl and (bl.get("total_packages") is not None or bl.get("gross_weight") is not None):
        d, lv, compared = [], PASS, False
        if bl.get("total_packages") is not None and pl.get("declared_packages") is not None:
            compared = True
            ok = bl["total_packages"] == pl["declared_packages"]
            lv = lv if ok else FAIL
            d.append(f"件數 {dt} {bl['total_packages']}／PL {pl['declared_packages']}")
        if bl.get("gross_weight") is not None and pl.get("declared_gross") is not None:
            compared = True
            ok = _eq(bl["gross_weight"], pl["declared_gross"])
            lv = lv if ok else FAIL
            d.append(f"毛重 {dt} {bl['gross_weight']:,.2f}／PL {pl['declared_gross']:,.2f}")
        if compared:
            _r(r, lv, "R15", f"件數毛重與{dt}相符", "｜".join(d))
        else:
            # {dt} 有件數或毛重、但 Packing List 對應欄位（declared_packages／
            # declared_gross）缺，兩邊湊不成一組可比的資料——之前這裡會落回
            # PASS 並印出跟「通過」意思矛盾的「缺欄位」，等於編造了一次
            # 根本沒發生過的比對，改成老實 SKIP。
            _r(r, SKIP, "R15", f"件數毛重與{dt}相符",
               f"{dt} 已載明件數/毛重，但 Packing List 缺 declared_packages"
               "／declared_gross 對應欄位，無法比對")
    else:
        _r(r, SKIP, "R15", f"件數毛重與{dt}相符", absent)

    # --- 稅則規定代號複查（純查表，非歸列）---
    # confirmed_ccc_code 只由人工填入（例如看過 tariff_lookup.py 候選後確認），
    # extract.py 不論規則式或視覺路徑都不會填這個欄位——這裡做的是「已經選定
    # 的號列在稅則書上寫了什麼輸出入規定」這種純查表動作，跟「這個品名該歸
    # 哪個號列」（R13、tariff_lookup.py 的事）是兩件不同性質的事，後者需要
    # 判斷，不屬於 checker.py 的確定性保證範圍。
    ccc = inv.get("confirmed_ccc_code")
    if not ccc:
        _r(r, SKIP, "R16", "稅則規定代號複查",
           "未提供 confirmed_ccc_code（此欄位僅由人工確認後填入，非抽取產生）")
    elif not TARIFF_DB.exists():
        _r(r, SKIP, "R16", "稅則規定代號複查",
           f"稅則資料庫不存在（{TARIFF_DB}），請先執行 build_tariff_db.py 建立。")
    else:
        row = _lookup_confirmed_ccc(ccc)
        if row is None:
            _r(r, SKIP, "R16", "稅則規定代號複查",
               f"tariff.db 查無 {ccc}，可能是新版稅則已變更號列或輸入有誤，須人工複核。")
        else:
            imp = [c for c in row["import_reg_resolved"] if c["code"] != "(空白)"]
            exp = [c for c in row["export_reg_resolved"] if c["code"] != "(空白)"]
            if imp or exp:
                parts = [f"輸入 {c['code']} {c['desc_zh']}" for c in imp]
                parts += [f"輸出 {c['code']} {c['desc_zh']}" for c in exp]
                _r(r, WARN, "R16", "稅則規定代號複查",
                   f"{row['ccc_code_display']} 帶有輸出入規定：" + "；".join(parts))
            else:
                _r(r, PASS, "R16", "稅則規定代號複查",
                   f"{row['ccc_code_display']} 無輸出入規定限制（免證）")

    # 抽取不完整時，任何「通過」都不可信 —— 一律降級為 SKIP。
    if incomplete:
        for c in r[1:]:
            if c["level"] == PASS:
                c["level"] = SKIP
                c["detail"] += "（抽取不完整，不予採信）"

    return r


def run_item_checks(doc: dict) -> list:
    """品項層級的獨立確定性檢查，跟 run_checks()（R0–R16）分開計數、
    分開呈現——R7/R8 仍只看 items[0]/items[-1]，不能讓「R0–R16 全過」
    被誤讀成「每個品項都查過」。I1/I3 是真正的數值矛盾（金額算術、
    加總跟表尾對不上），比照 R8 用 FAIL；I2 是抽取層可能缺漏的完整性
    （例如版面問題讓某一項的品名沒抓到），這是不確定不是矛盾，用 WARN，
    不影響 BLOCKED 判定。"""
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    r = []

    for it in items:
        ln = it.get("line_no")
        if (it.get("qty") is not None and it.get("unit_price") is not None
                and it.get("amount") is not None):
            calc = it["qty"] * it["unit_price"]
            _r(r, PASS if _eq(calc, it["amount"], 1) else FAIL, "I1", f"品項 {ln} 金額計算",
               f"{it['qty']:,.0f} × {it['unit_price']:,.4f} = {calc:,.2f}｜申報 {it['amount']:,.2f}")
        else:
            _r(r, SKIP, "I1", f"品項 {ln} 金額計算", "缺欄位")

    required = ("description", "qty", "unit", "unit_price", "amount")
    for it in items:
        ln = it.get("line_no")
        missing = [f for f in required if not it.get(f)]
        if missing:
            _r(r, WARN, "I2", f"品項 {ln} 欄位完整性",
               "缺：" + "、".join(missing) + "（抽取層可能沒讀到，非文件矛盾，僅供人工核對）")
        else:
            _r(r, PASS, "I2", f"品項 {ln} 欄位完整性", "齊備")

    if items and inv.get("total_amount") is not None:
        s = sum(it["amount"] for it in items if it.get("amount") is not None)
        _r(r, PASS if _eq(s, inv["total_amount"], 1) else FAIL, "I3", "品項金額加總與發票總額相符",
           f"逐項加總 {s:,.2f}｜發票表尾 {inv['total_amount']:,.2f}")
    else:
        _r(r, SKIP, "I3", "品項金額加總與發票總額相符", "缺欄位")

    return r


def write_checks_csv(path, results: list):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["level", "rule", "name", "detail"])
        w.writeheader()
        w.writerows(results)


def report(doc: dict, results: list, direction: str | None = None) -> str:
    inv = doc.get("invoice") or {}
    pl = doc.get("packing_list") or {}
    td = doc.get("transport_document") or doc.get("bill_of_lading") or {}
    direction = direction or doc.get("trade_direction")
    items = inv.get("items") or [{}]
    fr = inv.get("freight") or {}

    L = ["=" * 66, "  報關文件交叉核對報表", "=" * 66,
         f"  來源檔案      {doc.get('source_file', '-')}"
         f"（{doc.get('extraction_method', '-')}）",
         f"  申報方向      {DIRECTION_LABEL.get(direction, '未判定 — 請以 --direction 指定')}"
         f"   運輸單據 {DOC_LABEL.get(td.get('doc_type'), '未提供')}",
         f"  Invoice       {_disp(inv.get('invoice_no'))}   {_disp(inv.get('date'))}",
         f"  貿易條件      {_disp(inv.get('incoterm'))}"
         f"   {_fnum(items[-1].get('amount'))} {_disp(inv.get('currency'), '')}"
         f"   運費 {_fnum(fr.get('amount'), default='未載明')} {_disp(fr.get('currency'), '')}",
         f"  貨物          {_fnum(items[-1].get('qty'), '{:,.0f}')} {_disp(items[-1].get('unit'), '')}"
         f"   原產地 {_disp(inv.get('origin'))}",
         f"  Packing List  {_disp(pl.get('invoice_no'))}"
         f"   {len(pl.get('containers') or [])} 櫃"
         f" / {_fnum(pl.get('declared_packages'), '{:,.0f}')} 件"
         f" / {_fnum(pl.get('declared_gross'))} KGS",
         "-" * 66]

    icon = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}
    for c in results:
        L.append(f"  {icon[c['level']]} [{c['level']:4}] {c['rule']:3} {c['name']}")
        L.append(f"              {c['detail']}")

    n = {k: sum(1 for c in results if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    L += ["-" * 66,
          f"  共 {len(results)} 項｜不符 {n[FAIL]}｜待確認 {n[WARN]}"
          f"｜通過 {n[PASS]}｜略過 {n[SKIP]}",
          "  ※ 本報表為草稿輔助。送件前仍須報關人員逐項覆核，責任不因本工具而移轉。",
          "=" * 66]
    return "\n".join(L)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python3 checker.py <json路徑> [--direction import|export] [--csv 輸出路徑]")
    doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    direction = None
    if "--direction" in sys.argv:
        direction = sys.argv[sys.argv.index("--direction") + 1]
        if direction not in DIRECTION_LABEL:
            raise SystemExit("--direction 只接受 import 或 export")
    results = run_checks(doc, direction)
    print(report(doc, results, direction))

    if "--csv" in sys.argv:
        out = sys.argv[sys.argv.index("--csv") + 1]
        write_checks_csv(out, results)
        print(f"\n[checker] 已寫出 {out}")

    sys.exit(1 if any(c["level"] == FAIL for c in results) else 0)
