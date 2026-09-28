# 系統現況盤點

> 最後同步：2026-09-15。內容以 repo 目前的程式碼為準；架構有變動時需要同步更新本文件。
> 產生日期：2026-09-15
> **架構變動時需同步更新這份文件**——新增/移除腳本、改變驗證分層、
> 改變輸出目錄結構、或修正本文件記錄過的已知問題時，都要回來改這裡。
> 內容以實際程式碼為準，不確定的地方會明講「不確定」並說明原因，不編造。

---

## 第一部分：一句話與一段話

**一句話**：把報關三單（Invoice / Packing List / B-L）自動轉成結構化資料、用確定性規則逐項核對，產出異常清單跟供人工審核用的 ERP 前處理草稿。

**一段話**：輸入是使用者手上一票貨的幾份 PDF（商業發票、裝箱單、提單或訂艙確認書，可以合併成一份 PDF 也可以是分開的檔案）；輸出是一份結構化 JSON、一份人看的核對報告（文字版跟 HTML）、六到七份給 ERP 匯入用的 CSV／一份合併活頁簿，以及（只有全部核對通過且稅則都確認過才會有的）一份可匯入檔。它解決的問題是：報關前必須人工逐字比對三份文件的號碼、日期、件數、重量、金額是否一致，這是典型的「欄位分散在多頁、數字相近、注意力衰減」場景，人工比對容易漏、容易錯，而且錯了往往在裝船後或到港後才發現，代價是滯留費、退運甚至罰鍰。沒有這個系統，報關人員得自己攤開三份 PDF 逐項核對數字、自己查稅則書找號列、自己判斷申報方向該用哪套規則，全靠記憶跟經驗，容易因為單一個人疏忽而出包，而且無法規模化——每一票貨都要重新來一次人工比對。

---

## 第二部分：資料流（文字流程圖）

```
┌─────────────────────────────────────────────────────────────────┐
│ 使用者手上：一票貨的 PDF（合併成一份，或分開成幾個獨立檔案）      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 A：抽取層                                        │
   │ 誰執行：extract.py（單一合併 PDF）                     │
   │        或 run_case.py（多個獨立檔案，內部呼叫 extract.py │
   │        的共用核心 build_case_from_pages()）             │
   │ 吃：PDF（用 pdftotext -layout 轉文字，純規則式 regex 解析）│
   │ 吐：<case>.json（invoice / packing_list /               │
   │      transport_document / parties / case_issues /       │
   │      source_trace）                                      │
   │                                                          │
   │ 內部還會自動做兩件事：                                   │
   │  · validate.py 的檢查（Decimal 算術、格式、詞彙表）       │
   │    ──併入 case_issues（只有 run_case.py 路徑，見第七部分）│
   │  · manual_supplements.json（若存在）自動合併人工補件品項  │
   │                                                          │
   │ 可能失敗的方式：                                         │
   │  · PDF 完全無文字層（掃描件）→ extract.py 直接報錯；      │
   │    run_case.py 則記一條 NO_TEXT_LAYER case_issue，        │
   │    其餘檔案照常處理，不中止整案                           │
   │  · 品項加總對不上發票表尾總額 → 記 BLOCKING 的             │
   │    INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE，懷疑漏抓列      │
   │  · 版面完全不受支援（如異國數字格式）→ 核心欄位缺過半，    │
   │    後面 R0 會判 FAIL，不會偷偷回報 PASS                   │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼（失敗才觸發，不是每票都做）
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 B：既有視覺抽取補救（人＝模型執行）                  │
   │ 誰執行：模型（在 SKILL.md 指示下手動讀 PDF）                │
   │ 吃：原始 PDF 內容（人眼/模型讀）                          │
   │ 吐：直接改寫 <case>.json 對應欄位，extraction_method       │
   │     標成 "vision"；或整理成 manual_supplements.json        │
   │     供下次重跑 run_case.py 時自動合併（不直接手改產出的JSON）│
   │ 可能失敗的方式：模型讀錯、漏讀——這條路徑完全沒有自動        │
   │     防呆，靠人工抽樣核對                                   │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼（每票都跑，不是失敗才觸發）
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 C：自由文字欄位交叉比對                             │
   │ 誰執行：模型（讀出候選值）＋ extract_crosscheck.py（純Python比對）│
   │ 吃：<案件目錄>/vision_freetext.json（模型產出，只含船名/    │
   │     港口/parties/品項描述等自由文字欄位）                  │
   │ 吐：把 <case>.json 就地更新（併入 case_issues）＋           │
   │     <案件目錄>/crosscheck_review.csv（待人工裁決清單）      │
   │ 可能失敗的方式：                                          │
   │  · 兩邊不一致 → EXTRACTION_CROSSCHECK_MISMATCH（REVIEW）   │
   │  · 只有模型讀到 → VISION_ONLY_NEEDS_CONFIRM（REVIEW）      │
   │  · 兩邊剛好都錯成同一個值 → 安靜通過，這是本層的天花板       │
   │     （見第七部分）                                         │
   │ 人工核對原文、填 crosscheck_review.csv 的 resolution 欄後：  │
   │     extract_crosscheck.py resolve 讀回、寫入 <case>.json、  │
   │     留一筆 freetext_resolutions 裁決紀錄                    │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 D：驗證層                                          │
   │ 誰執行：checker.py（純 Python，確定性運算）               │
   │ 吃：<case>.json（+ --direction import|export）             │
   │ 吐：終端報告（R0–R16）＋ 可選 --csv 輸出                   │
   │ 可能失敗的方式：任何 FAIL 都會被印出來，腳本本身若崩潰        │
   │     （例如 None 值格式化，已修過的舊 bug）會回傳執行失敗，   │
   │     鐵律規定這種情況要照實回報，不能用推理代替               │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 E：ERP 前處理層                                     │
   │ 誰執行：export_erp.py（直接呼叫 checker.py／                │
   │         tariff_candidates.py 的既有函式，不重新驗證）        │
   │ 吃：<case>.json                                           │
   │ 吐：<案件目錄>/<發票號>/ 底下四個子資料夾：                 │
   │     structured/（JSON 鏡射）                                │
   │     review/（report.txt + review.html，人看）               │
   │     staging/（7 個 ERP-neutral CSV + 1 個合併 xlsx）         │
   │     import/（只有 READY 才產生 erp_import_ready.csv）        │
   │     還有一份 case_status.json 在頂層                        │
   │ 這一步同時自動對每個品項跑稅則候選檢索（tariff_candidates.py）│
   │ 可能失敗的方式：readiness 卡在 BLOCKED／REVIEW_REQUIRED——   │
   │     不是腳本失敗，是業務判斷結果，見第六部分完整規則          │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼（REVIEW_REQUIRED 才需要，READY 直接結束）
   ┌─────────────────────────────────────────────────────┐
   │ 步驟 F：人工稅則歸列確認                                  │
   │ 誰執行：人（看 tariff_candidates.csv／review.html 的候選，  │
   │         模型協助解讀候選但不得替人選定答案）                 │
   │ 吃：review.html 或 staging/*_items.csv 的                  │
   │     top_candidate_ccc_code                                 │
   │ 吐：export_erp.py --confirm-ccc-item <序號> <11碼CCC>       │
   │     寫回 <case>.json 的 items[].confirmed_ccc_code          │
   │ 確認後要重跑步驟 E，case_status 才會往 READY 前進            │
   └─────────────────────────────────────────────────────┘
                              │
                              ▼
              READY → import/<發票號>_erp_import_ready.csv
              （唯一「可匯入」的產物，交給人工審核後接手匯入客戶 ERP）
```

**一個貫穿全程的鐵律**：任何「兩個值是否相等」的判斷，永遠是 Python 程式碼做的（`checker.py`／`validate.py`／`extract_crosscheck.py`），模型只負責讀出候選值、解釋結果，不得自行心算或口頭裁定一致性。

---

## 第三部分：每支腳本逐一說明

依 import 關係由底層到上層排列。

### `extract.py`（990 行）
- **職責**：PDF（文字層）→ 結構化 JSON，純規則式 regex 解析，零成本、確定性。
- **對外介面**：
  - CLI：`python3 extract.py <pdf路徑> [輸出json路徑]`
  - 可 import 的核心：`build_case_from_pages(tagged_pages: list) -> dict`（真正的解析核心，輸入是「一批已標好來源檔名/頁碼的頁面文字」）、`parse_invoice(page) -> (dict, issues)`、`parse_packing_list(body) -> dict`、`parse_transport_doc(body) -> dict`、`parse_parties(...) -> (dict, issues)`、`detect_direction(text) -> str|None`、`finalize_invoice_item_checks(doc, supplement_path)`（套用 manual_supplements.json + 品項加總守門）、`UNITS`／`CURRENCIES`（validate.py 直接重用的詞彙表常數）。
- **不負責什麼**：不判斷任何數值是否相符（那是 checker.py）、不猜測抽不到的欄位（一律 null + case_issue）、不處理掃描件（無文字層直接報錯，要求改走視覺路徑）、不處理多檔案案件目錄（那是 run_case.py 的事，雖然共用同一套核心）。
- **依賴**：無內部依賴，只用標準函式庫（`pdftotext` 是外部命令，非 Python 套件）。

### `run_case.py`（161 行）
- **職責**：案件目錄（多個獨立檔案）→ 一個案件的結構化 JSON；`extract.py` 是這個核心「只有一個檔案」的特例，這支是一般化。
- **對外介面**：
  - CLI：`python3 run_case.py <案件目錄> [輸出json路徑]`
  - 可 import：`build_case(case_dir: str) -> dict`
- **不負責什麼**：不做自動貨物分群判斷（資料夾裡的檔案就是同一票）；不做欄位級的數值驗證（併入 `validate.py` 的結果，但本身不判斷）；不接自由文字交叉比對（`extract_crosscheck.py` 是獨立後續步驟，不在這支腳本執行範圍內）。
- **依賴**：`extract.py`、`validate.py`（`_merge_validate_issues()` 在 `build_case()` 收尾時呼叫）。

### `checker.py`（495 行）
- **職責**：系統中**唯一**有權判定「數值是否相符」的元件——跨文件（R0–R16）與逐品項（I1–I3）確定性比對。
- **對外介面**：
  - CLI：`python3 checker.py <json路徑> [--direction import|export] [--csv 輸出路徑]`
  - 可 import：`run_checks(doc, direction) -> list`、`run_item_checks(doc) -> list`、`report(doc, results, direction) -> str`、`write_checks_csv(path, results)`
- **不負責什麼**：不做單一欄位的格式/範圍檢查（那是 validate.py）、不比對模型讀出的候選值（那是 extract_crosscheck.py）、不決定 ERP readiness（那是 export_erp.py，只讀它的結果）、不做稅則歸列判斷（R13/R16 都只查表或提醒，不判斷哪個號列對）。
- **依賴**：無內部依賴（跟 extract.py 互不 import，各自可獨立執行）。

### `validate.py`（466 行）
- **職責**：單一欄位自身的格式/範圍/自洽性——Decimal 精確算術、CCC 格式、幣別/單位詞彙表、日期區間。
- **對外介面**：
  - CLI：`python3 validate.py <json路徑或目錄>... [--json 輸出路徑]`
  - 可 import：`validate_case(doc: dict) -> list[dict]`（主入口，內部呼叫 8 個獨立 `check_*` 函式）
- **不負責什麼**：不做跨文件判斷（那是 checker.py）、不比對模型讀出的值（那是 extract_crosscheck.py）、不修改傳入的 doc（純函式）。
- **依賴**：`extract.py`（只重用 `UNITS`／`CURRENCIES` 常數，不 import 其他邏輯）。

### `extract_crosscheck.py`（367 行）
- **職責**：規則式抽取 vs 模型視覺閱讀，自由文字欄位（vessel/voyage/doc_no/港口/parties/品項描述）交叉比對。
- **對外介面**：
  - CLI：`compare` 子指令、`resolve` 子指令（見第二部分）
  - 可 import：`compare_case(doc, vision_doc) -> list`、`merge_into_case_issues(doc, issues)`、`resolve_rows(doc, rows) -> list`、`get_field`/`set_field`（僅限交叉比對範圍內的欄位）
- **不負責什麼**：不判斷哪個值對（只判斷相不相等）、不碰金額/重量/件數欄位（那些已有 validate.py／checker.py R5/R15）、不自動改值成「看起來對」的拼法。
- **依賴**：無內部依賴（獨立於 extract.py／checker.py／validate.py），目前**沒有被任何其他腳本 import**——只能靠人（或模型）在 SKILL.md 流程裡手動呼叫。

### `tariff_lookup.py`（320 行）
- **職責**：品名關鍵字 → 候選稅則號列（查詢工具，不是歸列引擎）。
- **對外介面**：
  - CLI：`python3 tariff_lookup.py "<品名>" [--top N] [--db 路徑] [--chapter 章號]`
  - 可 import：`search(con, query, top_n, chapter) -> (rows, full_match)`、`resolve_reg_codes()`、`breadcrumb()`、`format_reg_line()`、`split_terms()`、`_is_meaningful()`（過濾通用詞）
- **不負責什麼**：不判斷哪個候選是正確答案（那永遠是人的事）、不驗證資料本身（純查表）。
- **依賴**：無內部依賴，直接讀 `tariff.db`（sqlite3）。

### `tariff_candidates.py`（272 行）
- **職責**：逐品項自動跑檢索，把 `tariff_lookup.py` 的結果轉成固定 schema 的候選列並標信賴等級（三種檢索策略：來源 HS6 直查 → 品名關鍵字 → 查無候選）。
- **對外介面**：
  - CLI：`python3 tariff_candidates.py "<品名>" [--top N] [--db 路徑] [--hs6 6碼HS前綴]`（人工測試用）
  - 可 import：`build_candidates_for_item(con, case_id, item, top_n) -> list`
- **不負責什麼**：不重寫比對邏輯（直接 import `tariff_lookup.py`）、`requires_human_confirmation` 恆為 `True`，沒有任何一列會被自動視為已歸類。
- **依賴**：`tariff_lookup.py`。

### `build_tariff_db.py`（867 行）
- **職責**：把財政部關務署公告的《中華民國海關進口稅則輸出入貨品分類表合訂本》（上冊＋下冊）PDF 解析成 `tariff.db`（SQLite）。用 `pdftotext -bbox-layout` 的逐字座標分欄，不靠空白猜欄位。
- **對外介面**：
  - CLI：`python3 build_tariff_db.py [上冊PDF] [下冊PDF] [--db 路徑] [--chunk-size N] [--verify-only]`
- **不負責什麼**：不做任何品名比對或分類判斷（純確定性轉錄）；建完會自動跑 `verify()` 驗證，有 FAIL 就不覆蓋既有 DB（暫存在別的檔名供檢查）。
- **依賴**：無內部依賴，是整條稅則鏈路最底層的建置工具，一次性/低頻執行（不是每票都跑）。

### `export_erp.py`（1300+ 行，全專案最大的腳本）
- **職責**：核對結果 → ERP 前處理產物。判斷 ERP readiness（READY／REVIEW_REQUIRED／BLOCKED）、整理 staging CSV、產生人看的報告。
- **對外介面**：
  - CLI：`python3 export_erp.py <structured_json> [--outdir 路徑] [--direction import|export] [--confirm-ccc 11碼CCC] [--confirm-ccc-item 品項序號 11碼CCC]... [--force-confirm]`
  - 可 import：`determine_readiness(doc, checks, item_checks) -> dict`（核心業務判斷）、`apply_confirm_ccc()`／`apply_confirm_ccc_item()`、`build_shipment_header_row()`／`build_item_rows_and_candidates()`／`build_container_rows()`／`build_exception_rows()`、`build_review_report()`／`build_review_html()`、`build_staging_workbook_sheets()`
- **不負責什麼**：**不重新實作任何驗證或稅則檢索邏輯**——直接呼叫 `checker.py`／`tariff_candidates.py` 的既有函式；readiness 判斷是這一層唯一新增的業務規則。不接 `extract_crosscheck.py`（沒有 import 它），只被動讀取它寫進 `case_issues` 的結果。
- **依賴**：`checker.py`、`tariff_candidates.py`、`xlsx_writer.py`。

### `xlsx_writer.py`（112 行）
- **職責**：純標準函式庫（zipfile + xml）寫出最小可用的 XLSX，不依賴 openpyxl。
- **對外介面**：`write_xlsx(path, sheets: list[(name, fieldnames, rows)])`
- **不負責什麼**：不做樣式、公式、凍結窗格——只做「多分頁、每格文字或數字」這個最小可用集合；**只能寫，不能讀**（沒有對應的 XLSX 讀取器）。
- **依賴**：無內部依賴。

---

## 第四部分：所有 `.md` 文件的角色

| 文件 | 寫給誰看 | 內容涵蓋 | 什麼情況下更新 |
|---|---|---|---|
| **`CLAUDE.md`** | 主要是模型（Claude Code 啟動時自動載入的專案層級指示），但人也看得懂 | 專案定位、環境需求、目錄結構總覽、**分層原則的權威版本**（不可違反）、真實客戶資料的去識別化規則 | 專案定位改變、目錄結構調整、分層原則本身要修改時 |
| **`SKILL.md`**（`.claude/skills/customs-check/`） | 模型（這是 skill 的操作手冊，模型執行 `/customs-check` 或被觸發這個 skill 時讀的） | **逐步驟的操作流程**（現在是 7 個步驟）、每支腳本的實際呼叫指令、遇到各種情況（缺 B/L、掃描件、品項加總對不上）時模型該怎麼做、呈現方式、注意事項 | 流程本身變動（新增/調整步驟）、腳本 CLI 介面改變、新的 case_issue 呈現規則 |
| **`README.md`** | 人（專案文件的標準位置，GitHub 上第一眼看到的），但也是模型查閱架構細節的地方 | 完整 workflow 圖、輸出目錄結構、readiness 判斷邏輯細節、**驗證層細分**（三支腳本權責邊界）、已知限制清單、已知落差 | 架構性變動（新增一層、改變輸出結構）、發現需要誠實記錄的限制時 |
| **`docs/OVERVIEW.md`**（本文件） | 專案擁有者（用來回答別人對這個專案的提問） | 整個系統的完整盤點——比 README 更全面（涵蓋每支腳本細節、所有輸出檔案、驗證層全貌、常見提問） | 架構變動時（見文件開頭的提醒） |
| **`tests/EXPECTED.md`** | 人（維護測試的人） | 每組 fixture（A_clean/B_faulty/C_altlayout/D_export/A_case/E_multiitem/GD-2609-003/GD-2609-004）的設計目的、預期的 PASS/WARN/FAIL/SKIP 計數、由這組測試找出過的真實缺陷 | 新增或修改 fixture、fixture 的預期結果改變時 |
| **`tests/fixtures/known_bugs/README.md`** | 人（評估驗證層盲區的人） | 三個歷史真實 bug 的重建方法、為什麼用這個重建方式、當初的錯誤程式碼位置 | 新增更多已知抓錯案例到 `known_bugs/` 時 |
| **`.claude/skills/customs-check/references/customs-rules.md`** | 模型（SKILL.md 步驟裡引用它來解釋規則背後的業務理由），輔助人理解 | `checker.py` 每條規則（R1–R16）的業務依據、出錯的實務後果、進出口方向差異對照表 | 領域知識本身有誤或需要補充時；**判斷邏輯本身不在這裡，只有「為什麼」** |

**SKILL.md 跟 CLAUDE.md 的分工差異（最容易被問混的地方）**：`CLAUDE.md` 是**專案層級**的固定指示，回答「這個專案是什麼、有哪些不可違反的原則」，範圍涵蓋整個 repo，很少變動；`SKILL.md` 是**這個特定 skill 的操作手冊**，回答「處理一票貨的時候，一步一步該做什麼」，只在使用者觸發 `customs-check` 這個 skill 時才會被載入，變動頻率隨流程調整而定。打個比方：`CLAUDE.md` 像公司規章（「不得偽造數字」），`SKILL.md` 像這個崗位的 SOP（「先做這個、再做那個、遇到 X 情況要這樣處理」）。兩者都給模型看，但 `CLAUDE.md` 一直生效，`SKILL.md` 只在這個 skill 被呼叫的當下生效。

---

## 第五部分：產出的所有檔案

### 中間產物（人工/模型互動用）

| 路徑 | 格式 | 由誰產生 | 給誰用 | 進版控？ | 刪掉能不能重建 |
|---|---|---|---|---|---|
| `<案件目錄>/manual_supplements.json` | JSON | 人（核對原文後手寫） | `run_case.py`/`extract.py` 下次重跑時自動合併 | 視情況（fixtures 底下的會進） | **不能重建**——是人工核對原文得到的值，沒有原始資料可回推 |
| `<案件目錄>/vision_freetext.json` | JSON | 模型（讀原文自由文字欄位） | `extract_crosscheck.py compare` | 不確定（尚未有明確慣例；且目前**沒有任何真實案例產生過這個檔案**，見第七部分）| 可以重新讓模型讀一次重建，但重建結果不保證跟上次一樣（模型輸出非完全確定性）|
| `<案件目錄>/crosscheck_review.csv` | CSV | `extract_crosscheck.py compare` | 人工填 resolution 欄 | 同上 | 未裁決前可以重新 compare 覆蓋重建；**已填的 resolution 欄若還沒跑 resolve 就被覆蓋，會遺失** |

### 案件 JSON（核心產物）

| 路徑 | 格式 | 由誰產生 | 給誰用 | 進版控？ | 刪掉能不能重建 |
|---|---|---|---|---|---|
| `<案件目錄>/<發票號>.json` | JSON | `extract.py`/`run_case.py` | `checker.py`/`export_erp.py`/`extract_crosscheck.py` 都讀這份 | fixtures 底下的案例會進（`GD-2609-003.json` 等已被 `.gitignore` 排除，只有輸入 PDF 進） | **視情況**：如果只是規則式抽取結果，重跑就重建；但一旦裡面有 `confirmed_ccc_code`、`freetext_resolutions`、`manual_supplements` 合併結果，重跑 `run_case.py` **會整個蓋掉**（見第七部分），這些人工痕跡刪了就真的回不來 |

### `export_erp.py` 的產出（`<案件目錄>/<發票號>/` 底下）

| 子資料夾/檔案 | 格式 | 給誰用 | 進版控？ | 刪掉能不能重建 |
|---|---|---|---|---|
| `structured/<發票號>.json` | JSON | 案件資料鏡射（含已確認的 CCC） | 否（`.gitignore` 排除，可重新產生） | 能，只要主案件 JSON（含已確認欄位）還在，重跑 `export_erp.py` 就重建 |
| `review/<發票號>_report.txt` | 純文字 | 人看的審核報告 | 否 | 能 |
| `review/<發票號>_review.html` | 自包含 HTML | 給業務/客戶看的完整報告，無外部資源依賴 | 否 | 能 |
| `staging/<發票號>_shipment_header.csv` | CSV | ERP 匯入的表頭欄位 | 否 | 能 |
| `staging/<發票號>_items.csv` | CSV | 逐品項欄位（含 `top_candidate_ccc_code`） | 否 | 能 |
| `staging/<發票號>_containers.csv` | CSV | 逐貨櫃欄位 | 否 | 能 |
| `staging/<發票號>_tariff_candidates.csv` | CSV | 逐品項稅則候選（可能多列） | 否 | 能 |
| `staging/<發票號>_checks.csv` | CSV | `checker.py` R0–R16 結果 | 否 | 能 |
| `staging/<發票號>_item_checks.csv` | CSV | `checker.py` I1–I3 結果 | 否 | 能 |
| `staging/<發票號>_exceptions.csv` | CSV | 所有待辦事項彙整（FAIL/WARN/case_issues/稅則未確認，INFO 級 issue 已排除，見第七部分） | 否 | 能 |
| `staging/<發票號>_staging.xlsx` | XLSX | 上面 CSV 的合併活頁簿，方便交接 | 否 | 能，純鏡射既有 CSV，不是新資料來源 |
| `import/<發票號>_erp_import_ready.csv` | CSV | **只有 `case_status == READY` 才產生**，唯一號稱「可匯入」的檔案 | 否 | 能（前提是案件狀態仍是 READY） |
| `<發票號>/case_status.json`（頂層，不在四個子資料夾內）| JSON | 這一票的完整狀態快照（readiness、blocking_reasons、review_items、所有輸出檔路徑） | 否 | 能 |

**不進版控的理由**：這些全部是「重跑腳本就能重新產生」的執行產出，跟 `data/output/` 的既有排除慣例一致；真正無法重建、需要保護的是**上游的人工輸入**（`manual_supplements.json`、`confirmed_ccc_code`、`freetext_resolutions` 這些寫在案件 JSON 裡的人工裁決紀錄）。

**輸出目錄結構跟舊版設計不同的兩個地方**（刻意覆蓋，不是遺漏）：

1. **`erp_import_ready.csv` 只有 `READY` 才產生。** 舊版刻意設計成
   `REVIEW_REQUIRED`（無 FAIL、只是稅則等欄位待確認）也會產生匯入檔，
   欄位留空讓 ERP 端接手補。這一版改成更保守的規則：只要案件還有任何
   待人工確認的事項（不管是稅則、抽取疑義、還是其他 WARN），就不產生
   號稱「可匯入」的檔案——`staging/` 底下的 `items.csv` 等檔案本來就會
   照實產生，需要「目前手上有什麼」的人去那邊看，不需要一個介於兩者
   之間、容易被誤讀成「可以匯」的檔案。
2. **輸出目錄改成 `structured/review/staging/import` 四個子資料夾**，
   取代舊版全部檔案放在同一層的扁平結構，讓「這份檔案是給人看的還是
   給 ERP 用的、能不能正式匯入」一眼就看得出來。

### 測試相關產出

| 路徑 | 說明 |
|---|---|
| `data/output/_test_run/` | `tests/run_tests.py` 執行時的暫存輸出，每次執行前整個清空重建，不進版控、不應手動維護 |
| `tests/fixtures/known_bugs/*.json` | 三個歷史 bug 重建輸出，**進版控**（這是刻意保留的回歸驗收資料，不是執行過程的暫存物） |

### 稅則資料庫

| 路徑 | 說明 |
|---|---|
| `.claude/skills/customs-check/data/tariff.db` | `build_tariff_db.py` 的輸出，10MB，`.gitignore` 排除（衍生資料，需要時本機重建），是唯一「刪掉能完全重建，但重建要花時間、且需要原始稅則 PDF」的資料 |

---

## 第六部分：驗證層全貌

### `checker.py`：R0–R16（單據層級，跨文件比對）

| 規則 | 一句話 |
|---|---|
| R0 | 核心欄位是否齊備（守門規則，缺過半就讓後面所有 PASS 降級成 SKIP） |
| R1 | Invoice 跟 Packing List 的文件編號是否一致 |
| R2 | Invoice 跟 Packing List 的文件日期是否一致 |
| R3 | Packing List 逐櫃列的數量是否等於申報櫃數 |
| R4 | 逐櫃件數加總是否等於表尾件數 |
| R5 | 逐櫃淨重/毛重加總是否等於表尾淨重/毛重 |
| R6 | 每個貨櫃的淨重是否小於毛重 |
| R7 | Packing List 數量加總是否等於 Invoice 最後一品項數量 |
| R8 | Invoice 第一品項的數量×單價是否等於金額 |
| R9 | 申報價格基礎（進口 CIF／出口 FOB）是否需要調整 |
| R10 | 貨價幣別跟運費幣別是否一致 |
| R11 | 貨櫃號格式是否符合 ISO 6346 |
| R12 | 原產地／輸出入管制規定提醒（依方向不同） |
| R13 | 稅則號列提醒（供應商標示 HS 僅為候選值，設計上永遠 WARN） |
| R14 | Packing List 貨櫃號是否跟 B/L（或 SO）一致 |
| R15 | Packing List 件數/毛重是否跟 B/L（或 SO）一致 |
| R16 | 已人工確認的 CCC 號列，純查表複查它的輸出入規定代號 |

### `checker.py`：I1–I3（品項層級，逐品項）

| 規則 | 一句話 |
|---|---|
| I1 | 每一品項各自的數量×單價是否等於金額 |
| I2 | 每一品項的必要欄位（品名/數量/單位/單價/金額）是否齊備 |
| I3 | 全部品項金額加總是否等於發票表尾總額 |

### `validate.py`：12 個 code

| code | severity | 一句話 |
|---|---|---|
| `NO_ITEMS` | BLOCKING | 發票品項清單為空 |
| `ARITHMETIC_LINE_MISMATCH` | BLOCKING | 品項金額 ≠ 數量×單價（Decimal 精確計算） |
| `ARITHMETIC_TOTAL_MISMATCH` | BLOCKING | 品項加總 ≠ 發票總額（Decimal 精確計算） |
| `NEGATIVE_VALUE` | BLOCKING | 任一金額/數量/重量欄位為負 |
| `INVALID_CCC_CODE_FORMAT` | BLOCKING | `confirmed_ccc_code` 非 11 碼數字 |
| `TARIFF_CODE_NOT_FOUND` | REVIEW | CCC 格式合法但查無此號列 |
| `SOURCE_HS_CODE_FORMAT_UNUSUAL` | REVIEW | 供應商標示 HS 位數不常見 |
| `CURRENCY_UNKNOWN_CODE` | REVIEW | 幣別不在已知集合 |
| `CURRENCY_INCONSISTENT` | REVIEW | 貨價/運費/保險費幣別不一致 |
| `DATE_UNPARSEABLE` | REVIEW | 日期無法解析 |
| `DATE_OUT_OF_RANGE` | REVIEW | 日期超出合理區間 |
| `UNIT_UNKNOWN` | REVIEW | 品項/貨櫃單位不在已知集合 |

### `extract_crosscheck.py`：3 個 code

| code | severity | 一句話 |
|---|---|---|
| `EXTRACTION_CROSSCHECK_MISMATCH` | REVIEW | 規則式抽取值跟模型讀出的值不一樣（自由文字欄位） |
| `VISION_ONLY_NEEDS_CONFIRM` | REVIEW | 規則式沒抓到、只有模型讀到（孤值） |
| `VISION_ADVISORY` | INFO | 模型的自由觀察（疑似拼字錯誤等），不擋流程 |

**`extract_crosscheck.py` 完整流程**——跟既有「失敗才補救」的視覺抽取
路徑（`SKILL.md` 步驟 2：整份 PDF 無文字層、或品項加總對不上表尾總額
才觸發，讀整份文件、補所有欄位）是**兩件不同的事，並存不衝突**：

|  | 觸發時機 | 讀取範圍 | 目的 |
|---|---|---|---|
| 既有視覺抽取（`SKILL.md` 步驟 2）| 失敗才補救（無文字層／品項表疑似不完整）| 整份文件、補所有欄位 | 填補規則式完全抽不到/抽不全的空缺 |
| 自由文字交叉比對（本節）| 每票都跑 | 只讀自由文字欄位 | 找出規則式「有抓到值、但可能抓錯」的自由文字欄位 |

流程（三個檔案都放在案件目錄，跟 `manual_supplements.json` 同一層）：

```
run_case.py <案件目錄> <case.json>
    │  （模型）讀原文的自由文字欄位，寫出 <案件目錄>/vision_freetext.json
    │  schema：{"fields": {"<欄位路徑>": {"value": ..., "context": "...原文片段..."}},
    │            "advisory_notes": [{"field": ..., "note": "疑似拼字錯誤等自由觀察"}]}
    ▼
extract_crosscheck.py compare <case.json> <案件目錄>/vision_freetext.json
    │  逐欄位比對（純 Python，只判斷「相不相等」），三種 issue 併入
    │  case_issues，完全一致的欄位安靜通過。同時寫出
    │  <案件目錄>/crosscheck_review.csv（只有兩種 REVIEW code 才進清單，
    │  人工填 resolution 欄）
    ▼
（人工核對原文，填 crosscheck_review.csv 的 resolution 欄；⚠️ 填寫期間
 跟裁決完成前都不要重跑 run_case.py，會整個重建案件 JSON 蓋掉裁決）
    ▼
extract_crosscheck.py resolve <case.json> <填好的清單.csv>
    │  resolution 有值的每一列：寫回 case.json 對應欄位，method 標為
    │  rule_confirmed／vision_confirmed／manual_visual_supplement（人工
    │  核對原文後鍵入第三個值時用這個，沿用既有慣例，不另造新詞彙），
    │  同時在 doc["freetext_resolutions"] 留一筆裁決紀錄；resolution
    │  留空的列跳過，維持 REVIEW_REQUIRED
    ▼
export_erp.py ...
    已裁決的欄位不再擋 readiness（逐欄位判斷，不是整份文件一次豁免：
    三個欄位不一致、只裁決兩個，剩下那個繼續擋）
```

### `extract.py`/`run_case.py` 自己的守門檢查：9 個 code

| code | severity | 一句話 |
|---|---|---|
| `NO_TEXT_LAYER` | BLOCKING 或 REVIEW（依檔名猜測文件類型） | 案件目錄裡某個檔案完全抽不到文字層 |
| `INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE` | BLOCKING | 品項加總跟發票表尾總額對不上，懷疑漏抓列 |
| `PACKING_LIST_EXTRACTION_INCOMPLETE` | BLOCKING | Packing List 核心欄位缺過半 |
| `TRANSPORT_DOC_EXTRACTION_INCOMPLETE` | BLOCKING | 運輸單據核心欄位缺過半 |
| `MANUAL_VISUAL_SUPPLEMENT_APPLIED` | REVIEW | 品項清單曾經人工視覺補件過 |
| `PARTY_EXTRACTION_AMBIGUOUS` | REVIEW | 賣方/買方欄位版面啟發式抓不準 |
| `AMBIGUOUS_DOCTYPE_SELECTION` | REVIEW | 多個檔案都符合同一種必要文件類型 |
| `INVOICE_CURRENCY_EXTRACTION_AMBIGUOUS` | REVIEW | Currency 欄位標籤有，但值抽取不到合法幣別代碼 |
| `UNRECOGNIZED_FILE` | INFO | 案件目錄裡有檔案沒辨識出已知文件類型 |

### 三層之間的權責邊界，為什麼這樣切

- **`checker.py`**：問「這份文件內部/跟另一份文件之間，數字合不合得起來」——需要**兩個以上資料點互相參照**才能判斷。系統中唯一有權判定「數值是否相符」的元件，這是 `CLAUDE.md` 的鐵律，模型跟其他腳本都不得重新判斷一次。
- **`validate.py`**：問「這一個值本身，格式/範圍合不合理」——**只看單一欄位自己**，不比對其他資料點。跟 `checker.py`（I1/I3）在算術檢查上刻意有重疊（`validate.py` 用 Decimal 更嚴格），但這是設計上的「更嚴格第二意見」，不是重複實作同一件事，兩者容差不同、目的不同（`checker.py` 給人看核對報告，`validate.py` 給獨立於執行路徑的第二層防呆）。
- **`extract_crosscheck.py`**：問「兩種獨立的讀取方法（正則 vs 模型），讀出來的值一不一樣」——不像前兩者依賴任何算術或格式規則，因為自由文字欄位（船名、港口、人名）根本沒有格式或算術關係可驗證，唯一能做的就是「兩個獨立來源互相印證」。
- 三者切分的核心邏輯是：**「能不能用確定性規則直接驗算」決定歸哪一層**——能靠算術/跨文件參照驗算的歸 `checker.py`；能靠格式/範圍/詞彙表驗算的歸 `validate.py`；兩者都不行、只能靠「換一種方法重讀一次、比對是否一致」的歸 `extract_crosscheck.py`。

### severity 三級對 readiness 的實際影響（`export_erp.py` `determine_readiness()`）

| severity | 對 `case_issues` 的影響 | 對 `checker.py` 結果（FAIL/WARN）的影響 |
|---|---|---|
| `BLOCKING` | 直接判 `BLOCKED`（`determine_readiness()` 第一輪迴圈只要有一條就短路回傳，`review_items` 甚至不會被填） | 對應的是 `FAIL`：任一 FAIL 就直接 `BLOCKED` |
| `REVIEW` | 阻擋 `READY`，案件停在 `REVIEW_REQUIRED`（除非 `NO_TEXT_LAYER`／`MANUAL_VISUAL_SUPPLEMENT_APPLIED`／`EXTRACTION_CROSSCHECK_MISMATCH`／`VISION_ONLY_NEEDS_CONFIRM` 這四種已被逐條件/逐欄位解除） | 對應的是 `WARN`：阻擋 `READY`，但 `R13` 因為已被逐品項 CCC 確認取代，不單獨擋 |
| `INFO` | **完全不影響 readiness**——`determine_readiness()` 裡明確只檢查 `== "BLOCKING"` 跟 `== "REVIEW"` 兩種字串，其他一律被忽略。也**不會出現在 `exceptions.csv`**（`build_exception_rows()` 明確排除 INFO，見第七部分修正紀錄） | （checker.py 沒有對應的 INFO 等級，只有 PASS/WARN/FAIL/SKIP）|

---

## 第七部分：已知限制與弱點（誠實列出）

1. **兩邊一起錯的盲區**
   - **影響**：`extract_crosscheck.py` 只能抓「規則式跟模型讀出的值不一樣」——若供應商單據本身印錯、或版面詭異到兩條獨立路徑剛好誤讀成同一個值，會安靜通過，不產生任何 issue。crosscheck 全綠不等於這個欄位已驗證無誤。
   - **目前怎麼緩解**：README.md／SKILL.md 都已明講這個天花板，要求呈現結果時不能把「沒比對出問題」講成「已驗證無誤」。
   - **真正要解決需要**：本質上無解（自由文字欄位沒有可驗證的格式/算術關係），只能靠第三種獨立來源（例如跟供應商系統對帳、OCR 掃描件另外比對）進一步降低機率，不可能完全消除。

2. **`run_case.py` 重跑會蓋掉人工裁決**
   - **影響**：`run_case.py` 是「從文件重新整個抽取」，不會讀回既有 JSON 保留任何事後人工寫入的欄位——`manual_supplements.json` 合併結果以外的所有人工痕跡（`confirmed_ccc_code`、`freetext_resolutions`、`_field_methods`、`_description_method`）重跑後全部消失，因為 `build_case()` 每次都是全新建構 `doc` dict，不是就地更新既有檔案。
   - **目前怎麼緩解**：SKILL.md 跟 `crosscheck_review.csv` 表頭都各寫了一句警告；`manual_supplements.json` 本身是設計成可以在重跑後自動重新合併的（不受影響）。
   - **真正要解決需要**：`run_case.py` 改成「讀回既有 JSON、只更新抽取欄位、保留人工欄位」的合併式重跑邏輯，目前完全沒有這個機制，是刻意的簡化設計，不是疏漏，但確實是操作上的真實風險點。

3. **`validate.py`／`extract_crosscheck.py` 只接在 `run_case.py`，`extract.py` 單一 PDF 路徑沒接**
   - **影響**：同一批文件，存成多個獨立檔案用 `run_case.py` 跑，會比存成一份合併 PDF 用 `extract.py` 跑多出這兩層的 `case_issues`——兩條輸入路徑目前不是等價的。
   - **目前怎麼緩解**：README.md 已列為已知落差，SKILL.md 兩條路徑都有寫清楚使用時機，但沒有阻止使用者選擇路徑後得到不一致的檢查覆蓋率。
   - **真正要解決需要**：把 `_merge_validate_issues()` 的邏輯提到 `extract.build_case_from_pages()`／`finalize_invoice_item_checks()` 共用層，或者乾脆兩條路徑都收斂成呼叫同一個「收尾」函式。

4. **測試資料的來源與樣本數限制**
   - **影響**：目前自動化測試（`tests/run_tests.py`／`tests/test_validate.py`／`tests/test_extract_crosscheck.py`／`tests/test_export_erp.py`）全部基於 `tests/make_fixtures.py` 產生的**合成資料**，不是真實客戶文件——涵蓋的版面變體大概是 8 組（A_clean/B_faulty/C_altlayout/D_export/A_case/E_multiitem/GD-2609-003/GD-2609-004，其中 A_case 跟 E_multiitem 分別是 A_clean 的多檔案版跟另一個全新多品項案例）+ 3 個 `known_bugs` 重建案例。真實客戶資料（`~/customs-shipments/` 底下）完全不在自動化測試範圍內，只能靠人工手動跑過（詳細狀況見下方第 5 項）。
   - **不確定的地方**：`~/customs-shipments/` 底下有幾份 PDF（`F_modelcode.pdf`、`G_cn_panel_taichung.pdf`、`H_bearing.pdf`、`E_food.pdf`）檔名看起來像額外的手動測試樣本，但 `tests/make_fixtures.py` 目前只會產生到 `A`–`E`（`E_multiitem`），沒有 `F`/`G`/`H` 的產生邏輯——不確定這幾份 PDF 當初是怎麼做出來的、是否還能重現，因為 `~/customs-shipments/` 本身刻意不進版控（真實客戶資料存放慣例），這次盤點沒有找到它們的產生腳本或來源紀錄。
   - **目前怎麼緩解**：無——這是純粹的覆蓋率缺口，沒有緩解機制。
   - **真正要解決需要**：擴大 `make_fixtures.py` 涵蓋更多版面變體，或建立一套「真實文件去識別化後留存為新 fixture」的流程（`CLAUDE.md` 已要求對外展示前去識別化，但目前沒有「去識別化後留作測資」這個機制）。

5. **實際使用狀況**（`~/customs-shipments/` 底下逐案盤點，2026-09-15 查核，本節 2026-09-15 再次修正）
   - 共 10 個案件資料夾，其中 1 個（`GD-2609-003`）只是一份 `DEMO_CASE.txt` 佔位說明，沒有任何實際文件。
   - 剩下 9 個資料夾裡，**至少 6 個已直接比對確認**：資料夾內的 PDF 跟 `fixtures/` 底下某份合成 fixture（`A_clean.pdf`／`B_faulty.pdf`／`C_altlayout.pdf`／`D_export.pdf`）**文字內容逐字相同**，只是換了資料夾名稱（發票號格式）——換句話說，這些不是真實客戶文件，是同一批合成測試文件用不同案號重新命名後放進這個資料夾的。
   - 另外 **3 個資料夾（檔名為 `F_modelcode.pdf`／`G_cn_panel_taichung.pdf`／`H_bearing.pdf`／`E_food.pdf`）在 `fixtures/` 裡找不到對應字母的合成 fixture**，但內容高度指向是針對特定功能手工造的測試文件而非真實貨物——例如 `H_bearing.pdf` 的品項是「BALL BEARING, DEEP GROOVE」，恰好對應這個專案 bug 修正歷史裡明確記載過、用來驗證稅則查詢排序修好了沒的同一個測試品名；`F_modelcode.pdf` 的檔名對應 `customs-rules.md` 裡舉例過的「品名欄是料號型號」情境。**這些沒有逐字比對確認，是依內容/命名模式推斷，不是確定**，但推斷方向一致指向「開發過程手造的測試文件」而非真實客戶資料。
   - 因此這 9 個資料夾裡，**只有 1 個（PDF 檔名跟案號本身同名，不套用上述任何一種字母測試檔命名模式）沒有證據顯示是測試文件，真實性無法確認也無法排除**——保守起見，這份公開文件不寫出是哪一個、也不引用其內容。
   - 不論是不是真實資料，這 9 個資料夾裡 **8 個是舊 schema**（JSON 裡沒有 `case_issues`／`parties` 這些鍵，在這些機制出現前跑的，只停留在 `extract.py` + `checker.py`，**從未跑過 `export_erp.py`**），**只有 1 個是新 schema**，完整跑過 `export_erp.py`，狀態 `REVIEW_REQUIRED`（卡在稅則未人工確認），**沒有 `import/` 資料夾**（從未到達 READY）。
   - **`vision_freetext.json`／`crosscheck_review.csv`／`manual_supplements.json` 這三個檔案在全部 9 個資料夾裡一次都沒出現過**——人工視覺補件、自由文字交叉比對這兩條機制，目前為止只在 `fixtures/` 的合成資料上驗證過。
   - **結論（照實寫，不美化，也不誇大「有真實案例」）**：這個系統目前沒有已確認的真實客戶案件完整跑過端到端流程到 READY。`~/customs-shipments/` 這個原本設計來放真實案件的資料夾，實際內容絕大部分似乎是開發過程留下的測試文件，不是真實貨物；這件事本身也是這次盤點才發現、過去的說法（包含本文件更早版本）錯誤地把它們當成真實案件描述，這裡一併更正。

6. **準確率：目前沒有數字，也說明為什麼**
   - **沒有準確率數字的原因**：計算準確率需要「已知正確答案的真實資料」（ground truth）——也就是找一批真實文件、由人工先逐欄位標註正確值，再拿系統的抽取結果去比對算命中率。目前完全沒有這個標註資料集：9 票真實案件裡沒有任何一票被系統性地人工覆核並記錄「哪些欄位系統抓對了、哪些抓錯了」，`known_bugs/` 裡的 3 筆也只是「已知曾經抓錯過」的個案重建，不是隨機抽樣後的準確率統計。
   - **目前用什麼替代指標**：
     1. **`known_bugs/` 回歸驗收集的攔截率**：3 個歷史真實抓錯案例裡，`validate.py` 直接攔下 1 個（bug1，算術類）、`extract_crosscheck.py` 直接攔下 1 個（bug2，vessel/voyage，已寫進 `tests/test_extract_crosscheck.py` 當驗收題）、`checker.py` 既有的 R5 間接攔下第 3 個（bug3，重量不一致，透過跨文件勾稽偵測到，不是這兩層新增的能力）——這是唯一一個有具體數字的「這個系統對已知錯誤模式的偵測能力」指標，但樣本數只有 3，遠不足以外推成通用準確率。
     2. **確定性檢查違規數**：每次執行後 `checker.py`（FAIL/WARN 計數）、`validate.py`、`extract_crosscheck.py` 各自回報的違規筆數，可以當作「這一票資料乾不乾淨」的相對指標，但這只反映「規則有沒有抓到已知的矛盾模式」，不代表抽取本身正不正確——規則式解析器可能對某個版面整段抓錯、卻剛好通過所有算術/格式檢查（正是 `extract_crosscheck.py` 想補的那個盲區）。
     3. **雙路徑（crosscheck）不一致率**：`EXTRACTION_CROSSCHECK_MISMATCH`／`VISION_ONLY_NEEDS_CONFIRM` 這兩個 code 的出現頻率，理論上可以當成「規則式解析在自由文字欄位上的可疑程度」指標——但目前完全沒有任何真實案例資料（見第 5 項），這個指標現在是 0 個資料點，無法計算。
   - **要有真正的準確率數字，需要**：挑一批真實案件（去識別化後）、人工逐欄位標註正確值、把系統輸出拿去對比算出欄位級命中率，並持續維護這個標註集隨新版面樣本擴充——目前完全沒有做這件事。

7. **模型讀取（`vision_freetext.json`）的 token 成本：未實際量測，以下是量級推估**
   - **為什麼沒有量測值**：如第 5 項所述，這條路徑至今沒有在任何真實案件上執行過，沒有可以拉出來看的真實 token 用量紀錄。
   - **粗略量級推估（非量測值，只是根據典型文件長度做的估算）**：一票貨通常牽涉 2–3 份來源文件（Invoice、Packing List、B/L 或 SO），模型只需要讀跟自由文字欄位有關的段落（船名、港口、buyer/seller、品項描述），不用整份重讀——估計輸入約 1,500–3,000 tokens（幾段原文文字），輸出（`vision_freetext.json` 本身，含 10 個左右扁平欄位加上品項描述、外加 context 片段）約 500–1,500 tokens，合計**粗估每票 2,000–4,500 tokens 量級**。這個數字沒有任何實測基礎，純粹是根據典型文件篇幅推算，實際可能因文件長度、品項數量、context 片段長短而有數倍差異。
   - **要有可信的成本數字，需要**：在至少一批真實案件（或有代表性的合成案例）上實際跑過這條路徑，記錄每次呼叫的實際 input/output token 用量並取統計分布，而不是單一估算值。

---

## 常見提問

### 為什麼不直接把 PDF 丟給 LLM 讀就好？

因為報關申報有法律責任，模型的數值判斷偶爾會出錯、而且不會自我察覺——同一份文件問兩次，模型可能給出不同答案，這種非確定性沒辦法拿來當唯一的權威來源。這個系統的設計核心是把「判斷數值是否相符」這件事，跟「讀取/解釋文件內容」這件事分開：規則式解析（`extract.py`／`checker.py`／`validate.py`）是免費、確定性、可重現、可稽核的基準線，每次跑同一份文件都會得到一模一樣的結果，出錯了也查得出是哪一行程式碼的問題；模型的角色被限縮在兩個地方——① 規則式完全失敗時的補救讀取（掃描件、品項表抓不全）、② 自由文字欄位（船名、港口、人名這種沒有格式或算術關係可驗證的欄位）的第二意見比對。就算是這兩個模型有參與的地方，「兩個值是否一致」這個最終判斷仍然是 Python 程式碼做的，不是模型自己說了算——模型只負責提供候選值，不負責裁定對錯。如果整套流程都讓 LLM 直接讀 PDF 出結論，等於把「無法重現、無法稽核、偶爾出錯不自知」的環節放在系統的核心，跟報關這種容錯率極低的場景不相容。

### 那 `~/customs-shipments/` 底下既有的 9 個案件資料夾現在是什麼狀態？

老實說，第一版盤點把這 9 個資料夾直接當成「真實客戶案件」描述，後來逐份比對發現這個講法不準確：**其中至少 6 個資料夾的 PDF 內容跟 `fixtures/` 的合成測試文件逐字相同**，只是換了個案號格式的資料夾名稱；另外 3 個雖然沒有對應的合成 fixture 可以逐字比對，但檔名跟品名內容（例如一個品項就叫「BALL BEARING, DEEP GROOVE」，剛好對應這個專案 bug 修正歷史裡驗證稅則查詢排序用的同一個測試品名）強烈指向也是開發過程手造的測試文件。真正查不出是不是真實資料的只剩 1 個資料夾，這裡保守起見不指認是哪一個、也不引用內容。技術面上：8 個是很早期 schema 版本留下的（`parties`／`case_issues`／`validate.py`／`extract_crosscheck.py` 這些機制都還沒出現前跑的），只做過最基本的抽取跟 `checker.py` 核對，沒有 ERP readiness 判斷、沒有自動稅則候選，也沒有任何 staging 輸出；只有 1 個用新版流程完整跑過 `export_erp.py`，狀態是 `REVIEW_REQUIRED`（卡在稅則還沒人工確認），沒有任何一個走到 READY、產出過可匯入檔。不管內容是不是真實資料，這都代表這套系統至今還沒有在**已確認的**真實案件上走完整條端到端流程——這件事本身，加上「一開始把測試文件誤記成真實案件」這個過程，都照實留在這裡，沒有修飾。
