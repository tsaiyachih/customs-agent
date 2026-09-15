"""
驗證層（再補一種）：規則式抽取 vs 模型視覺閱讀的自由文字欄位交叉比對。

範圍限定在「沒有算術/格式可驗證、只能靠語意判斷對不對」的自由文字欄位：
    transport_document.vessel / voyage / doc_no / port_of_loading / port_of_discharge
    parties.seller_name / buyer_name / shipper_name / consignee_name / notify_party_name
    invoice.items[].description
金額/重量/件數一律不碰——那些分別已經有 validate.py（Decimal 算術/格式）
跟 checker.py 的 R5/R15（跨文件重量件數勾稽）在管，這裡重做只會製造重複
issue，見 README.md〈驗證層細分〉一節。

分工原則（不可違反，CLAUDE.md）：
- 「模型讀到什麼值」是模型的事，這支腳本不讀 PDF、不猜測、不判斷哪個
  值才對——原文印錯就是印錯，申報須與單據一致，模型只能提醒疑似有誤，
  絕不自動改成它認為對的拼法（見 vision_freetext.json 的 advisory_notes，
  是 INFO，不是拿來覆蓋欄位值的）。
- 「兩個值相不相等」是這支腳本的事，純 Python 決定性比對，不由模型在
  對話裡口頭裁定。
- 「哪個值才是最終正確答案」只能由人工用 resolve 子指令裁決寫回，值的
  變更只能來自確定性規則（這裡沒有）或人工確認——這支腳本本身永遠不
  自己選邊。

跟既有「掃描件/品項加總對不上才觸發」的視覺抽取路徑（SKILL.md 步驟 2）
是兩件不同的事，並存不衝突：那條路徑失敗才補救、讀整份文件、補所有
欄位；這條路徑每票都跑、只讀上面列的自由文字欄位，見
vision_freetext.json 的 schema。

用法：
    python3 extract_crosscheck.py compare <case.json> <vision_freetext.json> \
        [--csv <批次清單輸出路徑，預設同資料夾 crosscheck_review.csv>]
    python3 extract_crosscheck.py resolve <case.json> <填好的清單.csv>

也可以被其他腳本 import：
    compare_case(doc, vision_doc) -> list[issue]
    resolve_rows(doc, rows) -> list[str]   # 回傳實際套用裁決的 field 清單
"""

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BLOCKING, REVIEW, INFO = "BLOCKING", "REVIEW", "INFO"

# ---------------------------------------------------------------------------
# 欄位路徑：這支腳本唯一認得的自由文字欄位集合，範圍以外的欄位一律不碰。
# transport_document／parties 是扁平 dict，直接列 (section, key)；
# invoice.items[].description 用 line_no（印在文件上的序號，不是內部
# idx+1）當索引，跟專案其他地方（manual_supplements.json）的既有慣例
# 一致。
# ---------------------------------------------------------------------------

FREETEXT_FIELD_SPECS = [
    ("transport_document", "vessel"),
    ("transport_document", "voyage"),
    ("transport_document", "doc_no"),
    ("transport_document", "port_of_loading"),
    ("transport_document", "port_of_discharge"),
    ("parties", "seller_name"),
    ("parties", "buyer_name"),
    ("parties", "shipper_name"),
    ("parties", "consignee_name"),
    ("parties", "notify_party_name"),
]

_ITEM_DESC_RE = re.compile(r"^invoice\.items\[(\d+)\]\.description$")

CROSSCHECK_CODES = {
    "EXTRACTION_CROSSCHECK_MISMATCH", "VISION_ONLY_NEEDS_CONFIRM", "VISION_ADVISORY",
}
# 進批次清單（需要人工裁決）的只有這兩種；VISION_ADVISORY 是純觀察，
# 不需要裁決，不進清單。
_REVIEW_CODES_FOR_LIST = {"EXTRACTION_CROSSCHECK_MISMATCH", "VISION_ONLY_NEEDS_CONFIRM"}

CSV_HEADER_WARNING = (
    "# ⚠️ 裁決後用 resolve 指令寫回 <case.json>；填寫期間及裁決完成前，"
    "不要重新執行 run_case.py——那會整個重建案件 JSON，蓋掉這份清單對應的"
    "所有裁決紀錄（跟 manual_supplements.json／confirmed_ccc_code 的既有"
    "限制一樣：run_case.py 是『從文件重新整個抽取』，不會保留任何事後人工"
    "寫入的欄位）。"
)
CSV_FIELDS = ["field", "issue_code", "rule_value", "vision_value", "context", "resolution"]


def _applicable_field_paths(doc: dict) -> list:
    paths = [f"{section}.{key}" for section, key in FREETEXT_FIELD_SPECS]
    items = ((doc.get("invoice") or {}).get("items")) or []
    for it in items:
        ln = it.get("line_no")
        if ln is not None:
            paths.append(f"invoice.items[{ln}].description")
    return paths


def _find_item(doc: dict, line_no: int) -> dict | None:
    items = ((doc.get("invoice") or {}).get("items")) or []
    return next((it for it in items if it.get("line_no") == line_no), None)


def get_field(doc: dict, path: str):
    """讀取欄位目前的值，路徑外的字串一律拋 ValueError——這支腳本只認
    上面列出的欄位集合，不是通用的任意路徑存取器，寫錯路徑要明確報錯，
    不要悄悄回傳 None 讓人誤以為欄位真的是空的。"""
    if m := _ITEM_DESC_RE.match(path):
        it = _find_item(doc, int(m.group(1)))
        return it.get("description") if it else None
    for section, key in FREETEXT_FIELD_SPECS:
        if path == f"{section}.{key}":
            return (doc.get(section) or {}).get(key)
    raise ValueError(f"不在交叉比對範圍內的欄位路徑：{path}")


def set_field(doc: dict, path: str, value) -> None:
    """只有 resolve（人工裁決後）會呼叫這裡——這是整支腳本裡唯一允許
    改動案件 JSON 主欄位值的地方，compare 絕不呼叫。"""
    if m := _ITEM_DESC_RE.match(path):
        it = _find_item(doc, int(m.group(1)))
        if it is None:
            raise ValueError(f"找不到 line_no={m.group(1)} 的品項，無法寫入：{path}")
        it["description"] = value
        return
    for section, key in FREETEXT_FIELD_SPECS:
        if path == f"{section}.{key}":
            if doc.get(section) is None:
                doc[section] = {}
            doc[section][key] = value
            return
    raise ValueError(f"不在交叉比對範圍內的欄位路徑：{path}")


def _mark_field_method(doc: dict, path: str, method: str) -> None:
    """把「這個欄位是怎麼被確認的」標記在欄位旁邊，用專案既有的底線前綴
    metadata 慣例（如 invoice._source_file／_source_page），不覆蓋
    items[].extraction_method——那個既有欄位是『整個品項』層級的標記
    （manual_visual_supplement 品項用它表示整列都來自人工視覺補件），
    這裡只確認了 description 這一個欄位，用另一個獨立的鍵，避免兩種
    語意互相覆蓋、讓人誤讀『整項都是人工填的』。"""
    if m := _ITEM_DESC_RE.match(path):
        it = _find_item(doc, int(m.group(1)))
        if it is not None:
            it["_description_method"] = method
        return
    for section, key in FREETEXT_FIELD_SPECS:
        if path == f"{section}.{key}":
            sect = doc.get(section)
            if sect is not None:
                sect.setdefault("_field_methods", {})[key] = method
            return


# ---------------------------------------------------------------------------
# 值比對：只判斷「相不相等」，不判斷「哪個對」。大小寫/內部空白差異視為
# 相等——這兩種差異幾乎都是模型轉寫習慣造成的表面差異（同一份文字層
# PDF，規則式跟模型讀的是同一段原文，不是各自 OCR 出來的兩份獨立結果），
# 不是實質內容不同，正規化後比對可以減少這類雜訊，report 裡仍然顯示
# 原始（未正規化）的兩個值，不會因為判定相等就看不出表面差異。
# ---------------------------------------------------------------------------

def _normalize_for_compare(s):
    if s is None:
        return None
    return re.sub(r"\s+", " ", s).strip().upper()


def _values_equal(a, b) -> bool:
    return _normalize_for_compare(a) == _normalize_for_compare(b)


def _source_hint(doc: dict, path: str):
    if path.startswith("transport_document."):
        return (doc.get("transport_document") or {}).get("_source_file")
    return (doc.get("invoice") or {}).get("_source_file")


def _issue(code, severity, field, rule_value, vision_value, context, detail):
    return {
        "code": code, "severity": severity, "file": None, "detail": detail,
        "field": field, "rule_value": rule_value, "vision_value": vision_value,
        "context": context,
    }


def compare_case(doc: dict, vision_doc: dict) -> list:
    """逐欄位比對，回傳新產生的 issue list（不修改 doc、不修改
    vision_doc）。三種 code，呼叫端自行決定要不要併入 case_issues、
    要不要印出來——這裡只負責產生。"""
    issues = []
    vision_fields = (vision_doc or {}).get("fields") or {}

    for path in _applicable_field_paths(doc):
        rule_value = get_field(doc, path)
        vf = vision_fields.get(path)
        if vf is None:
            continue  # 模型沒讀這個欄位，沒有東西可比對，不是問題
        vision_value = vf.get("value")
        context = vf.get("context")

        if rule_value is None and vision_value is None:
            continue  # 兩邊都沒有，沒有東西可比對
        if rule_value is not None and vision_value is None:
            continue  # 規則式有、模型沒讀到——不是孤值（孤值是相反情況），安靜通過
        hint = _source_hint(doc, path)
        if rule_value is None and vision_value is not None:
            issues.append(_issue(
                "VISION_ONLY_NEEDS_CONFIRM", REVIEW, path, rule_value, vision_value, context,
                f"{path}：規則式未抽取到值，模型讀取到「{vision_value}」。"
                "這是孤值（只有一邊有資料），需人工核對原文確認是否採用，"
                "不能逕行當成已確認的值。",
            ))
            continue
        if not _values_equal(rule_value, vision_value):
            issues.append(_issue(
                "EXTRACTION_CROSSCHECK_MISMATCH", REVIEW, path, rule_value, vision_value, context,
                f"{path}：規則式抽取值為「{rule_value}」，模型讀取值為「{vision_value}」，"
                "兩者不同。兩邊都只是候選值，不代表其中一個一定對（也可能是原文本身"
                "印刷有誤，兩邊都要照抄），需人工核對原文後裁決。",
            ))
        # 相等（含正規化後相等）：安靜通過，不產生任何 issue。

    for note in (vision_doc or {}).get("advisory_notes") or []:
        text = note.get("note")
        if not text:
            continue
        field = note.get("field")
        issues.append(_issue(
            "VISION_ADVISORY", INFO, field, None, None, None,
            (f"{field}：{text}" if field else text),
        ))

    return issues


def merge_into_case_issues(doc: dict, new_issues: list) -> None:
    """就地修改 doc：先移除舊的三種 crosscheck code（冪等，重跑 compare
    不會愈疊愈多），只動這三種 code，其他來源（validate.py、
    MANUAL_VISUAL_SUPPLEMENT_APPLIED 等）的 issue 原封不動，再把新算出
    的 issue 接上去。"""
    existing = doc.setdefault("case_issues", [])
    kept = [i for i in existing if i.get("code") not in CROSSCHECK_CODES]
    doc["case_issues"] = kept + new_issues


def write_review_csv(path, issues: list) -> None:
    """只有需要人工裁決的兩種 code 才進清單——VISION_ADVISORY 是純觀察，
    不需要裁決，不佔清單版面。"""
    rows = [i for i in issues if i["code"] in _REVIEW_CODES_FOR_LIST]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        f.write(CSV_HEADER_WARNING + "\n")
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for i in rows:
            w.writerow({
                "field": i["field"], "issue_code": i["code"],
                "rule_value": i["rule_value"] if i["rule_value"] is not None else "",
                "vision_value": i["vision_value"] if i["vision_value"] is not None else "",
                "context": i["context"] or "", "resolution": "",
            })


def read_review_csv(path) -> list:
    with open(path, encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def resolve_rows(doc: dict, rows: list) -> list:
    """rows：read_review_csv() 讀回的字典列表。resolution 欄空白的列
    跳過（維持 REVIEW_REQUIRED，未裁決的欄位不得自動套用任一邊——這是
    設計前提，寧可留著擋 readiness 也不要猜）。回傳實際套用裁決的欄位
    路徑清單。"""
    applied = []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for row in rows:
        resolution = (row.get("resolution") or "").strip()
        if not resolution:
            continue
        field = row["field"]
        rule_value = row.get("rule_value") or None
        vision_value = row.get("vision_value") or None

        set_field(doc, field, resolution)
        if resolution == rule_value:
            method = "rule_confirmed"
        elif resolution == vision_value:
            method = "vision_confirmed"
        else:
            # 人工核對原文後認定兩邊都不對、自己鍵入的第三個值——沿用
            # manual_supplements.json 既有的標記，不另造一個新詞彙，
            # 兩者語意一致：都是「人工從原文抽出的值，非規則式/模型結果」。
            method = "manual_visual_supplement"
        _mark_field_method(doc, field, method)

        doc.setdefault("freetext_resolutions", []).append({
            "field": field, "rule_value": rule_value, "vision_value": vision_value,
            "resolution": resolution, "method": method,
            "resolved_by": "human", "resolved_at": now,
        })
        applied.append(field)
    return applied


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_compare(argv):
    if len(argv) < 2:
        raise SystemExit("用法：python3 extract_crosscheck.py compare <case.json> "
                          "<vision_freetext.json> [--csv <輸出路徑>]")
    case_path, vision_path = Path(argv[0]), Path(argv[1])
    rest = argv[2:]
    if "--csv" in rest:
        csv_path = Path(rest[rest.index("--csv") + 1])
    else:
        csv_path = case_path.parent / "crosscheck_review.csv"

    doc = _load(case_path)
    vision_doc = _load(vision_path)
    issues = compare_case(doc, vision_doc)
    merge_into_case_issues(doc, issues)
    case_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    write_review_csv(csv_path, issues)

    # VISION_ADVISORY 這階段刻意只寫進 JSON，不印在終端——模型在真實單據
    # 上會講出什麼還無法預測，先收集一陣子再決定要不要浮到人面前。
    printable = [i for i in issues if i["code"] != "VISION_ADVISORY"]
    print(f"[extract_crosscheck] 已寫回 {case_path}（{len(issues)} 項 issue，"
          f"其中 {len(issues) - len(printable)} 項 VISION_ADVISORY 只寫進 JSON 不印出）")
    for i in printable:
        print(f"  [{i['severity']}] {i['code']}：{i['detail']}")
    n_list = sum(1 for i in issues if i["code"] in _REVIEW_CODES_FOR_LIST)
    if n_list:
        print(f"[extract_crosscheck] {n_list} 項待人工裁決，已寫出批次清單 {csv_path}")


def _cmd_resolve(argv):
    if len(argv) < 2:
        raise SystemExit("用法：python3 extract_crosscheck.py resolve <case.json> <填好的清單.csv>")
    case_path, csv_path = Path(argv[0]), Path(argv[1])
    doc = _load(case_path)
    rows = read_review_csv(csv_path)
    applied = resolve_rows(doc, rows)
    case_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[extract_crosscheck] 已寫回 {case_path}，套用 {len(applied)} 筆裁決：")
    for f in applied:
        print(f"  - {f}")
    remaining = sum(1 for r in rows if not (r.get("resolution") or "").strip())
    if remaining:
        print(f"[extract_crosscheck] 還有 {remaining} 項未裁決（resolution 留空），"
              "案件會維持 REVIEW_REQUIRED，直到補齊為止。")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("compare", "resolve"):
        raise SystemExit(
            "用法：\n"
            "  python3 extract_crosscheck.py compare <case.json> <vision_freetext.json> "
            "[--csv <輸出路徑>]\n"
            "  python3 extract_crosscheck.py resolve <case.json> <填好的清單.csv>"
        )
    {"compare": _cmd_compare, "resolve": _cmd_resolve}[sys.argv[1]](sys.argv[2:])
