# 報關文件交叉核對

將 Invoice / Packing List / B/L（或出口用的 SO／訂艙確認書）轉為結構化資料，
交由規則引擎核對，產出異常清單與報關草稿。支援兩種輸入方式：文件已合併
成一份 PDF、或分開成多個獨立檔案（同一個案件目錄）。

全程以繁體中文回應。

## 絕對規則

**你不得自行判斷任何數值是否相符。**

不得心算加總、不得目視比對數字、不得憑推理宣告「一致」或「不一致」。
所有一致性結論**只能**來自 `scripts/checker.py` 的輸出（單據層級 R0–R16、
品項層級 I1–I3 兩組獨立檢查都算）。若腳本未執行成功，就回報執行失敗，
不要用推理代替它。ERP readiness（READY／REVIEW_REQUIRED／BLOCKED）只能
來自 `scripts/export_erp.py` 的 `determine_readiness()`，同樣不得由對話
推理產生。

理由：報關申報有法律責任。模型的算術偶爾出錯且不會自我察覺，
規則腳本則是確定性、可重現、可稽核的。這條界線是本系統的設計核心。

## 路徑

**不要寫死任何絕對路徑。** 這個 skill 被呼叫時，系統會在訊息裡告知
「Base directory for this skill: `<base>`」——下面所有指令路徑都是相對
於這個 base directory（例如 `<base>/scripts/extract.py`），用當次呼叫
告知的實際路徑代入，因為這個資料夾可能被搬到不同專案或不同機器。

文件預設放在 `$HOME/customs-shipments/<票號>/`，可以是一份合併 PDF，也可
以是同一個資料夾底下多個獨立檔案（例如 `commercial_invoice.pdf`、
`packing_list.pdf`、`bill_of_lading.pdf`——檔名慣例很重要，見下方步驟 2；
檔名裡沒有可辨識的關鍵字時，仍會照文件內容判斷類型，只有在某個檔案
完全抽不到文字層、必須退而求其次用檔名猜測時才依賴檔名）。
`export_erp.py` 的產出物寫到 `<這個資料夾>/<發票號>/` 底下（見步驟 5）。
這個路徑跟 skill 本身的 base directory 無關，是逐票客戶資料的固定存放
慣例，不會因為 skill 搬家而改變。

## 流程

1. **盤點輸入**
   若使用者未指明路徑，列出 `$HOME/customs-shipments/` 看有哪幾票；
   有多票時先問要處理哪一票，不要自行猜測。
   缺 B/L（或出口情境下缺 SO／訂艙確認書）時照常進行，相關檢查會自動標為 SKIP。

2. **抽取**
   案件目錄裡是多個獨立檔案時：
   ```bash
   python3 "<base>/scripts/run_case.py" \
       "<案件目錄>" "<輸出json路徑>"
   ```
   只有一份合併 PDF 時：
   ```bash
   python3 "<base>/scripts/extract.py" \
       "<pdf路徑>" "<輸出json路徑>"
   ```
   兩者共用同一套解析核心，抽取邏輯一致。

   `run_case.py` 遇到某個檔案完全抽不到文字層（掃描件）時**不會中止整個
   案件**，會依檔名猜測那可能是哪種文件（invoice/packing list 判為較嚴重
   的 `BLOCKING`，運輸單據判為較寬鬆的 `REVIEW`），記進輸出 JSON 的
   `case_issues` 陣列，其餘檔案照常處理。這種情況下：
   - 改用視覺方式讀取**那一個檔案**，自行產出相同 schema 的片段
     （schema 見 `examples/GD-2609-003.json` 這份合成樣本——不是真實
     客戶資料，只是示範完整 schema 長什麼樣子），填回 JSON 對應的鍵
     （`invoice`／`packing_list`／`transport_document`），該片段的
     `extraction_method` 標成 `vision`，其餘鍵維持規則式抽取結果，
     不必整案重做。
   - `extract.py` 單一合併 PDF 若整份抽不到文字層會直接報錯，維持舊行為，
     此時整份都要改走視覺抽取。
   - 抽取不到的欄位一律填 `null`，**絕不臆測數值**；賣方/買方等版面
     啟發式抓不準時，抽取層本身就會把該欄位留 `null` 並記一條
     `case_issues`，不需要你去猜。

   同一份文件**有文字層、但規則式解析只抓到部分品項列**（例如浮水印、
   簽名章跟表格欄位版面重疊，造成某幾列的數量/單價/金額被拆到不相鄰的
   輸出列）時，`extract.py`／`run_case.py` 會自動比對「品項金額加總」跟
   「發票表尾總額」，兜不起來就記一條 `BLOCKING` 的 `case_issues`
   （`INVOICE_ITEM_TABLE_POSSIBLY_INCOMPLETE`），不會讓半份品項清單悄悄
   流向後面的檢查。要補齊，一樣改用視覺方式讀取原文，但**不要直接手改
   `extract.py`/`run_case.py` 產出的 JSON**（下次重跑會被蓋掉）：在案件
   目錄（`run_case.py` 的輸入資料夾；`extract.py` 單一 PDF 則是那份 PDF
   所在資料夾）下新增一個 `manual_supplements.json`，格式：
   ```json
   {
     "invoice": {
       "items": [
         {"line_no": 1, "description": "...", "qty": 9000, "unit": "KGS",
          "unit_price": 1.90, "amount": 17100.00,
          "source_hs_code_raw": "200989",
          "source_document": "01_....pdf",
          "supplement_reason": "浮水印造成規則式解析器抓不到本列"}
       ]
     }
   }
   ```
   `line_no` 是**印在發票上的真實序號**（不是規則式抽取內部重新編的
   idx+1），重跑 `extract.py`/`run_case.py` 時會自動讀取這個檔案、依
   `line_no` 插入正確位置、其餘既有品項依原順序補進剩下的位置，全部
   重新編號，並在 `case_issues` 留一條 `REVIEW` 等級的
   `MANUAL_VISUAL_SUPPLEMENT_APPLIED` 揭露紀錄（不會悄悄消失，也不會
   因為改跑別的案件而混進去——每個案件各自的 `manual_supplements.json`
   只對同一個資料夾生效）。這個檔案本身就是留痕：來源必須是你**實際
   讀過原文件**得到的值，絕不是為了讓加總兜起來而回推出來的數字。

3. **自由文字欄位交叉比對**（每票都跑，`extract_crosscheck.py`）

   跟步驟 2 提到的視覺抽取**不是同一件事**，兩者並存、觸發條件與目的
   都不同：

   | | 觸發時機 | 讀取範圍 | 目的 |
   |---|---|---|---|
   | 步驟 2 的視覺抽取 | 失敗才補救（無文字層／品項表疑似不完整）| 整份文件、補所有欄位 | 填補規則式完全抽不到/抽不全的空缺 |
   | 這一步 | **每票都跑** | 只讀自由文字欄位（見下） | 找出規則式「有抓到值、但可能抓錯」的自由文字欄位 |

   自由文字欄位只有這些（金額/重量/件數不在範圍內，那些已經有
   `validate.py`／`checker.py` R5／R15 在管，這裡重做只會製造重複
   `case_issues`）：
   ```
   transport_document.vessel / voyage / doc_no / port_of_loading / port_of_discharge
   parties.seller_name / buyer_name / shipper_name / consignee_name / notify_party_name
   invoice.items[].description
   ```

   你（模型）讀原文，只針對上面這些欄位，寫出
   `<案件目錄>/vision_freetext.json`：
   ```json
   {
     "extraction_method": "vision",
     "fields": {
       "transport_document.vessel": {"value": "EVER GLORY", "context": "Vessel / Voyage   EVER GLORY / 118N"}
     },
     "advisory_notes": [
       {"field": "transport_document.vessel", "note": "疑似拼字錯誤或格式可疑，僅供參考"}
     ]
   }
   ```
   **原文印錯就照抄，絕不自動改成你認為對的拼法**——申報須與單據一致，
   疑似有誤只能寫進 `advisory_notes`，不能拿去覆蓋 `value`。`context`
   是你讀到那個值時看到的原文片段，順手記下即可，不用另外回頭找。

   接著跑：
   ```bash
   python3 "<base>/scripts/extract_crosscheck.py" compare \
       "<json路徑>" "<案件目錄>/vision_freetext.json"
   ```
   純 Python 逐欄位比對（只判斷「相不相等」，不判斷哪個對），併入
   `case_issues`：`EXTRACTION_CROSSCHECK_MISMATCH`／
   `VISION_ONLY_NEEDS_CONFIRM`（REVIEW，擋 readiness）、
   `VISION_ADVISORY`（INFO，只寫進 JSON、不印在終端、不擋任何流程）。
   完全一致的欄位安靜通過。同時寫出
   `<案件目錄>/crosscheck_review.csv`，把待人工裁決的項目原樣呈現
   給使用者。

   人工核對原文、填好 `crosscheck_review.csv` 的 `resolution` 欄後：
   ```bash
   python3 "<base>/scripts/extract_crosscheck.py" resolve \
       "<json路徑>" "<填好的清單.csv>"
   ```
   ⚠️ **填寫清單期間、裁決完成前都不要重新執行 `run_case.py`**——那會
   整個重建案件 JSON，蓋掉這份清單對應的所有裁決紀錄（跟
   `manual_supplements.json`／`confirmed_ccc_code` 的既有限制一樣）。

4. **驗證**
   ```bash
   python3 "<base>/scripts/checker.py" \
       "<json路徑>" --direction import|export --csv "<csv路徑>"
   ```
   輸出含兩組獨立計數：R0–R16（單據層級，跨文件比對）與 I1–I3
   （品項層級，逐品項算術／完整性）——`checker.py` 目前 CLI 只印
   R0–R16；I1–I3 跟完整的 readiness 判斷會在步驟 5 `export_erp.py`
   的報告裡看到，不要把「R0–R16 全過」讀成「每個品項都查過」。

   腳本在有 FAIL 時回傳非零結束碼，此為預期行為，不是執行失敗。

   若報表顯示「申報方向」未判定（JSON 的 `trade_direction` 為
   `null`，通常是文件上找不到 Port of Loading/Discharge 或
   From/to 這類欄位），先問使用者這一票是進口還是出口，
   加上 `--direction import` 或 `--direction export` 重跑一次，
   不要自行猜測方向——進口／出口在完稅價格基礎、管制查核、
   第三份文件應為 B/L 或 SO 上規則相反（見
   `references/customs-rules.md`）。

5. **ERP 前處理與 readiness 判斷**
   ```bash
   python3 "<base>/scripts/export_erp.py" \
       "<json路徑>" --direction import|export
   ```
   會在 `<json所在資料夾>/<發票號>/` 底下產生
   `structured/`、`review/`、`staging/`、`import/` 四個子資料夾——細節
   跟 readiness 判斷邏輯見 `README.md`。稅則候選（`tariff_candidates.csv`）
   在這一步**自動對每個品項跑**，不用另外手動觸發 `tariff_lookup.py`。
   `import/<發票號>_erp_import_ready.csv` 只有 `case_status == READY`
   （所有核對通過、且每個品項稅則都已人工確認）才會產生。

   人工看過 `review/<發票號>_review.html`（或 `staging/
   <發票號>_items.csv` 裡的 `top_candidate_ccc_code`）決定要採用哪個
   候選後，逐品項確認：
   ```bash
   python3 "<base>/scripts/export_erp.py" "<json路徑>" \
       --confirm-ccc-item <品項序號> <11碼CCC號列>
   ```
   （單品項發票可沿用舊版 `--confirm-ccc <11碼CCC>`，會自動鏡射到唯一的
   品項。）確認後重跑 `export_erp.py`，`case_status` 才會往 READY 前進。

6. **輸出**
   將 `review/<發票號>_report.txt`（或 `review.html` 的重點）原樣呈現，
   然後針對每一項 FAIL 與 WARN（含 I1–I3、`case_issues`），用一句話說明
   代表什麼、下一步該找誰處理。這一步是你的工作，也只有這一步是。

7. **稅則號列的人工判斷（選擇候選、不是查詢本身）**
   稅則候選現在會自動產生（步驟 5），你不需要主動觸發查詢；這一步是
   幫使用者**解讀**已經產生的候選清單，判斷哪個候選在關鍵字/品名上最
   接近、規定代號代表什麼。若使用者想針對單一品名手動查（例如還沒
   跑過 `export_erp.py`、只想先看看有哪些候選），可以用：
   ```bash
   python3 "<base>/scripts/tariff_lookup.py" \
       "<品名>" --top 5
   ```
   若資料庫不存在，工具會提示先跑 `build_tariff_db.py`，照實回報，
   不要自己編候選。

   原樣呈現候選清單與兩則固定警語。**不得替使用者選定「正確答案」**，
   只能指出哪個候選在關鍵字/品名上最接近、規定代號代表什麼，最終歸列
   仍是報關人員的責任（與 R13 的原則一致）。`top_candidate_ccc_code`
   是檢索排序第一名，不是「系統推薦」，不要用「建議」這種字眼描述它。

   候選明顯不合理（例如品名是「加工方式＋用途」這種功能性描述，稅則卻
   用材質/形狀分類，關鍵字搜不到對的章節）時，可以換個更接近稅則慣用
   語彙的詞（例如把「MACHINED ALUMINIUM BRACKET」換成「aluminium
   articles」或「鋁製品」）重查一次，並跟使用者說明你換了詞、為什麼——
   這是你在解讀查詢結果時的判斷，不是幫資料庫做語意搜尋，換詞前後的
   查詢結果都要讓使用者看得到，不要只呈現你覺得對的那次。

   若使用者確認採用某個候選，用步驟 5 的 `--confirm-ccc-item` 寫入該
   品項的 `confirmed_ccc_code`，再重跑一次 `export_erp.py`。

## 呈現方式

- **R0 FAIL**：抽取不完整，本次結果不足採信。優先處理這一項，
  建議改走視覺抽取後重跑，不要討論其他規則的結果。
- **FAIL**：文件本身矛盾，送件前必須修正。指出該找供應商或船公司更正。
  I1/I3 的 FAIL（品項算術、品項金額加總跟發票總額對不上）同樣是矛盾，
  處理方式一樣。
- **WARN**：需要人為判斷，機器只負責提醒沒漏掉。I2 的 WARN（品項欄位
  抽不到）是抽取層的不確定，不是文件矛盾，但一樣需要人工核對原文。
- **SKIP**：缺少必要文件或欄位，說明補齊哪一份就能檢查。
- **`case_issues`**：抽取/文件層級的疑義（多檔案模糊選擇、某檔案抽不到
  文字層、party 抽取不確定、`EXTRACTION_CROSSCHECK_MISMATCH`／
  `VISION_ONLY_NEEDS_CONFIRM` 這類自由文字欄位不一致），依 `README.md`
  的嚴重度表說明對 readiness 的影響，`BLOCKING` 比照 FAIL 處理、
  `REVIEW` 比照 WARN 處理。`VISION_ADVISORY`（INFO）不擋任何流程，
  是模型的自由觀察，原樣附帶提一句即可，不要當成問題來呈現。

結尾必附一句：本報表為草稿輔助，送件前仍須報關人員逐項覆核。

## 注意事項

- 抽取層若用視覺讀取，回覆中須提醒使用者：該來源為推論結果，
  欄位值本身需人工抽樣核對。
- 步驟 3 的自由文字欄位交叉比對有天花板：只能抓「規則式跟模型讀出的
  值不一樣」，若供應商單據本身印錯、或兩條路徑剛好誤讀成同一個值，
  會安靜通過。跟使用者說明結果時，不要把「這幾個欄位沒有交叉比對出
  問題」講成「這幾個欄位已驗證無誤」，只是「兩種方法讀出同一個值」。
- 稅則號列（CCC）**不由本 skill 決定**。供應商標示的 HS、
  `tariff_candidates.py`／`tariff_lookup.py` 查到的候選都僅供縮小範圍
  參考，最終歸列與申報責任在報關人員；`confirmed_ccc_code` 只能是人工
  確認後才寫入的值。
- `tariff.db` 只涵蓋稅則本身印的規定代號（一般許可管制項目），不包含
  戰略性高科技貨品／兩用貨品／禁運國別清單，這點 `tariff_lookup.py`
  的輸出本身就會提醒，不需要你額外加註，但也不要漏掉這句話不呈現。
  `tariff_candidates.csv` 裡的 `tariff_rate_reference` 只是第一欄稅率的
  參考值，不是應適用稅率，這句提醒在 HTML 報告裡已經有固定文字，你
  複述給使用者聽時也不要省略。
- 文件多為真實客戶資料。產出任何對外內容前先去識別化。
- 領域規則的依據見 `references/customs-rules.md`。
