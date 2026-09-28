# known_bugs — validate.py 盲區評估用測資

這個目錄不是回歸測試的一部分（不會被 `tests/run_tests.py` 或
`tests/test_validate.py` 讀取），是一次性評估工作的產出物：重建三個
「規則式已修正抽取 bug」發生當下的錯誤 JSON，餵給 `validate.py`，看它
現在的檢查清單能不能攔下這些歷史上真實發生過的錯誤。

## 為什麼沒有 commit hash

這三個 bug 是在專案公開前的開發過程中修掉的，修正前的 `extract.py`
**不在本 repo 公開的 git 歷史裡**，因此無法用 commit hash 指向「bug 還在」
的版本。本目錄的 JSON 是當時用舊版程式實際跑出來的結果（見下方「重建
方式」），每個 bug 底下也附上了舊版程式片段與實際跑出的錯誤值，作為
紀錄。

另外還找到第四個候選（發票原產地欄位貪婪比對，把 "to" 吃進國名——見
`.claude/skills/customs-check/scripts/extract.py` 現有正則旁的註解），
但重建當時已經找不到保留「錯誤版本」的原始碼，
沒有任何可執行的舊程式碼可以重跑重建，只有事後的文字描述——手動編一個
「大概長這樣」的錯誤值不算重建，是編造，所以這次**沒有**把它算進三個
bug 裡，也沒有幫它造測資。三個已經是這次能誠實重建出來的全部數量，
不是硬湊的。

## 重建方式：一律用方式 B（實際執行舊版函式）

三個 bug 都用同一種方式重建，好過用方式 C（手改欄位猜錯誤值）——因為
重建當時舊版函式還在，可以對真實 PDF 的文字層實際跑一次，
拿到當時真的會產出的錯誤值，不是用人腦推理正則行為去猜。
以下為當時的做法紀錄（需要修正前的舊版 `extract.py`，本 repo 不包含，
無法直接照跑）：

```bash
# /tmp/extract_old.py = 修正前的舊版 extract.py
python3 -c "
import importlib.util, subprocess
spec = importlib.util.spec_from_file_location('extract_old', '/tmp/extract_old.py')
extract_old = importlib.util.module_from_spec(spec); spec.loader.exec_module(extract_old)
text = subprocess.run(['pdftotext','-layout', '<PDF路徑>'], ...).stdout
print(extract_old.parse_invoice(text))       # 或 parse_packing_list / parse_transport_doc
"
```

嚴格來說，舊版 `extract.py` 只支援單一合併 PDF（`run_case.py` 當時還不
存在，是與這三個修正同一批新增的檔案），沒辦法直接對 `GD-2609-003`／
`GD-2609-004` 這種多檔案案件目錄跑完整 CLI。因此實際做法是：直接呼叫
舊版模組裡對應的**單一解析函式**（`parse_invoice`／`parse_packing_list`／
`parse_transport_doc`，這三個函式的簽章跟呼叫方式在新舊版之間沒有變，
只有內部正則變了），餵入該案件對應那份 PDF 的 `pdftotext -layout` 文字，
取得舊版真正會算出的錯誤欄位值，再把**這一個欄位**替換進「目前案件的
完整正確 JSON」裡——其餘欄位（`case_issues`、`parties`、
`source_hs_code_*` 等）維持目前正確版本，因為這些欄位當初根本不存在，
沒有「錯誤版本」可言，也不是這次要測的東西。這次要回答的問題是「這個
特定的錯誤值，出現在一份其餘都正常的案件裡，validate.py 現在攔不攔得
下來」，不是「重建一份考古學意義上完全忠實的舊版 JSON」。

## 三個 bug

### bug1 — 多品項發票 `total_amount` 誤取自最後一品項金額

- **檔案**：`bug1_invoice_total_from_last_item__E_multiitem.json`
- **案件**：`fixtures/E_multiitem/`（3 品項發票，MDO-2609-005）
- **舊程式**（`extract.py` HEAD 版 `parse_invoice()` 第 102–103 行）：
  ```python
  if inv["items"]:
      inv["total_amount"] = inv["items"][-1]["amount"]
  ```
  單品項發票時「最後一項金額」剛好等於「發票總額」，掩蓋了這是個 bug；
  多品項時就是徹底錯的值，不是表尾真正的加總。
- **實際跑出來的錯誤值**：`total_amount = 2500.0`（正確應為
  `13100.0` = 6400+4200+2500，表尾也印著 `Total: ... 13,100.00`）。
- **現在的修法**：`_invoice_total_amount()` 改成優先解析表尾 `Total:`
  列本身，抓不到才退回舊行為。

### bug2 — Vessel/Voyage 同行合併版面，舊正則貪婪切錯

- **檔案**：`bug2_vessel_voyage_split__GD-2609-004.json`
- **案件**：`fixtures/GD-2609-004/`，B/L 原文該行為
  `Vessel / Voyage              EVER GLORY / 118N`
- **舊程式**（`parse_transport_doc()` 第 200–202 行）：
  ```python
  if m := re.search(r"Vessel\s*(?:/\s*Voyage)?\s*:?\s*([A-Z0-9 .\-]+?)\s*"
                    r"(?:V\.?|VOY\.?)\s*([A-Z0-9\-]+)", body, re.I):
      td["vessel"], td["voyage"] = m.group(1).strip(), m.group(2).strip()
  ```
  非貪婪的船名擷取群組跟 `V.`/`VOY.` 分隔符之間用 `\s*`（零個以上空白），
  只要船名字面中出現字母 `V`，就會被非貪婪比對誤判成分隔符提前截斷。
  `EVER GLORY` 的 `E-V-E-R` 裡剛好有一個 `V`。
- **實際跑出來的錯誤值**：`vessel = "E"`、`voyage = "ER"`
  （正確應為 `"EVER GLORY"` / `"118N"`）。
- **現在的修法**：新增「同行合併標籤＋值，以 `/` 分隔」的版面優先判斷，
  且退回規則加上「分隔符前必須有空白」的限制。

### bug3 — `declared_net`／`declared_gross` 誤取自表尾後 CONTAINER SUMMARY 的最後一櫃

- **檔案**：`bug3_declared_net_gross_container_summary__GD-2609-003.json`
- **案件**：`fixtures/GD-2609-003/`，品項表尾（`TOTAL 200 PKGS 16,300.00
  17,560.00`）後面還接了一段獨立的 `CONTAINER SUMMARY`（列出兩個貨櫃各自
  的淨重/毛重）。
- **舊程式**（`parse_packing_list()` 第 125–127 行）：
  ```python
  totals = re.findall(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", body)
  if totals:
      pl["declared_net"], pl["declared_gross"] = num(totals[-1][0]), num(totals[-1][1])
  ```
  掃全文取「最後一組」小數對，版面若在表尾之後還有東西，取到的就不是
  表尾總計。
- **實際跑出來的錯誤值**：`declared_net = 6300.0`、`declared_gross =
  6790.0`（`FSCU7654321` 那一櫃的重量，正確應為表尾總計
  `16300.0`／`17560.0`）。
- **現在的修法**：改成優先在「Total」關鍵字附近找，找不到才退回舊的
  「最後一組」heuristic。

## 使用的正確版基準 JSON

- bug1：`python3 .claude/skills/customs-check/scripts/run_case.py
  fixtures/E_multiitem <tmp路徑>` 現場重跑目前（已修好）的程式碼取得
  （`fixtures/E_multiitem/` 本身沒有委交的 `.json`，不影響任何既有測資）。
- bug2／bug3：直接取用已經委交在 repo 裡的
  `fixtures/GD-2609-004/GD-2609-004.json`／`fixtures/GD-2609-003/GD-2609-003.json`
  （複製後修改，不覆蓋原檔）。
