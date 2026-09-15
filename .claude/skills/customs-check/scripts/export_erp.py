"""
輸出層：核對結果 → ERP 前處理產物

**不重新實作任何驗證邏輯**——直接呼叫 checker.py 的 run_checks()（單據層級
R0–R16）與 run_item_checks()（品項層級 I1–I3），以及 tariff_candidates.py
的 build_candidates_for_item()（逐品項稅則候選檢索）。這支腳本只做：
判斷這一票的 ERP readiness、把已核對過的資料整理成人看得懂的報表跟給
ERP 用的 staging 表、列出人工待辦清單。

READY／REVIEW_REQUIRED／BLOCKED 不等於 PASS/WARN/FAIL 的簡單映射，是案件
整體層級的業務判斷（見 determine_readiness()），輸入來自四個確定性來源：
checker 的核對結果、必要欄位完整性、逐品項稅則確認狀態、case_issues
（抽取/文件層級的疑義嚴重度）。

**只有 READY 才產生 erp_import_ready.csv**——case_status 不是 READY，
就代表還有東西沒解決（不管是資料矛盾、必要欄位缺失、還是任何一個品項
稅則尚未人工確認），把這種案件的資料當「可匯入」對外流通比不匯還危險。
staging/exceptions/review 這些檔案不受此限，只要有結構化資料就會產生。

這是 prototype，不綁定任何真實 ERP 的匯入格式，見 README.md。

用法：
    python3 export_erp.py <structured_json>
        [--outdir 路徑] [--direction import|export]
        [--confirm-ccc 11碼CCC] [--confirm-ccc-item 品項序號 11碼CCC]...
        [--force-confirm]
"""

import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checker  # noqa: E402  （唯一允許重用的地方：run_checks()／run_item_checks()）
import tariff_candidates  # noqa: E402  （唯一允許重用的地方：build_candidates_for_item()）
import xlsx_writer  # noqa: E402  （純標準函式庫的 XLSX 寫出工具，不依賴 openpyxl）

FAIL, WARN, PASS, SKIP = checker.FAIL, checker.WARN, checker.PASS, checker.SKIP

READY, REVIEW_REQUIRED, BLOCKED = "READY", "REVIEW_REQUIRED", "BLOCKED"


def _disp(value, default="-"):
    """純文字報表用的防呆顯示：value 為 None 時顯示 default，不印出
    Python 的 'None' 字面值（例如 GD-2609-004 的 currency 沒抽到時，
    這裡的 .get('currency', '') 曾經印出「Invoice total: 20560.0
    None」——.get(key, default) 的 default 只在 key 不存在時生效，
    key 存在但值是 None 時不會用上）。跟 checker.py 的 _disp 是同一個
    邏輯，但這裡不 import checker._disp——這支腳本的架構明確只允許
    重用 checker.run_checks()／run_item_checks()（見檔案開頭 import
    註解），純顯示用的小工具各自維護一份，不算破例。"""
    return value if value is not None else default

# ---------------------------------------------------------------------------
# 可調整設定：以下幾張表是這支腳本「自訂業務規則」的地方，其餘一律沿用
# checker.py／tariff_candidates.py 的判斷結果，不重新推理。
# ---------------------------------------------------------------------------

# 實際 production 的 mandatory fields 應由客戶 ERP schema、報關流程及申報
# 要求共同決定；這裡只是 prototype demonstration，先列出這幾個最基本、
# 沒有它們就無法產生任何有意義 staging 資料的欄位。
ERP_MANDATORY_FIELDS = [
    ("trade_direction", lambda d: d.get("trade_direction")),
    ("invoice.invoice_no", lambda d: (d.get("invoice") or {}).get("invoice_no")),
    ("invoice.date", lambda d: (d.get("invoice") or {}).get("date")),
    ("invoice.items", lambda d: (d.get("invoice") or {}).get("items")),
    ("invoice.currency", lambda d: (d.get("invoice") or {}).get("currency")),
    ("packing_list.declared_gross",
     lambda d: (d.get("packing_list") or {}).get("declared_gross")),
]

# R13（稅則號列）在 checker.py 裡設計成永遠是 WARN——它提醒的是「供應商
# 自報的 HS 僅為候選值」。案件整體是否 READY 現在改成看『每個品項是否都
# 已人工確認稅則』（見 determine_readiness()），R13 這條提醒的目的已經被
# 這個更細的逐品項判斷取代，不該再讓它單獨擋著 READY。其餘規則
# （R9/R10/R12/R16...）的 WARN 都不做這種特殊化：它們代表的是每票都該讓
# 人看一眼的真實合規提醒，一直擋著 READY 是刻意、保守的設計，不是要修的
# bug。
SUPERSEDED_WARN_RULES = {
    "R13": lambda doc: True,
}

# 每條規則對應的簡短人工操作提示，全部手寫、固定，不由模型現場生成。
# rule_id 對不到的 fallback 成 "Review <name>"。
RULE_ACTION_HINTS = {
    "R0": "Re-run extraction (switch to vision reading if needed) — core fields missing",
    "R1": "Confirm correct invoice number",
    "R2": "Confirm correct document date",
    "R3": "Reconcile declared container count with packing list",
    "R4": "Reconcile package count totals across containers",
    "R5": "Reconcile weight totals across containers",
    "R6": "Correct net/gross weight entry (net must be less than gross)",
    "R7": "Reconcile quantity between invoice and packing list",
    "R8": "Verify unit price × quantity against invoice amount",
    "R9": "Confirm freight/insurance basis for declared value",
    "R10": "Confirm currency conversion basis (freight vs. goods currency differ)",
    "R11": "Correct container number format (ISO 6346)",
    "R12": "Review import/export regulation requirement",
    "R13": "Review tariff candidates and enter confirmed CCC per line item",
    "R14": "Reconcile container numbers against B/L or SO",
    "R15": "Reconcile package count/weight against B/L or SO",
    "R16": "Review regulation requirement for confirmed CCC",
    "I1": "Verify unit price × quantity against this line's amount",
    "I2": "Re-check extraction for this line item (field could not be read)",
    "I3": "Reconcile sum of line amounts against invoice total",
    "TARIFF_CONFIRMATION": "Review tariff candidates and enter confirmed CCC for this line",
    "AMBIGUOUS_DOCTYPE_SELECTION": "Confirm which file is authoritative for this document type; "
                                    "clean up the case folder if two files should not both be here",
    "NO_TEXT_LAYER": "Switch to vision extraction for this file and fill in the missing fields",
    "UNRECOGNIZED_FILE": "Confirm whether this file is relevant to the case",
    "PARTY_EXTRACTION_AMBIGUOUS": "Manually confirm the party name from the source document",

    # validate.py（單一欄位格式/範圍/自洽性，Decimal 精確計算）——見
    # README.md〈驗證層細分〉。措辭跟既有條目一致：講清楚下一步做什麼，
    # 不是把 code 名稱換句話說。
    "NO_ITEMS": "Re-run extraction or manually add missing line items — invoice has zero items",
    "ARITHMETIC_LINE_MISMATCH": "Verify unit price × quantity against this line's amount (Decimal-checked)",
    "ARITHMETIC_TOTAL_MISMATCH": "Reconcile sum of line amounts against invoice total (Decimal-checked)",
    "NEGATIVE_VALUE": "Correct the negative value — check source document for a sign or entry error",
    "INVALID_CCC_CODE_FORMAT": "Re-enter confirmed_ccc_code as exactly 11 digits",
    "TARIFF_CODE_NOT_FOUND": "Verify the CCC code against the current tariff schedule — may be outdated or mistyped",
    "SOURCE_HS_CODE_FORMAT_UNUSUAL": "Confirm the supplier-declared HS code length is plausible; reference only, not authoritative",
    "CURRENCY_UNKNOWN_CODE": "Manually confirm the currency code from the source document",
    "CURRENCY_INCONSISTENT": "Confirm which currency applies to each amount before converting",
    "DATE_UNPARSEABLE": "Manually confirm the date from the source document",
    "DATE_OUT_OF_RANGE": "Verify the date against the source document — likely a typo in year or digit",
    "UNIT_UNKNOWN": "Manually confirm the unit from the source document",

    # extract_crosscheck.py（規則式 vs 模型視覺閱讀，自由文字欄位交叉
    # 比對）——見 README.md〈自由文字欄位交叉比對〉。裁決一律走
    # crosscheck_review.csv + resolve 子指令，不得直接改 case JSON。
    "EXTRACTION_CROSSCHECK_MISMATCH":
        "Check the source document, fill 'resolution' in crosscheck_review.csv, "
        "then run extract_crosscheck.py resolve",
    "VISION_ONLY_NEEDS_CONFIRM":
        "Check the source document, fill 'resolution' in crosscheck_review.csv, "
        "then run extract_crosscheck.py resolve",
    "VISION_ADVISORY": "No action required — informational note only, does not block readiness",
}

SHIPMENT_HEADER_FIELDS = [
    "case_id", "trade_direction", "seller_name", "buyer_name", "shipper_name",
    "consignee_name", "invoice_no", "invoice_date", "transport_document_type",
    "transport_document_no", "vessel", "voyage", "port_of_loading",
    "port_of_discharge", "incoterm", "currency", "freight_amount",
    "freight_currency", "insurance_amount", "insurance_currency",
    "invoice_total", "origin_country", "total_packages", "total_net_weight",
    "total_gross_weight", "container_count", "case_status",
]

ITEM_FIELDS = [
    "case_id", "line_no", "description_original", "description_normalized",
    "quantity", "unit", "unit_price", "amount", "origin_country",
    "package_count", "package_type", "net_weight", "gross_weight",
    "source_hs_code_normalized", "source_hs_prefix", "top_candidate_ccc_code",
    "confirmed_ccc_code", "tariff_review_status",
]

CONTAINER_FIELDS = [
    "case_id", "container_no", "seal_no", "packages", "quantity", "unit",
    "net_weight", "gross_weight",
]

EXCEPTION_FIELDS = [
    "case_id", "severity", "rule", "field", "current_value", "reason", "required_action",
]

ERP_IMPORT_DISCLAIMER = (
    "# This is a prototype ERP-ready structured export. Production deployment\n"
    "# requires field mapping to the client's ERP import template or API.\n"
    "# Generated only because case_status == READY: all validation checks\n"
    "# passed and every line item's tariff classification has been\n"
    "# human-confirmed.\n"
)


# ---------------------------------------------------------------------------
# case_issues：讀取，不重新判斷
# ---------------------------------------------------------------------------

def _manual_supplement_resolved(doc: dict, item_checks: list) -> bool:
    """MANUAL_VISUAL_SUPPLEMENT_APPLIED 只是『曾經發生過人工視覺補件』的
    稽核紀錄，不是永久卡住 READY 的理由——一旦補件本身可稽核、齊備、且
    通過下游驗證，人工複核就算做完了，不該讓這條紀錄本身變成永遠過不了
    的關卡。三個條件同時成立才算解決（都是讀取既有欄位／既有 I1-I3
    結果，不重新做任何數值判斷）：

    1. 來源可追溯：每個補充品項都留有 `source_document`（不是空的
       `extraction_method` 標記就算數）。
    2. 欄位齊備：description/qty/unit/unit_price/amount 都不是 null。
    3. 下游驗證沒有 FAIL：`run_item_checks()`（I1/I3）沒有任何 FAIL。

    任何一項不成立，就不算解決，這條紀錄照舊出現在 review_items／
    exceptions.csv 裡卡住 READY——不是『補過就自動放行』。"""
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    supplemented = [it for it in items
                    if it.get("extraction_method") == "manual_visual_supplement"]
    if not supplemented:
        return False
    required = ("description", "qty", "unit", "unit_price", "amount")
    for it in supplemented:
        if not it.get("source_document"):
            return False
        if any(it.get(f) in (None, "") for f in required):
            return False
    return not any(c["level"] == FAIL for c in item_checks)


_FREETEXT_CROSSCHECK_REVIEW_CODES = {
    "EXTRACTION_CROSSCHECK_MISMATCH", "VISION_ONLY_NEEDS_CONFIRM",
}


def _freetext_field_resolved(doc: dict, field: str | None) -> bool:
    """extract_crosscheck.py 的 EXTRACTION_CROSSCHECK_MISMATCH／
    VISION_ONLY_NEEDS_CONFIRM 逐『欄位』產生（見該 issue 的 `field`
    鍵），不是逐『文件』或逐『整份案件』——解除也必須逐欄位判斷：
    `doc["freetext_resolutions"]`（extract_crosscheck.py 的 resolve
    子指令寫入）裡有沒有這個確切欄位路徑的裁決紀錄。同一份文件三個
    欄位不一致、人工只裁決了兩個，第三個沒有對應紀錄就不算解決，繼續
    擋 readiness——不是『這份文件曾經被人看過一次』就整類豁免。"""
    if not field:
        return False
    return any(r.get("field") == field
               for r in (doc.get("freetext_resolutions") or []))


def _effective_case_issues(doc: dict, item_checks: list | None = None) -> list:
    """NO_TEXT_LAYER 的嚴重度只在『對應文件仍缺失』時才生效——人工用視覺
    抽取補回該文件的資料後，這條歷史記錄還留著（可稽核／可在報表看到曾經
    發生過），但不再讓 readiness 卡住。MANUAL_VISUAL_SUPPLEMENT_APPLIED
    同理，見 `_manual_supplement_resolved()`；EXTRACTION_CROSSCHECK_
    MISMATCH／VISION_ONLY_NEEDS_CONFIRM 同理，見 `_freetext_field_
    resolved()`——三者都是『補齊/裁決就自動解除』只影響 readiness／
    exceptions 判斷，原始 `doc["case_issues"]`（寫進 case_status.json、
    review report／HTML 的稽核區塊）完全不受影響，永遠完整保留，不會
    因為解除而被移除或改寫。其餘 issue 類型（AMBIGUOUS_DOCTYPE_
    SELECTION、UNRECOGNIZED_FILE、PARTY_EXTRACTION_AMBIGUOUS、
    VISION_ADVISORY）沒有這種『補齊就自動解除』的判斷依據，維持原嚴重度
    （VISION_ADVISORY 本來就是 INFO，不影響 readiness，不需要解除）。"""
    effective = []
    for issue in doc.get("case_issues") or []:
        if issue.get("code") == "NO_TEXT_LAYER" and issue.get("target_doc"):
            if doc.get(issue["target_doc"]):
                continue
        if (issue.get("code") == "MANUAL_VISUAL_SUPPLEMENT_APPLIED"
                and item_checks is not None
                and _manual_supplement_resolved(doc, item_checks)):
            continue
        if (issue.get("code") in _FREETEXT_CROSSCHECK_REVIEW_CODES
                and _freetext_field_resolved(doc, issue.get("field"))):
            continue
        effective.append(issue)
    return effective


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def _missing_mandatory_fields(doc: dict) -> list:
    return [name for name, get in ERP_MANDATORY_FIELDS if not get(doc)]


def determine_readiness(doc: dict, checks: list, item_checks: list) -> dict:
    """回傳 {"status", "blocking_reasons", "review_items"}。輸入只有四個
    確定性來源：checker.run_checks()／run_item_checks() 的結果、
    ERP_MANDATORY_FIELDS 完整性、doc 既有的逐品項 confirmed_ccc_code、
    doc 既有的 case_issues——不重新做任何數值判斷，只套規則表。"""
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    blocking_reasons = []
    review_items = []

    fails = [c for c in checks if c["level"] == FAIL] + \
            [c for c in item_checks if c["level"] == FAIL]
    for c in fails:
        blocking_reasons.append(f"[{c['rule']}] {c['name']}: {c['detail']}")

    for name in _missing_mandatory_fields(doc):
        blocking_reasons.append(f"[MANDATORY_FIELD] Missing required field: {name}")

    issues = _effective_case_issues(doc, item_checks)
    for issue in issues:
        if issue["severity"] == "BLOCKING":
            blocking_reasons.append(f"[{issue['code']}] {issue['detail']}")

    if blocking_reasons:
        return {"status": BLOCKED, "blocking_reasons": blocking_reasons, "review_items": []}

    for issue in issues:
        if issue["severity"] == "REVIEW":
            review_items.append(f"[{issue['code']}] {issue['detail']}")

    for it in items:
        if not it.get("confirmed_ccc_code"):
            desc = it.get("description") or "（品名未抽取到）"
            review_items.append(
                f"[TARIFF_CONFIRMATION] 品項 {it.get('line_no')}（{desc}）"
                "尚未人工確認稅則號列")

    for c in checks:
        if c["level"] != WARN:
            continue
        superseded = SUPERSEDED_WARN_RULES.get(c["rule"])
        if superseded and superseded(doc):
            continue
        review_items.append(f"[{c['rule']}] {c['name']}: {c['detail']}")

    for c in item_checks:
        if c["level"] == WARN:
            review_items.append(f"[{c['rule']}] {c['name']}: {c['detail']}")

    if review_items:
        return {"status": REVIEW_REQUIRED, "blocking_reasons": [], "review_items": review_items}

    return {"status": READY, "blocking_reasons": [], "review_items": []}


# ---------------------------------------------------------------------------
# --confirm-ccc / --confirm-ccc-item：只做「把使用者明確給的字串寫進
# JSON」，不挑候選
# ---------------------------------------------------------------------------

def _validate_ccc(ccc_raw: str) -> str:
    digits = re.sub(r"\D", "", ccc_raw)
    if len(digits) != 11:
        raise SystemExit(f"[export_erp] CCC 必須是 11 碼數字（可含分隔符號），收到：{ccc_raw!r}")
    return digits


def apply_confirm_ccc(doc: dict, ccc_raw: str, force: bool) -> dict:
    _validate_ccc(ccc_raw)
    inv = doc.setdefault("invoice", {})
    existing = inv.get("confirmed_ccc_code")
    if existing and existing != ccc_raw and not force:
        raise SystemExit(
            f"[export_erp] invoice.confirmed_ccc_code 已有值 {existing!r}，"
            f"跟新值 {ccc_raw!r} 不同。若確定要覆寫，加上 --force-confirm 重跑。"
        )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inv["confirmed_ccc_code"] = ccc_raw
    inv["confirmed_by"] = "human"
    inv["confirmed_at"] = now
    items = inv.get("items") or []
    # 單品項發票時鏡射寫入該品項，向後相容既有 --confirm-ccc 用法；
    # 多品項時逐項確認須用 --confirm-ccc-item，這裡不猜要寫給哪一項。
    if len(items) == 1:
        items[0]["confirmed_ccc_code"] = ccc_raw
        items[0]["confirmed_by"] = "human"
        items[0]["confirmed_at"] = now
    return doc


def apply_confirm_ccc_item(doc: dict, line_no: int, ccc_raw: str, force: bool) -> dict:
    _validate_ccc(ccc_raw)
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    item = next((it for it in items if it.get("line_no") == line_no), None)
    if item is None:
        raise SystemExit(f"[export_erp] 找不到 line_no={line_no} 的品項")
    existing = item.get("confirmed_ccc_code")
    if existing and existing != ccc_raw and not force:
        raise SystemExit(
            f"[export_erp] 品項 {line_no} 的 confirmed_ccc_code 已有值 {existing!r}，"
            f"跟新值 {ccc_raw!r} 不同。若確定要覆寫，加上 --force-confirm 重跑。"
        )
    item["confirmed_ccc_code"] = ccc_raw
    item["confirmed_by"] = "human"
    item["confirmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return doc


# ---------------------------------------------------------------------------
# staging：shipment_header / items / containers / tariff_candidates
# ---------------------------------------------------------------------------

def _case_id(doc: dict, json_path: Path) -> str:
    invoice_no = (doc.get("invoice") or {}).get("invoice_no")
    return invoice_no or json_path.stem


def build_shipment_header_row(doc: dict, case_id: str, readiness: dict) -> dict:
    inv = doc.get("invoice") or {}
    pl = doc.get("packing_list") or {}
    td = doc.get("transport_document") or doc.get("bill_of_lading") or {}
    parties = doc.get("parties") or {}
    fr = inv.get("freight") or {}
    ins = inv.get("insurance") or {}
    containers = pl.get("containers") or []
    return {
        "case_id": case_id,
        "trade_direction": doc.get("trade_direction") or "",
        "seller_name": parties.get("seller_name") or "",
        "buyer_name": parties.get("buyer_name") or "",
        "shipper_name": parties.get("shipper_name") or "",
        "consignee_name": parties.get("consignee_name") or "",
        "invoice_no": inv.get("invoice_no") or "",
        "invoice_date": inv.get("date") or "",
        "transport_document_type": td.get("doc_type") or "",
        "transport_document_no": td.get("doc_no") or "",
        "vessel": td.get("vessel") or "",
        "voyage": td.get("voyage") or "",
        "port_of_loading": td.get("port_of_loading") or "",
        "port_of_discharge": td.get("port_of_discharge") or "",
        "incoterm": inv.get("incoterm") or "",
        "currency": inv.get("currency") or "",
        "freight_amount": fr.get("amount", ""),
        "freight_currency": fr.get("currency") or "",
        "insurance_amount": ins.get("amount", ""),
        "insurance_currency": ins.get("currency") or "",
        "invoice_total": inv.get("total_amount", ""),
        "origin_country": inv.get("origin") or "",
        "total_packages": pl.get("declared_packages", ""),
        "total_net_weight": pl.get("declared_net", ""),
        "total_gross_weight": pl.get("declared_gross", ""),
        "container_count": len(containers),
        "case_status": readiness["status"],
    }


def _category_context_relevant(con, description: str | None, prefix: str | None) -> bool:
    """呈現層專用的輔助判斷，**不影響任何檢索結果或 tariff_candidates.csv
    內容**：來源 HS6 範圍相符、但候選『這一筆』的品名字面沒對上時
    （SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT），用來分辨兩種不同情況——

    (a) 查詢字詞在該範圍的『類別名稱』（4 碼章名、6 碼標題名，例如
        「1108 澱粉；菊糖」）裡就找得到，只是最終那一筆更細的品名用了
        不同語彙（樹薯／manioc／cassava vs tapioca）——這是術語落差，
        範圍本身很有把握，標成「TERMINOLOGY / DOMAIN REVIEW」；
    (b) 連類別名稱都對不上（例如「乾燥椰子片」查到的整個範圍其實是
        「堅果」，完全沒有椰子相關字樣）——這是範圍跟品名真的衝突，
        標成「SOURCE HS / DESCRIPTION CONFLICT」。

    這個區分只影響 HTML 報表怎麼措辭跟歸類待辦事項，`overall_retrieval_status`
    等技術欄位完全不受影響——這是 README/HTML 說的『既有技術判定的呈現層
    解讀』，不是重新判斷。"""
    if not description or not prefix or len(prefix) < 4:
        return False
    terms = tariff_candidates._meaningful_terms(description)
    if not terms:
        return False
    texts = []
    row4 = con.execute("SELECT name_zh, name_en FROM tariff_headings WHERE code=?",
                       (prefix[:4],)).fetchone()
    if row4:
        texts.append(f"{row4['name_zh'] or ''} {row4['name_en'] or ''}")
    if len(prefix) >= 6:
        row6 = con.execute("SELECT name_zh, name_en FROM tariff_headings WHERE code=?",
                           (f"{prefix[:4]}.{prefix[4:6]}",)).fetchone()
        if row6:
            texts.append(f"{row6['name_zh'] or ''} {row6['name_en'] or ''}")
    haystack = " ".join(texts).upper()
    return any(t.upper() in haystack for t in terms)


def build_item_rows_and_candidates(doc: dict, case_id: str, tariff_db_path: Path):
    """回傳 (items.csv 列, tariff_candidates.csv 列)。稅則候選檢索直接呼叫
    tariff_candidates.build_candidates_for_item()，資料庫不存在時所有品項
    的候選一律是空清單、tariff_review_status 一律至少是 REVIEW_REQUIRED，
    不當硬錯誤讓其餘輸出跟著失敗。

    item_rows 額外帶一個底線開頭的 `_category_context_relevant` 欄位，
    只給 build_review_html() 的呈現層分類用，**不寫進 items.csv**
    （ITEM_FIELDS 沒有這個名字，write_csv 只挑 ITEM_FIELDS 裡列出的欄位）。"""
    inv = doc.get("invoice") or {}
    items = inv.get("items") or []
    con = None
    if tariff_db_path and Path(tariff_db_path).exists():
        con = sqlite3.connect(str(tariff_db_path))
        con.row_factory = sqlite3.Row

    item_rows, candidate_rows = [], []
    try:
        for it in items:
            candidates = tariff_candidates.build_candidates_for_item(con, case_id, it)
            candidate_rows.extend(candidates)
            # 只有信賴等級夠高（STRONG_SOURCE_HS_MATCH／DESCRIPTION_MATCH）
            # 才填 top_candidate_ccc_code——WEAK_MATCH／
            # OUTSIDE_SOURCE_HS_SCOPE 仍會列在 tariff_candidates.csv 供
            # 人工參考，但不能讓 items.csv 看起來像「已經找到候選」。
            reliable = (candidates and
                        candidates[0]["overall_retrieval_status"] in tariff_candidates.RELIABLE_STATUSES)
            top = candidates[0]["candidate_ccc_code"] if reliable else ""
            confirmed = it.get("confirmed_ccc_code") or ""
            description = it.get("description_normalized") or it.get("description")
            category_relevant = (con is not None and not reliable and
                                 _category_context_relevant(con, description, it.get("source_hs_prefix")))
            item_rows.append({
                "case_id": case_id,
                "line_no": it.get("line_no", ""),
                "description_original": it.get("description") or "",
                "description_normalized": it.get("description_normalized") or "",
                "quantity": it.get("qty", ""),
                "unit": it.get("unit") or "",
                "unit_price": it.get("unit_price", ""),
                "amount": it.get("amount", ""),
                "origin_country": inv.get("origin") or "",
                "package_count": "",
                "package_type": "",
                "net_weight": "",
                "gross_weight": "",
                "source_hs_code_normalized": it.get("source_hs_code_normalized") or "",
                "source_hs_prefix": it.get("source_hs_prefix") or "",
                "top_candidate_ccc_code": top,
                "confirmed_ccc_code": confirmed,
                "tariff_review_status": "CONFIRMED" if confirmed else "REVIEW_REQUIRED",
                "_category_context_relevant": category_relevant,
            })
    finally:
        if con:
            con.close()
    return item_rows, candidate_rows


def build_container_rows(doc: dict, case_id: str) -> list:
    pl = doc.get("packing_list") or {}
    rows = []
    for c in pl.get("containers") or []:
        rows.append({
            "case_id": case_id,
            "container_no": c.get("cntr_no") or "",
            "seal_no": "",
            "packages": c.get("packages", ""),
            "quantity": c.get("qty", ""),
            "unit": c.get("unit") or "",
            "net_weight": c.get("net_weight", ""),
            "gross_weight": c.get("gross_weight", ""),
        })
    return rows


def write_csv(path: Path, fieldnames: list, rows: list, header_comment: str | None = None):
    """只挑 fieldnames 裡列出的欄位寫出——呼叫端（例如 item_rows）可能帶
    底線開頭的內部呈現層欄位（如 `_category_context_relevant`，只給
    build_review_html() 用），這裡不讓它們外洩到 CSV schema 裡，也不用
    DictWriter 預設的『遇到多餘欄位就整批報錯』行為。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        if header_comment:
            f.write(header_comment)
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# exceptions.csv
# ---------------------------------------------------------------------------

def build_exception_rows(doc: dict, checks: list, item_checks: list, case_id: str,
                         item_rows: list | None = None, candidate_rows: list | None = None) -> list:
    rows = []
    for c in checks:
        if c["level"] not in (FAIL, WARN):
            continue
        rows.append({
            "case_id": case_id, "severity": "BLOCKING" if c["level"] == FAIL else "REVIEW",
            "rule": c["rule"], "field": c["name"], "current_value": c["detail"],
            "reason": c["detail"],
            "required_action": RULE_ACTION_HINTS.get(c["rule"], f"Review {c['name']}"),
        })
    for c in item_checks:
        if c["level"] not in (FAIL, WARN):
            continue
        rows.append({
            "case_id": case_id, "severity": "BLOCKING" if c["level"] == FAIL else "REVIEW",
            "rule": c["rule"], "field": c["name"], "current_value": c["detail"],
            "reason": c["detail"],
            "required_action": RULE_ACTION_HINTS.get(c["rule"], f"Review {c['name']}"),
        })

    inv = doc.get("invoice") or {}
    if item_rows is not None:
        # 用呈現層分類（跟 HTML 同一個 _human_tariff_state()）給出具體的
        # 待辦類型，取代單一籠統的 TARIFF_CONFIRMATION——這是措辭改善，
        # 不是重新判斷：分類依據仍是既有的 overall_retrieval_status 等
        # 技術欄位，這裡只是換個說法。
        by_line = {}
        for c in candidate_rows or []:
            by_line.setdefault(c["line_no"], []).append(c)
        for it in item_rows:
            if it.get("confirmed_ccc_code"):
                continue
            state = _human_tariff_state(it, by_line.get(it["line_no"], []))
            rows.append({
                "case_id": case_id, "severity": "REVIEW", "rule": state["code"],
                "field": f"items[{it['line_no']}].confirmed_ccc_code",
                "current_value": "",
                "reason": state["conclusion"],
                "required_action": state["action"],
            })
    else:
        for it in inv.get("items") or []:
            if it.get("confirmed_ccc_code"):
                continue
            rows.append({
                "case_id": case_id, "severity": "REVIEW", "rule": "TARIFF_CONFIRMATION",
                "field": f"items[{it.get('line_no')}].confirmed_ccc_code",
                "current_value": "",
                "reason": f"品項 {it.get('line_no')} 的稅則分類尚未人工確認",
                "required_action": RULE_ACTION_HINTS["TARIFF_CONFIRMATION"],
            })

    for name in _missing_mandatory_fields(doc):
        rows.append({
            "case_id": case_id, "severity": "BLOCKING", "rule": "MANDATORY_FIELD",
            "field": name, "current_value": "",
            "reason": f"Required ERP field is missing: {name}",
            "required_action": f"Provide missing {name}",
        })

    for issue in _effective_case_issues(doc, item_checks):
        # exceptions.csv 的定位是「待辦事項彙整」——INFO 級 issue
        # （目前只有 VISION_ADVISORY）設計上就不擋任何流程、不需要人工
        # 處理（見 CLAUDE.md／README.md 對 severity 三級的定義），不寫進
        # 這份清單，避免被誤讀成待辦。這不影響 determine_readiness()——
        # 那裡是直接讀 case_issues 原始的 severity 字串比對
        # （`== "BLOCKING"`／`== "REVIEW"`），本來就不受這裡的判斷式影響；
        # review_report.txt 的 EXTRACTION / DOCUMENT NOTES 區塊也是直接印
        # issue['severity'] 原始字串，同樣不受影響。之前這裡只用二元判斷
        # （非 BLOCKING 一律當 REVIEW），沒考慮到 INFO，會把 VISION_ADVISORY
        # 誤標成 REVIEW 寫進這份清單。
        if issue["severity"] not in ("BLOCKING", "REVIEW"):
            continue
        rows.append({
            "case_id": case_id,
            "severity": issue["severity"],
            "rule": issue["code"], "field": issue.get("file") or "-",
            "current_value": "", "reason": issue["detail"],
            "required_action": RULE_ACTION_HINTS.get(issue["code"], f"Review {issue['code']}"),
        })

    return rows


# ---------------------------------------------------------------------------
# review_report.txt（business-friendly，跟 checker.report() 的技術明細分開）
# ---------------------------------------------------------------------------

def build_review_report(doc: dict, checks: list, item_checks: list, readiness: dict,
                         case_id: str, generated: dict) -> str:
    inv = doc.get("invoice") or {}
    parties = doc.get("parties") or {}
    n = {k: sum(1 for c in checks if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    ni = {k: sum(1 for c in item_checks if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    td = doc.get("transport_document") or doc.get("bill_of_lading")
    docs_present = [
        ("Invoice", bool(inv)),
        ("Packing List", bool(doc.get("packing_list"))),
        (checker.DOC_LABEL.get((td or {}).get("doc_type"), "B/L or SO"), bool(td)),
    ]

    L = ["=" * 60, "CASE REVIEW", "=" * 60,
         f"Case:      {case_id}",
         f"Source(s): {', '.join(doc.get('source_files') or [doc.get('source_file') or '-'])}",
         f"Direction: {checker.DIRECTION_LABEL.get(doc.get('trade_direction'), 'unknown')}",
         "", "ERP Readiness:", f"  {readiness['status']}", "",
         "Documents"]
    for label, present in docs_present:
        L.append(f"  {'✓' if present else '✗'} {label}")

    L += ["", "Shipment-level validation (R0–R16, document-to-document cross-check)",
          f"  {n[PASS]} PASS  {n[WARN]} WARN  {n[FAIL]} FAIL  {n[SKIP]} SKIP",
          "",
          "Line-item validation (I1–I3, every commercial line separately — NOT the "
          "same coverage as R0–R16 above)",
          f"  {ni[PASS]} PASS  {ni[WARN]} WARN  {ni[FAIL]} FAIL  {ni[SKIP]} SKIP", ""]

    if readiness["blocking_reasons"] or readiness["review_items"]:
        L.append("ACTION REQUIRED")
        L.append("")
        for reason in readiness["blocking_reasons"]:
            L.append(f"[BLOCKING] {reason}")
        for item in readiness["review_items"]:
            L.append(f"[REVIEW] {item}")
        L.append("")

    L.append("PARTIES")
    for label, key in (("Seller", "seller_name"), ("Buyer", "buyer_name"),
                       ("Shipper", "shipper_name"), ("Consignee", "consignee_name")):
        L.append(f"  {label}: {parties.get(key) or 'not available'}")
    L.append("")

    L.append("CORE DATA")
    items = inv.get("items") or [{}]
    L.append(f"Invoice No:    {_disp(inv.get('invoice_no'))}")
    L.append(f"Date:          {_disp(inv.get('date'))}")
    L.append(f"Origin:        {_disp(inv.get('origin'))}")
    L.append(f"Incoterm:      {_disp(inv.get('incoterm'))}")
    L.append(f"Line items:    {len(items)}")
    L.append(f"Invoice total: {_disp(inv.get('total_amount'))} {_disp(inv.get('currency'), '')}")
    pl = doc.get("packing_list") or {}
    L.append(f"Gross Weight:  {_disp(pl.get('declared_gross'))}")
    L.append(f"Containers:    {len(pl.get('containers') or [])}")
    confirmed_n = sum(1 for it in items if it.get("confirmed_ccc_code"))
    L.append(f"Tariff confirmed: {confirmed_n}/{len(items)} line items")
    L.append("")

    if doc.get("case_issues"):
        L.append("EXTRACTION / DOCUMENT NOTES (case_issues)")
        for issue in doc["case_issues"]:
            L.append(f"  [{issue['severity']}] {issue['code']} ({issue.get('file') or '-'}): "
                     f"{issue['detail']}")
        L.append("")

    L.append("ERP EXPORT")
    L.append(f"Staging files: {'generated' if generated.get('items_csv') else 'not generated'}")
    if generated.get("erp_import_ready"):
        L.append("ERP Import-Ready CSV: generated")
    else:
        L.append(f"ERP Import-Ready CSV: not generated — case_status is {readiness['status']}")
    L.append("=" * 60)
    return "\n".join(L)


# ---------------------------------------------------------------------------
# <case_id>_staging.xlsx —— staging 層資料的合併活頁簿，方便人工檢視/交接。
# **不是新的資料來源**：每個分頁直接鏡射對應的 staging CSV（同一份
# item_rows/candidate_rows/... 清單），沒有另外算過的欄位，缺資料的地方
# 維持空白，不為了讓活頁簿看起來完整就編數字。這不是最終 ERP 匯入格式，
# 見 STAGING_WORKBOOK_NOTE。
# ---------------------------------------------------------------------------

STAGING_WORKBOOK_NOTE = (
    "ERP-neutral staging workbook. Final field mapping depends on the target ERP "
    "schema, master data, validation rules, and integration method."
)


def build_staging_workbook_sheets(case_id: str, header_row: dict, item_rows: list,
                                  container_rows: list, candidate_rows: list,
                                  checks: list, item_checks: list, exception_rows: list) -> list:
    """回傳給 xlsx_writer.write_xlsx() 的 [(分頁名, 欄位, 列), ...]。每個
    分頁的欄位/列都直接沿用既有 staging CSV 用的同一份資料結構——這支
    函式只負責『排成活頁簿』，不重新計算任何欄位。"""
    check_fields = ["level", "rule", "name", "detail"]
    return [
        ("Notes", ["note"], [
            {"note": STAGING_WORKBOOK_NOTE},
            {"note": f"case_id: {case_id}"},
            {"note": f"generated_at (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}"},
            {"note": "Sheets mirror the individual staging CSV files "
                     "(shipment_header/items/containers/tariff_candidates/checks/"
                     "item_checks/exceptions) — this workbook does not introduce new data."},
        ]),
        ("Shipment", SHIPMENT_HEADER_FIELDS, [header_row]),
        ("Items", ITEM_FIELDS, item_rows),
        ("Containers", CONTAINER_FIELDS, container_rows),
        ("Tariff Review", tariff_candidates.CANDIDATE_FIELDS, candidate_rows),
        ("Checks", check_fields, checks),
        ("Item Checks", check_fields, item_checks),
        ("Exceptions", EXCEPTION_FIELDS, exception_rows),
    ]


# ---------------------------------------------------------------------------
# review.html（自包含、無外部資源；給業務/客戶看的完整審核報告）
# ---------------------------------------------------------------------------

def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_STATUS_COPY = {
    READY: ("READY", "所有核對通過，且每個品項的稅則分類都已人工確認。"),
    REVIEW_REQUIRED: ("REVIEW REQUIRED", "核心資料沒有矛盾，但還有事項需要人工確認或補齊，見下方「待辦事項」。"),
    BLOCKED: ("BLOCKED", "文件本身有矛盾或必要資料缺失，送件前必須先修正，見下方「待辦事項」。"),
}


_HUMAN_STATE_COPY = {
    "CANDIDATE_CONFIRMATION": "CANDIDATE FOUND — HUMAN CONFIRMATION REQUIRED",
    "CLASSIFICATION_REVIEW": "SOURCE HS / DESCRIPTION CONFLICT — MANUAL CLASSIFICATION REVIEW",
    "MANUAL_TARIFF_SEARCH": "NO RELIABLE CANDIDATE — MANUAL TARIFF SEARCH REQUIRED",
    "TERMINOLOGY_REVIEW": "TERMINOLOGY / DOMAIN REVIEW REQUIRED",
}


def _human_tariff_state(item_row: dict, candidates: list) -> dict:
    """呈現層專用：把既有的技術欄位（overall_retrieval_status／
    source_hs_scope_match／description_relevance／_category_context_relevant）
    翻譯成給人看的結論／理由／下一步四個狀態之一。**不改變、不重新計算任何
    技術判定本身**——這裡只做語意翻譯，四個分支窮舉自
    `tariff_candidates.py` 已經算好的欄位，不引入新的檢索或推論。"""
    desc = item_row["description_original"] or item_row["description_normalized"] or "this item"
    reliable = [c for c in candidates
               if c["overall_retrieval_status"] in tariff_candidates.RELIABLE_STATUSES]
    scope_matched = [c for c in candidates if c["source_hs_scope_match"]]
    has_source_hs = bool(item_row["source_hs_code_normalized"])

    if reliable:
        top = reliable[0]
        return {
            "code": "CANDIDATE_CONFIRMATION",
            "label": _HUMAN_STATE_COPY["CANDIDATE_CONFIRMATION"],
            "conclusion": f"A system candidate was retrieved: {top['candidate_ccc_code']}.",
            "reason": top["match_basis"],
            "action": f"Confirm whether candidate CCC {top['candidate_ccc_code']} is appropriate "
                      f"for {desc}.",
            "top_candidate": top,
        }

    if scope_matched and item_row.get("_category_context_relevant"):
        return {
            "code": "TERMINOLOGY_REVIEW",
            "label": _HUMAN_STATE_COPY["TERMINOLOGY_REVIEW"],
            "conclusion": "Source HS scope is consistent, but terminology could not be "
                          "deterministically matched.",
            "reason": "The system does not infer domain synonyms unless explicitly configured.",
            "action": f"Source HS {item_row['source_hs_code_normalized']} aligns with a matching "
                      f"tariff scope, but the system does not deterministically equate the source "
                      f"product terminology with the retrieved tariff description for {desc}. "
                      "Human confirmation required.",
            "top_candidate": None,
        }

    if scope_matched:
        return {
            "code": "CLASSIFICATION_REVIEW",
            "label": _HUMAN_STATE_COPY["CLASSIFICATION_REVIEW"],
            "conclusion": "Source HS scope was found, but no description-consistent Taiwan CCC "
                          "line was identified.",
            "reason": "The supplier-provided HS range and retrieved Taiwan CCC descriptions do "
                      "not align closely enough with the product description.",
            "action": f"Supplier HS {item_row['source_hs_code_normalized']} matched a tariff scope, "
                      f"but no description-consistent Taiwan CCC line was retrieved for {desc}. "
                      "Review classification manually.",
            "top_candidate": None,
        }

    return {
        "code": "MANUAL_TARIFF_SEARCH",
        "label": _HUMAN_STATE_COPY["MANUAL_TARIFF_SEARCH"],
        "conclusion": "No reliable CCC candidate could be identified from the available source data.",
        "reason": ("The source documents do not contain an HS reference for this line, and "
                  "description-only retrieval produced only weak or ambiguous matches."
                  if not has_source_hs else
                  "The declared source HS has no corresponding entries in the tariff database, "
                  "and description-only retrieval produced only weak or ambiguous matches."),
        "action": f"No source HS and no reliable candidate for {desc}. Review product "
                  "composition, intended use, processing method, or technical specification "
                  "before classification.",
        "top_candidate": None,
    }


def build_review_html(doc: dict, checks: list, item_checks: list, readiness: dict,
                       case_id: str, item_rows: list, candidate_rows: list) -> str:
    inv = doc.get("invoice") or {}
    parties = doc.get("parties") or {}
    pl = doc.get("packing_list") or {}
    n = {k: sum(1 for c in checks if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    ni = {k: sum(1 for c in item_checks if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    status_label, status_desc = _STATUS_COPY[readiness["status"]]
    status_class = readiness["status"].lower()

    def row(cells):
        return "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in cells) + "</tr>"

    # 每個品項算一次呈現層決策狀態，後面的 Action Summary / Item Review
    # Summary / Tariff Review Details 三段共用同一份結果，確保三處講的是
    # 同一件事，不會各自對同一個品項給出不同措辭。
    by_line = {}
    for c in candidate_rows:
        by_line.setdefault(c["line_no"], []).append(c)
    states = {it["line_no"]: _human_tariff_state(it, by_line.get(it["line_no"], []))
              for it in item_rows}

    # ---- Shipment summary ----
    summary_rows = [
        ("Seller", parties.get("seller_name")), ("Buyer", parties.get("buyer_name")),
        ("Shipper", parties.get("shipper_name")), ("Consignee", parties.get("consignee_name")),
        ("Invoice No.", inv.get("invoice_no")), ("Invoice Date", inv.get("date")),
        ("Incoterm", inv.get("incoterm")), ("Currency", inv.get("currency")),
        ("Invoice Total", inv.get("total_amount")), ("Origin", inv.get("origin")),
        ("Packages", pl.get("declared_packages")), ("Net Weight (KGS)", pl.get("declared_net")),
        ("Gross Weight (KGS)", pl.get("declared_gross")),
        ("Containers", len(pl.get("containers") or [])),
    ]
    td_doc = doc.get("transport_document") or doc.get("bill_of_lading") or {}
    summary_rows += [
        ("Transport Doc.", f"{td_doc.get('doc_type') or '-'} {td_doc.get('doc_no') or ''}"),
        ("Vessel / Voyage", f"{td_doc.get('vessel') or '-'} / {td_doc.get('voyage') or '-'}"),
        ("Port of Loading", td_doc.get("port_of_loading")),
        ("Port of Discharge", td_doc.get("port_of_discharge")),
    ]
    summary_html = "\n".join(
        f'<div class="kv"><span class="k">{_esc(k)}</span>'
        f'<span class="v">{_esc(v) if v not in (None, "") else "Not available"}</span></div>'
        for k, v in summary_rows
    )

    # ---- Action Summary：先講結論分佈，不是先丟一堆技術細節 ----
    action_counts = {}
    for it in item_rows:
        code = states[it["line_no"]]["code"]
        action_counts[code] = action_counts.get(code, 0) + 1
    action_summary_items = "\n".join(
        f'<li class="action-{code.lower().replace("_", "-")}">'
        f'<strong>{count}</strong> — {_esc(_HUMAN_STATE_COPY[code])}</li>'
        for code, count in action_counts.items()
    ) or '<li class="action-none">No line items require tariff-related action.</li>'
    other_actions = "".join(
        f'<li class="blocking">{_esc(reason)}</li>' for reason in readiness["blocking_reasons"]
    ) + "".join(
        f'<li class="review">{_esc(item)}</li>' for item in readiness["review_items"]
        if not item.startswith("[TARIFF_CONFIRMATION]")
    )

    # ---- Item Review Summary：一列一個品項，決策狀態＋一句話下一步 ----
    item_summary_html = "\n".join(row([
        it["line_no"], it["description_original"] or it["description_normalized"],
        it["source_hs_code_normalized"] or "—",
        states[it["line_no"]]["label"],
        (states[it["line_no"]]["top_candidate"]["candidate_ccc_code"]
         if states[it["line_no"]]["top_candidate"] else "—"),
        states[it["line_no"]]["action"],
    ]) for it in item_rows) or '<tr><td colspan="6">No line items extracted.</td></tr>'

    # ---- Tariff Review Details：每個品項先給結論／理由／下一步，技術
    # 欄位（overall_retrieval_status／source_hs_scope_match／
    # description_relevance／match_basis...）收在下面的 <details> 裡，
    # 不是報表主要視覺焦點，但完全沒有拿掉——可稽核性在，只是不搶版面。
    tariff_blocks = []
    for it in item_rows:
        ln = it["line_no"]
        candidates = by_line.get(ln, [])
        state = states[ln]
        reliable = [c for c in candidates
                    if c["overall_retrieval_status"] in tariff_candidates.RELIABLE_STATUSES]
        unreliable = [c for c in candidates
                      if c["overall_retrieval_status"] not in tariff_candidates.RELIABLE_STATUSES]
        confirmed = it["confirmed_ccc_code"]

        evidence_rows = "\n".join(row([
            c["candidate_rank"], c["overall_retrieval_status"],
            "matched" if c["source_hs_scope_match"] else "—",
            c["description_relevance"], c["candidate_ccc_code"], c["candidate_description"],
            f"{c['tariff_rate_reference'] or '—'} (Reference Rate, Rate 1)",
            c["import_regulations"], c["export_regulations"], c["match_basis"],
        ]) for c in reliable) if reliable else ""
        low_conf_rows = "\n".join(row([
            c["candidate_rank"], c["overall_retrieval_status"],
            "matched" if c["source_hs_scope_match"] else "—",
            c["description_relevance"], c["candidate_ccc_code"], c["candidate_description"],
            c["match_basis"],
        ]) for c in unreliable) if unreliable else ""

        badge = (f'<span class="badge confirmed">HUMAN-CONFIRMED: {_esc(confirmed)}</span>'
                 if confirmed else '<span class="badge review">REVIEW REQUIRED — not yet confirmed</span>')

        tariff_blocks.append(f'''
        <div class="tariff-block">
          <h4>Line {_esc(ln)} — {_esc(it["description_original"] or it["description_normalized"] or "—")}</h4>
          <p class="meta">Source-provided HS reference:
            <span class="badge source">{_esc(it["source_hs_code_normalized"] or "not stated in source")}</span>
            &nbsp; {badge}</p>
          <div class="decision-card {state["code"].lower().replace("_", "-")}">
            <div class="decision-label">{_esc(state["label"])}</div>
            <div class="decision-conclusion">{_esc(state["conclusion"])}</div>
            <div class="decision-row"><span class="k">Reason</span><span class="v">{_esc(state["reason"])}</span></div>
            <div class="decision-row"><span class="k">Action</span><span class="v">{_esc(state["action"])}</span></div>
          </div>
          <details class="tech-detail">
            <summary>Technical evidence (retrieval status, HS scope, description relevance, match basis)</summary>
            {f'<table class="candidates"><thead><tr><th>Rank</th><th>overall_retrieval_status</th>'
              '<th>source_hs_scope_match</th><th>description_relevance</th>'
              '<th>Candidate CCC</th><th>Description</th><th>Reference Rate</th>'
              '<th>Import Reg.</th><th>Export Reg.</th><th>match_basis</th></tr></thead>'
              f'<tbody>{evidence_rows}</tbody></table>' if evidence_rows else
              '<p class="note">No candidate met the reliability threshold for this line.</p>'}
            {f'<p class="note" style="margin-top:.75rem">Low-confidence / conflicting results — reference only, not treated as a match:</p>'
              '<table class="candidates"><thead><tr><th>Rank</th><th>overall_retrieval_status</th>'
              '<th>source_hs_scope_match</th><th>description_relevance</th>'
              '<th>CCC</th><th>Description</th><th>match_basis</th></tr></thead>'
              f'<tbody>{low_conf_rows}</tbody></table>' if low_conf_rows else ''}
          </details>
        </div>''')
    tariff_html = "\n".join(tariff_blocks)

    # ---- Validation summary (blocking/warn first, PASS collapsed) ----
    def check_rows(results):
        ordered = ([c for c in results if c["level"] == FAIL]
                   + [c for c in results if c["level"] == WARN]
                   + [c for c in results if c["level"] == SKIP]
                   + [c for c in results if c["level"] == PASS])
        return "\n".join(
            f'<tr class="lvl-{c["level"].lower()}"><td>{c["level"]}</td><td>{_esc(c["rule"])}</td>'
            f'<td>{_esc(c["name"])}</td><td>{_esc(c["detail"])}</td></tr>' for c in ordered)

    # ---- Source traceability ----
    trace_rows = "\n".join(
        row([t.get("source_document") or "-", t.get("source_page") or "-", t.get("source_text") or "-",
             t.get("extraction_rule") or "-"])
        for t in (doc.get("source_trace") or [])
    )
    issues_html = "\n".join(
        f'<tr><td>{_esc(i["severity"])}</td><td>{_esc(i["code"])}</td>'
        f'<td>{_esc(i.get("file"))}</td><td>{_esc(i["detail"])}</td></tr>'
        for i in (doc.get("case_issues") or [])
    ) or '<tr><td colspan="4">No extraction/document ambiguity recorded.</td></tr>'

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>{_esc(case_id)} — Customs Pre-Declaration Review</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, "Segoe UI", "Noto Sans TC", sans-serif;
          background: #f7f7f5; color: #1f2328; margin: 0; padding: 0 0 4rem; }}
  header {{ background: #1f2328; color: #fff; padding: 2rem 3rem; }}
  header h1 {{ margin: 0 0 .25rem; font-size: 1.4rem; }}
  header .sub {{ opacity: .75; font-size: .9rem; }}
  main {{ max-width: 980px; margin: -2rem auto 0; padding: 0 1.5rem; }}
  section {{ background: #fff; border: 1px solid #e3e3e0; border-radius: 10px;
             padding: 1.5rem 1.75rem; margin-bottom: 1.25rem; }}
  h2 {{ font-size: 1.05rem; margin: 0 0 1rem; color: #1f2328; }}
  h4 {{ margin: 1.25rem 0 .35rem; }}
  .status-card {{ display: flex; align-items: center; gap: 1.5rem; }}
  .status-pill {{ font-weight: 700; font-size: 1.1rem; padding: .5rem 1rem; border-radius: 999px; }}
  .status-pill.ready {{ background: #e3f5e8; color: #14722f; }}
  .status-pill.review_required {{ background: #fff3d6; color: #8a5a00; }}
  .status-pill.blocked {{ background: #fde3e1; color: #a3271b; }}
  .status-counts {{ font-size: .85rem; color: #555; }}
  .kv {{ display: flex; justify-content: space-between; border-bottom: 1px dashed #eee;
         padding: .3rem 0; font-size: .9rem; }}
  .kv .k {{ color: #666; }} .kv .v {{ font-weight: 600; text-align: right; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
  th, td {{ text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #eee; vertical-align: top; }}
  th {{ color: #666; font-weight: 600; background: #fafaf8; }}
  tr.lvl-fail {{ background: #fdecea; }} tr.lvl-warn {{ background: #fff8e6; }}
  tr.lvl-skip {{ color: #999; }}
  .badge {{ display: inline-block; padding: .15rem .5rem; border-radius: 6px; font-size: .75rem;
            font-weight: 600; }}
  .badge.source {{ background: #eef1ff; color: #33399e; }}
  .badge.confirmed {{ background: #e3f5e8; color: #14722f; }}
  .badge.review {{ background: #fff3d6; color: #8a5a00; }}
  .tariff-block {{ border-top: 1px solid #eee; padding-top: 1rem; margin-top: 1rem; }}
  .tariff-block:first-of-type {{ border-top: none; margin-top: 0; }}
  .meta {{ font-size: .82rem; color: #555; }}
  /* 決策卡：每個狀態一個柔和的底色＋左邊條，狀態文字本身用中性動詞
     （found／required／matched／could not be matched），不用
     correct／confirmed／recommended 這類過度肯定的字眼。 */
  .decision-card {{ border-left: 4px solid #999; background: #f7f7f5; border-radius: 6px;
                     padding: .75rem 1rem; margin: .5rem 0 .75rem; }}
  .decision-card.candidate-confirmation {{ border-color: #2f6fed; background: #eef4ff; }}
  .decision-card.classification-review {{ border-color: #a3271b; background: #fdecea; }}
  .decision-card.manual-tariff-search {{ border-color: #a3271b; background: #fdecea; }}
  .decision-card.terminology-review {{ border-color: #8a5a00; background: #fff3d6; }}
  .decision-label {{ font-weight: 700; font-size: .82rem; letter-spacing: .01em; margin-bottom: .35rem; }}
  .decision-conclusion {{ font-size: .88rem; margin-bottom: .5rem; }}
  .decision-row {{ display: flex; gap: .5rem; font-size: .82rem; margin-top: .2rem; }}
  .decision-row .k {{ color: #666; min-width: 4.5rem; flex-shrink: 0; }}
  .decision-row .v {{ color: #1f2328; }}
  details.tech-detail {{ margin-top: .5rem; font-size: .8rem; color: #666; }}
  details.tech-detail summary {{ cursor: pointer; color: #33399e; }}
  ul.action-summary {{ margin: 0; padding-left: 1.2rem; font-size: .92rem; }}
  ul.action-summary li {{ margin-bottom: .3rem; }}
  ul.action-summary .action-candidate-confirmation {{ color: #2f6fed; }}
  ul.action-summary .action-classification-review, ul.action-summary .action-manual-tariff-search {{ color: #a3271b; }}
  ul.action-summary .action-terminology-review {{ color: #8a5a00; }}
  ul.action-summary .action-none {{ color: #14722f; }}
  ul.actions {{ margin: .5rem 0 0; padding-left: 1.2rem; font-size: .9rem; }}
  ul.actions li.blocking {{ color: #a3271b; }} ul.actions li.review {{ color: #8a5a00; }}
  .note {{ font-size: .8rem; color: #777; margin-top: .75rem; }}
  .footer-note {{ text-align: center; font-size: .8rem; color: #888; margin-top: 2rem; }}
</style>
</head>
<body>
<header>
  <h1>{_esc(case_id)} — Pre-Declaration Review</h1>
  <div class="sub">Source file(s): {_esc(', '.join(doc.get('source_files') or [doc.get('source_file') or '-']))}
    &nbsp;·&nbsp; Direction: {_esc(checker.DIRECTION_LABEL.get(doc.get('trade_direction'), 'unknown'))}</div>
</header>
<main>

<section>
  <h2>1. Case Status</h2>
  <div class="status-card">
    <span class="status-pill {status_class}">{status_label}</span>
    <div>
      <div>{_esc(status_desc)}</div>
      <div class="status-counts">
        Blocking issues: {len(readiness['blocking_reasons'])} &nbsp;·&nbsp;
        Review items: {len(readiness['review_items'])} &nbsp;·&nbsp;
        Line items pending tariff confirmation:
        {sum(1 for it in item_rows if it['tariff_review_status'] != 'CONFIRMED')}/{len(item_rows)}
      </div>
    </div>
  </div>
</section>

<section>
  <h2>2. Action Summary</h2>
  <p class="note">What the system knows, does not know, and what to do next — before the details.</p>
  <ul class="action-summary">{action_summary_items}</ul>
  {f'<ul class="actions">{other_actions}</ul>' if other_actions else ''}
</section>

<section>
  <h2>3. Shipment Summary</h2>
  {summary_html}
</section>

<section>
  <h2>4. Item Review Summary</h2>
  <table>
    <thead><tr><th>Line</th><th>Description</th><th>Source HS</th><th>Decision Status</th>
      <th>Top Candidate (if reliable)</th><th>Required Action</th></tr></thead>
    <tbody>{item_summary_html}</tbody>
  </table>
</section>

<section>
  <h2>5. Tariff Review Details</h2>
  <p class="note">SOURCE-PROVIDED HS references, SYSTEM CANDIDATE retrieval results, and
    HUMAN-CONFIRMED classifications are shown separately below and must never be read as
    equivalent. A candidate CCC is a retrieval result, not a legal classification — never
    "correct", "recommended", or "confirmed" until a human sets confirmed_ccc_code.</p>
  {tariff_html}
</section>

<section>
  <h2>6. Validation Summary</h2>
  <h4>Shipment-level (R0–R16, document-to-document cross-check)</h4>
  <p class="note">{n[FAIL]} FAIL · {n[WARN]} WARN · {n[SKIP]} SKIP · {n[PASS]} PASS</p>
  <table><thead><tr><th>Level</th><th>Rule</th><th>Name</th><th>Detail</th></tr></thead>
    <tbody>{check_rows(checks)}</tbody></table>
  <h4>Line-item level (I1–I3, every commercial line — separate coverage from R0–R16 above)</h4>
  <p class="note">{ni[FAIL]} FAIL · {ni[WARN]} WARN · {ni[SKIP]} SKIP · {ni[PASS]} PASS</p>
  <table><thead><tr><th>Level</th><th>Rule</th><th>Name</th><th>Detail</th></tr></thead>
    <tbody>{check_rows(item_checks)}</tbody></table>
</section>

<section>
  <h2>7. Source Traceability</h2>
  <h4>Key fields</h4>
  <table><thead><tr><th>Source document</th><th>Page</th><th>Source text</th><th>Extraction rule</th></tr></thead>
    <tbody>{trace_rows or '<tr><td colspan="4">No field-level trace recorded for this case.</td></tr>'}</tbody></table>
  <h4>Extraction / document notes (case_issues, includes manual supplementation disclosures)</h4>
  <table><thead><tr><th>Severity</th><th>Code</th><th>File</th><th>Detail</th></tr></thead>
    <tbody>{issues_html}</tbody></table>
</section>

<p class="footer-note">This report is a draft aid for pre-declaration review. A licensed
customs broker must review every item before filing. Not a final declaration; not legal
or tariff-classification advice.</p>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    positional, outdir, direction, force = [], None, None, False
    confirm_ccc, confirm_ccc_items = None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--outdir":
            outdir = args[i + 1]; i += 2; continue
        if a == "--direction":
            direction = args[i + 1]; i += 2; continue
        if a == "--confirm-ccc":
            confirm_ccc = args[i + 1]; i += 2; continue
        if a == "--confirm-ccc-item":
            confirm_ccc_items.append((int(args[i + 1]), args[i + 2])); i += 3; continue
        if a == "--force-confirm":
            force = True; i += 1; continue
        positional.append(a); i += 1

    if not positional:
        raise SystemExit(
            "用法：python3 export_erp.py <structured_json> [--outdir 路徑] "
            "[--direction import|export] [--confirm-ccc 11碼CCC] "
            "[--confirm-ccc-item 品項序號 11碼CCC] [--force-confirm]"
        )
    json_path = Path(positional[0])
    if not json_path.exists():
        raise SystemExit(f"[export_erp] 找不到 JSON：{json_path}")
    if direction is not None and direction not in checker.DIRECTION_LABEL:
        raise SystemExit("--direction 只接受 import 或 export")

    doc = json.loads(json_path.read_text(encoding="utf-8"))

    if confirm_ccc:
        doc = apply_confirm_ccc(doc, confirm_ccc, force)
        print(f"[export_erp] 已將 confirmed_ccc_code={confirm_ccc!r} 寫入 invoice 層級")
    for line_no, ccc in confirm_ccc_items:
        doc = apply_confirm_ccc_item(doc, line_no, ccc, force)
        print(f"[export_erp] 已將品項 {line_no} 的 confirmed_ccc_code={ccc!r} 寫入")
    if confirm_ccc or confirm_ccc_items:
        json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[export_erp] 已寫回 {json_path}")

    case_id = _case_id(doc, json_path)
    base_out = Path(outdir) if outdir else json_path.parent
    case_out = base_out / case_id
    dirs = {name: case_out / name for name in ("structured", "review", "staging", "import")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    checks = checker.run_checks(doc, direction)
    item_checks = checker.run_item_checks(doc)
    readiness = determine_readiness(doc, checks, item_checks)

    item_rows, candidate_rows = build_item_rows_and_candidates(doc, case_id, checker.TARIFF_DB)
    header_row = build_shipment_header_row(doc, case_id, readiness)
    container_rows = build_container_rows(doc, case_id)
    exception_rows = build_exception_rows(doc, checks, item_checks, case_id, item_rows, candidate_rows)

    generated = {}

    write_csv(dirs["staging"] / f"{case_id}_shipment_header.csv", SHIPMENT_HEADER_FIELDS, [header_row])
    write_csv(dirs["staging"] / f"{case_id}_items.csv", ITEM_FIELDS, item_rows)
    write_csv(dirs["staging"] / f"{case_id}_containers.csv", CONTAINER_FIELDS, container_rows)
    write_csv(dirs["staging"] / f"{case_id}_tariff_candidates.csv",
              tariff_candidates.CANDIDATE_FIELDS, candidate_rows)
    checker.write_checks_csv(dirs["staging"] / f"{case_id}_checks.csv", checks)
    checker.write_checks_csv(dirs["staging"] / f"{case_id}_item_checks.csv", item_checks)
    write_csv(dirs["staging"] / f"{case_id}_exceptions.csv", EXCEPTION_FIELDS, exception_rows)
    generated["items_csv"] = True

    # 合併活頁簿：直接鏡射上面已經寫出的 staging CSV 資料，不是新的資料
    # 來源，也不是 ERP 匯入格式（見 STAGING_WORKBOOK_NOTE，工作簿裡的
    # Notes 分頁也會寫這句話）。
    workbook_path = dirs["staging"] / f"{case_id}_staging.xlsx"
    xlsx_writer.write_xlsx(workbook_path, build_staging_workbook_sheets(
        case_id, header_row, item_rows, container_rows, candidate_rows,
        checks, item_checks, exception_rows))

    erp_import_path = None
    if readiness["status"] == READY:
        import_rows = [dict(header_row, **{k: v for k, v in it.items() if k != "case_id"})
                       for it in item_rows]
        erp_import_path = dirs["import"] / f"{case_id}_erp_import_ready.csv"
        write_csv(erp_import_path, SHIPMENT_HEADER_FIELDS + ITEM_FIELDS[1:], import_rows,
                  header_comment=ERP_IMPORT_DISCLAIMER)
    generated["erp_import_ready"] = str(erp_import_path) if erp_import_path else None

    structured_path = dirs["structured"] / f"{case_id}.json"
    structured_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    review_report_path = dirs["review"] / f"{case_id}_report.txt"
    review_report_path.write_text(
        build_review_report(doc, checks, item_checks, readiness, case_id, generated),
        encoding="utf-8")

    review_html_path = dirs["review"] / f"{case_id}_review.html"
    review_html_path.write_text(
        build_review_html(doc, checks, item_checks, readiness, case_id, item_rows, candidate_rows),
        encoding="utf-8")

    case_status = {
        "case_id": case_id,
        "source_files": doc.get("source_files") or [doc.get("source_file")],
        "erp_readiness": readiness["status"],
        "validation_summary": {
            "shipment_level": {k.lower(): sum(1 for c in checks if c["level"] == k)
                               for k in (PASS, WARN, FAIL, SKIP)},
            "item_level": {k.lower(): sum(1 for c in item_checks if c["level"] == k)
                           for k in (PASS, WARN, FAIL, SKIP)},
        },
        "blocking_issue_count": len(readiness["blocking_reasons"]),
        "warning_count": len(readiness["review_items"]),
        "tariff_review_status": {
            "confirmed": sum(1 for it in item_rows if it["tariff_review_status"] == "CONFIRMED"),
            "total_items": len(item_rows),
        },
        "case_issues": doc.get("case_issues") or [],
        "blocking_reasons": readiness["blocking_reasons"],
        "review_items": readiness["review_items"],
        "outputs": {
            "structured": str(structured_path),
            "review_report": str(review_report_path),
            "review_html": str(review_html_path),
            "shipment_header": str(dirs["staging"] / f"{case_id}_shipment_header.csv"),
            "items": str(dirs["staging"] / f"{case_id}_items.csv"),
            "containers": str(dirs["staging"] / f"{case_id}_containers.csv"),
            "tariff_candidates": str(dirs["staging"] / f"{case_id}_tariff_candidates.csv"),
            "checks": str(dirs["staging"] / f"{case_id}_checks.csv"),
            "item_checks": str(dirs["staging"] / f"{case_id}_item_checks.csv"),
            "exceptions": str(dirs["staging"] / f"{case_id}_exceptions.csv"),
            "staging_workbook": str(workbook_path),
            "erp_import_ready": generated["erp_import_ready"],
        },
    }
    case_status_path = case_out / "case_status.json"
    case_status_path.write_text(json.dumps(case_status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Case status: {readiness['status']}")
    print(f"Output directory: {case_out}")
    print("Generated: structured/, review/ (txt + html), staging/ (7 CSVs + staging.xlsx), case_status.json")
    if erp_import_path:
        print(f"- import/{erp_import_path.name}")
    else:
        print()
        print(f"ERP import-ready CSV not generated because case_status = {readiness['status']}:")
        for reason in (readiness["blocking_reasons"] or readiness["review_items"]):
            print(f"- {reason}")

    sys.exit(1 if readiness["status"] == BLOCKED else 0)


if __name__ == "__main__":
    main()
