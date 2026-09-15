# customs-agent

報關文件自動核對 + ERP 前處理原型。將 Invoice / Packing List / B/L 轉為
結構化資料，以規則引擎交叉核對，產出報關草稿與異常清單；核對過的資料
可再整理成 ERP staging / import 中繼格式，供人工審核後接手匯入客戶自己
的 ERP 系統。**不做自動報關，也不綁定任何特定 ERP。**

完整 workflow（細節見 `README.md`）：

```
文件 → 抽取 → 驗證 → 稅則候選 → 人工確認 → ERP readiness 判斷 → staging → import
```

## 環境

- Python 3.11+，僅使用標準函式庫
- 外部依賴：poppler（提供 `pdftotext`），以 `brew install poppler` 安裝
- 作業系統：macOS (Apple Silicon)

## 目錄

- `$HOME/customs-shipments/<發票號>/` — 真實客戶案件的正式存放位置：
  原始文件（PDF）與抽取/核對輸出物都在這裡。每票貨一個子資料夾，
  以發票號命名，可以是一份合併 PDF，也可以是同一資料夾底下多個獨立
  檔案（後者抽取層改用 `run_case.py`）。`export_erp.py` 的產出物預設
  也寫在同一資料夾底下的 `structured/`、`review/`、`staging/`、
  `import/` 四個子資料夾（`import/<發票號>_erp_import_ready.csv` 只有
  `case_status == READY` 才產生），可用 `--outdir` 指定別的路徑。細節見
  `.claude/skills/customs-check/SKILL.md`
- `fixtures/` — 測試用的合成文件（非真實客戶資料），是自動化回歸測試
  的標準測資來源；每組 fixture 的設計目的與預期結果見 `tests/EXPECTED.md`
- `.claude/skills/customs-check/` — 本專案的 skill 與腳本
- `tests/` — 自動化回歸測試（`run_tests.py`）與測資產生腳本
  （`make_fixtures.py`）
- `data/output/_test_run/` — `tests/run_tests.py` 執行時的暫存輸出，
  每次執行前會整個清空重建，不需要、也不應該手動維護或進版控

## 分層原則（不可違反）

1. **抽取層**（模型或規則）：把文件轉成 JSON。允許不確定。
2. **驗證層**（`checker.py`）：判斷數值是否相符。**只由 Python 執行，模型不得代勞。**
3. **稅則查表層**（`build_tariff_db.py`／`tariff_lookup.py`）：品名關鍵字
   → 候選稅則號列，純檢索、不做最終歸列。候選永遠只是候選，最終歸列
   （寫入 `invoice.confirmed_ccc_code`）只能由人工確認。
4. **ERP 前處理層**（`export_erp.py`）：把已核對過的資料整理成 ERP
   readiness 判斷（READY／REVIEW_REQUIRED／BLOCKED）與 staging/exception
   /import 產出物。**直接呼叫 `checker.py` 的 `run_checks()`，不重新
   實作任何驗證邏輯**；`erp_import.csv` 只要不是 BLOCKED（資料本身矛盾）
   就會產生，未確認的欄位留空由 ERP 端接手補，但絕不能因為想方便匯出
   就把 FAIL（資料矛盾）的值當成已確認。細節見 `README.md`。
5. **輸出層**（模型）：解釋異常、建議後續處理。

任何數值比較、加總、一致性判斷都必須來自 `checker.py` 的輸出，
不得由對話中的推理產生。

## 資料處理

`$HOME/customs-shipments/<發票號>/` 內為真實客戶文件。對外展示前必須
去識別化：公司名、人名、電話、Email、地址一律替換；金額、重量、天數
保留。
