# customs-agent

報關文件自動核對 + ERP 前處理原型。將 Invoice / Packing List / 運輸單據
（B/L 或 SO）轉為結構化資料，以規則引擎交叉核對，產出報關草稿與異常清單；
核對過的資料可以再整理成 ERP staging / import 格式，供人工審核後接手匯入
到客戶自己的 ERP 系統。

**這不是自動報關系統，也不綁定任何特定 ERP。** 產出的是通用（generic）
中繼格式，正式匯入前一定要人工審核，也一定要對應到客戶實際使用的 ERP
匯入樣板或 API。

## 環境需求與安裝

- **Python 3.11+**。核心程式（`.claude/skills/customs-check/scripts/`
  底下全部腳本）只用標準函式庫，`pip install` 不需要裝任何東西——見
  `requirements.txt`（裡面唯一列的 `reportlab` 只有重新產生
  `fixtures/` 底下合成測試 PDF 才需要，正常使用/跑測試都用不到）。
- **poppler**（提供 `pdftotext` 這個外部命令列工具，抽取層靠它把 PDF
  轉成文字）——這不是 pip 套件，`pip install` 裝不到，是系統層級的
  套件，沒裝的話幾乎每支腳本一啟動就會找不到指令而失敗：
  - macOS：`brew install poppler`
  - Ubuntu／Debian：`sudo apt-get install poppler-utils`
- **稅則資料庫（`tariff.db`）是選用的，不是必要的**——這是從財政部
  關務署公告的《中華民國海關進口稅則輸出入貨品分類表合訂本》（上冊＋
  下冊）PDF 解析出來的衍生資料庫，約 10MB，**刻意不進版控**（見
  `.gitignore`），clone 下來的 repo 預設不會有這個檔案，也不會有那
  兩份稅則 PDF。沒有它的情況下：
  - 抽取、三單交叉核對（`extract.py`／`run_case.py`／`checker.py` 的
    R0–R15）、`validate.py`、`extract_crosscheck.py`、ERP readiness
    判斷（`export_erp.py`）**都正常運作**，不受影響。
  - `checker.py` 的 R13／R16（稅則號列相關提醒）、`tariff_lookup.py`／
    `tariff_candidates.py`（稅則候選查詢）會優雅跳過（`tariff_candidates.csv`
    照樣產生，但每個品項的候選一律是空清單），不會讓其他功能跟著失敗。
  - `tests/run_tests.py` 裡依賴 `tariff.db` 的斷言會印出略過訊息，
    不算測試失敗。
  - 要取得完整功能：自行下載上述兩份稅則 PDF（財政部關務署網站公告
    的現行稅則合訂本），預設路徑放在
    `~/Downloads/112年上冊final.pdf`／`~/Downloads/112年下冊final.pdf`
    （也可以用 `--db`/位置參數指到別的路徑），執行：
    ```bash
    python3 .claude/skills/customs-check/scripts/build_tariff_db.py \
        ~/Downloads/112年上冊final.pdf ~/Downloads/112年下冊final.pdf
    ```
    建置完會自動跑內建驗證，通過才會寫入
    `.claude/skills/customs-check/data/tariff.db`；驗證沒過會保留在
    暫存檔名供檢查，不會覆蓋已存在的舊資料庫。

## 完整 Workflow

```
案件目錄（一或多個 PDF）
    │  scripts/run_case.py（多個獨立檔案）
    │  scripts/extract.py（單一已合併 PDF）——兩者共用同一套解析核心
    ▼
結構化 JSON（parties / invoice / packing_list / transport_document /
             trade_direction / source_trace / case_issues）
    │  scripts/checker.py
    ├─ run_checks()      單據層級 R0–R16（跨文件比對）
    └─ run_item_checks() 品項層級 I1–I3（逐品項算術／完整性）
    │  scripts/tariff_candidates.py（自動、逐品項，不用手動觸發）
    ▼
稅則候選（僅供參考，top_candidate_ccc_code 是檢索排序結果，不是歸列）
    │  人工用 --confirm-ccc-item 逐項確認
    ▼
items[].confirmed_ccc_code（寫回 JSON，只能人工填入）
    │  scripts/export_erp.py
    ▼
ERP readiness 判斷（READY / REVIEW_REQUIRED / BLOCKED）
    │
    └─ <案件目錄>/<case_id>/
         ├─ structured/<case_id>.json        案件資料鏡射
         ├─ review/<case_id>_report.txt      文字版審核報告
         ├─ review/<case_id>_review.html     給業務/客戶看的完整報告
         ├─ staging/                          6 個 ERP-neutral CSV
         └─ import/<case_id>_erp_import_ready.csv   只有 READY 才產生
```

`run_case.py`（案件目錄，多個獨立檔案）跟 `extract.py`（單一合併 PDF）
共用同一個解析核心 `extract.build_case_from_pages()`——`extract.py` 是
這個核心「只有一個檔案」的特例，不是另外維護的一套邏輯。有多份分開的
Invoice / Packing List / B-L 檔案時優先用 `run_case.py`；文件本來就是
一份合併 PDF（例如 `data/raw/` 底下多數真實客戶檔案）時用 `extract.py`。

## 輸出目錄結構

```
<outdir>/<case_id>/
├── structured/
│   └── <case_id>.json          export_erp 的資料鏡射（含已確認的 CCC）
├── review/
│   ├── <case_id>_report.txt    文字版審核報告
│   └── <case_id>_review.html   自包含 HTML 報告，無外部資源依賴
├── staging/
│   ├── <case_id>_shipment_header.csv   1 案件一列
│   ├── <case_id>_items.csv             1 品項一列
│   ├── <case_id>_containers.csv        1 貨櫃一列（跟品項無綁定關係）
│   ├── <case_id>_tariff_candidates.csv 1 候選一列（同一品項可多列）
│   ├── <case_id>_checks.csv            R0–R16（單據層級）
│   └── <case_id>_item_checks.csv       I1–I3（品項層級，逐項）
└── import/
    └── <case_id>_erp_import_ready.csv  只有 case_status == READY 才產生
```

`staging/` 底下的檔案只要有結構化資料就會產生（即使 BLOCKED），因為
「目前手上有的資料」跟「可以正式匯入」是兩件事。`import/` 底下的檔案
**只有 READY 才產生**——見下方〈跟舊版設計不同的兩個地方〉。

## ERP Readiness 判斷邏輯

三種狀態，判斷邏輯集中在 `export_erp.py` 的 `determine_readiness()`，
輸入是四個確定性來源（都只是讀取既有結果，不重新做任何數值判斷）：

1. `checker.run_checks()`（R0–R16）與 `run_item_checks()`（I1–I3）的結果
2. `ERP_MANDATORY_FIELDS` 完整性
3. 每個 `invoice.items[]` 的 `confirmed_ccc_code`（逐品項稅則確認狀態）
4. `case_issues`（抽取/文件層級的疑義，見下方嚴重度表）

| 狀態 | 條件 |
|---|---|
| **BLOCKED** | 任一 FAIL（R 或 I 檢查皆算），或任一必要欄位缺失，或任一 `case_issues` 為 `BLOCKING` |
| **REVIEW_REQUIRED** | 上述皆無，但有未解決的 WARN（R 或 I 檢查皆算），或任一品項 `confirmed_ccc_code` 未填，或任一 `case_issues` 為 `REVIEW` |
| **READY** | 無 FAIL、必要欄位齊全、所有 WARN 已解決、**所有品項都已人工確認稅則**、沒有未解決的 REVIEW/BLOCKING `case_issues` |

R13（稅則號列）在 `checker.py` 裡設計上永遠回報 WARN——case 是否 READY
現在改成看「每個品項是否都已人工確認稅則」這個更細的逐品項判斷，R13
這則提醒的目的已被取代，不再單獨擋著 READY（`SUPERSEDED_WARN_RULES`）。
**其餘 WARN（R9/R10/R12/R16/I2）不做任何特殊化**，一律照實讓案件停在
REVIEW_REQUIRED——這是刻意的保守設計，不是 bug。I2（品項欄位完整性）
是 WARN 不是 FAIL：品名/欄位抽不到是抽取層的不確定，不是文件本身矛盾，
但一樣會擋著 READY，不能因為稅則都確認了就跳過抽取完整性這一關。

### `case_issues` 嚴重度

`run_case.py`／`extract.py` 遇到「有資料但不確定」時寫進案件 JSON 的
`case_issues` 陣列，`determine_readiness()` 只讀不重新判斷：

| severity | 例子 | 對 readiness 的影響 |
|---|---|---|
| `BLOCKING` | 案件目錄裡某個檔案（依檔名判斷像是 invoice/packing list）完全抽不到文字層 | 直接 `BLOCKED` |
| `REVIEW` | 兩個檔案都像同一種必要文件類型、B-L 檔案抽不到文字層（可能只是還沒拿到，不等於缺件）、賣方抬頭抽取結果不夠確定 | 阻擋 `READY`，至少 `REVIEW_REQUIRED` |
| `INFO` | 案件目錄裡有個檔案完全沒辨識出已知文件類型 | 不影響狀態，只是提示 |

## 跟舊版設計不同的兩個地方（刻意覆蓋，不是遺漏）

1. **`erp_import_ready.csv` 只有 `READY` 才產生。** 舊版（commit
   `3119426`）刻意設計成 `REVIEW_REQUIRED`（無 FAIL、只是稅則等欄位待
   確認）也會產生匯入檔，欄位留空讓 ERP 端接手補。這一版改成更保守的
   規則：只要案件還有任何待人工確認的事項（不管是稅則、抽取疑義、還是
   其他 WARN），就不產生號稱「可匯入」的檔案——`staging/` 底下的
   `items.csv` 等檔案本來就會照實產生，需要「目前手上有什麼」的人去
   那邊看，不需要一個介於兩者之間、容易被誤讀成「可以匯」的檔案。
2. **輸出目錄改成 `structured/review/staging/import` 四個子資料夾**，
   取代舊版全部檔案放在同一層的扁平結構，讓「這份檔案是給人看的還是
   給 ERP 用的、能不能正式匯入」一眼就看得出來。

## 已知限制（誠實列出，不是要藏起來的坑）

- **案件邊界由資料夾本身定義**：`run_case.py` 不做自動貨物分群判斷，
  資料夾裡的檔案就當作同一票；同一種必要文件類型被多個檔案匹配到時，
  取第一筆並記一條 `REVIEW` case_issue，不做智慧選擇。
- **R0–R16（`checker.run_checks()`）仍是單據層級／單品項導向**：R7、R8
  這類規則只看 `items[0]`/`items[-1]`，不是逐品項比對。新增的 I1–I3
  （`run_item_checks()`）才是真正逐品項的算術與完整性檢查，跟 R0–R16
  是兩套獨立計數，報表也分兩段呈現，不能把「R0–R16 全過」讀成「每個
  品項都查過」。
- **品項的 `source_hs_code`** 只在供應商標示的 HS 碼緊接在該品項描述
  區塊下方、或以表格最右欄「每列一個參考碼」呈現時才抓得到；版面模糊
  或多品項交錯排版（例如文件本身帶有浮水印造成的行位錯位）時寧可留空，
  不臆測歸屬到別的品項——這種情況下 I3（品項金額加總與發票總額相符）
  通常會 FAIL，這是**設計上刻意讓它 FAIL**，藉此把「抽取結果不完整」
  這件事變成看得到、擋得住的訊號，而不是靜默少算。
- **`tariff_candidates.csv` 的 `tariff_rate_reference` 只呈現第一欄
  （`rate1`）稅率**，`tariff_rate_basis` 固定標成 `"rate1"`，不宣稱這是
  應適用稅率——實際適用哪一欄（一般/WTO/協定稅率）視原產地、協定及其他
  條件而定，仍須人工判斷。`top_candidate_ccc_code` 是檢索排序第一名的
  結果，不是「系統推薦」，`confirmed_ccc_code` 才是人工確認過的值，
  兩者在 CSV 欄位跟 HTML 報告裡都不能混用。
- **`confirmed_ccc_code` 的逐品項確認機制**：`--confirm-ccc` 是舊版
  沿用下來的 invoice 層級 CLI，單品項發票時會自動鏡射寫入
  `items[0].confirmed_ccc_code`；多品項須用
  `--confirm-ccc-item <line_no> <11碼CCC>`（可重複多次）逐項確認。
  兩者都只是「把使用者明確給的字串寫進 JSON」，不查資料庫、不挑候選。
- **`ERP_MANDATORY_FIELDS`／`RULE_ACTION_HINTS` 都是 prototype
  demonstration 用的固定清單**，不代表任何真實 ERP 系統的必要欄位或
  操作流程。實際 production 的必要欄位應由客戶 ERP schema、報關流程及
  申報要求共同決定。
- **`review.html` 是純本機靜態檔案**，沒有伺服器、沒有外部資源依賴，
  需要用瀏覽器打開。

## 分層原則（不可違反，見 `CLAUDE.md`）

1. **抽取層**（`extract.py`／`run_case.py` 或模型視覺抽取）：把文件轉成
   JSON。允許不確定，抽不到的值一律 `null`，絕不猜測；猜不準的欄位
   （如賣方抬頭）改記一條 `case_issues`，不是硬塞一個值。
2. **驗證層**（`checker.py`／`validate.py`／`extract_crosscheck.py`）：
   判斷數值是否相符、是否合理、跟模型另外讀一次的結果對不對得起來。
   **「相不相等」的判斷只由 Python 執行，模型不得代勞**——模型可以讀
   出候選值（`extract_crosscheck.py` 那條路徑），但兩個值一不一樣永遠
   是 Python 決定。三支腳本的權責邊界見下方〈驗證層細分〉一節，不是
   同一件事的三份重複實作。
3. **稅則查表層**（`build_tariff_db.py`／`tariff_lookup.py`／
   `tariff_candidates.py`）：候選檢索是確定性查表，逐品項自動觸發；
   「哪個候選對」永遠只是候選，最終歸列責任在人，`confirmed_ccc_code`
   只能人工填入。
4. **ERP 前處理層**（`export_erp.py`）：**直接呼叫 `checker.py` 跟
   `tariff_candidates.py` 的既有函式，不重新實作任何驗證或檢索邏輯**；
   readiness 判斷是這一層唯一新增的業務規則，邏輯集中在單一函式
   `determine_readiness()`，可稽核。
5. **輸出層**（模型）：解釋異常、建議後續處理。

任何數值比較、加總、一致性判斷都必須來自 `checker.py`／`validate.py`
的輸出，不得由對話中的推理產生。

## 驗證層細分：`checker.py`／`validate.py`／`extract_crosscheck.py` 的權責邊界

「驗證層」實際上是三支互不 import、各自可獨立執行的腳本，抓的是三種
不同性質的問題：

- **`checker.py`**：跨文件／跨欄位的勾稽比對——Invoice／Packing List／
  運輸單據**互相之間**、或同一份發票**不同欄位之間**（R0–R16 單據
  層級、I1–I3 品項層級）數字合不合得起來。**系統中唯一有權判定「數值
  是否相符」的元件**，這件事只能由它做出結論，不得由 `validate.py`／
  `extract_crosscheck.py` 或模型重新判斷一次。
- **`validate.py`**：單一欄位自身的格式／範圍／自洽性——不看其他文件、
  也不看同一份文件裡的其他欄位，只問「這個值本身合不合理」（金額是不是
  Decimal 算得起來、幣別代碼在不在已知集合裡、日期解不解得開、CCC
  碼是不是 11 碼數字…）。**不做跨文件判斷**，那是 `checker.py` 的事；
  兩者刻意在 `run_case.py`（`_merge_validate_issues()`）接線時把
  `ARITHMETIC_TOTAL_MISMATCH` 跟 `checker.py`/`extract.py` 既有的
  `INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE` 去重，避免同一個判斷被
  講兩遍，但去重邏輯放在接線層，不代表兩支腳本的職責因此混在一起。
- **`extract_crosscheck.py`**：規則式抽取結果 vs 模型另外讀一次
  （`vision_freetext.json`）的**自由文字欄位**交叉比對——`vessel`／
  `voyage`／`doc_no`／`port_of_loading`／`port_of_discharge`／
  `parties.*`／`invoice.items[].description`，這些欄位沒有算術關係、
  也不在任何已知詞彙表裡，`validate.py` 管不到、`checker.py` 也沒有
  另一份文件可以拿來對。**模型只負責讀出候選值，「兩個值相不相等」
  一樣是純 Python 決定**（`compare_case()`），模型不得覆蓋既有欄位值，
  也不會自動判定哪個候選才是對的——供應商單據本身若印錯（例如船名
  真的打錯字），規則式抽出這個錯字是**正確**的抽取結果，申報本來就須
  與單據一致，比對到的只是「兩邊讀到的不一樣」，不是「其中一邊錯了」，
  最終要不要採用、採用哪一個，只能由人工核對原文後用 `resolve` 子指令
  裁決，見〈自由文字欄位交叉比對〉一節的完整流程。
- **`tests/fixtures/known_bugs/`**：歷史上真實發生過的「規則式有抓到
  值、但值抓錯」案例的重建輸出（見該目錄 `README.md`），是回歸驗收集，
  用來持續檢驗 `validate.py`／`extract_crosscheck.py`（未來也可能是
  其他新檢查層）到底攔不攔得住這些已知會發生的抓錯模式。**新發現的
  抓錯案例應該持續補進這個目錄**，不是只在發現當下口頭記一下就算了。

## 自由文字欄位交叉比對（`extract_crosscheck.py`）

跟既有「失敗才補救」的視覺抽取路徑（`SKILL.md` 步驟 2：整份 PDF 無
文字層、或品項加總對不上表尾總額才觸發，讀整份文件、補所有欄位）是
**兩件不同的事，並存不衝突**：

|  | 觸發時機 | 讀取範圍 | 目的 |
|---|---|---|---|
| 既有視覺抽取（`SKILL.md` 步驟 2）| 失敗才補救（無文字層／品項表疑似不完整）| 整份文件、補所有欄位 | 填補規則式完全抽不到/抽不全的空缺 |
| 自由文字交叉比對（本節）| 每票都跑 | 只讀自由文字欄位（見上） | 找出規則式「有抓到值、但可能抓錯」的自由文字欄位 |

流程（三個檔案都放在案件目錄，跟 `manual_supplements.json` 同一層）：

```
run_case.py <案件目錄> <case.json>
    │  （模型）讀原文的自由文字欄位，寫出 <案件目錄>/vision_freetext.json
    │  schema：{"fields": {"<欄位路徑>": {"value": ..., "context": "...原文片段..."}},
    │            "advisory_notes": [{"field": ..., "note": "疑似拼字錯誤等自由觀察"}]}
    ▼
extract_crosscheck.py compare <case.json> <案件目錄>/vision_freetext.json
    │  逐欄位比對（純 Python，只判斷「相不相等」），三種 issue 併入
    │  case_issues：EXTRACTION_CROSSCHECK_MISMATCH／VISION_ONLY_NEEDS_CONFIRM
    │  （REVIEW，會讓 readiness 停在 REVIEW_REQUIRED）、VISION_ADVISORY
    │  （INFO，不擋流程，這階段只寫進 JSON 不印終端——模型在真實單據上
    │  會講出什麼還無法預測，先收集一陣子）。完全一致的欄位安靜通過。
    │  同時寫出 <案件目錄>/crosscheck_review.csv（只有前兩種 REVIEW code
    │  才進清單，人工填 resolution 欄）
    ▼
（人工核對原文，填 crosscheck_review.csv 的 resolution 欄；⚠️ 填寫期間
 跟裁決完成前都不要重跑 run_case.py，會整個重建案件 JSON 蓋掉裁決）
    ▼
extract_crosscheck.py resolve <case.json> <填好的清單.csv>
    │  resolution 有值的每一列：寫回 case.json 對應欄位，
    │  method 標為 rule_confirmed／vision_confirmed／manual_visual_supplement
    │  （人工核對原文後鍵入第三個值，兩邊都不對時用這個，沿用既有慣例，
    │  不另造新詞彙），同時在 doc["freetext_resolutions"] 留一筆裁決紀錄；
    │  resolution 留空的列跳過，維持 REVIEW_REQUIRED
    ▼
export_erp.py ...
    已裁決的欄位不再擋 readiness（逐欄位判斷，不是整份文件一次豁免：
    三個欄位不一致、只裁決兩個，剩下那個繼續擋）
```

### 已知落差（誠實列出，不是要藏起來的坑）

1. **這一層的天花板：兩邊一起錯，會安靜通過。** `extract_crosscheck.py`
   只能抓「規則式跟模型讀出的值不一樣」——如果供應商單據本身印錯、
   或版面詭異到兩條完全獨立的路徑（正則跟模型）剛好誤讀成同一個結果，
   兩邊會顯示一致，不會產生任何 issue。**crosscheck 全綠不等於這個
   欄位沒問題**，只代表「兩種獨立方法讀出同一個值」，不是「這個值
   經過驗證是對的」——自由文字欄位終究沒有可驗證的格式或算術關係，
   這是語意層面的限制，不是實作疏漏。
2. **`validate.py`／`extract_crosscheck.py` 目前只接在 `run_case.py`
   （多檔案案件）路徑。** `extract.py` 的單一合併 PDF CLI 路徑
   **不會**呼叫 `validate_case()`，也沒有對應的 crosscheck 流程，
   所以同一批文件，存成多個獨立檔案用 `run_case.py` 跑，會比存成一份
   合併 PDF 用 `extract.py` 跑多出這兩層的 `case_issues`——
   兩條路徑目前不是等價的，這不是自動同步的，是待補的落差。
