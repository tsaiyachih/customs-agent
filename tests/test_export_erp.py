"""
export_erp.py 的單元測試：目前只鎖定 build_exception_rows() 的 severity
處理（這裡發生過真實 bug：INFO 被二元判斷式誤標成 REVIEW，見
commit 修正該問題的訊息）。不是整支 export_erp.py 的完整測試——那支
腳本的端到端行為已經由 tests/run_tests.py 對真實 fixture 跑過完整流程
覆蓋，這裡只補「純函式、純 JSON」層級測不到的細粒度情境，跟
test_validate.py／test_extract_crosscheck.py 同樣的分工原則。

用法：
    python3 tests/test_export_erp.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude/skills/customs-check/scripts"
sys.path.insert(0, str(SCRIPTS))
import export_erp  # noqa: E402

FAILURES = []


def check(label: str, cond: bool, detail=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        FAILURES.append(label)
        print(f"  ✗ {label}    實際：{detail}")


def _doc_with_issues(issues):
    """滿足 ERP_MANDATORY_FIELDS、品項已確認稅則，避免這兩類無關的列
    混進 build_exception_rows() 的輸出，干擾這裡只想測的 case_issues
    部分。"""
    return {
        "trade_direction": "import",
        "invoice": {
            "invoice_no": "T-1", "date": "AUG.01.2026", "currency": "USD",
            "items": [{"line_no": 1, "confirmed_ccc_code": "12345678901"}],
        },
        "packing_list": {"declared_gross": 100.0},
        "case_issues": issues,
    }


def test_info_issue_excluded_from_exceptions_csv():
    print("build_exception_rows：INFO 級 issue 不寫進 exceptions.csv")
    doc = _doc_with_issues([
        {"code": "VISION_ADVISORY", "severity": "INFO", "file": None,
         "detail": "模型的自由觀察，僅供參考", "field": "transport_document.vessel"},
    ])
    rows = export_erp.build_exception_rows(doc, checks=[], item_checks=[], case_id="T-1")
    check("INFO 級的 VISION_ADVISORY 完全沒有出現在輸出裡",
          not any(r["rule"] == "VISION_ADVISORY" for r in rows), rows)


def test_blocking_and_review_still_written_with_correct_severity():
    print("build_exception_rows：BLOCKING／REVIEW 照常寫入，且 severity 不再被二元判斷式覆蓋")
    doc = _doc_with_issues([
        {"code": "ARITHMETIC_TOTAL_MISMATCH", "severity": "BLOCKING", "file": None,
         "detail": "品項加總跟總額對不上", "field": "invoice.total_amount"},
        {"code": "EXTRACTION_CROSSCHECK_MISMATCH", "severity": "REVIEW", "file": None,
         "detail": "規則式跟模型讀出的值不同", "field": "transport_document.voyage"},
    ])
    rows = export_erp.build_exception_rows(doc, checks=[], item_checks=[], case_id="T-1")
    check("剛好兩列（沒有多也沒有少）", len(rows) == 2, len(rows))
    blocking_row = next(r for r in rows if r["rule"] == "ARITHMETIC_TOTAL_MISMATCH")
    review_row = next(r for r in rows if r["rule"] == "EXTRACTION_CROSSCHECK_MISMATCH")
    check("BLOCKING issue 的 severity 欄位正確寫成 BLOCKING",
          blocking_row["severity"] == "BLOCKING", blocking_row["severity"])
    check("REVIEW issue 的 severity 欄位正確寫成 REVIEW（不是被二元判斷式覆蓋出來的巧合）",
          review_row["severity"] == "REVIEW", review_row["severity"])


def test_rule_action_hints_cover_validate_and_crosscheck_codes():
    print("RULE_ACTION_HINTS：validate.py／extract_crosscheck.py 的 15 個 code 都有具體提示")
    codes = [
        # validate.py 的 12 個
        "NO_ITEMS", "ARITHMETIC_LINE_MISMATCH", "ARITHMETIC_TOTAL_MISMATCH",
        "NEGATIVE_VALUE", "INVALID_CCC_CODE_FORMAT", "TARIFF_CODE_NOT_FOUND",
        "SOURCE_HS_CODE_FORMAT_UNUSUAL", "CURRENCY_UNKNOWN_CODE",
        "CURRENCY_INCONSISTENT", "DATE_UNPARSEABLE", "DATE_OUT_OF_RANGE",
        "UNIT_UNKNOWN",
        # extract_crosscheck.py 的 3 個
        "EXTRACTION_CROSSCHECK_MISMATCH", "VISION_ONLY_NEEDS_CONFIRM", "VISION_ADVISORY",
    ]
    check("剛好 15 個 code（沒有漏、沒有重複列）", len(codes) == 15, len(codes))
    missing = [c for c in codes if c not in export_erp.RULE_ACTION_HINTS]
    check("全部 15 個 code 都在 RULE_ACTION_HINTS 裡", missing == [], missing)
    generic_fallback = [c for c in codes
                        if export_erp.RULE_ACTION_HINTS.get(c, "") == f"Review {c}"]
    check("沒有一個落到通用 fallback 文字（不是把 code 名稱換句話說）",
          generic_fallback == [], generic_fallback)


def test_exceptions_csv_uses_specific_hint_not_fallback():
    print("build_exception_rows：exceptions.csv 裡的 required_action 是具體提示，不是 fallback")
    doc = _doc_with_issues([
        {"code": "EXTRACTION_CROSSCHECK_MISMATCH", "severity": "REVIEW", "file": None,
         "detail": "d", "field": "transport_document.vessel"},
    ])
    rows = export_erp.build_exception_rows(doc, checks=[], item_checks=[], case_id="T-1")
    row = rows[0]
    check("required_action 提到 crosscheck_review.csv 跟 resolve（具體操作，不是泛用文字）",
          "crosscheck_review.csv" in row["required_action"] and "resolve" in row["required_action"],
          row["required_action"])


def test_mixed_severities_only_info_dropped():
    print("build_exception_rows：BLOCKING／REVIEW／INFO 混在一起時，只有 INFO 被排除")
    doc = _doc_with_issues([
        {"code": "NO_ITEMS", "severity": "BLOCKING", "file": None, "detail": "d1", "field": None},
        {"code": "CURRENCY_UNKNOWN_CODE", "severity": "REVIEW", "file": None, "detail": "d2", "field": None},
        {"code": "VISION_ADVISORY", "severity": "INFO", "file": None, "detail": "d3", "field": None},
    ])
    rows = export_erp.build_exception_rows(doc, checks=[], item_checks=[], case_id="T-1")
    rules = {r["rule"] for r in rows}
    check("只剩 BLOCKING 跟 REVIEW 那兩條，INFO 那條被排除",
          rules == {"NO_ITEMS", "CURRENCY_UNKNOWN_CODE"}, rules)


if __name__ == "__main__":
    test_info_issue_excluded_from_exceptions_csv()
    test_blocking_and_review_still_written_with_correct_severity()
    test_rule_action_hints_cover_validate_and_crosscheck_codes()
    test_exceptions_csv_uses_specific_hint_not_fallback()
    test_mixed_severities_only_info_dropped()

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 項測試失敗：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("✓ 全部通過")
