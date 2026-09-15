"""
案件層入口：一個案件目錄（多個獨立檔案）→ 一個案件的結構化 JSON

跟 extract.py 共用同一套解析核心（extract.build_case_from_pages()）——
extract.py 是這個核心的 N=1 檔案特例，這支腳本是它的一般化：案件目錄下
每個檔案獨立辨識文件類型、獨立抽取，記錄各自的來源檔名，最後才 consolidate
成一個案件。不手動合併 PDF 當替代方案。

案件邊界由目錄本身定義：這個目錄裡的檔案就是同一票，系統不做自動貨物
分群判斷。

某個檔案抽不到文字層（掃描件）時不中止整個案件——記一條 case_issues
（依檔名判斷可能的文件類型決定嚴重度：invoice/packing list 判為
BLOCKING，運輸單據判為 REVIEW，因為運輸單據本來就可能合法缺席，
『檔案存在但讀不到』跟『本來就沒有』是兩回事，不能被既有 R14/R15 的
SKIP 邏輯悄悄蓋過去），其餘檔案照常處理。

用法：
    python3 run_case.py <案件目錄> [輸出json路徑]
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402
import validate  # noqa: E402

_INVOICE_NAME_RE_ = r"invoice"
_PACKING_NAME_RE_ = r"packing"
_TRANSPORT_NAME_RE_ = r"bill.*lading|b_?l\b|shipping.*order|\bs_?o\b|bill_of_lading"


def _guess_doc_kind(filename: str) -> str:
    """檔案完全抽不到文字層時無法用內容判斷文件類型，只能退而求其次看
    檔名——這正是為什麼案例目錄的檔名慣例（commercial_invoice.pdf／
    packing_list.pdf／bill_of_lading.pdf）很重要。猜不出來就當作
    『不確定』，用較保守（REVIEW 而非直接放行）的方式處理。"""
    import re
    name = filename.lower()
    if re.search(_INVOICE_NAME_RE_, name):
        return "invoice"
    if re.search(_PACKING_NAME_RE_, name):
        return "packing_list"
    if re.search(_TRANSPORT_NAME_RE_, name):
        return "transport_document"
    return "unknown"


def _pdftotext_or_none(path: Path) -> str | None:
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True,
    )
    text = out.stdout
    return text if text.strip() else None


# ---------------------------------------------------------------------------
# validate.py 接線：把「值本身合不合理」的檢查併進同一份 case_issues，
# 讓 export_erp.py 的 determine_readiness()（只看 severity 字串，不管
# code）自動吃到，不用改 checker.py／export_erp.py 任何一行。
#
# 只在這裡（build_case() 收尾）呼叫一次，而且要放在
# finalize_invoice_item_checks() 之後——那一步會先套用
# manual_supplements.json 合併品項，validate_case() 要驗證的是合併後
# 最終的品項清單，不是補件前的半份清單。
# ---------------------------------------------------------------------------

def _merge_validate_issues(doc: dict) -> None:
    """把 validate.validate_case(doc) 的結果併進 doc['case_issues']，就地
    修改。這裡只做「接線＋去重」，不判斷任何數值、不改 validate.py 的
    檢查邏輯。

    去重只處理一組已知重疊：validate.py 的 ARITHMETIC_TOTAL_MISMATCH
    跟 extract.py 自己的 INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE（都是
    「品項加總 vs 發票總額對不上」同一個判斷，只是後者用 float、容差
    較鬆），兩條同時出現時對人來說是同一件事被講了兩遍。已有 extract.py
    那條時就不重複附加 validate.py 這條，但 validate.py 用 Decimal 算出
    的精確差額不能白算——併進保留下來那條的 detail 裡，去重不能讓人工
    核對時損失這個數字。其餘 issue（包含品項層級的
    ARITHMETIC_LINE_MISMATCH，沒有對應的既有檢查）照常併入，不做任何
    特殊化。"""
    new_issues = validate.validate_case(doc)

    extract_total_issue = next(
        (i for i in doc["case_issues"]
         if i["code"] == "INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE"), None)
    if extract_total_issue is not None:
        dup = next((i for i in new_issues if i["code"] == "ARITHMETIC_TOTAL_MISMATCH"), None)
        if dup is not None:
            extract_total_issue["detail"] += (
                f" （validate.py 以 Decimal 精確計算得出的差額：{dup['diff']}，"
                "跟本條同一個判斷，不重複列出。）")
        new_issues = [i for i in new_issues if i["code"] != "ARITHMETIC_TOTAL_MISMATCH"]

    doc["case_issues"].extend(new_issues)


def build_case(case_dir: str) -> dict:
    case_path = Path(case_dir)
    if not case_path.is_dir():
        raise SystemExit(f"[run_case] 不是資料夾：{case_dir}")

    pdfs = sorted(case_path.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"[run_case] {case_dir} 底下沒有任何 PDF 檔案。")

    tagged = []
    no_text_layer_issues = []
    for pdf in pdfs:
        text = _pdftotext_or_none(pdf)
        if text is None:
            kind = _guess_doc_kind(pdf.name)
            severity = "BLOCKING" if kind in ("invoice", "packing_list") else "REVIEW"
            no_text_layer_issues.append({
                "code": "NO_TEXT_LAYER", "severity": severity, "file": pdf.name,
                "target_doc": kind if kind != "unknown" else None,
                "detail": f"{pdf.name} 無文字層（掃描件），規則式解析不適用。"
                          "其餘檔案仍照常以規則式抽取；這一份需要改走視覺模型"
                          "抽取，填回對應的欄位後再重跑 checker.py。"
                          + ("（依檔名判斷可能是必要文件，缺了無法驗證，"
                             "暫列為 BLOCKING）" if severity == "BLOCKING" else
                             "（依檔名判斷像是運輸單據，可能只是尚未取得，"
                             "暫列為 REVIEW，不直接視為缺件）"),
            })
            continue
        for i, page in enumerate(text.split("\f")):
            tagged.append((pdf.name, i + 1, page))

    if not tagged:
        # 全部檔案都沒有文字層——沒有任何東西可以 consolidate，仍要產生
        # 一個結構（全部欄位 null），讓後續流程能看到 case_issues 而不是
        # 直接崩潰；case_status 會因為 BLOCKING case_issues 判 BLOCKED。
        doc = extract.build_case_from_pages([])
    else:
        doc = extract.build_case_from_pages(tagged)

    doc["case_issues"] = no_text_layer_issues + doc["case_issues"]
    doc["source_files"] = [p.name for p in pdfs]
    extract.finalize_invoice_item_checks(doc, case_path / "manual_supplements.json")
    _merge_validate_issues(doc)
    return doc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法：python3 run_case.py <案件目錄> [輸出json路徑]")
    data = build_case(sys.argv[1])
    default_out = Path(sys.argv[1]).resolve().name + ".json"
    out = sys.argv[2] if len(sys.argv) > 2 else default_out
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[run_case] 已寫出 {out}")
    if data["case_issues"]:
        print(f"[run_case] {len(data['case_issues'])} 項 case_issues：")
        for issue in data["case_issues"]:
            print(f"  [{issue['severity']}] {issue['code']}（{issue.get('file') or '-'}）"
                  f"：{issue['detail']}")
