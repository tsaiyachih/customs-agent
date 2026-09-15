"""
抽取層：PDF → 結構化 JSON

只處理有文字層的 PDF（規則式解析，確定性、零成本）。
無文字層（掃描件）時明確失敗並提示改走視覺模型路徑，
不猜測欄位值。

核心解析邏輯集中在 build_case_from_pages()：輸入是「一批已標好來源檔名／
頁碼的頁面文字」，輸出是一個案件的結構化 JSON。這支腳本的 CLI
（單一 PDF）只是那個核心的 N=1 檔案特例；多檔案案件見 run_case.py，
兩者共用同一套解析函式，不是兩套邏輯。

用法：
    python3 extract.py <pdf路徑> [輸出json路徑]
"""

import json
import re
import subprocess
import sys
from pathlib import Path

UNITS = r"㎡|M2|KGS|KG|PCS|SET|EA|CTN"
CURRENCIES = r"CNY|USD|EUR|JPY|TWD|HKD"
INCOTERMS = r"EXW|FOB|CIF|CFR|FCA|CPT|CIP|DAP|DDP|DPU"
# 件數／包裝單位——PKG 之外，CTNS／CARTONS／DRUMS 等版面也很常見；
# Packing List 表尾件數（parse_packing_list）跟運輸單據總件數
# （parse_transport_doc）共用同一份，兩處各自的版面差異只在數字前後
# 有沒有 "Total"／"No. of" 這類前綴，單位詞本身沒有理由分開維護。
PKG_UNITS = r"PKGS?|PACKAGES?|CTNS?|CARTONS?|DRUMS?"


def pdf_to_text(path: str) -> str:
    out = subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        capture_output=True, text=True, check=True,
    )
    if not out.stdout.strip():
        raise SystemExit(
            f"[extract] {path} 無文字層（掃描件）。\n"
            "         規則式解析不適用，請改走視覺模型抽取路徑，\n"
            "         產出相同 schema 的 JSON 後再送入 checker.py。"
        )
    return out.stdout


def num(s):
    return float(s.replace(",", "")) if s else None


def _normalize_desc(s: str | None) -> str | None:
    """安全的品名正規化：只壓縮內部空白、去頭尾空白，不改大小寫、
    不加任何原文沒有的屬性字詞。"""
    if not s:
        return None
    t = re.sub(r"\s+", " ", s).strip()
    return t or None


_DESC_NOISE_RE = re.compile(
    r"Description|Q'?ty|Unit\s*Price|Amount|Terms of Delivery|Total\b", re.I
)


def _item_description(page: str, qty_start: int) -> str | None:
    """在數量欄位之前找品名：版面不同，品名有時跟數量同一行（序號 品名 ... 數量），
    有時各佔一行（序號\\n品名\\n數量\\n...）。兩種都試，找不到像樣的文字就回傳
    None，不臆測——這跟其他欄位『抽取不到就填 null』的原則一致。"""
    line_start = page.rfind("\n", 0, qty_start) + 1
    same_line = re.sub(r"^[\s\-]*\d+\s*", "", page[line_start:qty_start]).strip()
    if same_line and re.search(r"[A-Za-z]", same_line) and not _DESC_NOISE_RE.search(same_line):
        return same_line
    if line_start > 0:
        prev_end = line_start - 1
        prev_start = page.rfind("\n", 0, prev_end) + 1
        prev_line = re.sub(r"^[\s\-]*\d+\s*", "", page[prev_start:prev_end]).strip()
        if prev_line and re.search(r"[A-Za-z]", prev_line) and not _DESC_NOISE_RE.search(prev_line):
            return prev_line
    return None


_HS_LINE_RE = re.compile(r"HS\s*Code\s*:?\s*([\d\.]{4,})", re.I)
_DIVIDER_RE = re.compile(r"-{5,}")
_HS_NOT_STATED_RE = re.compile(r"\bNOT\s+STATED\b", re.I)
_HS_TRAILING_RE = re.compile(r"\b(\d{4,10}(?:\.\d{2,4})*)\b")


def _hs_fields(raw: str | None) -> dict:
    if not raw:
        return {"source_hs_code_raw": None, "source_hs_code_normalized": None,
                "source_hs_prefix": None, "source_hs_origin": None}
    normalized = re.sub(r"\D", "", raw) or None
    prefix = normalized[:6] if normalized and len(normalized) >= 6 else None
    return {"source_hs_code_raw": raw, "source_hs_code_normalized": normalized,
            "source_hs_prefix": prefix, "source_hs_origin": "invoice"}


def _hs_for_item(page: str, item_match, window_end: int) -> dict:
    """找這個品項的供應商 HS 參考，依序試兩種常見版面慣例，找不到就三層
    欄位全部留 null，不臆測歸屬到別的品項：

    1.『HS Code: xxx』緊接在品項描述區塊下方（下一品項或分隔線之前）。
    2. 表格最右欄，緊接在金額之後、同一列的參考碼（純數字，可能有分節點）
       ——供應商明確寫「NOT STATED」時視為『明確表示沒有』，一樣回傳 null，
       跟『沒寫』不特別區分，反正對外都是同一個 null。
    """
    window_start = item_match.end()
    div = _DIVIDER_RE.search(page, window_start, window_end)
    div_end = min(window_end, div.start()) if div else window_end
    if m := _HS_LINE_RE.search(page, window_start, div_end):
        return _hs_fields(m.group(1))

    line_end = page.find("\n", window_start)
    if line_end == -1 or line_end > window_end:
        line_end = window_end
    tail = page[window_start:line_end]
    if _HS_NOT_STATED_RE.search(tail):
        return _hs_fields(None)
    if m := _HS_TRAILING_RE.search(tail):
        return _hs_fields(m.group(1))
    return _hs_fields(None)


def _invoice_total_amount(page: str, items: list) -> float | None:
    """發票表尾『Total:』列本身的金額——不是最後一個品項的金額。單品項
    發票兩者剛好相等，掩蓋了這是個 bug：多品項時最後一項金額≠發票總額。
    優先解析表尾列（冒號可有可無，"Total:" 跟 "TOTAL" 兩種印法都支援）；
    解析不到才退回舊行為（沿用最後一項金額），對版面差異較大的文件
    （如 C_altlayout）維持一樣『安全失敗』的容錯。"""
    if m := re.search(r"\bTotal\b(?!\s*Package)\s*:?\s*[\d,]+[\s\S]{0,300}?([\d,]+\.\d{2})",
                      page, re.I):
        return num(m.group(1))
    if m := re.search(r"\bTotal\s*:?[^\n]*?([\d,]+\.\d{2})\s*$", page, re.I | re.M):
        return num(m.group(1))
    if items:
        return items[-1]["amount"]
    return None


_CURRENCY_VALUE_RE = re.compile(rf"(?:Invoice\s+)?Currency\s*:?\s*([A-Za-z]{{3}})\b", re.I)
_CURRENCY_LABEL_PRESENT_RE = re.compile(r"(?:Invoice\s+)?Currency\s*:?", re.I)


def parse_invoice(page: str) -> tuple[dict, list]:
    inv = {
        "invoice_no": None, "date": None, "origin": None,
        "incoterm": None, "currency": None,
        "freight": None, "insurance": None,
        "items": [], "total_amount": None,
    }
    issues = []
    if m := re.search(r"INVOICE NO\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]+)", page, re.I):
        inv["invoice_no"] = m.group(1).strip()
    if m := re.search(r"Date\s*:?\s*([A-Z]{3}\.?\s?\d{1,2}[.,]?\s?\d{4})", page, re.I):
        inv["date"] = m.group(1).strip()
    # 原產地：於連續兩個以上空白或行尾截斷，避免吞入同一行的其他欄位。
    # 第二個字不能是 "to"——有些版面「Goods of origin: Singapore to KAOHSIUNG」
    # 國名跟後面的 "to ...(目的地)" 欄位只隔 1 個空白，貪婪比對會把 "to" 也
    # 吃進國名裡（已用實際文件驗證過這個坑）。
    if m := re.search(r"(?:Goods of origin|Country of origin)\s*[:.]*\s*"
                      r"([A-Za-z][A-Za-z]*(?:\s(?!to\b)[A-Za-z]+)?)(?=\s{2,}|\s+to\b|\s*$)",
                      page, re.I | re.M):
        inv["origin"] = m.group(1).strip()
    # 幣別有兩種常見版面：表格表頭的括號式「Amount (USD)」，跟獨立的
    # 標籤/值列「Currency  USD」／「Currency: USD」／「Invoice Currency
    # USD」（GD-2609-004 用的是後者，沒有括號，原本的規則完全抓不到）。
    # 括號式優先（既有行為，維持不動）；抓不到才試標籤式。CURRENCIES
    # 是既有的已知幣別白名單（不是寫死 USD），標籤式抓到的三個字母
    # 不在這個白名單裡時，不當成有效幣別硬塞進去——留 null，另外記一條
    # case_issue 讓人知道「這裡有 Currency 欄位，但抽取層看不懂那個值」，
    # 跟『這份文件根本沒有幣別欄位』分開，事後才好判斷是該去改抽取規則
    # 還是這份文件真的沒寫。
    if m := re.search(rf"\(({CURRENCIES})\)", page):
        inv["currency"] = m.group(1)
    elif m := _CURRENCY_VALUE_RE.search(page):
        code = m.group(1).upper()
        if re.fullmatch(CURRENCIES, code, re.I):
            inv["currency"] = code
        else:
            issues.append({
                "code": "INVOICE_CURRENCY_EXTRACTION_AMBIGUOUS", "severity": "REVIEW",
                "file": None,
                "detail": f"文件標示 Currency 欄位，值為「{m.group(1)}」，"
                          "不在目前已知幣別代碼清單內，currency 留空待人工確認"
                          "（可能是版面沒對齊抓錯字、或這個幣別代碼還沒收錄）。",
            })
    elif _CURRENCY_LABEL_PRESENT_RE.search(page):
        issues.append({
            "code": "INVOICE_CURRENCY_EXTRACTION_AMBIGUOUS", "severity": "REVIEW",
            "file": None,
            "detail": "文件有 Currency／Invoice Currency 標籤，但緊接著的值"
                      "抽取不到（非三個字母的幣別代碼），currency 留空待人工確認。",
        })
    if m := re.search(rf"\b({INCOTERMS})\b", page):
        inv["incoterm"] = m.group(1)
    if m := re.search(rf"Freight\s*Charge\s*:?\s*([\d,\.]+)\s*({CURRENCIES})", page, re.I):
        inv["freight"] = {"amount": num(m.group(1)), "currency": m.group(2)}
    if m := re.search(rf"Insurance\s*:?\s*([\d,\.]+)\s*({CURRENCIES})", page, re.I):
        inv["insurance"] = {"amount": num(m.group(1)), "currency": m.group(2)}

    # HS code 若供應商已標註（僅為候選值，非最終申報號列）。這是既有、餵給
    # checker.py R13 的欄位，維持不動；下面每一品項另外各自關聯自己的
    # source_hs_code_*，兩者服務不同呼叫端，刻意並存。
    if m := re.search(r"HS\s*Code\s*:?\s*([\d\.]{6,})", page, re.I):
        inv["supplier_hs_code"] = m.group(1)

    # 明細行：數量 單位 單價 金額
    matches = list(re.finditer(
        rf"(\d[\d,]*)\s+({UNITS})\s+([\d,]+\.?\d*)\s+([\d,]+\.\d{{2}})", page))
    for idx, m in enumerate(matches):
        window_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page)
        item = {
            "line_no": idx + 1,
            "description": _item_description(page, m.start()),
            "qty": num(m.group(1)), "unit": m.group(2),
            "unit_price": num(m.group(3)), "amount": num(m.group(4)),
        }
        item["description_normalized"] = _normalize_desc(item["description"])
        item.update(_hs_for_item(page, m, window_end))
        inv["items"].append(item)

    inv["total_amount"] = _invoice_total_amount(page, inv["items"])
    return inv, issues


# ---------------------------------------------------------------------------
# 品項金額加總守門 + 人工視覺補充合併
#
# 規則式解析對『有文字層但版面/浮水印造成欄位跨行、彼此重疊』的文件會
# 安全失敗（找不到就不硬湊，見 _item_description/_hs_for_item 的說明），
# 但過去這種『少抓了幾列』的落差完全無聲——只會在 checker.py 的 I3 冒出
# 一個沒有脈絡的 FAIL，讓人誤以為是發票本身矛盾。這裡把這個訊號提前到
# 抽取層本身：加總跟表尾對不上，先當「解析可能不完整」記一條 case_issue，
# 而不是直接把半份品項清單當成最終結果往下送。
#
# 對應的補救管道是 manual_supplements.json（跟輸入檔案放同一個資料夾）：
# 人工改用視覺方式讀出抓不到的那幾列之後，把原文數值（不是推算值）填進
# 這個檔案，重跑抽取時會自動合併回結構化品項清單，不必每次重跑都手動
# 貼回 JSON、也不綁定任何特定案件——沒有這個檔案就只做加總檢查，
# 有就先合併再檢查。
# ---------------------------------------------------------------------------

def load_manual_supplements(path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _build_supplement_item(raw: dict) -> dict:
    item = {
        "line_no": raw.get("line_no"),
        "description": raw.get("description"),
        "qty": raw.get("qty"),
        "unit": raw.get("unit"),
        "unit_price": raw.get("unit_price"),
        "amount": raw.get("amount"),
    }
    item["description_normalized"] = _normalize_desc(item["description"])
    item.update(_hs_fields(raw.get("source_hs_code_raw")))
    item["extraction_method"] = "manual_visual_supplement"
    # source_document 是這個品項本身可稽核的來源標記——export_erp.py 判斷
    # 這條人工補件紀錄是否已經解決（可以不再卡住 READY）時，逐品項檢查
    # 這個欄位是否存在，所以留在合併後的品項字典裡，不只放在彙總的
    # case_issue 訊息文字裡。
    if raw.get("source_document"):
        item["source_document"] = raw["source_document"]
    if raw.get("supplement_reason"):
        item["supplement_reason"] = raw["supplement_reason"]
    return item


def apply_manual_supplements(doc: dict, supplements: dict) -> list:
    """把 manual_supplements.json 裡的品項合併進 doc['invoice']['items']。

    每個補充品項要聲明自己在發票上真正的 line_no（印在文件上的序號，不是
    規則式解析器內部依 regex 命中順序重新編的那個 idx+1）；既有（規則式
    抽取到）的品項依原順序，依序填入其餘還空著的位置，最後整體重新按
    位置編號成 1..N。這個合併演算法只看『補充品項聲明的位置＋既有品項
    的相對順序』，不假設哪個案件、也不假設一定是漏了哪幾列，任何案件的
    manual_supplements.json 都能套用。

    回傳一條 REVIEW 等級的 case_issue，揭露『這裡用了人工視覺補充』，
    不悄悄合併了事——即使合併後 I3 轉為 PASS，這個事實仍要留痕。"""
    inv_supp = (supplements or {}).get("invoice") or {}
    supp_items_raw = inv_supp.get("items") or []
    if not supp_items_raw or not doc.get("invoice"):
        return []

    existing = doc["invoice"].get("items") or []
    total = len(existing) + len(supp_items_raw)
    slots: list = [None] * total
    overflow: list = []
    for raw in supp_items_raw:
        item = _build_supplement_item(raw)
        ln = raw.get("line_no")
        if isinstance(ln, int) and 1 <= ln <= total and slots[ln - 1] is None:
            slots[ln - 1] = item
        else:
            overflow.append(item)  # 位置衝突或未聲明位置，仍不遺失，放最後
    it = iter(existing)
    for i in range(total):
        if slots[i] is None:
            slots[i] = next(it, None)
    # empty 位置數必為 total-placed_in_bounds >= len(existing)，it 只會被
    # 恰好耗盡或提前耗盡（overflow 時），不會有多餘既有品項留在 it 裡。
    merged = [s for s in slots if s is not None] + overflow
    for idx, item in enumerate(merged):
        item["line_no"] = idx + 1
    doc["invoice"]["items"] = merged

    src_files = sorted({raw.get("source_document") for raw in supp_items_raw
                         if raw.get("source_document")})
    return [{
        "code": "MANUAL_VISUAL_SUPPLEMENT_APPLIED",
        "severity": "REVIEW",
        "file": ", ".join(src_files) if src_files else doc["invoice"].get("_source_file"),
        "detail": f"{len(supp_items_raw)} 個品項因規則式解析在版面重疊／浮水印"
                  "干擾下未能取得對應欄位，已改由人工視覺方式讀取原文後合併"
                  "（manual_visual_supplement）。數值取自原文件、非推測值，"
                  "但仍需人工抽樣核對；這些品項的稅則號列一樣尚未歸列。",
    }]


def _invoice_item_total_issue(invoice: dict, file_hint: str | None) -> dict | None:
    """品項金額加總對不上發票表尾總額時，回傳一條 BLOCKING case_issue，
    而不是讓這個落差被靜默吞掉、直接流向 checker.py 的 I3 變成一個沒有
    脈絡的 FAIL。只依賴已經抽到（可能已套用人工補充）的 items/total_amount，
    跟哪個案件無關，任何發票文字層抽取不完整（不論成因）都會被攔到。"""
    items = invoice.get("items") or []
    total = invoice.get("total_amount")
    if not items or total is None:
        return None
    items_sum = round(sum(it["amount"] for it in items if it.get("amount") is not None), 2)
    if abs(items_sum - round(total, 2)) <= 0.01:
        return None
    return {
        "code": "INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE",
        "severity": "BLOCKING",
        "file": file_hint,
        "detail": f"品項金額加總（{items_sum:,.2f}）與發票表尾總額"
                  f"（{total:,.2f}）不符，差額 {abs(items_sum - total):,.2f}。"
                  "可能是規則式解析漏抓部分品項列（例如版面重疊、浮水印、"
                  "跨行斷欄），也可能是發票本身金額有誤——兩者都需要人工"
                  "改用視覺方式核對原文件，必要時整理成 manual_supplements.json"
                  "（跟輸入檔案放同一個資料夾）供下次重跑自動合併，不可逕行"
                  "採用目前的品項清單或加總結果。",
    }


def finalize_invoice_item_checks(doc: dict, supplement_path=None) -> None:
    """extract() 跟 run_case.py 的共用收尾步驟：套用（若有）人工視覺補充，
    再檢查品項金額加總是否與發票表尾總額相符。沒有補充檔案就只做檢查，
    有就先合併再檢查——兩條輸入路徑（單一合併 PDF／案件目錄多檔案）
    都呼叫這裡，不重複實作、也不因為重跑而讓已合併過的補充品項消失。"""
    if not doc.get("invoice"):
        return
    if supplement_path is not None:
        supplements = load_manual_supplements(supplement_path)
        if supplements:
            doc["case_issues"].extend(apply_manual_supplements(doc, supplements))
    hint = doc["invoice"].get("_source_file")
    issue = _invoice_item_total_issue(doc["invoice"], hint)
    if issue:
        doc["case_issues"].append(issue)


def parse_packing_list(body: str) -> dict:
    pl = {
        "invoice_no": None, "date": None,
        "declared_containers": None, "declared_packages": None,
        "declared_net": None, "declared_gross": None,
        "shipping_mark": None, "containers": [],
    }
    if m := re.search(r"INVOICE NO\.?\s*:?\s*([A-Z0-9][A-Z0-9\-/]+)", body, re.I):
        pl["invoice_no"] = m.group(1).strip()
    if m := re.search(r"Date\s*:?\s*([A-Z]{3}\.?\s?\d{1,2}[.,]?\s?\d{4})", body, re.I):
        pl["date"] = m.group(1).strip()
    if m := re.search(r"Total Package\s+(\d+)\s*CNTR", body, re.I):
        pl["declared_containers"] = int(m.group(1))
    # 件數單位不是只有 PKG——CTNS／CARTONS／DRUMS 等版面也很常見
    # （GD-2609-004 的表尾是 "TOTAL   120 CTNS   6,700   7,280"），只認
    # PKG 會讓這種版面整個抓不到 declared_packages。
    if m := re.search(rf"Total\s*:?\s*([\d,]+)\s*(?:{PKG_UNITS})", body, re.I):
        pl["declared_packages"] = int(m.group(1).replace(",", ""))
    if m := re.search(r"Shipping Mark\s*:?\s*(\S+)", body, re.I):
        pl["shipping_mark"] = m.group(1)

    # 表尾淨/毛重優先在「TOTAL」附近找——版面若在表尾之後還有另一段（例如
    # GD-2609-003 這種 item 表尾後面又接一段獨立的 CONTAINER SUMMARY），
    # 直接取全文『最後一組』小數對會誤取到最後一個貨櫃的重量，不是真正
    # 表尾總計（已用這份文件驗證過這個坑）。找不到才退回舊的『最後一組』
    # heuristic，對只有一段小數對的版面（A/B/C/D、TPV）行為不變。
    # 小數位刻意設成可有可無——GD-2609-004 的表尾淨重/毛重是整數
    # （"6,700" "7,280"，沒有 ".00"），原本要求一定要有小數點會讓這個
    # 版面整段抓不到；退回的『最後一組』heuristic 維持嚴格要求小數點，
    # 那條是掃全文找數字對，範圍沒有「Total」這種關鍵字錨定，放寬可能
    # 誤吃到不相關的整數對，風險不對稱，維持原樣。
    if m := re.search(r"\bTotal\b(?!\s*Package)[\s\S]{0,400}?"
                      r"([\d,]+(?:\.\d{1,2})?)\s+([\d,]+(?:\.\d{1,2})?)", body, re.I):
        pl["declared_net"], pl["declared_gross"] = num(m.group(1)), num(m.group(2))
    else:
        totals = re.findall(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", body)
        if totals:
            pl["declared_net"], pl["declared_gross"] = num(totals[-1][0]), num(totals[-1][1])

    for b in re.split(r"CNTR NO\.?\s*:", body, flags=re.I)[1:]:
        cntr = re.match(r"\s*([A-Z]{4}\s?\d+)", b)
        pkg = re.search(r"-\s*(\d+)\s*PKG", b, re.I)
        nums = re.search(rf"(\d[\d,]*)\s+({UNITS})\s+([\d,]+\.\d{{2}})\s+([\d,]+\.\d{{2}})", b)
        if not (cntr and nums):
            continue
        pl["containers"].append({
            "cntr_no": cntr.group(1).replace(" ", ""),
            "packages": int(pkg.group(1)) if pkg else None,
            "qty": num(nums.group(1)), "unit": nums.group(2),
            "net_weight": num(nums.group(3)), "gross_weight": num(nums.group(4)),
        })

    if not pl["containers"]:
        pl["containers"] = _parse_container_summary(body)
    if not pl["containers"]:
        pl["containers"] = _parse_single_container_from_totals(
            body, pl["declared_packages"], pl["declared_net"], pl["declared_gross"])
    # declared_containers 刻意不從 containers 列表長度反推——這個文件版面
    # 沒有獨立的『申報櫃數』陳述可供交叉比對，反推出來的值會讓 R3
    # 變成套套邏輯（申報值＝實列值，恆為真），不如老實留 null、讓 R3
    # SKIP，如實反映「這份文件沒有這項可比對的資訊」。

    return pl


_SINGLE_CONTAINER_LABEL_RE = re.compile(r"Container\s*No\.?\s*:?\s*([A-Z]{4}\s?\d{7})", re.I)


def _parse_single_container_from_totals(body: str, declared_packages, declared_net,
                                         declared_gross) -> list:
    """另一種常見版面：逐『品項』列（不是逐『貨櫃』列），整份文件只在
    表頭／表尾宣告一個貨櫃號，件數/淨重/毛重只以表尾總計出現一次
    （例如 GD-2609-004：4 個品項各自的 Packages/Net/Gross Weight，
    貨櫃只在表頭印一次 "Container No. TGHU7654321"）。這種版面沒有
    『每個品項屬於哪個貨櫃』的資訊可拆分，也不需要拆——只有一個貨櫃，
    表尾總計本來就等於這個貨櫃的總計，直接把表尾總計歸給這唯一的
    貨櫃號即可，不是逐品項湊出來的推測值。qty/unit 留 null：不同品項
    單位可能不同（KGS／PCS 混雜），加總沒有意義，比照
    `_parse_container_summary()` 的既有原則不臆測。

    安全門檻：只有在整份文件裡『唯一』出現一個貨櫃號、且表尾總計
    （件數/淨重/毛重）三項都齊備時才生成；只要有任何一項不成立
    （多個貨櫃號、或表尾總計本身就缺）就回傳空清單，交由呼叫端另外
    記 case_issue，不用這個函式去猜版面。"""
    nums = sorted({m.group(1).replace(" ", "")
                   for m in _SINGLE_CONTAINER_LABEL_RE.finditer(body)})
    if len(nums) != 1:
        return []
    if declared_packages is None or declared_net is None or declared_gross is None:
        return []
    return [{
        "cntr_no": nums[0], "packages": declared_packages,
        "qty": None, "unit": None,
        "net_weight": declared_net, "gross_weight": declared_gross,
    }]


_CONTAINER_NO_RE = re.compile(r"\b([A-Z]{4}\d{7})\b")


def _parse_container_summary(body: str) -> list:
    """另一種常見版面：獨立的『CONTAINER SUMMARY』表格，只列 Container No.
    / Packages / Net Weight / Gross Weight，沒有 CNTR NO.: 這種行內標記、
    也沒有逐櫃的數量（品項數量是在品項表另外列，不是逐櫃對應）——貨櫃跟
    數量之間的對應關係本來就不在來源文件裡，qty/unit 一律留 null，
    不臆測。容許貨櫃號跟後面的數字之間夾雜浮水印雜訊字元（非數字、
    非小數點），只要求數字本身的相對順序（件數、淨重、毛重）成立。"""
    idx = body.find("CONTAINER SUMMARY")
    section = body[idx:] if idx != -1 else body
    containers = []
    for cm in _CONTAINER_NO_RE.finditer(section):
        window = section[cm.end():cm.end() + 200]
        if m := re.search(r"(\d[\d,]*)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", window):
            containers.append({
                "cntr_no": cm.group(1),
                "packages": int(m.group(1).replace(",", "")),
                "qty": None, "unit": None,
                "net_weight": num(m.group(2)), "gross_weight": num(m.group(3)),
            })
    return containers


TW_PORTS = (r"TAIWAN|KAOHSIUNG|KEELUNG|TAICHUNG|TAIPEI|TAOYUAN|HUALIEN|SUAO"
            r"|TWKHH|TWKEL|TWTXG|TWTPE|R\.O\.C")


def detect_direction(text: str) -> str | None:
    """
    判斷進出口方向。台灣為卸貨地/目的地 → import；台灣為裝貨地/起運地 → export。

    方向會改變適用規則（完稅價格基礎、管制規定、第三份文件為何），
    因此判不出來時回傳 None，由 checker 要求人工指定，絕不預設。
    """
    # Port of Loading / Discharge（運輸單據）
    pol = re.search(r"Port of Loading\s*:?\s*([^\n]+)", text, re.I)
    pod = re.search(r"Port of Discharge\s*:?\s*([^\n]+)", text, re.I)
    if pol and pod:
        in_tw = lambda m: bool(re.search(TW_PORTS, m.group(1), re.I))
        if in_tw(pod) and not in_tw(pol):
            return "import"
        if in_tw(pol) and not in_tw(pod):
            return "export"

    # From / to（發票常見寫法）。"to" 常常跟 "Goods of origin" 擠在同一行
    # （如 "Goods of origin: Singapore   to    KAOHSIUNG, TAIWAN"），
    # 不能要求 "to" 在行首，改成要求 "to" 前後有明顯欄位間距（2 個以上空白），
    # 用間距而非行首位置判斷這是欄位而非句子中的介系詞。
    fr = re.search(r"\bFrom\s+([^\n]+)", text, re.I)
    to = re.search(r"\b[Tt]o\s{2,}([A-Z][^\n]+)", text)
    if fr and to:
        in_tw = lambda m: bool(re.search(TW_PORTS, m.group(1), re.I))
        if in_tw(to) and not in_tw(fr):
            return "import"
        if in_tw(fr) and not in_tw(to):
            return "export"
    return None


DOC_TYPES = [
    ("BL", r"BILL OF LADING|B/L NO"),
    ("AWB", r"AIR\s?WAY\s?BILL|AWB NO"),
    ("SO", r"SHIPPING ORDER|BOOKING (?:NOTE|CONFIRMATION)|\bS/O NO"),
]

# 只給 case_issue 訊息文字用的人類可讀標籤，跟 checker.py 的 DOC_LABEL
# 是同一份對照表但刻意各自維護一份——extract.py 跟 checker.py 兩支腳本
# 不互相 import，保持各自獨立可單獨執行。
DOC_LABEL_FOR_ISSUES = {"BL": "B/L", "AWB": "AWB", "SO": "SO／訂艙確認書"}


# 有些運輸單據版面把欄位名跟值拆成『表頭列』+『值列』兩列（值靠字元欄位
# 對齊，不是同一行的 "Label: Value"）——找同時含 2 個以上已知欄位名的列，
# 欄界取相鄻欄位名起始位置的中點（跟 build_tariff_db.py 的
# calibrate_zones() 同樣的技巧），從表頭列後第一個非空白列依欄界切值。
_ROW_LABELS = [
    "Bill of Lading No.", "Port of Loading", "Port of Discharge",
    "Notify Party", "Vessel", "Voyage", "Shipper", "Consignee",
    "Total Packages", "Total Gross Weight",
]


def _scan_label_value_rows(text: str) -> dict:
    """找不到就不放進結果，呼叫端自行 fallback 到既有的『同一行 Label:
    Value』正規表示式——兩種版面慣例都要支援，不是互斥的。"""
    lines = text.split("\n")
    result = {}
    for i, line in enumerate(lines):
        found = {lbl: line.find(lbl) for lbl in _ROW_LABELS if lbl in line}
        if len(found) < 2:
            continue
        # 這一列要是『純標籤』表頭列，才能套用『值在下一列』的版面假設。
        # 反例：像 "Vessel / Voyage              EVER GLORY / 118N" 這種
        # 同一行就帶著值的版面，"Vessel" 跟 "Voyage" 剛好都是這行的子字串，
        # 會被誤判成表頭列，接著把下一行（其實是別的欄位，例如 "Port of
        # Loading ..."）錯當成這一列的值來源，切出面目全非的欄位值——
        # 已用真實文件驗證過這個坑。判斷依據很直接：最後一個標籤後面
        # 若還有非空白字元，代表值就印在這一行本身，不是表頭＋下一行
        # 分離的版面，這裡要放棄，讓呼叫端 fallback 到別的同行
        # "Label: Value" 規則。
        last_label = max(found, key=found.get)
        after_last_label = line[found[last_label] + len(last_label):]
        if after_last_label.strip():
            continue
        value_line = None
        for j in range(i + 1, min(i + 4, len(lines))):
            if lines[j].strip():
                value_line = lines[j]
                break
        if value_line is None:
            continue
        # 值列通常跟表頭列一樣左對齊：每欄的值從該欄標籤起始欄位開始，
        # 印到下一欄標籤起始欄位為止（不是取兩個標籤中點）——已用實際
        # 文件的欄位座標驗證過，中點切法會把值切在單字中間。
        order = sorted(found, key=found.get)
        bounds = [found[k] for k in order] + [len(value_line)]
        for idx, lbl in enumerate(order):
            if lbl in result:
                continue
            val = value_line[bounds[idx]:bounds[idx + 1]].strip()
            if val:
                result[lbl] = val
    return result


def parse_transport_doc(body: str) -> dict:
    doc_type = next((t for t, pat in DOC_TYPES
                     if re.search(pat, body, re.I)), None)
    td = {
        "doc_type": doc_type, "doc_no": None,
        "vessel": None, "voyage": None,
        "port_of_loading": None, "port_of_discharge": None,
        "total_packages": None, "gross_weight": None,
        "containers": [],
    }
    rows = _scan_label_value_rows(body)

    if m := re.search(r"(?:B/?L|AWB|S/?O|BOOKING)\s*(?:NO|NUMBER)\.?\s*:?\s*"
                      r"([A-Z0-9][A-Z0-9\-/]+)", body, re.I):
        td["doc_no"] = m.group(1).strip()
    elif "Bill of Lading No." in rows:
        td["doc_no"] = rows["Bill of Lading No."]

    if "Vessel" in rows or "Voyage" in rows:
        td["vessel"] = rows.get("Vessel") or None
        td["voyage"] = rows.get("Voyage") or None
    # 同一行合併標籤＋合併值："Vessel / Voyage    EVER GLORY / 118N"——標籤
    # 跟值各自用 "/" 分隔、印在同一行，跟下面 "V."/"VOY." 分隔符版面、
    # 跟單獨 "Vessel:" 版面都不同，必須先試這個，否則會被下面兩條規則
    # 誤判或直接抓空。要求標籤跟值之間至少 2 個空白，避免誤吃一般語句
    # 裡湊巧出現的 "vessel / voyage" 字樣。
    elif m := re.search(r"Vessel\s*/\s*Voyage\s{2,}([A-Z0-9][A-Z0-9 .\-]*?)\s*/\s*"
                        r"([A-Z0-9][A-Z0-9\-]*)\s*$", body, re.I | re.M):
        td["vessel"], td["voyage"] = m.group(1).strip(), m.group(2).strip()
    # "V"/"VOY" 航次分隔符前面一定要有空白，否則像 "ST EVER V. 017B" 這種
    # 船名裡剛好含有字母 V（EVER）的情況，非貪婪比對會把船名裡的 V 誤判成
    # 分隔符，切成 vessel="ST E", voyage="ER"（已用真實文件驗證過這個坑）。
    elif m := re.search(r"Vessel\s*(?:/\s*Voyage)?\s*:?\s*([A-Z0-9][A-Z0-9 .\-]*?)\s+"
                        r"(?:V\.?|VOY\.?)\s*([A-Z0-9\-]+)", body, re.I):
        td["vessel"], td["voyage"] = m.group(1).strip(), m.group(2).strip()
    elif m := re.search(r"Vessel\s*:?\s*([A-Z0-9 .\-]{3,})", body, re.I):
        td["vessel"] = m.group(1).strip()

    if "Port of Loading" in rows:
        td["port_of_loading"] = rows["Port of Loading"]
    elif m := re.search(r"Port of Loading\s*:?\s*([A-Za-z ,.\-]+)", body, re.I):
        td["port_of_loading"] = m.group(1).strip()
    if "Port of Discharge" in rows:
        td["port_of_discharge"] = rows["Port of Discharge"]
    elif m := re.search(r"Port of Discharge\s*:?\s*([A-Za-z ,.\-]+)", body, re.I):
        td["port_of_discharge"] = m.group(1).strip()

    if "Total Packages" in rows and (m := re.search(r"([\d,]+)", rows["Total Packages"])):
        td["total_packages"] = int(m.group(1).replace(",", ""))
    elif m := re.search(rf"([\d,]+)\s*(?:{PKG_UNITS})", body, re.I):
        td["total_packages"] = int(m.group(1).replace(",", ""))

    if "Total Gross Weight" in rows and (m := re.search(r"([\d,]+\.?\d*)", rows["Total Gross Weight"])):
        td["gross_weight"] = num(m.group(1))
    elif m := re.search(r"(?:GROSS WEIGHT|G\.W\.)\s*:?\s*([\d,]+\.?\d*)\s*(?:KGS?|KG)",
                        body, re.I):
        td["gross_weight"] = num(m.group(1))

    if "Shipper" in rows:
        td["shipper_name"] = rows["Shipper"]
    elif m := re.search(r"Shipper\s*:?\s*([^\n]+)", body, re.I):
        td["shipper_name"] = m.group(1).strip()
    if "Consignee" in rows:
        td["consignee_name"] = rows["Consignee"]
    elif m := re.search(r"Consignee\s*:?\s*([^\n]+)", body, re.I):
        td["consignee_name"] = m.group(1).strip()

    td["containers"] = sorted(set(re.findall(r"\b([A-Z]{4}\d{7})\b", body)))
    return td


# ---------------------------------------------------------------------------
# Parties（seller/buyer）：版面啟發式，prototype 可接受的做法，但不能把
# 「猜」偽裝成「確定」——猜不準就回傳 null 並記一條 case_issues，不硬塞值。
# ---------------------------------------------------------------------------

_PARTY_NOISE_RE = re.compile(
    r"^(COMMERCIAL\s+INVOICE|INVOICE|PACKING\s+LIST|PACKING\s+SPECIFICATION|"
    r"BILL\s+OF\s+LADING|SHIPPING\s+ORDER|MESSRS|INVOICE\s+NO|DATE|"
    r"SYNTHETIC\s+DEMO)\b", re.I)


def _clean_party_value(raw: str) -> str | None:
    """去掉標籤後緊接的雜項標點（句點/斜線/冒號），再壓縮內部空白——
    純粹清理原文既有的文字，不改內容、不猜測。"""
    return _normalize_desc(re.sub(r"^[\s./:]+", "", raw))


def _extract_seller_line(page_text: str) -> str | None:
    """賣方抬頭抽取，依序試兩種常見版面慣例：

    1. 明確標籤「Seller」/「Seller / Shipper」開頭、同一行後面接值
       （欄位間距 2 個以上空白，避免誤吃句子中的 "seller" 字樣）。
    2. 找不到標籤時，退回『頁面最上方第一行非雜訊文字』這個所有樣本
       文件共用的版面慣例（公司抬頭當作第一行印刷）。

    第一個非空白行若本身就是雜訊字樣（表頭、MESSRS 等）或不像公司名稱
    （沒有足夠英文字母），視為無法確定，回傳 None，不用第二、三行去猜——
    那已經不是同一個版面慣例能保證的事了。"""
    if m := re.search(r"^\s*Seller(?:\s*/\s*Shipper)?\s{2,}(.+)$", page_text, re.M):
        val = _clean_party_value(m.group(1))
        if val:
            return val
    for line in page_text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if _PARTY_NOISE_RE.match(s) or not re.search(r"[A-Za-z]{2,}", s):
            return None
        return s
    return None


_BUYER_LABEL_RE = re.compile(
    r"\b(?:Buyer\s*/\s*)?(?:MESSRS\.?|Buyer(?:\s*Name)?|Sold\s*To)\s*:?\s*([^\n]+)",
    re.I)


def parse_parties(inv_text: str | None, pl_text: str | None,
                   td_text: str | None) -> tuple[dict, list]:
    """回傳 (parties_dict, case_issues)。seller/buyer 取 Invoice 頁（找不到
    再試 Packing List 頁）；shipper/consignee 取運輸單據頁的明確標籤欄位
    （已在 parse_transport_doc 抽過，這裡只是彙整）。notify_party 目前
    所有樣本文件都沒有，維持 null，不臆測。"""
    parties = {
        "seller_name": None, "buyer_name": None,
        "shipper_name": None, "consignee_name": None,
        "notify_party_name": None,
    }
    issues = []

    seller_source = inv_text or pl_text
    if seller_source:
        seller = _extract_seller_line(seller_source)
        if seller:
            parties["seller_name"] = seller
        else:
            issues.append({
                "code": "PARTY_EXTRACTION_AMBIGUOUS", "severity": "REVIEW",
                "file": None,
                "detail": "頁面最上方一行不像公司抬頭（可能是雜訊或版面不同），"
                          "seller_name 無法確定，留空待人工確認",
            })

    buyer_found = False
    for src in (inv_text, pl_text):
        if not src:
            continue
        # "MESSRS:"（A/B/C/D、TPV 版面）、"Buyer / Messrs."（標籤式版面）、
        # 以及純 "Buyer"／"Buyer Name"／"Sold To" 標籤（GD-2609-004 這種
        # 版面沒有 MESSRS 字樣，只印 "Buyer  UNI-LEAVES TRADING LTD."）都要
        # 支援，捕捉組後面可能黏著句點/斜線，交給 _clean_party_value 清掉。
        # 刻意不把 Consignee 當成 buyer 的同義詞——那是運輸單據上的收貨人
        # 角色，跟發票上的買方是不同概念，兩者剛好相同只是常見巧合，
        # 不能在解析規則裡替換掉。
        if m := _BUYER_LABEL_RE.search(src):
            val = _clean_party_value(m.group(1))
            if val:
                parties["buyer_name"] = val
                buyer_found = True
                break

    if not buyer_found and (inv_text or pl_text):
        # 跟 seller 的處理原則一致：抓不到就老實留 null＋記一條 REVIEW，
        # 不要讓「這份文件明明有 Buyer 欄位、只是版面沒吃到」跟「這份
        # 文件真的沒寫」兩種情況都變成無聲的 null，事後很難分辨是抽取
        # 缺口還是文件本身資訊不全。
        issues.append({
            "code": "PARTY_EXTRACTION_AMBIGUOUS", "severity": "REVIEW",
            "file": None,
            "detail": "Invoice／Packing List 皆未能辨識 buyer_name"
                      "（可能是欄位標籤版面不支援，或文件本身未載明），"
                      "留空待人工確認",
        })

    return parties, issues


# ---------------------------------------------------------------------------
# 文件層級抽取完整性守門（跟 checker.py 的 R0 是兩件事）：R0 是
# checker.py 在跨文件比對前，對『這個案件』的核心欄位做一次總體健檢；
# 這裡是 extract.py 對『單一文件』剛解析完就地檢查，核心欄位缺過半就
# 記一條 case_issue，把「這份文件的抽取落差」跟「文件之間互相矛盾」
# 從源頭分開——前者是 BLOCKING case_issue（本來就該讓案件卡住，
# 不該讓 checker.py 因為缺欄位而到處 SKIP，看起來像是「查過了沒問題」），
# 後者才是 checker.py 的 FAIL。兩種原因都會讓案件卡住，但寫進報告的
# 理由不同、後續要找的人也不同（前者找抽取層或改視覺抽取，後者找
# 供應商或船公司對帳）。
# ---------------------------------------------------------------------------

def _doc_completeness_issue(doc_label: str, code: str, values: dict,
                             file_hint: str | None) -> dict | None:
    missing = [k for k, v in values.items() if v is None or v == [] or v == ""]
    if not missing or len(missing) <= len(values) // 2:
        return None
    return {
        "code": code, "severity": "BLOCKING", "file": file_hint,
        "detail": f"{doc_label} 已辨識出文件類型，但核心欄位缺 "
                  f"{len(missing)}/{len(values)}：" + "、".join(missing) + "。"
                  "版面可能不受目前規則式解析器支援，這是抽取層的落差，"
                  "不是文件矛盾——請改用視覺方式核對原文後人工複核，"
                  "不可逕行採用目前的空值結果。",
    }


# ---------------------------------------------------------------------------
# 共用核心：一批 (filename, page_index, page_text) → 一個案件的結構化 JSON
# ---------------------------------------------------------------------------

def build_case_from_pages(tagged_pages: list) -> dict:
    """tagged_pages = [(filename, page_index_in_file, page_text), ...]。
    不假設這些頁面來自同一份實體檔案——extract.py 的 CLI（單一合併 PDF）
    跟 run_case.py（案件目錄下多個獨立檔案）都呼叫這個函式，是同一套
    分類/解析邏輯的兩種輸入方式，不是分開維護的兩套邏輯。"""
    td_pat = "|".join(p for _, p in DOC_TYPES)
    td_pages = [t for t in tagged_pages if re.search(td_pat, t[2], re.I)]
    rest = [t for t in tagged_pages if t not in td_pages]
    inv_pages = [t for t in rest
                 if re.search(r"\bINVOICE\b", t[2], re.I)
                 and not re.search(r"Packing\s*List", t[2], re.I)]
    pl_pages = [t for t in rest if re.search(r"Packing\s*List", t[2], re.I)]

    case_issues = []

    def _files_of(pages):
        seen = []
        for f, _, _ in pages:
            if f not in seen:
                seen.append(f)
        return seen

    def _check_ambiguous(pages, label):
        files = _files_of(pages)
        if len(files) > 1:
            case_issues.append({
                "code": "AMBIGUOUS_DOCTYPE_SELECTION", "severity": "REVIEW",
                "file": ", ".join(files),
                "detail": f"{label}：{len(files)} 個不同檔案的內容都符合此文件類型"
                          f"（{', '.join(files)}），依規則僅採用第一筆，"
                          "案件邊界由資料夾定義，系統不做自動判斷，須人工確認"
                          "是否真的都屬於同一票。",
            })

    _check_ambiguous(td_pages, "運輸單據")
    _check_ambiguous(inv_pages, "Invoice")
    _check_ambiguous(pl_pages, "Packing List")

    used_files = set(_files_of(td_pages)) | set(_files_of(inv_pages)) | set(_files_of(pl_pages))
    for f in _files_of(tagged_pages):
        if f not in used_files:
            case_issues.append({
                "code": "UNRECOGNIZED_FILE", "severity": "INFO", "file": f,
                "detail": "未辨識出已知文件類型（Invoice／Packing List／運輸單據），"
                          "如為報關相關文件請人工確認。",
            })

    invoice = None
    inv_text = None
    if inv_pages:
        fname, pidx, inv_text = inv_pages[0]
        invoice, invoice_issues = parse_invoice(inv_text)
        invoice["_source_file"] = fname
        invoice["_source_page"] = pidx
        for issue in invoice_issues:
            issue["file"] = issue["file"] or fname
        case_issues.extend(invoice_issues)

    packing_list = None
    pl_text = None
    if pl_pages:
        pl_text = "\n".join(t[2] for t in pl_pages)
        packing_list = parse_packing_list(pl_text)
        packing_list["_source_file"] = pl_pages[0][0]
        packing_list["_source_page"] = pl_pages[0][1]
        pl_issue = _doc_completeness_issue(
            "Packing List", "PACKING_LIST_EXTRACTION_INCOMPLETE",
            {"declared_packages": packing_list.get("declared_packages"),
             "declared_net": packing_list.get("declared_net"),
             "declared_gross": packing_list.get("declared_gross"),
             "containers": packing_list.get("containers")},
            pl_pages[0][0])
        if pl_issue:
            case_issues.append(pl_issue)

    transport_document = None
    td_text = None
    if td_pages:
        td_text = "\n".join(t[2] for t in td_pages)
        transport_document = parse_transport_doc(td_text)
        transport_document["_source_file"] = td_pages[0][0]
        transport_document["_source_page"] = td_pages[0][1]
        td_label = DOC_LABEL_FOR_ISSUES.get(transport_document.get("doc_type"), "運輸單據")
        td_issue = _doc_completeness_issue(
            td_label, "TRANSPORT_DOC_EXTRACTION_INCOMPLETE",
            {"doc_no": transport_document.get("doc_no"),
             "total_packages": transport_document.get("total_packages"),
             "gross_weight": transport_document.get("gross_weight")},
            td_pages[0][0])
        if td_issue:
            case_issues.append(td_issue)

    parties, party_issues = parse_parties(inv_text, pl_text, td_text)
    if transport_document:
        parties["shipper_name"] = transport_document.get("shipper_name") or parties["shipper_name"]
        parties["consignee_name"] = transport_document.get("consignee_name") or parties["consignee_name"]
    for issue in party_issues:
        issue["file"] = issue["file"] or (inv_pages[0][0] if inv_pages else
                                           (pl_pages[0][0] if pl_pages else None))
    case_issues.extend(party_issues)

    full_text = "\n".join(t[2] for t in tagged_pages)
    inv_file = inv_pages[0][0] if inv_pages else None
    inv_page = inv_pages[0][1] if inv_pages else None
    td_file = td_pages[0][0] if td_pages else None
    td_page = td_pages[0][1] if td_pages else None
    source_trace = _build_source_trace(inv_text, inv_file, inv_page, td_text, td_file, td_page)
    if parties.get("seller_name") and inv_file:
        source_trace.append({"source_document": inv_file, "source_page": inv_page,
                             "source_text": parties["seller_name"],
                             "extraction_rule": "first_non_noise_line_or_seller_label"})
    if parties.get("buyer_name") and inv_file:
        source_trace.append({"source_document": inv_file, "source_page": inv_page,
                             "source_text": parties["buyer_name"], "extraction_rule": "buyer_label_line"})
    if parties.get("shipper_name") and td_file:
        source_trace.append({"source_document": td_file, "source_page": td_page,
                             "source_text": parties["shipper_name"], "extraction_rule": "bl_shipper_label"})
    if parties.get("consignee_name") and td_file:
        source_trace.append({"source_document": td_file, "source_page": td_page,
                             "source_text": parties["consignee_name"], "extraction_rule": "bl_consignee_label"})

    return {
        "source_files": _files_of(tagged_pages),
        "extraction_method": "text_layer_rules",
        "trade_direction": detect_direction(full_text),
        "parties": parties,
        "invoice": invoice,
        "packing_list": packing_list,
        "transport_document": transport_document,
        "case_issues": case_issues,
        "source_trace": source_trace,
    }


def _build_source_trace(inv_text, inv_file, inv_page, td_text, td_file, td_page) -> list:
    """幾個關鍵欄位的可追溯紀錄：從原文重新比對出『抓到的那一行原文』，
    讓人看得出這個值從哪個檔案、第幾頁、原文哪一句話來的。不是每個欄位
    都做——只挑最關鍵的幾個，避免 schema 難以維護；找不到來源就不記，
    不臆測 page/text。"""
    trace = []

    def add(source_document, file, page, text_pat, rule):
        if not text_pat:
            return
        m = re.search(text_pat, source_document or "", re.I)
        if m:
            trace.append({
                "source_document": file, "source_page": page,
                "source_text": m.group(0).strip(), "extraction_rule": rule,
            })

    if inv_text:
        add(inv_text, inv_file, inv_page, r"INVOICE NO\.?\s*:?\s*[A-Z0-9][A-Z0-9\-/]+",
            "invoice_no_inline_label")
        add(inv_text, inv_file, inv_page, r"(?:Goods of origin|Country of origin)[^\n]{0,60}",
            "origin_inline_label")
        add(inv_text, inv_file, inv_page,
            r"\bTotal\b(?!\s*Package)[^\n]{0,60}", "invoice_total_footer_line")
    if td_text:
        add(td_text, td_file, td_page,
            r"(?:B/?L|AWB|S/?O|BOOKING)\s*(?:NO|NUMBER)\.?\s*:?\s*[A-Z0-9][A-Z0-9\-/]+",
            "transport_doc_no_inline_label")
        add(td_text, td_file, td_page, r"Vessel[^\n]{0,80}", "vessel_voyage_line")

    return trace


def extract(path: str, supplement_path=None) -> dict:
    text = pdf_to_text(path)
    pages = text.split("\f")
    filename = Path(path).name
    tagged = [(filename, i + 1, p) for i, p in enumerate(pages)]
    doc = build_case_from_pages(tagged)
    doc["source_file"] = filename  # 舊欄位名，向後相容單一合併 PDF 的既有用法
    if supplement_path is None:
        supplement_path = Path(path).with_name("manual_supplements.json")
    finalize_invoice_item_checks(doc, supplement_path)
    return doc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python3 extract.py <pdf路徑> [輸出json路徑]")
    data = extract(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else Path(sys.argv[1]).with_suffix(".json")
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[extract] 已寫出 {out}")
