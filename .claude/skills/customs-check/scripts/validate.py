"""
驗證層（補充）：抓「抽取層抓到了值、但值本身不合理／格式不合法」這類問題。

純 Python、確定性運算，不呼叫模型、不做「哪個值才對」的推理——這點跟
checker.py 一樣，但**檢查的性質不同**：

- `checker.py`：判斷「數值是否相符」——單一發票內部算術（I1/I3）、
  跨文件一致性（R0–R16）。系統中唯一有權判定「相符」的元件（見
  `CLAUDE.md`／`SKILL.md` 的分層原則），本檔不取代它、也不引用它的
  函式，保持兩支腳本互相獨立可執行的既有慣例（跟 extract.py／
  checker.py 互不 import 的原因相同）。
- `validate.py`（本檔）：判斷「值本身合不合理」——格式、範圍、是否落在
  已知詞彙表內。跟 checker.py 的算術檢查（I1/I3）及 extract.py 自己的
  `_invoice_item_total_issue` 守門刻意有重疊：那兩處的守門只在特定執行
  路徑上跑一次（CLI 抽取當下、或 checker.py 被呼叫當下），一旦 JSON
  後續被人工編輯、或視覺抽取路徑把值直接填回 JSON（見 SKILL.md 步驟 2
  「改用視覺方式讀取」）卻沒有重新跑過那些守門，這個落差就不會再被
  查到。本檔設計成「不管這份 JSON 從哪裡來，都能獨立重新驗一次」，是
  刻意疊加的第二層防呆，不是要重做 checker.py 的權威判定——因此本檔
  的算術容差刻意收得比 checker.py 更緊（見下方常數），會出現本檔標出
  異常、但 checker.py I1/I3 仍判 PASS 的案例，這是預期行為。

輸出沿用專案既有的 case_issues 結構（`{code, severity, file, detail}`，
見 extract.py／run_case.py），severity 只用 "BLOCKING"／"REVIEW"（跟既有
慣例一致——export_erp.py 的 determine_readiness() 是用字串完全比對
`severity == "BLOCKING"` 判斷是否擋下 READY，其餘一律當 REVIEW 處理，
沒有 "BLOCK" 這個值）。每筆 issue 額外附上 `field`／`actual`／`expected`／
`diff`（不是每種檢查都有 diff）方便直接核對，不是只有一句話描述。

介面：
    validate_case(doc: dict) -> list[dict]   單一案件 JSON → issue list
    可被 run_case.py／export_erp.py 之類的呼叫端 import 使用（本次不主動
    接線進去，只提供介面——是否/何時接線是後續決定）。

CLI：
    python3 validate.py <json路徑或目錄>... [--json 輸出路徑]
    目錄輸入為非遞迴（只掃該目錄底下的 *.json，不深入子目錄），因為
    案件輸出目錄底下常有 export_erp.py 產生的巢狀 structured/ 等子資料夾，
    裡面的 JSON 跟本檔要驗證的抽取輸出 schema 不同，不應該被誤掃進來。
"""

import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402  沿用 UNITS／CURRENCIES 已知詞彙表，不另外維護一份

BLOCKING, REVIEW = "BLOCKING", "REVIEW"

# 相對於這支腳本自己的位置算路徑，不寫死安裝目錄（跟 checker.py 同做法）。
TARIFF_DB = Path(__file__).resolve().parent.parent / "data" / "tariff.db"

# extract.py 的 UNITS／CURRENCIES 是給 regex 用的純字母/符號交替字串
# （沒有 regex 特殊字元、沒有量詞），可以直接切成集合重用，不必另外
# 維護第二份詞彙表、也不會因為 extract.py 加了新單位而各自漂移。
KNOWN_UNITS = set(extract.UNITS.split("|"))
KNOWN_CURRENCIES = set(extract.CURRENCIES.split("|"))

# --- 具名容差常數（不得散落魔術數字）---------------------------------
#
# 用 Decimal 精確計算，只保留「四捨五入到分」量級的誤差空間——比
# checker.py 的 float 容差（I1/I3 用 tol=1，等於允許差到一整元）嚴格
# 很多，是使用者已確認採用的「更嚴格第二意見」（見本次對話紀錄），
# 不是為了跟 checker.py 對齊而刻意放寬。
LINE_AMOUNT_TOLERANCE = Decimal("0.01")
TOTAL_AMOUNT_TOLERANCE = Decimal("0.01")

# 日期合理區間：2015-01-01 ～ 執行當下 + 1 年（使用者指定）。下界抓
# 「年份明顯打錯」（例如少打一位變成 1926），上界容許正常的前置作業
# 前導期，但排除年份多打一位這類落到遠未來的打字錯誤。上界是相對執行
# 當下算的，因此刻意不做成模組載入時就固定的常數，改在檢查函式內即時
# 算，避免腳本長時間常駐時用到過期的上界。
DATE_MIN = date(2015, 1, 1)
DATE_MAX_FUTURE_DAYS = 365

# 常見 HS 碼位數（章 2 碼／目 4 碼／款 6 碼／國家延伸 8～10 碼）——
# source_hs_code_normalized 只是供應商標示的參考值，位數不尋常不代表
# 抽取錯誤（可能只是供應商寫法特殊），所以只用 REVIEW 提醒，不是
# BLOCKING。
USUAL_HS_CODE_LENGTHS = {6, 8, 10}

MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _issue(code, severity, file, detail, **extra):
    issue = {"code": code, "severity": severity, "file": file, "detail": detail}
    issue.update(extra)
    return issue


def _dec(x):
    """JSON 數值轉 Decimal：先轉 str 再轉 Decimal，避免直接吃 float 的
    二進位誤差（`Decimal(0.1) != Decimal('0.1')`，但 JSON 本來就只給得
    出 float／int，抽取層也一律輸出 JSON number，不是字串）。輸入不是
    數值時回傳 None，呼叫端自行決定要不要略過，不在這裡拋例外中斷整支
    腳本——遇到型別不對的髒資料應該回報一個具體的 issue，不是讓整個
    案件的驗證因為一個壞欄位而整批失敗。"""
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _doc_source_hint(doc):
    inv = doc.get("invoice") or {}
    if inv.get("_source_file"):
        return inv["_source_file"]
    if doc.get("source_file"):
        return doc["source_file"]
    files = doc.get("source_files") or []
    return files[0] if files else None


def _item_source_hint(doc, item):
    return item.get("source_document") or _doc_source_hint(doc)


def _parse_doc_date(raw: str):
    """容錯解析發票／裝箱單常見的日期寫法：3 碼英文月份縮寫 + 日 + 年，
    分隔符可有可無、可以是句點/逗號/空白混雜（"AUG.02.2026"／
    "AUG 02.2026"／"SEP. 07, 2026" 都要能解析——實際案件三種都出現過，
    見 extract.py 的日期擷取正則）。解析不出來回傳 None，由呼叫端記一條
    DATE_UNPARSEABLE，不臆測、不拋例外。"""
    if not raw:
        return None
    s = re.sub(r"[.,]", " ", raw.upper())
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split(" ")
    if len(parts) != 3:
        return None
    mon, day, year = parts
    month_num = MONTH_ABBR.get(mon[:3])
    if month_num is None:
        return None
    try:
        return date(int(year), month_num, int(day))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 各項檢查：每個函式只做一件事，回傳 issue list（可能是空 list）。
# 所有欄位存取一律走 .get()／is not None，缺欄位（None／鍵不存在）一律
# 視為「這裡沒有可比對的資料」直接跳過，不產生 issue——跟「有值但不
# 合理」是兩種性質不同的事，不能混為一談。
# ---------------------------------------------------------------------------

def check_item_count(doc: dict) -> list:
    inv = doc.get("invoice")
    if inv is None:
        return []
    items = inv.get("items") or []
    if items:
        return []
    return [_issue(
        "NO_ITEMS", BLOCKING, _doc_source_hint(doc),
        "invoice.items 為空清單，沒有任何品項可供後續核對／稅則歸列。",
        field="invoice.items",
    )]


def check_arithmetic(doc: dict) -> list:
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    issues = []

    for it in items:
        ln = it.get("line_no")
        qty, price, amount = _dec(it.get("qty")), _dec(it.get("unit_price")), _dec(it.get("amount"))
        if qty is None or price is None or amount is None:
            continue
        calc = qty * price
        diff = abs(calc - amount)
        if diff > LINE_AMOUNT_TOLERANCE:
            issues.append(_issue(
                "ARITHMETIC_LINE_MISMATCH", BLOCKING, _item_source_hint(doc, it),
                f"品項 {ln}：{qty} × {price} = {calc}，申報金額為 {amount}，"
                f"差額 {diff} 超過容差 {LINE_AMOUNT_TOLERANCE}。",
                field=f"invoice.items[line_no={ln}].amount",
                actual=str(amount), expected=str(calc), diff=str(diff),
            ))

    total = _dec(inv.get("total_amount"))
    if items and total is not None:
        amounts = [_dec(it.get("amount")) for it in items]
        present = [a for a in amounts if a is not None]
        if present:
            s = sum(present)
            diff = abs(s - total)
            if diff > TOTAL_AMOUNT_TOLERANCE:
                missing_n = len(amounts) - len(present)
                note = (f"（另有 {missing_n} 個品項缺 amount，加總只計入有值的部分，"
                         "差額可能部分來自缺漏而非金額本身有誤）" if missing_n else "")
                issues.append(_issue(
                    "ARITHMETIC_TOTAL_MISMATCH", BLOCKING, _doc_source_hint(doc),
                    f"品項金額加總 {s}，發票表尾總額 {total}，"
                    f"差額 {diff} 超過容差 {TOTAL_AMOUNT_TOLERANCE}。{note}",
                    field="invoice.total_amount",
                    actual=str(total), expected=str(s), diff=str(diff),
                ))
    return issues


def check_negative_values(doc: dict) -> list:
    """數值欄位非負——單價允許為 0（贈品／樣品是合法值，checker.py 的
    R8 註解已經記載過這件事，這裡刻意不重複判斷單價是否為 0，只查
    負值，避免跟既有領域知識互相矛盾、對贈品類發票大量誤報）。"""
    issues = []
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []

    def _neg(value, field, hint):
        d = _dec(value)
        if d is not None and d < 0:
            issues.append(_issue(
                "NEGATIVE_VALUE", BLOCKING, hint,
                f"{field} 為負值：{d}。", field=field, actual=str(d),
            ))

    for it in items:
        ln = it.get("line_no")
        hint = _item_source_hint(doc, it)
        _neg(it.get("qty"), f"invoice.items[line_no={ln}].qty", hint)
        _neg(it.get("unit_price"), f"invoice.items[line_no={ln}].unit_price", hint)
        _neg(it.get("amount"), f"invoice.items[line_no={ln}].amount", hint)

    hint = _doc_source_hint(doc)
    _neg(inv.get("total_amount"), "invoice.total_amount", hint)
    for key in ("amount",):
        for section in ("freight", "insurance"):
            block = inv.get(section) or {}
            _neg(block.get(key), f"invoice.{section}.{key}", hint)

    pl = doc.get("packing_list") or {}
    for field in ("declared_containers", "declared_packages", "declared_net", "declared_gross"):
        _neg(pl.get(field), f"packing_list.{field}", hint)
    for c in pl.get("containers") or []:
        chint = pl.get("_source_file") or hint
        for field in ("packages", "qty", "net_weight", "gross_weight"):
            _neg(c.get(field), f"packing_list.containers[{c.get('cntr_no')}].{field}", chint)

    return issues


def check_ccc_codes(doc: dict) -> list:
    """items[].confirmed_ccc_code 只由人工填入（見 checker.py 的 R16
    註解），目前沒有任何地方驗證過這個人工輸入本身的格式——這裡補上：
    格式不合法（非 11 碼數字）直接 BLOCKING（會汙染 ERP import 那一
    列）；格式合法但查無此號列，只是 REVIEW（可能新版稅則已變更號列，
    見 checker.py R16 同樣的處理原則），且只做存在性查詢，不重做 R16
    的規定代號解析——那是 checker.py 的事，這裡不重複。"""
    issues = []
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    con = None
    if TARIFF_DB.exists():
        con = sqlite3.connect(str(TARIFF_DB))
    try:
        for it in items:
            raw = it.get("confirmed_ccc_code")
            if not raw:
                continue
            ln = it.get("line_no")
            hint = _item_source_hint(doc, it)
            digits = re.sub(r"\D", "", raw)
            if len(digits) != 11:
                issues.append(_issue(
                    "INVALID_CCC_CODE_FORMAT", BLOCKING, hint,
                    f"品項 {ln} 的 confirmed_ccc_code「{raw}」正規化後為 {len(digits)} 碼"
                    "數字，台灣進口稅則號列應為 11 碼。",
                    field=f"invoice.items[line_no={ln}].confirmed_ccc_code",
                    actual=raw, expected="11 碼數字",
                ))
                continue
            if con is not None:
                row = con.execute(
                    "SELECT 1 FROM tariff_lines WHERE ccc_code_digits=?", (digits,)
                ).fetchone()
                if row is None:
                    issues.append(_issue(
                        "TARIFF_CODE_NOT_FOUND", REVIEW, hint,
                        f"品項 {ln} 的 confirmed_ccc_code「{raw}」格式合法，"
                        "但查無此號列，可能是新版稅則已變更號列或輸入有誤，須人工複核。",
                        field=f"invoice.items[line_no={ln}].confirmed_ccc_code",
                        actual=raw,
                    ))
    finally:
        if con is not None:
            con.close()
    return issues


def check_source_hs_codes(doc: dict) -> list:
    inv = doc.get("invoice") or {}
    issues = []
    for it in inv.get("items") or []:
        norm = it.get("source_hs_code_normalized")
        if not norm:
            continue
        if len(norm) not in USUAL_HS_CODE_LENGTHS:
            ln = it.get("line_no")
            issues.append(_issue(
                "SOURCE_HS_CODE_FORMAT_UNUSUAL", REVIEW, _item_source_hint(doc, it),
                f"品項 {ln} 的 source_hs_code_normalized「{norm}」為 {len(norm)} 碼，"
                f"不在常見的 HS 碼位數 {sorted(USUAL_HS_CODE_LENGTHS)} 內"
                "（僅供參考值，非最終申報號列，不影響歸列責任）。",
                field=f"invoice.items[line_no={ln}].source_hs_code_normalized",
                actual=norm,
            ))
    return issues


def check_currency(doc: dict) -> list:
    inv = doc.get("invoice") or {}
    issues = []
    hint = _doc_source_hint(doc)
    cur = inv.get("currency")
    if cur and cur not in KNOWN_CURRENCIES:
        issues.append(_issue(
            "CURRENCY_UNKNOWN_CODE", REVIEW, hint,
            f"invoice.currency「{cur}」不在已知幣別代碼集合 {sorted(KNOWN_CURRENCIES)} 內。",
            field="invoice.currency", actual=cur,
        ))
    for section in ("freight", "insurance"):
        block = inv.get(section) or {}
        sc = block.get("currency")
        if cur and sc and sc != cur:
            issues.append(_issue(
                "CURRENCY_INCONSISTENT", REVIEW, hint,
                f"invoice.currency「{cur}」與 invoice.{section}.currency「{sc}」不同，"
                "需分別依當旬匯率換算，不能直接相加。",
                field=f"invoice.{section}.currency", actual=sc, expected=cur,
            ))
    return issues


def check_dates(doc: dict) -> list:
    issues = []
    today = date.today()
    date_max = today + timedelta(days=DATE_MAX_FUTURE_DAYS)
    for section, field in (("invoice", "invoice.date"), ("packing_list", "packing_list.date")):
        raw = (doc.get(section) or {}).get("date")
        if not raw:
            continue
        hint = (doc.get(section) or {}).get("_source_file") or _doc_source_hint(doc)
        parsed = _parse_doc_date(raw)
        if parsed is None:
            issues.append(_issue(
                "DATE_UNPARSEABLE", REVIEW, hint,
                f"{field}「{raw}」無法解析為日期。",
                field=field, actual=raw,
            ))
        elif not (DATE_MIN <= parsed <= date_max):
            issues.append(_issue(
                "DATE_OUT_OF_RANGE", REVIEW, hint,
                f"{field}「{raw}」解析為 {parsed.isoformat()}，"
                f"超出合理區間 {DATE_MIN.isoformat()} ～ {date_max.isoformat()}。",
                field=field, actual=parsed.isoformat(),
                expected=f"{DATE_MIN.isoformat()} ~ {date_max.isoformat()}",
            ))
    return issues


def check_units(doc: dict) -> list:
    issues = []
    inv = doc.get("invoice") or {}
    for it in inv.get("items") or []:
        u = it.get("unit")
        if u and u not in KNOWN_UNITS:
            ln = it.get("line_no")
            issues.append(_issue(
                "UNIT_UNKNOWN", REVIEW, _item_source_hint(doc, it),
                f"品項 {ln} 的 unit「{u}」不在已知單位集合 {sorted(KNOWN_UNITS)} 內。",
                field=f"invoice.items[line_no={ln}].unit", actual=u,
            ))
    pl = doc.get("packing_list") or {}
    hint = pl.get("_source_file") or _doc_source_hint(doc)
    for c in pl.get("containers") or []:
        u = c.get("unit")
        if u and u not in KNOWN_UNITS:
            issues.append(_issue(
                "UNIT_UNKNOWN", REVIEW, hint,
                f"貨櫃 {c.get('cntr_no')} 的 unit「{u}」不在已知單位集合 {sorted(KNOWN_UNITS)} 內。",
                field=f"packing_list.containers[{c.get('cntr_no')}].unit", actual=u,
            ))
    return issues


def validate_case(doc: dict) -> list:
    """單一案件抽取輸出 JSON → issue list。呼叫端（`run_case.py`／
    `export_erp.py`／或獨立 CLI）自行決定要不要把結果併入
    `doc['case_issues']`——本函式本身不修改傳入的 doc，純函式。"""
    issues = []
    issues += check_item_count(doc)
    issues += check_arithmetic(doc)
    issues += check_negative_values(doc)
    issues += check_ccc_codes(doc)
    issues += check_source_hs_codes(doc)
    issues += check_currency(doc)
    issues += check_dates(doc)
    issues += check_units(doc)
    return issues


# 案件目錄裡除了 <case_id>.json 之外，唯一固定會出現的 *.json 是
# manual_supplements.json（見 SKILL.md 步驟 2）——這不是抽取輸出，是
# 供 extract.py／run_case.py 重跑時合併用的補充資料，schema 完全不同，
# 掃目錄時明確排除，不要讓它以「一個空案件、無異常」的假象混進批次報表。
_NON_CASE_JSON_NAMES = {"manual_supplements.json"}


def _iter_json_files(paths):
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            yield from sorted(
                f for f in p.glob("*.json") if f.name not in _NON_CASE_JSON_NAMES)
        elif p.is_file():
            yield p
        else:
            print(f"[validate] 略過不存在的路徑：{raw}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python3 validate.py <json路徑或目錄>... [--json 輸出路徑]")
    args = sys.argv[1:]
    out_json = None
    if "--json" in args:
        i = args.index("--json")
        out_json = args[i + 1]
        del args[i:i + 2]

    by_case = {}
    for f in _iter_json_files(args):
        case_id = f.stem
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[validate] {f}：無法讀取／解析（{e}），略過")
            continue
        issues = validate_case(doc)
        by_case[case_id] = issues
        if issues:
            print(f"=== {case_id}（{f}）：{len(issues)} 項 ===")
            for iss in issues:
                print(f"  [{iss['severity']}] {iss['code']}：{iss['detail']}")
        else:
            print(f"=== {case_id}（{f}）：無異常 ===")

    if out_json:
        Path(out_json).write_text(
            json.dumps(by_case, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[validate] 已寫出 {out_json}")

    sys.exit(1 if any(i["severity"] == BLOCKING for v in by_case.values() for i in v) else 0)
