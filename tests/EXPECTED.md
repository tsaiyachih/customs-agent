# 測試組預期結果

以 `tests/make_fixtures.py` 產生的合成文件（非真實客戶資料）。
每次改動 `extract.py` 或 `checker.py` 後重跑，比對是否相符。

```bash
python3 tests/make_fixtures.py data/raw/fixtures
S=.claude/skills/customs-check/scripts
for f in A_clean B_faulty C_altlayout; do
  python3 $S/extract.py data/raw/fixtures/$f.pdf data/output/$f.json
  python3 $S/checker.py data/output/$f.json
done
```

## A_clean — 日本供應商，CIF，三櫃，附 B/L

文件完全一致。用來檢驗**偽陽性**：不該把正確的文件判成有問題。

預期：不符 0｜待確認 1（R13 稅則，供應商標示 HS 848180 僅為候選值）｜通過 15

## B_faulty — 中國供應商，EXW，三櫃，附 B/L

刻意植入十一項錯誤。用來檢驗**偽陰性**：每條規則都抓得到對應的錯。

| 規則 | 植入的錯誤 |
|---|---|
| R1 | 發票號 SHG-2608-77 vs SHG-2608-7 |
| R2 | 日期 AUG.05 vs AUG.06 |
| R3 | 申報 4 櫃，實列 3 櫃 |
| R4 | 逐櫃 900 PKG，表尾 950 PKG |
| R5 | 逐櫃淨重 12,000，表尾 12,500 |
| R6 | CSNU9988776 淨重 4050 > 毛重 3990 |
| R7 | Packing List 900 vs Invoice 1000 |
| R8 | 1000 × 32.50 = 32,500，申報 33,500 |
| R11 | 櫃號 ABCD12345 不合 ISO 6346 |
| R14 | B/L 有 TGHU5566778，Packing List 沒有 |
| R15 | B/L 900 PKGS / 12,260 KGS vs PL 950 / 12,760 |

另有 WARN：R9（EXW 須加計保費）、R10（貨價 CNY 運費 USD）、
R12（原產地中國）、R13（無 HS Code）。

預期：不符 11｜待確認 4｜通過 1

## C_altlayout — 荷蘭供應商，歐陸版面，兩櫃，無 B/L

版面與 A/B 完全不同：歐陸數字格式（`49.920,00`）、
櫃號帶空格（`MRKU 664 4550`）、欄位以點線對齊而非表格。

用來檢驗**泛化能力的邊界**。規則式解析器預期會失敗，
重點在於它必須**安全地失敗**——R0 判定抽取不完整，
其餘規則一律不得回報 PASS。

預期：R0 FAIL｜通過 0｜其餘 SKIP

## D_export — 台灣出口日本，CIF 條件，兩櫃，附 S/O（無 B/L）

出口案例（台灣→日本）。用來檢驗方向判斷與「出口不應要求 B/L」這條規則。

## A_case — 跟 A_clean 同內容，三個獨立檔案

`commercial_invoice.pdf` / `packing_list.pdf` / `bill_of_lading.pdf`
三個獨立檔案，內容完全等同 A_clean。用來證明 `run_case.py`（分離檔案）
跟 `extract.py`（合併 PDF）這兩條輸入路徑共用同一套解析核心，對同樣的
內容必須得到完全一樣的抽取結果與 checker 計數。

## E_multiitem — 3 品項乾淨合成案例，三個獨立檔案

供應商越南、進口、單櫃，3 個品項：品項 1 有供應商 HS Code、品項 2 沒有、
品項 3 故意讓 `_item_description()` 抓不到品名（測 I2）。R0–R16 應與
A_clean 同樣的 profile（FAIL=0, WARN=1[R13], PASS=15, SKIP=1）；
I1–I3（`run_item_checks()`）預期 PASS=6, WARN=1（品項 3 缺品名）,
FAIL=0, SKIP=0。用來穩定測試 `export_erp.py` 的多品項 staging／自動稅則
候選檢索／readiness 邏輯，不受真實文件雜訊影響。

## GD-2609-003 — 真實（合成但非人工植入錯誤）多品項展示案例，三個獨立檔案

5 個品項（COCONUT SAP CONCENTRATE / COCONUT MILK POWDER / DRIED COCONUT
CHIPS / NATURAL COCONUT FLAVOR EXTRACT / TAPIOCA STARCH）、2 個貨櫃，
文件本身帶有對角線浮水印（"SAMPLE" 字樣）。用來驗收「分離檔案 → 文件
辨識 → 各自抽取 → consolidate → 跨文件驗證 → 品項層驗證 → 自動稅則候選
→ readiness → staging → HTML 報告」整條鏈路，**不手動合併三個 PDF**。

**已知、刻意保留的行為**：`pdftotext -layout` 對這份文件的浮水印跟
表格欄位發生座標碰撞，使品項 1、2（COCONUT SAP CONCENTRATE / COCONUT
MILK POWDER）的數量/單價/金額被拆到不相鄰的輸出列，規則式解析抓不到，
只能拿到品項 3–5。這不是靜默漏算——`total_amount` 改抓發票表尾真正
印出的金額（35,350.00），新增的 **I3（品項金額加總與發票總額相符）
會 FAIL**（3 品項加總 13,450.00 ≠ 35,350.00），使 `case_status` 直接
`BLOCKED`，正確攔下這個抽取缺口，而不是讓案件看起來比實際驗證過的還
完整。要往下走，需要針對這份文件改用視覺方式補齊品項 1、2
（`extraction_method` 標成 `vision`），這正是 `SKILL.md` 描述的
流程——補齊後重跑會得到 5 品項、`case_status` 變成 `REVIEW_REQUIRED`
（5 項都待稅則確認），逐項 `--confirm-ccc-item` 後變成 `READY`。

## GD-2609-004 — 真實多品項案例，三個獨立檔案；版面變異＋缺欄位防呆回歸測試

4 個品項（COCONUT CREAM POWDER / ROASTED CASHEW NUTS / CASSAVA STARCH,
FOOD GRADE / PANDAN FLAVORING EXTRACT）、1 個貨櫃。跟 GD-2609-003 分工
不同：這票的 Invoice 本身文字層乾淨、品項齊全，重點考驗三個跟
GD-2609-003 不同的版面變體，外加一個 checker.py 本身的防呆缺口：

1. **Packing List 是「逐品項列＋單一貨櫃」版面**（不是 GD-2609-003 那種
   逐櫃列）：件數/淨重/毛重只在表尾出現一次（`120 CTNS / 6,700 / 7,280`，
   整數、沒有小數點），貨櫃號只在表頭出現一次（`Container No.
   TGHU7654321`），舊版規則式解析器對這種版面完全抓不到任何一個欄位。
2. **Buyer 欄位是純 `Buyer` 標籤**，沒有 `MESSRS` 字樣——舊版 buyer 抽取
   規則只認 `MESSRS`，這種版面會悄悄留空、且不記 `case_issue`。
3. **B/L 是「Vessel / Voyage」合併標籤＋合併值印在同一行**
   （`Vessel / Voyage    EVER GLORY / 118N`），舊版的『多欄表頭列＋值在
   下一行』版面判斷邏輯（`_scan_label_value_rows`）會誤把這一行當成
   表頭列，錯把下一行（`Port of Loading ...`）的內容切進 vessel/voyage，
   得到面目全非的 `vessel="Port of L"`／`voyage="oading ... CAT LAI
   PORT, VIET NAM"`。
4. **checker.py 對缺欄位的格式化不夠防呆**：Packing List 整份抽取失敗
   時（`declared_gross` 等欄位是 `None`），`report()` 表頭直接對 `None`
   做 `:,.2f` 格式化，丟 `TypeError` 讓整支腳本崩潰——這不是「查到不
   符」，是程式本身沒執行完，鐵律規定這種情況要照實回報執行失敗，
   不能用推理代替。
5. **Invoice 的 Currency 是純標籤版面**（`Currency    USD`），沒有括號
   ——舊版只認表格表頭裡的括號式「Amount (USD)」，這種獨立標籤/值列的
   版面完全抓不到，`invoice.currency` 悄悄留 `null`，連帶讓
   `MANDATORY_FIELD: invoice.currency` 變成一個掩蓋真正矛盾（R15 毛重
   對不上）的假性 blocker。

**預期（五個版面/防呆修正都到位後）**：Packing List 抽到
`declared_packages=120｜declared_net=6,700｜declared_gross=7,280｜
container=TGHU7654321`；buyer_name 抽到
`UNI-LEAVES TRADING LTD.`；B/L 抽到 `vessel=EVER GLORY｜voyage=118N`；
`invoice.currency` 抽到 `USD`（標籤式版面，CURRENCIES 白名單比對，
不是寫死 USD）。`checker.py` 完整跑完不崩潰，R0–R16 為
`FAIL=1,WARN=1,PASS=12,SKIP=3`——這個 FAIL 落在 **R15**：Packing List
毛重（7,280）跟 B/L 毛重（7,180）真的對不上，這是文件本身的矛盾
（兩份文件對同一批貨物的毛重申報不一致），不是抽取缺口偽裝的假訊號；
R10（幣別一致）因為 currency 抓到了轉為 PASS。I1–I3 為
`PASS=9,WARN=0,FAIL=0,SKIP=0`（Invoice 本身沒有矛盾）。`case_status`
因此是 `BLOCKED`，`blocking_reasons` 只剩 R15 這一條真正的業務矛盾
（`MANDATORY_FIELD: invoice.currency` 這條假性 blocker 已隨 currency
抽取修正消失），不產生 `erp_import_ready.csv`；稅則候選
（`tariff_candidates.csv`）仍照常產生（BLOCKING 只擋 import，不擋稅則
檢索）。

## ERP 前處理層（`export_erp.py`）測試

不重新解析 PDF，直接吃上面幾組已經跑過 `checker.py` 的 JSON，
只驗證 `determine_readiness()` 這一層的業務判斷。

```bash
S=.claude/skills/customs-check/scripts
python3 $S/extract.py fixtures/A_clean.pdf data/output/A_clean.json
python3 $S/extract.py fixtures/B_faulty.pdf data/output/B_faulty.json
python3 $S/extract.py fixtures/C_altlayout.pdf data/output/C_altlayout.json
python3 $S/extract.py fixtures/D_export.pdf data/output/D_export.json

python3 $S/export_erp.py data/output/A_clean.json --outdir data/output/A_clean
python3 $S/export_erp.py data/output/A_clean.json --outdir data/output/A_clean \
    --confirm-ccc 84818000090
python3 $S/export_erp.py data/output/B_faulty.json --outdir data/output/B_faulty
python3 $S/export_erp.py data/output/C_altlayout.json --outdir data/output/C_altlayout
python3 $S/export_erp.py data/output/D_export.json --outdir data/output/D_export
```

| Case | 輸入 | 預期 readiness | `erp_import.csv` | 關鍵 review/blocking 項 |
|---|---|---|---|---|
| A（步驟1，未確認 CCC） | A_clean | REVIEW_REQUIRED | 產生（`confirmed_ccc_code` 留空） | CCC_CONFIRMATION、R13 |
| A（步驟2，`--confirm-ccc` 後） | A_clean | READY | 產生（完整） | 無 |
| B | B_faulty | BLOCKED | 不產生 | R1/R2/R3/R4/R5/R6/R7/R8/R11/R14/R15（11 個 FAIL） |
| C | C_altlayout | BLOCKED | 不產生 | R0（抽取不完整）+ 5 個 MANDATORY_FIELD |
| D | 同 A 步驟1 | 見上 | — | （跟 CASE A 步驟 1 是同一次測試，不重複列） |
| E | D_export | REVIEW_REQUIRED | 產生（`confirmed_ccc_code` 留空） | CCC_CONFIRMATION、R9（出口 CIF 須轉 FOB）、R12（出口管制品確認）、R13 |

四組（A/B/C/E）不管 readiness 是什麼，`erp_staging.csv`、
`exceptions.csv`、`review_report.txt`、`case_status.json` 一律都要產生。
`erp_import.csv` 只要不是 `BLOCKED` 就會產生（`READY` 或
`REVIEW_REQUIRED`）——`BLOCKED` 代表資料本身矛盾（不是缺欄位，是不知道
哪個值才對），才真的不產生；`REVIEW_REQUIRED` 產生的檔案裡，待確認的
欄位（如 `confirmed_ccc_code`）會留空，`erp_readiness` 欄位標明這筆
還沒完全確認。

## 兩個由此測試組找出的缺陷（已修正）

**一、原產地欄位貪婪匹配。** 原正規表示式 `([A-Za-z ]+)` 會把同一行後面的
`to  KAOHSIUNG` 一併吞入，使 origin 變成 `China    to      KAOHSIUNG`，
與比對集合不符，導致 **R12 誤判為 PASS**。
R12 正是應在裝船前攔截輸入許可問題的規則，誤放的代價最高。
已改為在連續空白或行尾截斷。

**二、部分抽取仍回報 PASS。** C 組幾乎整份抽不出來，
但 R9、R10 僅需單一欄位，仍回報「CIF，無須調整」與「EUR」，
形成「看起來檢查過了」的假象。
已加入 R0 守門規則：核心欄位缺過半即判定抽取不完整，
並將該次所有 PASS 降級為 SKIP。
