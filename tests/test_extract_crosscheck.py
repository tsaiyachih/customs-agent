"""
extract_crosscheck.py 的單元測試：純 JSON 造測資（不需要 PDF/模型）。

這層自己出錯比不做更危險（比對邏輯錯了會誤放行或誤擋，兩種都糟），
所以除了逐項核對相等/不等/單邊缺值/兩邊皆缺/大小寫與空白差異，也專門
測了冪等合併（compare 重跑不能誤刪其他來源的 issue）跟 resolve 的三種
裁決結果。跟 tests/run_tests.py／tests/test_validate.py 一樣走「僅標準
函式庫」、手捲 assert 計數的風格。

用法：
    python3 tests/test_extract_crosscheck.py
"""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude/skills/customs-check/scripts"
sys.path.insert(0, str(SCRIPTS))
import extract_crosscheck as xc  # noqa: E402

FAILURES = []


def check(label: str, cond: bool, detail=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        FAILURES.append(label)
        print(f"  ✗ {label}    實際：{detail}")


def codes(issues):
    return [i["code"] for i in issues]


def has(issues, code, field=None):
    return any(i["code"] == code and (field is None or i["field"] == field) for i in issues)


def _doc(td=None, parties=None, items=None):
    return {
        "invoice": {"_source_file": "inv.pdf", "items": items or []},
        "transport_document": {"_source_file": "bl.pdf", **(td or {})},
        "parties": parties or {},
        "case_issues": [],
    }


def _vision(fields=None, advisory=None):
    return {"extraction_method": "vision", "fields": fields or {},
            "advisory_notes": advisory or []}


def _vf(value, context="..."):
    return {"value": value, "context": context}


# --- compare_case：相等／不等／單邊缺值／兩邊皆缺 --------------------------

def test_equal_no_issue():
    print("compare_case：兩邊相等")
    doc = _doc(td={"vessel": "EVER GLORY"})
    vision = _vision({"transport_document.vessel": _vf("EVER GLORY")})
    issues = xc.compare_case(doc, vision)
    check("完全相同的字串不觸發任何 issue", issues == [], codes(issues))


def test_case_difference_treated_equal():
    print("compare_case：大小寫差異")
    doc = _doc(td={"vessel": "EVER GLORY"})
    vision = _vision({"transport_document.vessel": _vf("ever glory")})
    issues = xc.compare_case(doc, vision)
    check("大小寫不同但內容相同，視為相等、不觸發", issues == [], codes(issues))


def test_whitespace_difference_treated_equal():
    print("compare_case：空白差異")
    doc = _doc(td={"port_of_loading": "CAT LAI PORT, VIET NAM"})
    vision = _vision({"transport_document.port_of_loading":
                       _vf("CAT LAI  PORT,   VIET NAM")})
    issues = xc.compare_case(doc, vision)
    check("內部空白數量不同但字面相同，視為相等、不觸發", issues == [], codes(issues))


def test_mismatch():
    print("compare_case：真的不一致")
    doc = _doc(td={"vessel": "E", "voyage": "ER"})
    vision = _vision({"transport_document.vessel": _vf("EVER GLORY"),
                       "transport_document.voyage": _vf("118N")})
    issues = xc.compare_case(doc, vision)
    check("兩個欄位都不一致，各觸發一條 EXTRACTION_CROSSCHECK_MISMATCH",
          has(issues, "EXTRACTION_CROSSCHECK_MISMATCH", "transport_document.vessel")
          and has(issues, "EXTRACTION_CROSSCHECK_MISMATCH", "transport_document.voyage"),
          codes(issues))
    m = next(i for i in issues if i["field"] == "transport_document.vessel")
    check("severity 是 REVIEW", m["severity"] == xc.REVIEW)
    check("detail 裡兩個值都看得到",
          "E" in m["detail"] and "EVER GLORY" in m["detail"], m["detail"])


def test_vision_only():
    print("compare_case：只有模型有值（孤值）")
    doc = _doc(parties={"seller_name": None})
    vision = _vision({"parties.seller_name": _vf("SOME SELLER CO.")})
    issues = xc.compare_case(doc, vision)
    check("觸發 VISION_ONLY_NEEDS_CONFIRM", has(issues, "VISION_ONLY_NEEDS_CONFIRM"), codes(issues))
    i = issues[0]
    check("severity 是 REVIEW", i["severity"] == xc.REVIEW)
    check("rule_value 是 None、vision_value 是模型讀到的值",
          i["rule_value"] is None and i["vision_value"] == "SOME SELLER CO.")


def test_rule_only_silent():
    print("compare_case：只有規則式有值（模型沒讀到）")
    doc = _doc(td={"doc_no": "DEMO-123"})
    vision = _vision({"transport_document.doc_no": _vf(None)})
    issues = xc.compare_case(doc, vision)
    check("規則式有值、模型讀到 None——不是孤值（孤值定義是相反情況），不觸發任何 issue",
          issues == [], codes(issues))


def test_both_missing_silent():
    print("compare_case：兩邊都沒有值")
    doc = _doc(parties={"notify_party_name": None})
    vision = _vision({"parties.notify_party_name": _vf(None)})
    issues = xc.compare_case(doc, vision)
    check("兩邊皆缺，沒有東西可比對，不觸發任何 issue", issues == [], codes(issues))


def test_field_not_covered_by_vision_silent():
    print("compare_case：模型完全沒讀這個欄位（vision 的 fields 裡沒有這個 key）")
    doc = _doc(td={"vessel": "EVER GLORY"})
    vision = _vision({})  # 完全沒填任何欄位
    issues = xc.compare_case(doc, vision)
    check("模型沒讀到的欄位不比對、不誤判成孤值或不一致", issues == [], codes(issues))


def test_item_description_by_line_no():
    print("compare_case：品項描述用 line_no 對應")
    doc = _doc(items=[{"line_no": 1, "description": "SESAME OIL"},
                       {"line_no": 2, "description": "PEANUT OIL"}])
    vision = _vision({"invoice.items[2].description": _vf("PEANUT  OIL")})  # 只讀了第2項，且有多餘空白
    issues = xc.compare_case(doc, vision)
    check("只比對模型有讀到的那個 line_no，且空白差異視為相等", issues == [], codes(issues))


def test_advisory_note_is_info_and_not_confirmable():
    print("compare_case：VISION_ADVISORY")
    doc = _doc(td={"vessel": "EVER GLROY"})
    vision = _vision(
        {"transport_document.vessel": _vf("EVER GLROY")},
        advisory=[{"field": "transport_document.vessel", "note": "疑似拼字錯誤"}],
    )
    issues = xc.compare_case(doc, vision)
    check("兩邊字面相同（都是 GLROY）不觸發 MISMATCH，但 advisory 照樣產生",
          not has(issues, "EXTRACTION_CROSSCHECK_MISMATCH") and has(issues, "VISION_ADVISORY"),
          codes(issues))
    a = next(i for i in issues if i["code"] == "VISION_ADVISORY")
    check("VISION_ADVISORY 必須是 INFO", a["severity"] == xc.INFO, a["severity"])
    check("VISION_ADVISORY 不在批次清單的兩種待裁決 code 裡",
          a["code"] not in xc._REVIEW_CODES_FOR_LIST)


# --- 驗收題：known_bugs 的 bug2 必須被攔下 -----------------------------------

def test_known_bug2_is_caught():
    print("驗收：tests/fixtures/known_bugs/bug2 必須被 EXTRACTION_CROSSCHECK_MISMATCH 攔下")
    doc = json.loads((ROOT / "tests/fixtures/known_bugs/"
                       "bug2_vessel_voyage_split__GD-2609-004.json").read_text(encoding="utf-8"))
    vision = _vision({
        "transport_document.vessel": _vf("EVER GLORY", "Vessel / Voyage EVER GLORY / 118N"),
        "transport_document.voyage": _vf("118N", "Vessel / Voyage EVER GLORY / 118N"),
    })
    issues = xc.compare_case(doc, vision)
    check("vessel 觸發 MISMATCH（規則值 'E' vs 模型值 'EVER GLORY'）",
          has(issues, "EXTRACTION_CROSSCHECK_MISMATCH", "transport_document.vessel"), codes(issues))
    check("voyage 觸發 MISMATCH（規則值 'ER' vs 模型值 '118N'）",
          has(issues, "EXTRACTION_CROSSCHECK_MISMATCH", "transport_document.voyage"), codes(issues))


# --- merge_into_case_issues：冪等，只動三個 code ----------------------------

def test_merge_is_idempotent_and_scoped():
    print("merge_into_case_issues：冪等、只影響三個 crosscheck code")
    doc = _doc()
    doc["case_issues"] = [
        {"code": "PARTY_EXTRACTION_AMBIGUOUS", "severity": "REVIEW", "file": None, "detail": "既有的、無關的 issue"},
        {"code": "ARITHMETIC_TOTAL_MISMATCH", "severity": "BLOCKING", "file": None, "detail": "validate.py 產生的、無關的 issue"},
    ]
    first_batch = [
        {"code": "EXTRACTION_CROSSCHECK_MISMATCH", "severity": "REVIEW", "file": None,
         "detail": "d1", "field": "transport_document.vessel", "rule_value": "E",
         "vision_value": "EVER GLORY", "context": None},
    ]
    xc.merge_into_case_issues(doc, first_batch)
    check("第一次合併後：既有 2 條 + 新 1 條 = 3 條",
          len(doc["case_issues"]) == 3, len(doc["case_issues"]))

    second_batch = [
        {"code": "EXTRACTION_CROSSCHECK_MISMATCH", "severity": "REVIEW", "file": None,
         "detail": "d2（重新比對過一次，內容可能不同）", "field": "transport_document.vessel",
         "rule_value": "E", "vision_value": "EVER GLORY", "context": None},
        {"code": "VISION_ONLY_NEEDS_CONFIRM", "severity": "REVIEW", "file": None,
         "detail": "d3", "field": "parties.seller_name", "rule_value": None,
         "vision_value": "X", "context": None},
    ]
    xc.merge_into_case_issues(doc, second_batch)
    result_codes = codes(doc["case_issues"])
    check("重跑 compare 合併：舊的 crosscheck issue 被換掉、不是疊加（不是 4 條變 5 條）",
          len(doc["case_issues"]) == 4, result_codes)
    check("無關的 PARTY_EXTRACTION_AMBIGUOUS 跟 ARITHMETIC_TOTAL_MISMATCH 都還在，原封不動",
          result_codes.count("PARTY_EXTRACTION_AMBIGUOUS") == 1
          and result_codes.count("ARITHMETIC_TOTAL_MISMATCH") == 1, result_codes)
    check("crosscheck 的部分變成最新一次算出來的兩條（不是舊的那條殘留）",
          result_codes.count("EXTRACTION_CROSSCHECK_MISMATCH") == 1
          and result_codes.count("VISION_ONLY_NEEDS_CONFIRM") == 1, result_codes)
    mismatch = next(i for i in doc["case_issues"] if i["code"] == "EXTRACTION_CROSSCHECK_MISMATCH")
    check("內容確實是第二次算出來的版本，不是第一次的殘留",
          mismatch["detail"] == second_batch[0]["detail"], mismatch["detail"])


# --- resolve_rows：三種裁決結果 + 未裁決跳過 --------------------------------

def test_resolve_rule_confirmed():
    print("resolve_rows：採用規則值 → rule_confirmed")
    doc = _doc(td={"voyage": "118M"})
    rows = [{"field": "transport_document.voyage", "rule_value": "118M",
             "vision_value": "118N", "resolution": "118M"}]
    applied = xc.resolve_rows(doc, rows)
    check("有套用這個欄位", applied == ["transport_document.voyage"], applied)
    check("主欄位值寫回", doc["transport_document"]["voyage"] == "118M")
    check("method 標記為 rule_confirmed",
          doc["transport_document"]["_field_methods"]["voyage"] == "rule_confirmed")
    check("freetext_resolutions 有一筆紀錄，resolved_by=human",
          len(doc["freetext_resolutions"]) == 1
          and doc["freetext_resolutions"][0]["resolved_by"] == "human")


def test_resolve_vision_confirmed():
    print("resolve_rows：採用模型值 → vision_confirmed")
    doc = _doc(parties={"seller_name": None})
    rows = [{"field": "parties.seller_name", "rule_value": "",
             "vision_value": "SOME SELLER CO.", "resolution": "SOME SELLER CO."}]
    xc.resolve_rows(doc, rows)
    check("主欄位值寫回", doc["parties"]["seller_name"] == "SOME SELLER CO.")
    check("method 標記為 vision_confirmed",
          doc["parties"]["_field_methods"]["seller_name"] == "vision_confirmed")


def test_resolve_third_value_is_manual_visual_supplement():
    print("resolve_rows：兩邊都不對，人工鍵入第三個值 → manual_visual_supplement")
    doc = _doc(td={"vessel": "E"})
    rows = [{"field": "transport_document.vessel", "rule_value": "E",
             "vision_value": "EVER GLROY", "resolution": "EVER GLORY"}]  # 人工核對原文後的正確拼法
    xc.resolve_rows(doc, rows)
    check("主欄位值寫回人工鍵入的值", doc["transport_document"]["vessel"] == "EVER GLORY")
    check("method 標記為既有慣例 manual_visual_supplement（不是自造新詞彙）",
          doc["transport_document"]["_field_methods"]["vessel"] == "manual_visual_supplement")


def test_resolve_item_description():
    print("resolve_rows：品項描述用 line_no 寫回，不覆蓋既有 items[].extraction_method")
    doc = _doc(items=[{"line_no": 3, "description": "OLD", "extraction_method": "manual_visual_supplement"}])
    rows = [{"field": "invoice.items[3].description", "rule_value": "OLD",
             "vision_value": "NEW", "resolution": "NEW"}]
    xc.resolve_rows(doc, rows)
    it = doc["invoice"]["items"][0]
    check("description 寫回", it["description"] == "NEW")
    check("用獨立的 _description_method，不覆蓋既有品項層級 extraction_method",
          it["_description_method"] == "vision_confirmed"
          and it["extraction_method"] == "manual_visual_supplement")


def test_resolve_skips_blank_resolution():
    print("resolve_rows：resolution 留空的列跳過，不自動套用任一邊")
    doc = _doc(td={"vessel": "E"})
    rows = [{"field": "transport_document.vessel", "rule_value": "E",
             "vision_value": "EVER GLORY", "resolution": ""}]
    applied = xc.resolve_rows(doc, rows)
    check("沒有套用任何裁決", applied == [], applied)
    check("主欄位值維持原樣（沒被自動改成任一邊）", doc["transport_document"]["vessel"] == "E")
    check("沒有產生 freetext_resolutions 紀錄", "freetext_resolutions" not in doc
          or doc["freetext_resolutions"] == [])


# --- CSV 讀寫往返（含表頭警告行要能正確略過）--------------------------------

def test_csv_round_trip(tmp_path=Path("/tmp/xc_unit_csv_test")):
    print("CSV 讀寫往返")
    tmp_path.mkdir(exist_ok=True)
    issues = [
        {"code": "EXTRACTION_CROSSCHECK_MISMATCH", "severity": "REVIEW", "file": None,
         "detail": "d", "field": "transport_document.vessel", "rule_value": "E",
         "vision_value": "EVER GLORY", "context": "原文片段"},
        {"code": "VISION_ADVISORY", "severity": "INFO", "file": None, "detail": "純觀察",
         "field": "transport_document.vessel", "rule_value": None, "vision_value": None,
         "context": None},
    ]
    csv_path = tmp_path / "review.csv"
    xc.write_review_csv(csv_path, issues)
    text = csv_path.read_text(encoding="utf-8-sig")
    check("表頭警告行有寫入（提醒不要重跑 run_case.py）",
          "run_case.py" in text.splitlines()[0])
    check("VISION_ADVISORY 沒有進 CSV（只有兩種待裁決 code 才進）",
          "VISION_ADVISORY" not in text)

    rows = xc.read_review_csv(csv_path)
    check("讀回剛好 1 列（VISION_ADVISORY 那筆本來就沒寫進去）", len(rows) == 1, len(rows))
    check("讀回的欄位值正確", rows[0]["field"] == "transport_document.vessel"
          and rows[0]["rule_value"] == "E" and rows[0]["vision_value"] == "EVER GLORY"
          and rows[0]["context"] == "原文片段" and rows[0]["resolution"] == "")


if __name__ == "__main__":
    test_equal_no_issue()
    test_case_difference_treated_equal()
    test_whitespace_difference_treated_equal()
    test_mismatch()
    test_vision_only()
    test_rule_only_silent()
    test_both_missing_silent()
    test_field_not_covered_by_vision_silent()
    test_item_description_by_line_no()
    test_advisory_note_is_info_and_not_confirmable()
    test_known_bug2_is_caught()
    test_merge_is_idempotent_and_scoped()
    test_resolve_rule_confirmed()
    test_resolve_vision_confirmed()
    test_resolve_third_value_is_manual_visual_supplement()
    test_resolve_item_description()
    test_resolve_skips_blank_resolution()
    test_csv_round_trip()

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 項測試失敗：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("✓ 全部通過")
