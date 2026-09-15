"""
自動化回歸測試：把 tests/EXPECTED.md 記載的預期結果變成真的會擋人的斷言。

跑過 fixtures/ 底下所有合成文件（非真實客戶資料）的完整流程——
extract/run_case → checker → export_erp——比對預期的 PASS/WARN/FAIL/SKIP
計數、ERP readiness 狀態、新架構（多文件輸入、品項層檢查、稅則候選、
case_issues）的行為，任何一項不符就非零結束碼。

這支腳本存在的理由：這個 session 前後多次抓到真的 bug（原產地欄位貪婪
比對、進出口方向判斷漏抓版面、稅則候選排序沒分數、vessel/voyage 誤判、
發票總額誤取自最後一品項、Total Package 誤判成表尾金額），全部都是手動
重跑才發現，沒有一次是自動化測試主動抓出來的。有這支腳本之後，改壞東西
至少會被擋下來，不用每次都靠人記得要重新手動核對一遍。

用法：
    python3 tests/run_tests.py
"""

import csv as _csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".claude/skills/customs-check/scripts"
FIXTURES = ROOT / "fixtures"
OUT = ROOT / "data/output/_test_run"  # 跟手動操作用的 data/output 隔開

FAILURES = []


def check(label: str, cond: bool, detail=""):
    if cond:
        print(f"  ✓ {label}")
    else:
        FAILURES.append(label)
        print(f"  ✗ {label}    實際：{detail}")


def run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def extract(pdf: Path, out_json: Path):
    r = run(["python3", str(SCRIPTS / "extract.py"), str(pdf), str(out_json)])
    if r.returncode != 0:
        check(f"extract {pdf.name} 應成功執行", False, r.stderr.strip()[:200])
    return out_json


def run_case(case_dir: Path, out_json: Path):
    r = run(["python3", str(SCRIPTS / "run_case.py"), str(case_dir), str(out_json)])
    if r.returncode != 0:
        check(f"run_case {case_dir.name} 應成功執行", False, r.stderr.strip()[:200])
    return out_json


def checker_counts(json_path: Path) -> dict | None:
    r = run(["python3", str(SCRIPTS / "checker.py"), str(json_path)])
    m = re.search(r"不符 (\d+)｜待確認 (\d+)｜通過 (\d+)｜略過 (\d+)", r.stdout)
    if not m:
        return None
    fail, warn, passn, skip = map(int, m.groups())
    return {"FAIL": fail, "WARN": warn, "PASS": passn, "SKIP": skip}


def export_erp(json_path: Path, outdir: Path, extra_args=None) -> str | None:
    cmd = ["python3", str(SCRIPTS / "export_erp.py"), str(json_path), "--outdir", str(outdir)]
    if extra_args:
        cmd += extra_args
    r = run(cmd)
    for line in r.stdout.splitlines():
        if line.startswith("Case status:"):
            return line.split(":", 1)[1].strip()
    return None


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    # ======================================================================
    # 既有四組合成文件：R0–R16 迴歸（extract.py 改動後，這幾組計數不得變）
    # ======================================================================
    print("=== A_clean（日本供應商，CIF，三櫃，附 B/L；預期完全一致）===")
    a_json = extract(FIXTURES / "A_clean.pdf", OUT / "A_clean.json")
    a_clean_counts = checker_counts(a_json)
    check("A_clean: FAIL=0,WARN=1,PASS=15,SKIP=1",
          a_clean_counts == {"FAIL": 0, "WARN": 1, "PASS": 15, "SKIP": 1}, a_clean_counts)

    a_doc = json.loads(a_json.read_text(encoding="utf-8"))
    a_td = a_doc.get("transport_document") or {}
    check("A_clean: vessel/voyage 修正結果 = ONE HARBOUR / 025E",
          (a_td.get("vessel"), a_td.get("voyage")) == ("ONE HARBOUR", "025E"),
          (a_td.get("vessel"), a_td.get("voyage")))
    parties = a_doc.get("parties") or {}
    check("A_clean: seller/buyer 都有抽到",
          bool(parties.get("seller_name")) and bool(parties.get("buyer_name")), parties)

    print("=== B_faulty（中國供應商，EXW，植入 11 項錯誤）===")
    b_json = extract(FIXTURES / "B_faulty.pdf", OUT / "B_faulty.json")
    counts = checker_counts(b_json)
    check("B_faulty: FAIL=11,WARN=4,PASS=1,SKIP=1",
          counts == {"FAIL": 11, "WARN": 4, "PASS": 1, "SKIP": 1}, counts)

    print("=== C_altlayout（歐陸版面，預期抽取不完整、安全失敗）===")
    c_json = extract(FIXTURES / "C_altlayout.pdf", OUT / "C_altlayout.json")
    counts = checker_counts(c_json)
    check("C_altlayout: FAIL=1,WARN=3,PASS=0,SKIP=13（R0 抽取不完整，其餘不得 PASS）",
          counts == {"FAIL": 1, "WARN": 3, "PASS": 0, "SKIP": 13}, counts)

    print("=== D_export（台灣出口日本，CIF，附 S/O）===")
    d_json = extract(FIXTURES / "D_export.pdf", OUT / "D_export.json")
    counts = checker_counts(d_json)
    check("D_export: FAIL=0,WARN=3,PASS=13,SKIP=1",
          counts == {"FAIL": 0, "WARN": 3, "PASS": 13, "SKIP": 1}, counts)

    # ======================================================================
    # run_case.py（分離檔案）vs extract.py（合併 PDF）：同樣內容必須同樣結果
    # ======================================================================
    print("=== A_case（跟 A_clean 同內容，三個獨立檔案，走 run_case.py）===")
    acase_json = run_case(FIXTURES / "A_case", OUT / "A_case.json")
    acase_counts = checker_counts(acase_json)
    check("A_case: checker 計數跟 A_clean（合併 PDF）完全一致",
          acase_counts == a_clean_counts, {"A_case": acase_counts, "A_clean": a_clean_counts})
    acase_doc = json.loads(acase_json.read_text(encoding="utf-8"))
    for key in ("invoice", "packing_list", "transport_document"):
        av = {k: v for k, v in (a_doc.get(key) or {}).items() if not k.startswith("_")}
        bv = {k: v for k, v in (acase_doc.get(key) or {}).items() if not k.startswith("_")}
        check(f"A_case vs A_clean：{key} 內容一致（分離檔案跟合併 PDF 抽取結果相同）",
              av == bv, {"merged": av, "separate": bv} if av != bv else "match")
    check("A_case: source_files 記錄 3 個獨立檔名",
          len(acase_doc.get("source_files") or []) == 3, acase_doc.get("source_files"))

    # ======================================================================
    # E_multiitem（乾淨合成多品項案例，三個獨立檔案）：I1–I3、稅則候選、
    # readiness 的多輸入源判斷
    # ======================================================================
    print("=== E_multiitem（3 品項乾淨合成案例，測 I1–I3／稅則候選／readiness）===")
    e_json = run_case(FIXTURES / "E_multiitem", OUT / "E_multiitem.json")
    e_counts = checker_counts(e_json)
    check("E_multiitem: R0-R16 FAIL=0,WARN=1,PASS=15,SKIP=1",
          e_counts == {"FAIL": 0, "WARN": 1, "PASS": 15, "SKIP": 1}, e_counts)

    sys.path.insert(0, str(SCRIPTS))
    import checker as checker_mod
    e_doc = json.loads(e_json.read_text(encoding="utf-8"))
    item_checks = checker_mod.run_item_checks(e_doc)
    i_counts = {k: sum(1 for c in item_checks if c["level"] == k)
                for k in ("PASS", "WARN", "FAIL", "SKIP")}
    check("E_multiitem: I1-I3 PASS=6,WARN=1,FAIL=0,SKIP=0（品項 3 缺品名 → I2 WARN，不擋 BLOCKED）",
          i_counts == {"PASS": 6, "WARN": 1, "FAIL": 0, "SKIP": 0}, i_counts)

    status = export_erp(e_json, OUT / "E_multiitem")
    check("E_multiitem: 未確認任何品項稅則前 → REVIEW_REQUIRED", status == "REVIEW_REQUIRED", status)
    e_import = OUT / "E_multiitem/MDO-2609-005/import/MDO-2609-005_erp_import_ready.csv"
    check("E_multiitem: 未確認前 import/ 沒有檔案", not e_import.exists())

    items_csv = OUT / "E_multiitem/MDO-2609-005/staging/MDO-2609-005_items.csv"
    with open(items_csv, encoding="utf-8-sig") as f:
        item_rows = list(_csv.DictReader(f))
    check("E_multiitem: items.csv 3 列（每品項一列）", len(item_rows) == 3, len(item_rows))
    check("E_multiitem: items.csv 欄位是 top_candidate_ccc_code（不是 recommended_ccc_code）",
          "top_candidate_ccc_code" in item_rows[0] and "recommended_ccc_code" not in item_rows[0],
          list(item_rows[0].keys()))

    cand_csv = OUT / "E_multiitem/MDO-2609-005/staging/MDO-2609-005_tariff_candidates.csv"
    with open(cand_csv, encoding="utf-8-sig") as f:
        cand_rows = list(_csv.DictReader(f))
    # 這三條斷言需要 tariff.db 真的有候選資料可查——沒有這個 10MB 衍生
    # 資料庫（刻意不進版控，見 .gitignore／README.md）時，
    # build_candidates_for_item() 會如預期回傳空清單，不是壞掉，這裡
    # 略過而不是回報失敗（斷言本身不變，只是有沒有資料可測的條件判斷，
    # 跟前面「稅則查詢／稅則候選相關度」那個大區塊同樣的處理原則）。
    if checker_mod.TARIFF_DB.exists():
        check("E_multiitem: tariff_candidates.csv 有列（自動逐品項檢索，不用手動觸發）",
              len(cand_rows) > 0, len(cand_rows))
        check("E_multiitem: 候選欄位是 tariff_rate_reference/_basis（不宣稱是應適用稅率）",
              cand_rows and "tariff_rate_reference" in cand_rows[0] and "tariff_rate_basis" in cand_rows[0],
              list(cand_rows[0].keys()) if cand_rows else None)
        check("E_multiitem: 每一列 requires_human_confirmation 都是 True",
              cand_rows and all(r["requires_human_confirmation"] == "True" for r in cand_rows),
              {r["candidate_ccc_code"] for r in cand_rows if r["requires_human_confirmation"] != "True"})
    else:
        print(f"  · 略過 3 項稅則候選斷言（找不到 {checker_mod.TARIFF_DB}）")

    # 確認全部 3 品項的 CCC 後：I2 的 WARN（品項 3 缺品名）仍未解決，
    # 案件應該繼續停在 REVIEW_REQUIRED，不能因為稅則都確認了就跳過
    # 抽取完整性這一關。
    status2 = export_erp(e_json, OUT / "E_multiitem", [
        "--confirm-ccc-item", "1", "1515.50.00.00.5",
        "--confirm-ccc-item", "2", "1207.30.00.11.0",
        "--confirm-ccc-item", "3", "1515.90.00.90.7",
    ])
    check("E_multiitem: 全部品項確認 CCC 後仍是 REVIEW_REQUIRED"
          "（品項 3 缺品名的 I2 WARN 還沒解決，不能只看稅則）",
          status2 == "REVIEW_REQUIRED", status2)
    check("E_multiitem: 即使稅則都確認了，import/ 仍沒有檔案（I2 WARN 未解決）",
          not e_import.exists())

    # ======================================================================
    # GD-2609-003（真實多品項展示案例）：不是拿它測新規則，是釘住既有行為
    # 不能因為改了共用解析函式（parse_parties／parse_packing_list／
    # parse_transport_doc）而跟著跑掉——這票的 B/L 是『表頭列＋下一列
    # 才是值』的版面（Vessel/Voyage/Port of Loading/Port of Discharge
    # 四個標籤同一列，值在下一列），跟 GD-2609-004 的『同一列就帶值』
    # 版面剛好互為對照組，兩者不能顧此失彼。
    # ======================================================================
    print("=== GD-2609-003（既有行為回歸：多欄表頭列版面不能因為改壞）===")
    g3_json = run_case(FIXTURES / "GD-2609-003", OUT / "GD-2609-003.json")
    g3_doc = json.loads(g3_json.read_text(encoding="utf-8"))
    check("GD-2609-003: manual_supplements.json 合併後 5 品項",
          len(g3_doc["invoice"]["items"]) == 5, len(g3_doc["invoice"]["items"]))
    check("GD-2609-003: vessel/voyage 仍正確（表頭列＋下一列版面）＝EVER VISION / 097S",
          (g3_doc["transport_document"]["vessel"], g3_doc["transport_document"]["voyage"])
          == ("EVER VISION", "097S"),
          (g3_doc["transport_document"]["vessel"], g3_doc["transport_document"]["voyage"]))
    check("GD-2609-003: buyer_name 仍正確（'Buyer / Messrs.' 版面）",
          g3_doc["parties"]["buyer_name"] == "UNI-LEAVES TRADING LTD.",
          g3_doc["parties"]["buyer_name"])
    g3_counts = checker_counts(g3_json)
    check("GD-2609-003: R0-R16 FAIL=0,WARN=1,PASS=13,SKIP=3（跟修正前一致）",
          g3_counts == {"FAIL": 0, "WARN": 1, "PASS": 13, "SKIP": 3}, g3_counts)
    g3_item_checks = checker_mod.run_item_checks(g3_doc)
    g3_i_counts = {k: sum(1 for c in g3_item_checks if c["level"] == k)
                   for k in ("PASS", "WARN", "FAIL", "SKIP")}
    check("GD-2609-003: I1-I3 PASS=11,WARN=0,FAIL=0,SKIP=0（跟修正前一致）",
          g3_i_counts == {"PASS": 11, "WARN": 0, "FAIL": 0, "SKIP": 0}, g3_i_counts)
    g3_status = export_erp(g3_json, OUT / "GD-2609-003")
    check("GD-2609-003: readiness 仍是 REVIEW_REQUIRED（跟修正前一致）",
          g3_status == "REVIEW_REQUIRED", g3_status)

    # ======================================================================
    # GD-2609-004（真實多品項案例，回歸測試：Packing List 版面變異 +
    # None 防呆）
    #
    # 這票原本會讓 checker.py 直接丟 TypeError 崩潰——Packing List 是
    # 『逐品項列＋單一貨櫃』版面，規則式解析器完全沒吃到，
    # declared_gross 等欄位是 None，report() 表頭對 None 做 :,.2f
    # 格式化直接爆掉。這裡釘住修好之後的完整鏈路，且刻意不對
    # GD-2609-004 這個票號寫任何特判——所有修正都是通用的版面規則。
    # ======================================================================
    print("=== GD-2609-004（Packing List 版面變異 + None 防呆回歸測試）===")
    g4_json = run_case(FIXTURES / "GD-2609-004", OUT / "GD-2609-004.json")
    g4_doc = json.loads(g4_json.read_text(encoding="utf-8"))

    check("GD-2609-004: Invoice 4 品項、總額 20560（Invoice 本來就沒壞，不受影響）",
          len(g4_doc["invoice"]["items"]) == 4 and g4_doc["invoice"]["total_amount"] == 20560.0,
          (len(g4_doc["invoice"]["items"]), g4_doc["invoice"]["total_amount"]))
    check("GD-2609-004: buyer_name 抽到（純 'Buyer' 標籤版面，非 MESSRS）",
          g4_doc["parties"]["buyer_name"] == "UNI-LEAVES TRADING LTD.",
          g4_doc["parties"]["buyer_name"])

    g4_pl = g4_doc["packing_list"]
    check("GD-2609-004: Packing List 逐品項列版面抽到 declared_packages/net/gross",
          (g4_pl["declared_packages"], g4_pl["declared_net"], g4_pl["declared_gross"])
          == (120, 6700.0, 7280.0),
          (g4_pl["declared_packages"], g4_pl["declared_net"], g4_pl["declared_gross"]))
    check("GD-2609-004: Packing List 單一貨櫃號正確歸入 containers（來自 PL 本身，不是抄 B/L）",
          [c["cntr_no"] for c in g4_pl["containers"]] == ["TGHU7654321"], g4_pl["containers"])
    check("GD-2609-004: Packing List 沒有殘留 PACKING_LIST_EXTRACTION_INCOMPLETE"
          "（核心欄位都抽到了，不該再卡 BLOCKING case_issue）",
          not any(i["code"] == "PACKING_LIST_EXTRACTION_INCOMPLETE"
                  for i in g4_doc["case_issues"]), g4_doc["case_issues"])

    g4_td = g4_doc["transport_document"]
    check("GD-2609-004: B/L vessel/voyage 沒有吃到下一個欄位標籤的字"
          "（同一行 'Vessel / Voyage  NAME / CODE' 版面）＝EVER GLORY / 118N",
          (g4_td["vessel"], g4_td["voyage"]) == ("EVER GLORY", "118N"),
          (g4_td["vessel"], g4_td["voyage"]))
    check("GD-2609-004: B/L total_packages 抽到（CARTONS 單位，不是只認 PKG）",
          g4_td["total_packages"] == 120, g4_td["total_packages"])

    r = run(["python3", str(SCRIPTS / "checker.py"), str(g4_json), "--direction", "import"])
    check("GD-2609-004: checker.py 不崩潰（stderr 沒有 Traceback，缺欄位一律 SKIP 不是丟例外）",
          "Traceback" not in r.stderr, r.stderr[:300])
    check("GD-2609-004: invoice.currency 抽到 USD（純標籤版面 'Currency  USD'，不是括號式）",
          g4_doc["invoice"]["currency"] == "USD", g4_doc["invoice"]["currency"])
    g4_counts = checker_counts(g4_json)
    check("GD-2609-004: R0-R16 FAIL=1,WARN=1,PASS=12,SKIP=3"
          "（R15 毛重 7,280 vs 7,180 真的對不上 → 這是文件矛盾，不是抽取缺口；"
          "R10 幣別一致轉為 PASS，因為 currency 抓到了）",
          g4_counts == {"FAIL": 1, "WARN": 1, "PASS": 12, "SKIP": 3}, g4_counts)
    g4_item_checks = checker_mod.run_item_checks(g4_doc)
    g4_i_counts = {k: sum(1 for c in g4_item_checks if c["level"] == k)
                   for k in ("PASS", "WARN", "FAIL", "SKIP")}
    check("GD-2609-004: I1-I3 PASS=9,WARN=0,FAIL=0,SKIP=0（Invoice 本身沒有矛盾）",
          g4_i_counts == {"PASS": 9, "WARN": 0, "FAIL": 0, "SKIP": 0}, g4_i_counts)

    g4_status = export_erp(g4_json, OUT / "GD-2609-004")
    check("GD-2609-004: readiness = BLOCKED（毛重真的對不上，不是抽取缺口偽裝成的假 BLOCKED）",
          g4_status == "BLOCKED", g4_status)
    g4_case_status = json.loads(
        (OUT / "GD-2609-004/GD-2609-004/case_status.json").read_text(encoding="utf-8"))
    check("GD-2609-004: blocking_reasons 只剩 R15 毛重矛盾"
          "（currency 抓到後，MANDATORY_FIELD: invoice.currency 這條假性 blocker 消失）",
          g4_case_status.get("blocking_reasons") == [
              "[R15] 件數毛重與B/L相符: 件數 B/L 120／PL 120｜毛重 B/L 7,180.00／PL 7,280.00"],
          g4_case_status.get("blocking_reasons"))
    g4_import = OUT / "GD-2609-004/GD-2609-004/import/GD-2609-004_erp_import_ready.csv"
    check("GD-2609-004: BLOCKED 時沒有產生 erp_import_ready.csv", not g4_import.exists())
    g4_cand_csv = OUT / "GD-2609-004/GD-2609-004/staging/GD-2609-004_tariff_candidates.csv"
    check("GD-2609-004: 稅則候選仍照常產生（BLOCKING 不會蓋掉稅則檢索，只是擋住 import）",
          g4_cand_csv.exists() and g4_cand_csv.stat().st_size > 0)

    # ======================================================================
    # ERP readiness：CASE A（A_clean，確認 CCC 前後，單品項乾淨案例）
    # ======================================================================
    print("=== ERP readiness：CASE A（A_clean，確認 CCC 前後）===")
    a_out = OUT / "A_clean"
    status = export_erp(a_json, a_out)
    check("CASE A 步驟1: REVIEW_REQUIRED（CCC 未確認）", status == "REVIEW_REQUIRED", status)
    status = export_erp(a_json, a_out, ["--confirm-ccc", "84818000090", "--force-confirm"])
    check("CASE A 步驟2: READY（確認 CCC 後）", status == "READY", status)
    a_import = a_out / "NKS-2608-114/import/NKS-2608-114_erp_import_ready.csv"
    check("CASE A: erp_import_ready.csv 有產生（READY 才產生）", a_import.exists())

    print("=== ERP readiness：CASE B（B_faulty，預期 BLOCKED）===")
    b_out = OUT / "B_faulty"
    status = export_erp(b_json, b_out)
    check("CASE B: BLOCKED", status == "BLOCKED", status)
    check("CASE B: import/ 沒有 erp_import_ready.csv",
          not any((b_out / "SHG-2608-77/import").glob("*.csv")))

    print("=== ERP readiness：CASE C（C_altlayout，預期 BLOCKED）===")
    c_out = OUT / "C_altlayout"
    status = export_erp(c_json, c_out)
    check("CASE C: BLOCKED", status == "BLOCKED", status)

    print("=== ERP readiness：CASE E（D_export，預期 REVIEW_REQUIRED、無 FAIL）===")
    d_out = OUT / "D_export"
    status = export_erp(d_json, d_out)
    check("CASE E: REVIEW_REQUIRED", status == "REVIEW_REQUIRED", status)
    check("CASE E: 未確認 CCC 前 import/ 沒有檔案（新規則比舊版更保守，只有 READY 才產生）",
          not any((d_out / "ULT-2608-039/import").glob("*.csv")))

    # ======================================================================
    # case_issues 嚴重度確實影響 readiness
    # ======================================================================
    print("=== case_issues：NO_TEXT_LAYER／AMBIGUOUS_DOCTYPE_SELECTION ===")
    scan_dir = OUT / "scan_case"
    scan_dir.mkdir(parents=True, exist_ok=True)
    (scan_dir / "invoice.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")  # 沒有文字層
    shutil.copy(FIXTURES / "A_case" / "packing_list.pdf", scan_dir / "packing_list.pdf")
    shutil.copy(FIXTURES / "A_case" / "bill_of_lading.pdf", scan_dir / "bill_of_lading.pdf")
    scan_json = run_case(scan_dir, OUT / "scan_case.json")
    scan_doc = json.loads(scan_json.read_text(encoding="utf-8"))
    no_text_issues = [i for i in scan_doc["case_issues"] if i["code"] == "NO_TEXT_LAYER"]
    check("scan_case: invoice.pdf 無文字層被記為 NO_TEXT_LAYER／BLOCKING（依檔名判斷是必要文件）",
          bool(no_text_issues) and no_text_issues[0]["severity"] == "BLOCKING", no_text_issues)
    scan_status = export_erp(scan_json, OUT / "scan_case")
    check("scan_case: BLOCKING case_issue 讓案件直接 BLOCKED", scan_status == "BLOCKED", scan_status)

    dup_dir = OUT / "dup_case"
    dup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "A_case" / "commercial_invoice.pdf", dup_dir / "commercial_invoice.pdf")
    shutil.copy(FIXTURES / "A_case" / "commercial_invoice.pdf", dup_dir / "commercial_invoice_copy.pdf")
    shutil.copy(FIXTURES / "A_case" / "packing_list.pdf", dup_dir / "packing_list.pdf")
    shutil.copy(FIXTURES / "A_case" / "bill_of_lading.pdf", dup_dir / "bill_of_lading.pdf")
    dup_json = run_case(dup_dir, OUT / "dup_case.json")
    dup_doc = json.loads(dup_json.read_text(encoding="utf-8"))
    ambiguous = [i for i in dup_doc["case_issues"] if i["code"] == "AMBIGUOUS_DOCTYPE_SELECTION"]
    check("dup_case: 兩個檔案都像 Invoice → AMBIGUOUS_DOCTYPE_SELECTION／REVIEW",
          bool(ambiguous) and ambiguous[0]["severity"] == "REVIEW", ambiguous)
    dup_status = export_erp(dup_json, OUT / "dup_case")
    check("dup_case: REVIEW case_issue 讓案件至少停在 REVIEW_REQUIRED（不因為其他都過就 READY）",
          dup_status == "REVIEW_REQUIRED", dup_status)

    # ======================================================================
    # checker.py None 防呆：不依賴任何 fixture 檔案，直接建構邊界情況，
    # 釘住「缺欄位」永遠走 SKIP／老實列出缺什麼，不會走到格式化 crash、
    # 也不會編造一個看起來像通過的 PASS。這幾個邊界不是 GD-2609-004
    # 剛好觸發的那一種缺法，是 GD-2609-004 事件之後全面稽核 checker.py
    # 抓出的其他潛在 crash／假 PASS 點，用建構出來的 doc 直接命中，
    # 不必依賴某份 PDF 剛好長成那樣。
    # ======================================================================
    print("=== checker.py None 防呆（直接建構邊界情況，不靠 fixture）===")

    def run_checks_safely(doc, direction=None):
        try:
            results = checker_mod.run_checks(doc, direction)
            checker_mod.report(doc, results, direction)  # 表頭格式化本身也要過
            return results, None
        except Exception as e:  # noqa: BLE001 － 這裡就是要抓任何例外，不是要重拋
            return None, e

    # R5：declared_net 有、declared_gross 缺（GD-2609-004 事件本身是兩個都
    # 缺，不會走到這個分支；這裡專門補這個沒被那次事件覆蓋到的另一半）。
    doc = {"invoice": {"invoice_no": "T1", "items": [{"qty": 1, "unit": "KGS", "unit_price": 1, "amount": 1}]},
           "packing_list": {"invoice_no": "T1",
                             "containers": [{"cntr_no": "ABCD1234567", "packages": 1,
                                             "qty": None, "net_weight": 50, "gross_weight": None}],
                             "declared_net": 50, "declared_gross": None}}
    results, exc = run_checks_safely(doc)
    check("R5 防呆：declared_gross 缺（declared_net 有）不崩潰、SKIP 不是 PASS",
          exc is None and next(c["level"] for c in results if c["rule"] == "R5") == checker_mod.SKIP,
          exc or [c for c in results if c["rule"] == "R5"])

    # R7：Packing List 有帶 qty 的櫃列，但 Invoice 最後一項自己的 qty 是
    # None（例如那一項專門測 I2 缺欄位的合成案例）。
    doc = {"invoice": {"invoice_no": "T2",
                        "items": [{"qty": None, "unit": None, "unit_price": None, "amount": None}]},
           "packing_list": {"invoice_no": "T2",
                             "containers": [{"cntr_no": "ABCD1234567", "packages": 1,
                                             "qty": 10, "net_weight": 10, "gross_weight": 12}]}}
    results, exc = run_checks_safely(doc)
    check("R7 防呆：Invoice 最後一項 qty 是 None 不崩潰、SKIP 不是 PASS",
          exc is None and next(c["level"] for c in results if c["rule"] == "R7") == checker_mod.SKIP,
          exc or [c for c in results if c["rule"] == "R7"])

    # R8：單價是合法的 0（贈品／樣品），不該被 truthy 檢查誤判成缺欄位。
    # 核心欄位（CORE_FIELDS）刻意都填滿，避免 R0「抽取不完整」的降級
    # 邏輯把這裡要單獨驗證的 R8 PASS 蓋成 SKIP，混淆了在測哪一條規則。
    doc = {"invoice": {"invoice_no": "T3", "date": "SEP.07,2026",
                        "items": [{"qty": 100, "unit": "PCS", "unit_price": 0, "amount": 0}]},
           "packing_list": {"invoice_no": "T3",
                             "containers": [{"cntr_no": "ABCD1234567", "packages": 1,
                                             "qty": None, "net_weight": 1, "gross_weight": 1}],
                             "declared_gross": 1}}
    results, exc = run_checks_safely(doc)
    check("R8 防呆：單價=0（合法值，非缺欄位）仍能算出 PASS，不被 truthy 檢查誤判成 SKIP",
          exc is None and next(c["level"] for c in results if c["rule"] == "R8") == checker_mod.PASS,
          exc or [c for c in results if c["rule"] == "R8"])
    # R8：qty/unit_price 都有，但 amount 缺——原本的 truthy 檢查只看
    # unit_price，這種情況會直接拿 None 去跟 calc 比、格式化時崩潰。
    doc = {"invoice": {"invoice_no": "T4",
                        "items": [{"qty": 10, "unit": "PCS", "unit_price": 5, "amount": None}]}}
    results, exc = run_checks_safely(doc)
    check("R8 防呆：amount 缺（qty/unit_price 都有）不崩潰、SKIP 並列出缺 amount",
          exc is None
          and next(c["level"] for c in results if c["rule"] == "R8") == checker_mod.SKIP
          and "amount" in next(c["detail"] for c in results if c["rule"] == "R8"),
          exc or [c for c in results if c["rule"] == "R8"])

    # R15：B/L 有毛重，但 Packing List 的 declared_gross 缺——原本這裡會
    # 因為 d 是空清單而印出跟「通過」意思矛盾的假 PASS。
    doc = {"invoice": {"invoice_no": "T5", "items": [{"qty": 1, "unit": "KGS", "unit_price": 1, "amount": 1}]},
           "packing_list": {"invoice_no": "T5", "containers": [], "declared_packages": None,
                             "declared_gross": None},
           "transport_document": {"doc_type": "BL", "total_packages": None, "gross_weight": 999,
                                   "containers": []}}
    results, exc = run_checks_safely(doc)
    check("R15 防呆：B/L 有毛重、PL declared_gross 缺，不崩潰、SKIP 不是假 PASS",
          exc is None and next(c["level"] for c in results if c["rule"] == "R15") == checker_mod.SKIP,
          exc or [c for c in results if c["rule"] == "R15"])

    # 報表表頭：Packing List／Invoice 幾乎整份是 None（GD-2609-004 那次
    # 事件實際崩潰的位置），直接對表頭格式化本身做最小重現。
    doc = {"invoice": {"invoice_no": None, "date": None, "incoterm": None, "currency": None,
                        "origin": None, "items": [], "total_amount": None},
           "packing_list": {"invoice_no": None, "declared_packages": None,
                             "declared_gross": None, "containers": []},
           "transport_document": {}}
    results, exc = run_checks_safely(doc, "import")
    check("報表表頭防呆：Invoice／Packing List 幾乎全 None 時 report() 不崩潰"
          "（GD-2609-004 原始崩潰點的最小重現）",
          exc is None, exc)

    # 這整段（稅則查詢／稅則候選相關度）都依賴 tariff.db，那是
    # build_tariff_db.py 從財政部關務署公告的稅則 PDF 解析出來的衍生
    # 資料庫，10MB，刻意不進版控（見 .gitignore）——全新 clone 下來的
    # repo 預設不會有這個檔案，沒有它不代表程式有問題，是這份資料本身
    # 太大且需要另外的原始 PDF 才能重建。沒有 tariff.db 時完整略過這段，
    # 不當成失敗（既有斷言本身完全不變，只是有沒有資料可測的條件判斷）。
    if checker_mod.TARIFF_DB.exists():
        # ======================================================================
        # 稅則查詢：已知強比對案例不能壞掉
        # ======================================================================
        print("=== 稅則查詢：已知強比對案例不能壞掉 ===")
        r = run(["python3", str(SCRIPTS / "tariff_lookup.py"), "GALVANIZED STEEL COIL", "--top", "1"])
        check("GALVANIZED STEEL COIL 命中 7306.69", "7306.69" in r.stdout, r.stdout[:200])
        r = run(["python3", str(SCRIPTS / "tariff_lookup.py"), "BRAKE PAD FOR PASSENGER CAR", "--top", "1"])
        check("BRAKE PAD FOR PASSENGER CAR 命中 6813.20（詞界比對修復後）",
              "6813.20" in r.stdout, r.stdout[:200])

        # ======================================================================
        # 稅則候選相關度：來源 HS6 前綴優先、不靜默放寬到不相關章節、
        # 通用詞不能單獨撐起候選、查無可信候選時 top_candidate_ccc_code 留空
        # ======================================================================
        print("=== 稅則候選相關度（來源 HS6 優先、負向相關度檢查、安全棄權）===")
        import sqlite3
        import tariff_candidates as tc

        con = sqlite3.connect(str(checker_mod.TARIFF_DB))
        con.row_factory = sqlite3.Row

        # 樹薯澱粉：來源 HS6=110814，資料庫裡確實有對應號列（強篩選）→ 候選
        # 必須全部落在 110814 範圍內，不能被「小麥/玉米/馬鈴薯澱粉」這類
        # 同章節但不對應的候選蓋過——這條測「範圍」這件事本身有沒有做對。
        cands = tc.build_candidates_for_item(
            con, "T", {"line_no": 1, "description": "TAPIOCA STARCH",
                       "description_normalized": "TAPIOCA STARCH", "source_hs_prefix": "110814"})
        check("TAPIOCA STARCH + source_hs_prefix=110814：候選都落在 110814 範圍內（source_hs_scope_match=True）",
              bool(cands) and all(c["candidate_ccc_code"].replace(".", "").startswith("110814")
                                  and c["source_hs_scope_match"] is True for c in cands),
              [(c["candidate_ccc_code"], c["source_hs_scope_match"]) for c in cands])
        # 但「TAPIOCA」是英文商業慣用語，資料庫用的是「Manioc (cassava)」這個
        # 稅則慣用語彙，兩者字面上不重疊——範圍對不代表品名字面也對得上，
        # 這種情況必須誠實標成衝突，不能假裝是強候選、也不能自動判斷
        # tapioca＝manioc 這種語意等價（那是需要人類知識的判斷，不是字面比對
        # 能做的事）。
        check("TAPIOCA STARCH + source_hs_prefix=110814：品名字面不重疊 → SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT（不是 STRONG）",
              cands and all(c["overall_retrieval_status"] ==
                            "SOURCE_HS_SCOPE_MATCH_DESCRIPTION_CONFLICT" for c in cands),
              [(c["candidate_ccc_code"], c["overall_retrieval_status"]) for c in cands])
        reliable_top = (cands and cands[0]["overall_retrieval_status"] in tc.RELIABLE_STATUSES)
        check("TAPIOCA STARCH：範圍對品名不對時，rank-1 不屬於可信狀態（items.csv 的 top_candidate_ccc_code 應留空）",
              not reliable_top, cands[0]["overall_retrieval_status"] if cands else None)

        # 椰漿粉：來源 HS6=210690，該標題底下有 22 筆候選，其中一筆
        # 「2106.90.93.00.8 椰漿粉／Coconut milk powder」品名字面完全對得上
        # ——用來證明範圍優先＋品名相關度重排序合起來真的能把它排到第一名，
        # 而不是停在數字排序的第一筆（糖漿）。
        cands = tc.build_candidates_for_item(
            con, "T", {"line_no": 0, "description": "COCONUT MILK POWDER",
                       "description_normalized": "COCONUT MILK POWDER", "source_hs_prefix": "210690"})
        check("COCONUT MILK POWDER + source_hs_prefix=210690：rank-1 是「椰漿粉」且狀態為 STRONG_SOURCE_HS_MATCH",
              cands and cands[0]["candidate_ccc_code"] == "2106.90.93.00.8"
              and cands[0]["overall_retrieval_status"] == "STRONG_SOURCE_HS_MATCH",
              cands[0] if cands else None)

        # 椰子萃取液：沒有來源 HS，純品名檢索——不能因為「NATURAL」「EXTRACT」
        # 這種通用詞跟豬脂肪/家禽脂肪/乳品的品名字根巧合而把它們列為候選。
        cands = tc.build_candidates_for_item(
            con, "T", {"line_no": 2, "description": "NATURAL COCONUT FLAVOR EXTRACT",
                       "description_normalized": "NATURAL COCONUT FLAVOR EXTRACT"})
        bad = [c for c in cands if any(w in c["candidate_description"].upper()
                                        for w in ("PIG FAT", "POULTRY FAT", "MILK", "PORK"))]
        check("NATURAL COCONUT FLAVOR EXTRACT：候選不含豬脂肪/家禽脂肪/乳品（通用詞不能單獨命中）",
              not bad, [(c["candidate_ccc_code"], c["candidate_description"]) for c in bad])
        check("NATURAL COCONUT FLAVOR EXTRACT：候選（若有）都含 COCONUT 字樣",
              all("COCONUT" in c["candidate_description"].upper() for c in cands) if cands else True,
              [c["candidate_description"] for c in cands])

        # 來源 HS 查無對應號列時：不能靜默放寬，退路候選必須明確標
        # OUTSIDE_SOURCE_HS_SCOPE，不能跟 STRONG_SOURCE_HS_MATCH 混在一起讓人
        # 誤以為同樣可信。用一個資料庫裡確定不存在的 HS6 前綴（999999）測試。
        cands = tc.build_candidates_for_item(
            con, "T", {"line_no": 3, "description": "DRIED COCONUT CHIPS",
                       "description_normalized": "DRIED COCONUT CHIPS", "source_hs_prefix": "999999"})
        check("來源 HS6（999999）資料庫查無對應號列時，退路候選明確標 OUTSIDE_SOURCE_HS_SCOPE（不是靜默放寬）",
              not cands or all(c["overall_retrieval_status"] == "OUTSIDE_SOURCE_HS_SCOPE" for c in cands),
              [(c["candidate_ccc_code"], c["overall_retrieval_status"]) for c in cands])

        # 查無可信候選：品名太籠統/查無關鍵字命中 → 回傳空清單，不硬湊候選。
        cands = tc.build_candidates_for_item(
            con, "T", {"line_no": 4, "description": "XYZZQWK NONSENSE ITEM 12345",
                       "description_normalized": "XYZZQWK NONSENSE ITEM 12345"})
        check("查無可信候選時回傳空清單（不硬湊不相關候選）", cands == [], cands)

        con.close()
    else:
        print(f"=== 稅則查詢／稅則候選相關度：略過（找不到 {checker_mod.TARIFF_DB}，"
              "見 README.md「取得 tariff.db」）===")

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} 項測試失敗：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("✓ 全部測試通過")
    sys.exit(0)


if __name__ == "__main__":
    main()
