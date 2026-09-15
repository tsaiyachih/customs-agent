"""
validate.py 的單元測試：純 JSON 造測資（不需要 PDF），逐項檢查各自的
正例（應該冒出對應 issue）與反例（不應該冒出任何 issue）至少各一組。

這一層本身出錯比不做更危險（誤報會讓人白忙、漏報等於防呆形同虛設），
所以不能只靠 tests/run_tests.py 那種跑過 fixtures PDF 全流程再看計數
的整合測試方式覆蓋——那種測法沒辦法針對「單一欄位不合理」這種細粒度
情境造出乾淨的最小案例。跟 run_tests.py 一樣走「僅標準函式庫」、手捲
assert 計數的風格，不引入 pytest 依賴。

用法：
    python3 tests/test_validate.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude/skills/customs-check/scripts"
sys.path.insert(0, str(SCRIPTS))
import validate  # noqa: E402

FAILURES = []


def check(label: str, cond: bool, detail=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        FAILURES.append(label)
        print(f"  ✗ {label}    實際：{detail}")


def codes(issues):
    return [i["code"] for i in issues]


def has(issues, code):
    return any(i["code"] == code for i in issues)


# --- 造測資的小工具：只填該檢查會用到的欄位，其餘留預設，避免每個 -----
# 測試都要手打一份完整 schema。
def _item(**kw):
    it = {"line_no": 1, "description": "WIDGET", "qty": 10, "unit": "PCS",
          "unit_price": 5.0, "amount": 50.0}
    it.update(kw)
    return it


def _doc(items=None, invoice_extra=None, packing_list_extra=None, no_invoice=False):
    doc = {
        "source_file": "test.pdf",
        "invoice": None if no_invoice else {
            "invoice_no": "T-1", "date": "AUG.02.2026", "origin": "CHINA",
            "incoterm": "FOB", "currency": "USD", "freight": None, "insurance": None,
            "items": items if items is not None else [_item()],
            "total_amount": sum(i["amount"] for i in (items if items is not None else [_item()])
                                 if i.get("amount") is not None),
            "_source_file": "test.pdf",
        },
        "packing_list": {"containers": []},
    }
    if invoice_extra and doc["invoice"] is not None:
        doc["invoice"].update(invoice_extra)
    if packing_list_extra:
        doc["packing_list"].update(packing_list_extra)
    return doc


# --- check_item_count ------------------------------------------------------

def test_item_count():
    print("check_item_count")
    pos = validate.check_item_count(_doc(items=[]))
    check("空 items 觸發 NO_ITEMS", has(pos, "NO_ITEMS"), codes(pos))
    check("NO_ITEMS 為 BLOCKING", pos[0]["severity"] == validate.BLOCKING if pos else False)

    neg = validate.check_item_count(_doc(items=[_item()]))
    check("有品項時不觸發", neg == [], codes(neg))

    neg2 = validate.check_item_count(_doc(no_invoice=True))
    check("invoice 整份缺失時不觸發（不是這個檢查的責任）", neg2 == [], codes(neg2))


# --- check_arithmetic -------------------------------------------------------

def test_arithmetic_line():
    print("check_arithmetic（單項金額）")
    pos = validate.check_arithmetic(_doc(items=[_item(qty=1000, unit_price=32.5, amount=33500.0)]))
    check("金額≠數量×單價時觸發 ARITHMETIC_LINE_MISMATCH", has(pos, "ARITHMETIC_LINE_MISMATCH"), codes(pos))
    m = next(i for i in pos if i["code"] == "ARITHMETIC_LINE_MISMATCH")
    check("BLOCKING 等級", m["severity"] == validate.BLOCKING)
    # diff 的字串表示法會依輸入精度而定（Decimal 不強制補到小數 2 位，
    # 跟四捨五入到分是兩回事），比對數值而非字面字串。
    check("diff 欄位算對（1000）", validate.Decimal(m["diff"]) == validate.Decimal("1000"), m["diff"])

    neg = validate.check_arithmetic(_doc(items=[_item(qty=1000, unit_price=32.5, amount=32500.0)]))
    check("金額相符時不觸發", not has(neg, "ARITHMETIC_LINE_MISMATCH"), codes(neg))

    skip = validate.check_arithmetic(_doc(items=[_item(qty=None, unit_price=32.5, amount=32500.0)]))
    check("缺欄位（qty=None）視為無資料可比對，不觸發", not has(skip, "ARITHMETIC_LINE_MISMATCH"), codes(skip))


def test_arithmetic_total():
    print("check_arithmetic（加總）")
    items = [_item(line_no=1, amount=100.0), _item(line_no=2, amount=100.0)]
    pos = validate.check_arithmetic(_doc(items=items, invoice_extra={"total_amount": 250.0}))
    check("加總≠表尾總額時觸發 ARITHMETIC_TOTAL_MISMATCH", has(pos, "ARITHMETIC_TOTAL_MISMATCH"), codes(pos))

    neg = validate.check_arithmetic(_doc(items=items, invoice_extra={"total_amount": 200.0}))
    check("加總＝表尾總額時不觸發", not has(neg, "ARITHMETIC_TOTAL_MISMATCH"), codes(neg))


# --- check_negative_values ---------------------------------------------------

def test_negative_values():
    print("check_negative_values")
    pos = validate.check_negative_values(_doc(items=[_item(qty=-5)]))
    check("負數量觸發 NEGATIVE_VALUE", has(pos, "NEGATIVE_VALUE"), codes(pos))

    # 關鍵領域規則：單價 0 是贈品／樣品的合法值，checker.py 的 R8 註解已
    # 明文記載，這裡必須確認「不」被誤判為違規。
    zero_price = validate.check_negative_values(_doc(items=[_item(unit_price=0.0, amount=0.0)]))
    check("單價 0（贈品／樣品）不觸發違規", zero_price == [], codes(zero_price))

    neg = validate.check_negative_values(_doc(items=[_item()]))
    check("全部正值時不觸發", neg == [], codes(neg))


# --- check_ccc_codes ---------------------------------------------------------

def test_ccc_codes():
    print("check_ccc_codes")
    pos = validate.check_ccc_codes(_doc(items=[_item(confirmed_ccc_code="123")]))
    check("非 11 碼觸發 INVALID_CCC_CODE_FORMAT", has(pos, "INVALID_CCC_CODE_FORMAT"), codes(pos))
    check("格式錯誤為 BLOCKING",
          next(i for i in pos if i["code"] == "INVALID_CCC_CODE_FORMAT")["severity"] == validate.BLOCKING)

    neg_absent = validate.check_ccc_codes(_doc(items=[_item()]))
    check("未填 confirmed_ccc_code 時不觸發（人工尚未確認，不是錯誤）", neg_absent == [], codes(neg_absent))

    if validate.TARIFF_DB.exists():
        real_code = "01012100003"  # tariff.db 裡確實存在的號列，用來驗證「格式合法＋查得到」不誤報
        neg_found = validate.check_ccc_codes(_doc(items=[_item(confirmed_ccc_code=real_code)]))
        check("格式合法且查得到時不觸發任何 issue", neg_found == [], codes(neg_found))

        fake_code = "99999999999"  # 格式合法（11 碼數字）但不存在的號列
        pos_missing = validate.check_ccc_codes(_doc(items=[_item(confirmed_ccc_code=fake_code)]))
        check("格式合法但查無此號列觸發 TARIFF_CODE_NOT_FOUND", has(pos_missing, "TARIFF_CODE_NOT_FOUND"),
              codes(pos_missing))
        check("查無號列為 REVIEW（非 BLOCKING）",
              next(i for i in pos_missing if i["code"] == "TARIFF_CODE_NOT_FOUND")["severity"] == validate.REVIEW)
    else:
        print("  · tariff.db 不存在，略過「查得到／查不到」兩組測試"
              "（需先執行 build_tariff_db.py；格式檢查本身已在上面測過，不受影響）")


# --- check_source_hs_codes ----------------------------------------------------

def test_source_hs_codes():
    print("check_source_hs_codes")
    pos = validate.check_source_hs_codes(_doc(items=[_item(source_hs_code_normalized="12345")]))
    check("5 碼（非常見位數）觸發 SOURCE_HS_CODE_FORMAT_UNUSUAL",
          has(pos, "SOURCE_HS_CODE_FORMAT_UNUSUAL"), codes(pos))
    check("為 REVIEW（僅供參考值，非 BLOCKING）",
          next(i for i in pos if i["code"] == "SOURCE_HS_CODE_FORMAT_UNUSUAL")["severity"] == validate.REVIEW)

    neg = validate.check_source_hs_codes(_doc(items=[_item(source_hs_code_normalized="123456")]))
    check("6 碼（常見位數）不觸發", neg == [], codes(neg))


# --- check_currency ------------------------------------------------------------

def test_currency():
    print("check_currency")
    pos_unknown = validate.check_currency(_doc(invoice_extra={"currency": "XXX"}))
    check("不在已知幣別集合時觸發 CURRENCY_UNKNOWN_CODE", has(pos_unknown, "CURRENCY_UNKNOWN_CODE"),
          codes(pos_unknown))

    neg_known = validate.check_currency(_doc(invoice_extra={"currency": "USD"}))
    check("已知幣別不觸發", not has(neg_known, "CURRENCY_UNKNOWN_CODE"), codes(neg_known))

    pos_incons = validate.check_currency(_doc(invoice_extra={
        "currency": "USD", "freight": {"amount": 100.0, "currency": "EUR"}}))
    check("貨價與運費幣別不同時觸發 CURRENCY_INCONSISTENT", has(pos_incons, "CURRENCY_INCONSISTENT"),
          codes(pos_incons))

    neg_cons = validate.check_currency(_doc(invoice_extra={
        "currency": "USD", "freight": {"amount": 100.0, "currency": "USD"}}))
    check("貨價與運費幣別相同不觸發", not has(neg_cons, "CURRENCY_INCONSISTENT"), codes(neg_cons))


# --- check_dates -----------------------------------------------------------

def test_dates():
    print("check_dates")
    pos_unparse = validate.check_dates(_doc(invoice_extra={"date": "not a date"}))
    check("無法解析的日期觸發 DATE_UNPARSEABLE", has(pos_unparse, "DATE_UNPARSEABLE"), codes(pos_unparse))

    pos_old = validate.check_dates(_doc(invoice_extra={"date": "JAN.01.1990"}))
    check("早於 2015-01-01 觸發 DATE_OUT_OF_RANGE", has(pos_old, "DATE_OUT_OF_RANGE"), codes(pos_old))

    pos_future = validate.check_dates(_doc(invoice_extra={"date": "JAN.01.2099"}))
    check("遠未來日期觸發 DATE_OUT_OF_RANGE", has(pos_future, "DATE_OUT_OF_RANGE"), codes(pos_future))

    neg = validate.check_dates(_doc(invoice_extra={"date": "AUG.02.2026"}))
    check("合理區間內、格式常見的日期不觸發", neg == [], codes(neg))

    # extract.py 實際看過的另外兩種分隔符寫法，確認 parser 都吃得下去。
    neg2 = validate.check_dates(_doc(invoice_extra={"date": "SEP. 07, 2026"}))
    check("「SEP. 07, 2026」這種逗號＋空白寫法能正確解析", neg2 == [], codes(neg2))


# --- check_units -----------------------------------------------------------

def test_units():
    print("check_units")
    pos = validate.check_units(_doc(items=[_item(unit="FOO")]))
    check("未知單位觸發 UNIT_UNKNOWN", has(pos, "UNIT_UNKNOWN"), codes(pos))

    neg = validate.check_units(_doc(items=[_item(unit="PCS")]))
    check("已知單位不觸發", neg == [], codes(neg))

    pos_cntr = validate.check_units(_doc(packing_list_extra={
        "containers": [{"cntr_no": "ABCU1234567", "unit": "BAR"}]}))
    check("貨櫃單位未知也觸發 UNIT_UNKNOWN", has(pos_cntr, "UNIT_UNKNOWN"), codes(pos_cntr))


# --- validate_case（整合：多項問題＋完全乾淨兩種情況）------------------------

def test_validate_case_integration():
    print("validate_case（整合）")
    dirty = validate.validate_case(_doc(items=[
        _item(qty=-1, unit="FOO", amount=50.0, unit_price=5.0, source_hs_code_normalized="999"),
    ], invoice_extra={"currency": "ZZZ", "date": "not a date"}))
    dirty_codes = set(codes(dirty))
    check("多重問題的案件會同時冒出對應的各個 code", {
        "NEGATIVE_VALUE", "UNIT_UNKNOWN", "SOURCE_HS_CODE_FORMAT_UNUSUAL",
        "CURRENCY_UNKNOWN_CODE", "DATE_UNPARSEABLE",
    } <= dirty_codes, dirty_codes)

    clean = validate.validate_case(_doc())
    check("乾淨案件回傳空 list", clean == [], codes(clean))

    # 舊 schema（沒有 case_issues／parties／source_hs_code_* 等鍵）不能讓
    # 任何一個檢查函式炸掉——這是真實案件（如 SCH-2608-448.json）的實際
    # 形狀，不是假設情境。
    minimal = {"invoice": {"items": [{"qty": 1, "unit_price": 2, "amount": 2.0}]},
               "packing_list": {}}
    try:
        result = validate.validate_case(minimal)
        check("最簡 schema（缺大量鍵）不會拋例外", True)
    except Exception as e:  # noqa: BLE001 — 這裡就是要抓任何例外，不限定型別
        check("最簡 schema（缺大量鍵）不會拋例外", False, repr(e))


if __name__ == "__main__":
    test_item_count()
    test_arithmetic_line()
    test_arithmetic_total()
    test_negative_values()
    test_ccc_codes()
    test_source_hs_codes()
    test_currency()
    test_dates()
    test_units()
    test_validate_case_integration()

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 項測試失敗：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("✓ 全部通過")
