#!/usr/bin/env python3
"""
建置層：稅則 PDF → 結構化查詢資料庫

把中華民國海關進口稅則輸出入貨品分類表合訂本（上冊＋下冊）解析成
SQLite 資料庫，供 tariff_lookup.py 查詢候選稅則號列與輸出入規定代號。
解析用 pdftotext -bbox-layout 的逐字座標分欄，不用空白猜欄位；
每個表頭列都會重新校準欄位 x 座標，因此上下冊版面位移也能正確處理。

本檔只負責「稅則書本身寫了什麼」的確定性轉錄，不做任何品名比對或分類判斷——
那是 tariff_lookup.py 的事，而且那邊的結果永遠只是候選，不是最終認定。

用法：
    python3 build_tariff_db.py [上冊PDF路徑] [下冊PDF路徑]
        [--db 輸出DB路徑] [--chunk-size N] [--verify-only]
"""
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_UPPER = os.path.expanduser("~/Downloads/112年上冊final.pdf")
DEFAULT_LOWER = os.path.expanduser("~/Downloads/112年下冊final.pdf")
# 相對於這支腳本自己的位置算路徑，不寫死 $HOME 或任何固定安裝路徑——
# 這個 skill 資料夾放在哪裡都能動。
DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "tariff.db")
DEFAULT_CHUNK = 50

FAIL, WARN, PASS, SKIP = "FAIL", "WARN", "PASS", "SKIP"
ICON = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}

TABLE_FOOTER_Y = 820.0  # 表格頁頁尾（頁碼＋章號）的 y 起點，實測約 834；抓 820 留一點餘裕。

# 表頭錨點字（每頁重新校準欄位邊界用）；順序即欄位順序
ZONE_ANCHORS = [
    ("identifier", "稅則號別"),
    ("name_zh", "貨"),
    ("name_en", "Description"),
    ("unit", "Unit"),
    ("rate1", "第一欄"),
    ("rate2", "第二欄"),
    ("rate3", "第三欄"),
    ("cr", "CR"),
    ("import", "輸入"),
    ("export", "輸出"),
]

CODE8_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2})$")
CODE6_RE = re.compile(r"^(\d{4}\.\d{2})$")
CODE4_RE = re.compile(r"^(\d{4})$")
STATCD_RE = re.compile(r"^(\d{2})\s*(\d)$")
CHAPTER_RE = re.compile(r"第\s*(\d{1,2})\s*章|Ch\.?\s*(\d{1,2})\b")


# ---------- 小工具 ----------

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_page_count(path: str) -> int:
    out = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    if not m:
        raise SystemExit(f"[build] 無法讀取頁數：{path}")
    return int(m.group(1))


class Word:
    __slots__ = ("x0", "y0", "x1", "y1", "text")

    def __init__(self, x0, y0, x1, y1, text):
        self.x0, self.y0, self.x1, self.y1, self.text = x0, y0, x1, y1, text


def _local(tag: str) -> str:
    """去掉 ElementTree 的命名空間前綴（-bbox-layout 輸出是 XHTML，預設命名空間
    會讓 root.iter('page') 這種寫法完全比對不到，必須比對 local name）。"""
    return tag.rsplit("}", 1)[-1]


def iter_pages_numbered(pdf_path: str, first: int, last: int, chunk_size: int):
    """真正對外用的介面：yield (page_no 1-indexed, page_width, [Word,...])。"""
    for start in range(first, last + 1, chunk_size):
        end = min(start + chunk_size - 1, last)
        proc = subprocess.run(
            ["pdftotext", "-bbox-layout", "-f", str(start), "-l", str(end), pdf_path, "-"],
            capture_output=True, text=True,
        )
        html = proc.stdout
        if not html.strip():
            raise SystemExit(
                f"[build] {pdf_path} 頁 {start}-{end} 的 pdftotext 輸出為空，"
                f"停止並保留現場供檢查。stderr：{proc.stderr[:500]}"
            )
        try:
            root = ET.fromstring(html)
        except ET.ParseError as e:
            raise SystemExit(f"[build] {pdf_path} 頁 {start}-{end} bbox HTML 解析失敗：{e}")
        page_els = [el for el in root.iter() if _local(el.tag) == "page"]
        page_no = start
        for page_el in page_els:
            pw = float(page_el.get("width"))
            words = []
            for w in page_el.iter():
                if _local(w.tag) != "word":
                    continue
                if w.text and w.text.strip():
                    words.append(Word(
                        float(w.get("xMin")), float(w.get("yMin")),
                        float(w.get("xMax")), float(w.get("yMax")),
                        w.text,
                    ))
            yield page_no, pw, words
            page_no += 1
        root.clear()


def group_lines(words: list, tol: float = 1.6) -> list:
    """把同頁的字依 yMin 聚成列，回傳依 y 排序的 [[Word,...], ...]，列內依 x 排序。"""
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w.y0, w.x0))
    lines = []
    cur = [ws[0]]
    cur_y = ws[0].y0
    for w in ws[1:]:
        if abs(w.y0 - cur_y) <= tol:
            cur.append(w)
        else:
            lines.append(sorted(cur, key=lambda w: w.x0))
            cur = [w]
            cur_y = w.y0
    lines.append(sorted(cur, key=lambda w: w.x0))
    return lines


# ---------- 表頭偵測與欄位校準 ----------

def find_header_anchors(line: list) -> dict | None:
    """給一列 Word，若同時含所有欄位錨點字則回傳 {zone_name: xMin}，否則 None。"""
    texts = {w.text: w.x0 for w in line}
    if "稅則號別" in texts and "貨" not in texts:
        return None
    found = {}
    for zone, anchor in ZONE_ANCHORS:
        if anchor in texts:
            found[zone] = texts[anchor]
    if len(found) >= 8:  # 允許個別字因斷行分佈到不同行，至少抓到大半才算表頭
        return found
    return None


def find_header_in_lines(lines: list) -> dict | None:
    """表頭其實橫跨好幾個實體列（中英文各自換行），因此把同頁前 14 列的錨點字合併找。
    回傳 {zone_name: xMin, ..., '_header_bottom_y': y} 或 None；
    `_header_bottom_y` 是表頭區塊最下緣的 y，呼叫端用它把表頭本身的字（如
    「中華民國輸出入」「貨品分類號列」這些跟識別欄同一個 x 範圍的標題字）
    從資料列解析中排除，避免被誤判成一筆稅則列。"""
    merged = {}
    cd_xmax = None
    bottom_y = 0.0
    for line in lines[:14]:
        for w in line:
            if w.text in dict(ZONE_ANCHORS).values():
                merged.setdefault(w.text, w.x0)
                bottom_y = max(bottom_y, w.y1)
            if w.text == "CD":
                cd_xmax = w.x1 if cd_xmax is None else max(cd_xmax, w.x1)
    if "稅則號別" in merged and "Description" in merged and "輸入" in merged and "輸出" in merged:
        zone_x = {}
        for zone, anchor in ZONE_ANCHORS:
            if anchor in merged:
                zone_x[zone] = merged[anchor]
        zone_x["_header_bottom_y"] = bottom_y
        # 「識別欄」(稅則號別+統計號+檢查碼) 的實際右邊界要看 CD 子欄的右緣，
        # 不能只用「稅則號別」錨點跟下一欄「貨」錨點取中點——那條中點線會切在
        # CD 子欄中間，害檢查碼的字被誤歸進「貨名」欄（已用真實資料驗證過這個坑）。
        if cd_xmax is not None:
            zone_x["_cd_end"] = cd_xmax
        return zone_x
    return None


def calibrate_zones(zone_x: dict, page_width: float) -> dict:
    """給每個 zone 起點 x，算出 (start,end) 邊界：邊界＝相鄰錨點中點，
    但「識別欄→貨名欄」這條邊界改用 CD 子欄右緣（`_cd_end`）跟貨名欄起點的
    中點，不能用「稅則號別」錨點本身（見 find_header_in_lines 的說明）。"""
    cd_end = zone_x.get("_cd_end")
    order = [z for z, _ in ZONE_ANCHORS if z in zone_x]
    starts = [zone_x[z] for z in order]
    bounds = [0.0]
    for i in range(1, len(starts)):
        if order[i - 1] == "identifier" and cd_end is not None:
            bounds.append((cd_end + starts[i]) / 2)
        else:
            bounds.append((starts[i - 1] + starts[i]) / 2)
    bounds.append(page_width)
    return {order[i]: (bounds[i], bounds[i + 1]) for i in range(len(order))}


def zone_of(word: Word, zone_map: dict) -> str | None:
    cx = (word.x0 + word.x1) / 2
    for zone, (lo, hi) in zone_map.items():
        if lo <= cx < hi:
            return zone
    return None


# ---------- 稅則正文解析狀態機 ----------

class BuildState:
    def __init__(self, volume: str):
        self.volume = volume
        self.zone_map = None
        self.header_bottom_y = None
        self.cur_record = None
        self.cur_8 = None
        self.cur_h6 = None
        self.cur_h4 = None
        self.cur_chapter = None
        self.lines_out = []       # tariff_lines 待寫入列
        self.headings_out = {}    # code -> heading dict
        self.warnings = []


def ident_text_of(id_words: list) -> str:
    return " ".join(w.text for w in id_words)


def split_code_stat_cd(id_words: list):
    """從識別欄的字裡切出 (code|None, stat|None, cd|None)。
    識別欄可能同時有 '0106.19.30' '00' '6' 三個 word，也可能只有 '11' '1'。"""
    texts = [w.text for w in id_words]
    joined = " ".join(texts)
    code = None
    for tok in texts:
        if CODE8_RE.match(tok) or CODE6_RE.match(tok) or CODE4_RE.match(tok):
            code = tok
            break
    # 找 stat+cd：兩個獨立 token，各自是 2 位數／1 位數，且不是 code 本身
    nums = [t for t in texts if t != code and re.fullmatch(r"\d{1,2}", t)]
    stat, cd = None, None
    if len(nums) >= 2:
        stat, cd = nums[0], nums[1]
    return code, stat, cd


def new_heading(state: BuildState, code: str, level: int, name_zh_words, page_no, source_volume):
    parent = None
    if level == 4:
        parent = None
    elif level == 6:
        parent = code.split(".")[0]
    elif level == 8:
        parent = ".".join(code.split(".")[:2])
    chapter = int(code[:2])
    h = {
        "code": code, "level": level, "name_zh": None, "name_en": None,
        "parent_code": parent, "chapter": chapter,
        "source_volume": source_volume, "source_page": page_no,
    }
    state.headings_out[code] = h
    return h


def new_leaf(state: BuildState, code8: str, stat: str, cd: str, page_no, source_volume):
    digits = code8.replace(".", "") + stat + cd
    display = f"{code8}.{stat}.{cd}"
    chapter = int(code8[:2])
    rec = {
        "ccc_code_digits": digits, "ccc_code_display": display,
        "heading_code": code8, "chapter": chapter,
        "name_zh": None, "name_en": None, "unit": None,
        "rate1": None, "rate2": None, "rate3": None, "cr": None,
        "import_reg_codes": [], "export_reg_codes": [],
        "source_volume": source_volume, "source_page": page_no,
        "raw_stat_no": stat, "raw_check_digit": cd,
        "kind": "leaf",
    }
    state.lines_out.append(rec)
    return rec


def populate_zones(rec: dict, line: list, zone_map: dict, is_new: bool):
    """把一列裡『非識別欄』的字依 zone 填進 rec，is_new=True 表示這是記錄的第一行。"""
    buckets = {}
    for w in line:
        z = zone_of(w, zone_map)
        if z is None or z == "identifier":
            continue
        buckets.setdefault(z, []).append(w)
    for z, ws in buckets.items():
        ws = sorted(ws, key=lambda w: w.x0)
        if z == "name_zh":
            txt = "".join(w.text for w in ws)
            rec["name_zh"] = txt if is_new or not rec.get("name_zh") else rec["name_zh"] + txt
        elif z == "name_en":
            txt = " ".join(w.text for w in ws)
            rec["name_en"] = txt if is_new or not rec.get("name_en") else rec["name_en"] + " " + txt
        elif z in ("unit", "rate1", "rate2", "rate3", "cr"):
            txt = " ".join(w.text for w in ws)
            prev = rec.get(z)
            rec[z] = txt if is_new or not prev else (prev + " " + txt)
        elif z in ("import", "export"):
            key = f"{z}_reg_codes"
            rec.setdefault(key, [])
            for w in ws:
                if w.text not in rec[key]:
                    rec[key].append(w.text)


def process_table_page(state: BuildState, page_no: int, page_width: float, lines: list, volume: str):
    hdr = find_header_in_lines(lines)
    if hdr:
        state.header_bottom_y = hdr.pop("_header_bottom_y")
        state.zone_map = calibrate_zones(hdr, page_width)
    if state.zone_map is None:
        return  # 還沒校準過就先跳過這頁（通常是章註頁混進表格範圍判定，罕見）

    header_bottom = state.header_bottom_y if state.header_bottom_y is not None else -1

    for line in lines:
        # 跳過表頭區塊本身（「中華民國輸出入」「貨品分類號列」...這些標題字
        # 剛好落在識別欄同一個 x 範圍，若不排除會被誤判成資料列）
        if line and min(w.y0 for w in line) <= header_bottom + 1.0:
            continue
        # 跳過頁尾（頁碼＋「Ch. NN」，實測 y≈834，x≈60~65，剛好也落在識別欄
        # 左緣，會被誤判成延續統計號列——已用實跑資料驗證過這個坑）
        if line and min(w.y0 for w in line) >= TABLE_FOOTER_Y:
            continue

        # 章名偵測（獨立於列解析，遇到就記錄，不影響 cur_record）
        line_text = "".join(w.text for w in line)
        if m := CHAPTER_RE.search(line_text):
            ch = int(m.group(1) or m.group(2))
            state.cur_chapter = ch

        id_words = [w for w in line if zone_of(w, state.zone_map) == "identifier"]
        if not id_words:
            if state.cur_record is not None:
                populate_zones(state.cur_record, line, state.zone_map, is_new=False)
            continue

        code, stat, cd = split_code_stat_cd(id_words)
        if code and CODE8_RE.match(code) and stat and cd:
            rec = new_leaf(state, code, stat, cd, page_no, volume)
            state.cur_8 = code
        elif code and CODE8_RE.match(code) and not stat:
            h = new_heading(state, code, 8, None, page_no, volume)
            rec = h
            state.cur_8 = code
        elif code and CODE6_RE.match(code) and not stat:
            h = new_heading(state, code, 6, None, page_no, volume)
            rec = h
            state.cur_h6 = code
            state.cur_8 = None
        elif code and CODE4_RE.match(code) and not stat:
            h = new_heading(state, code, 4, None, page_no, volume)
            rec = h
            state.cur_h4 = code
            state.cur_h6 = None
            state.cur_8 = None
        elif stat and cd and not code:
            if state.cur_8 is None:
                state.warnings.append(
                    f"[warn] {volume} 頁{page_no}：延續統計號列 {stat}.{cd} 但無開放中的 8 碼字頭，略過"
                )
                continue
            rec = new_leaf(state, state.cur_8, stat, cd, page_no, volume)
        else:
            state.warnings.append(
                f"[warn] {volume} 頁{page_no}：識別欄內容無法辨識 {ident_text_of(id_words)!r}，略過"
            )
            continue

        populate_zones(rec, line, state.zone_map, is_new=True)
        state.cur_record = rec


# ---------- 規定代號附錄解析 ----------

APPENDIX_MID_X = 300.0    # 中文欄(含代號)在 x<300，英文欄在 x>=300（英文欄實測起點約 335）；
                           # 兩者不是逐行對齊——中文說明通常一行寫完，英文說明常常要拆
                           # 好幾行，所以「新條目」要看左欄，不能假設同一行左右一定同屬一條。
APPENDIX_CODE_COL_X = (75.0, 105.0)  # 代號本身固定印在這個窄 x 範圍（實測 90~92）。
                           # 只看「x<300」還不夠：頁尾頁碼（x~65，貼頁面底部）跟內文裡
                           # 偶爾單獨換行的數字（如「不超過 6 公斤」的「6」，x~118）
                           # 也會落在 x<300，若只憑數字形狀判斷會被誤認成新條目、
                           # 把正確條目從中間打斷——所以「是不是新條目」除了形狀比對，
                           # 還要求那個字剛好落在代號欄本身的 x 位置。
APPENDIX_CODE_SHAPE_RE = re.compile(r"^([A-Z]{1,3}\d{1,3}|\d{2,4}|\(空白\))$")
# 附錄的規定代號其實有兩種格式混在一起：純數字（111、364...）跟英數混合
# （B01、F01、MW0、C02、S01...），一開始只認純數字漏掉了整批英數代號
# （已用實跑資料驗證：B01/F01/MW0 等在稅則表格裡大量被引用，但附錄卻查無
# 定義，才發現漏抓的是這一批）。


APPENDIX_TOP_Y = 100.0     # 每頁重複出現的「Imp./輸入」或「Exp./輸出」跑頭，位置在頁碼
                            # 靠左或靠右會依單雙頁交替，x 座標因此不可靠，只能用 y 排除。
APPENDIX_BOTTOM_Y = 800.0   # 頁尾頁碼；同樣因為左右交替，只能用 y 排除，不能用 x。


def process_appendix_page(direction: str, page_no: int, page_width: float, lines: list,
                           volume: str, out: list, state_holder: dict):
    for line in lines:
        if line and (line[0].y0 < APPENDIX_TOP_Y or line[0].y0 > APPENDIX_BOTTOM_Y):
            continue
        left = [w for w in line if w.x0 < APPENDIX_MID_X]
        right = [w for w in line if w.x0 >= APPENDIX_MID_X]
        left_text = "".join(w.text for w in left).strip()
        right_text = " ".join(w.text for w in right).strip()
        if not left_text and not right_text:
            continue

        is_new_entry = (
            len(left) >= 1
            and APPENDIX_CODE_COL_X[0] <= left[0].x0 <= APPENDIX_CODE_COL_X[1]
            and APPENDIX_CODE_SHAPE_RE.match(left[0].text)
        )
        if is_new_entry:
            code = left[0].text
            zh_start = "".join(w.text for w in left[1:])  # 極少數代號跟中文同行的情況
            entry = {
                "direction": direction, "code": code,
                "desc_zh": zh_start, "desc_en": right_text,
                "source_volume": volume, "source_page": page_no,
            }
            out.append(entry)
            state_holder["cur"] = entry
        else:
            cur = state_holder.get("cur")
            if cur is None:
                continue  # 附錄開頭的標題列（如「(二) 輸入規定代號說明」），還沒進入條目本文
            if left_text:
                cur["desc_zh"] += left_text
            if right_text:
                cur["desc_en"] += (" " if cur["desc_en"] else "") + right_text


# ---------- 頁面分類與整體驅動 ----------

def classify_and_process(pdf_path: str, volume: str, chunk_size: int,
                          state: BuildState, reg_codes_out: list):
    total_pages = pdf_page_count(pdf_path)
    mode = "front_matter"
    appendix_state = {"cur": None}
    n_table_pages = 0
    n_import_pages = 0
    n_export_pages = 0

    for page_no, page_width, words in iter_pages_numbered(pdf_path, 1, total_pages, chunk_size):
        lines = group_lines(words)
        text_joined = "".join(w.text for line in lines for w in line)
        has_header = find_header_in_lines(lines) is not None

        # 表格頁的判斷只看「這一頁自己有沒有表頭」，不能沿用前一頁的 mode——
        # 每章之間夾著 1~2 頁純文字的類註/章註（沒有表頭），若誤把這種頁面當
        # 表格繼續解析，章註裡的法條文字（配額規定、自由貿易協定條款等）會被
        # 誤判成稅則列，混進資料庫（已用實跑資料驗證過這個坑：例如第 87、281、
        # 291 頁這類章註頁曾經生出格式錯誤的 CCC 號）。
        if "輸入規定代號說明" in text_joined or "Instruction of Import Regulation" in text_joined:
            mode = "import_appendix"
            appendix_state = {"cur": None}
        elif "輸出規定代號說明" in text_joined or "Instruction of Export Regulation" in text_joined:
            mode = "export_appendix"
            appendix_state = {"cur": None}
        elif has_header:
            mode = "table"
        elif mode not in ("import_appendix", "export_appendix"):
            mode = "other"  # 章註/目錄/其他非表格頁——不解析，也不讓它污染下一頁的判斷

        if has_header:
            process_table_page(state, page_no, page_width, lines, volume)
            n_table_pages += 1
        elif mode == "import_appendix":
            process_appendix_page("import", page_no, page_width, lines, volume,
                                   reg_codes_out, appendix_state)
            n_import_pages += 1
        elif mode == "export_appendix":
            process_appendix_page("export", page_no, page_width, lines, volume,
                                   reg_codes_out, appendix_state)
            n_export_pages += 1

        if page_no % 50 == 0 or page_no == total_pages:
            print(f"[build] {volume} 頁 {page_no}/{total_pages} 完成，"
                  f"累計 leaf {sum(1 for r in state.lines_out if r.get('kind')=='leaf')} 筆")

    print(f"[build] {volume} 完成：表格頁 {n_table_pages}、輸入規定頁 {n_import_pages}、"
          f"輸出規定頁 {n_export_pages}")


def preflight_volume(pdf_path: str, label: str, expected_dims: tuple | None):
    """回傳這一冊一般內容頁的 (width, height)，並做基本檢查。"""
    total = pdf_page_count(pdf_path)
    probe_page = min(6, total)
    dims = None
    for page_no, page_width, words in iter_pages_numbered(pdf_path, probe_page, probe_page, 1):
        if words:
            dims = (page_width, max(w.y1 for w in words))
        break
    if dims is None:
        raise SystemExit(f"[build] {label} 前置檢查失敗：第 {probe_page} 頁抓不到任何文字。")
    if expected_dims is not None:
        if abs(dims[0] - expected_dims[0]) > 2:
            raise SystemExit(
                f"[build] {label} 頁面寬度與上冊不符（{dims[0]:.1f} vs {expected_dims[0]:.1f}），"
                "版面可能不同，已中止，需人工確認後再繼續。"
            )
    # 上冊目錄＋規定代號附錄佔了前 77 頁，第一個稅則表頭要到第 78 頁才出現，
    # 因此掃描範圍要夠寬，不能只看前 30 頁。
    scan_last = min(150, total)
    found_header = False
    for page_no, page_width, words in iter_pages_numbered(pdf_path, 1, scan_last, 30):
        if find_header_in_lines(group_lines(words)):
            found_header = True
            break
    if not found_header:
        raise SystemExit(f"[build] {label} 前 {scan_last} 頁未偵測到稅則表頭，無法確認版面，已中止。")
    print(f"[build] {label} 前置檢查通過：頁面尺寸 {dims}，稅則表頭偵測正常。")
    return dims


# ---------- 資料庫建置 ----------

SCHEMA_SQL = """
CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE regulation_codes (
    direction  TEXT NOT NULL CHECK (direction IN ('import','export')),
    code       TEXT NOT NULL,
    desc_zh    TEXT, desc_en TEXT,
    source_volume TEXT NOT NULL, source_page INTEGER NOT NULL,
    PRIMARY KEY (direction, code)
);

CREATE TABLE tariff_headings (
    code TEXT PRIMARY KEY,
    level INTEGER NOT NULL,
    name_zh TEXT, name_en TEXT,
    parent_code TEXT,
    chapter INTEGER NOT NULL,
    source_volume TEXT NOT NULL, source_page INTEGER NOT NULL
);
CREATE INDEX idx_headings_parent ON tariff_headings(parent_code);
CREATE INDEX idx_headings_chapter ON tariff_headings(chapter);

CREATE TABLE tariff_lines (
    ccc_code_digits TEXT PRIMARY KEY,
    ccc_code_display TEXT NOT NULL,
    heading_code TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    name_zh TEXT, name_en TEXT, unit TEXT,
    rate1 TEXT, rate2 TEXT, rate3 TEXT, cr TEXT,
    import_reg_codes TEXT,
    export_reg_codes TEXT,
    source_volume TEXT NOT NULL, source_page INTEGER NOT NULL,
    raw_stat_no TEXT, raw_check_digit TEXT
);
CREATE INDEX idx_lines_heading ON tariff_lines(heading_code);
CREATE INDEX idx_lines_chapter ON tariff_lines(chapter);
"""


def detect_search_mode(con: sqlite3.Connection) -> str:
    try:
        con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE _probe")
        return "fts5_trigram"
    except sqlite3.OperationalError:
        return "like_fallback"


def write_db(db_path: str, upper_state: BuildState, lower_state: BuildState,
             reg_codes: list, meta: dict) -> str:
    building_path = db_path + ".building"
    if os.path.exists(building_path):
        os.remove(building_path)
    con = sqlite3.connect(building_path)
    con.executescript(SCHEMA_SQL)

    all_headings = {}
    all_headings.update(upper_state.headings_out)
    all_headings.update(lower_state.headings_out)
    con.executemany(
        "INSERT OR REPLACE INTO tariff_headings "
        "(code, level, name_zh, name_en, parent_code, chapter, source_volume, source_page) "
        "VALUES (:code,:level,:name_zh,:name_en,:parent_code,:chapter,:source_volume,:source_page)",
        list(all_headings.values()),
    )

    all_lines = upper_state.lines_out + lower_state.lines_out
    for rec in all_lines:
        rec["import_reg_codes"] = ",".join(rec["import_reg_codes"])
        rec["export_reg_codes"] = ",".join(rec["export_reg_codes"])
    con.executemany(
        "INSERT OR REPLACE INTO tariff_lines "
        "(ccc_code_digits, ccc_code_display, heading_code, chapter, name_zh, name_en, unit, "
        " rate1, rate2, rate3, cr, import_reg_codes, export_reg_codes, "
        " source_volume, source_page, raw_stat_no, raw_check_digit) "
        "VALUES (:ccc_code_digits,:ccc_code_display,:heading_code,:chapter,:name_zh,:name_en,:unit,"
        " :rate1,:rate2,:rate3,:cr,:import_reg_codes,:export_reg_codes,"
        " :source_volume,:source_page,:raw_stat_no,:raw_check_digit)",
        all_lines,
    )

    con.executemany(
        "INSERT OR REPLACE INTO regulation_codes "
        "(direction, code, desc_zh, desc_en, source_volume, source_page) "
        "VALUES (:direction,:code,:desc_zh,:desc_en,:source_volume,:source_page)",
        reg_codes,
    )

    search_mode = detect_search_mode(con)
    if search_mode == "fts5_trigram":
        con.execute(
            "CREATE VIRTUAL TABLE tariff_lines_fts USING fts5("
            "name_zh, name_en, content='tariff_lines', content_rowid='rowid', tokenize='trigram')"
        )
        con.execute(
            "INSERT INTO tariff_lines_fts(rowid, name_zh, name_en) "
            "SELECT rowid, name_zh, name_en FROM tariff_lines"
        )
    else:
        con.execute(
            "CREATE TABLE tariff_lines_like_idx (ccc_code_digits TEXT, field TEXT, text_norm TEXT)"
        )
        con.execute("CREATE INDEX idx_like_text ON tariff_lines_like_idx(text_norm)")
        rows = []
        for rec in all_lines:
            if rec.get("name_zh"):
                rows.append((rec["ccc_code_digits"], "zh", rec["name_zh"]))
            if rec.get("name_en"):
                rows.append((rec["ccc_code_digits"], "en", rec["name_en"].lower()))
        con.executemany(
            "INSERT INTO tariff_lines_like_idx (ccc_code_digits, field, text_norm) VALUES (?,?,?)",
            rows,
        )

    meta["search_mode"] = search_mode
    con.executemany("INSERT OR REPLACE INTO build_meta (key, value) VALUES (?,?)",
                     list(meta.items()))
    con.commit()
    con.close()
    return building_path


# ---------- 驗證 ----------

def _r(results, level, rule_id, name, detail):
    results.append({"level": level, "rule": rule_id, "name": name, "detail": detail})


def verify(db_path: str) -> list:
    r = []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    for vol in ("upper", "lower"):
        rows = con.execute(
            "SELECT MIN(chapter), MAX(chapter), COUNT(DISTINCT chapter), COUNT(*) "
            "FROM tariff_lines WHERE source_volume=?", (vol,)
        ).fetchone()
        if rows[3] == 0:
            _r(r, FAIL, "V1", f"雙冊涵蓋（{vol}）", f"{vol} 冊沒有任何稅則列，建置可能失敗")
        else:
            _r(r, PASS, "V1", f"雙冊涵蓋（{vol}）",
               f"{vol} 冊涵蓋第 {rows[0]}–{rows[1]} 章（{rows[2]} 個章有資料），共 {rows[3]} 筆")

    zero_chapters = con.execute(
        "SELECT DISTINCT chapter FROM tariff_headings WHERE level=2 "
        "AND chapter NOT IN (SELECT DISTINCT chapter FROM tariff_lines)"
    ).fetchall()
    if zero_chapters:
        chs = ",".join(str(c[0]) for c in zero_chapters)
        _r(r, WARN, "V2", "章節涵蓋", f"以下章節偵測到章名但沒有稅則列：{chs}（可能為保留章或偵測誤差）")
    else:
        _r(r, PASS, "V2", "章節涵蓋", "所有偵測到的章都至少有一筆稅則列")

    # 注意：實測 bbox 座標（page idx 81／pdftotext -f 82）確認老虎列的 B01 落在
    # 輸入(Imp.)欄、輸出(Exp.)欄是空的——跟這份計畫最早用純文字目視猜測的方向
    # 相反，這裡以座標驗證過的結果為準。
    tiger = con.execute(
        "SELECT import_reg_codes, export_reg_codes FROM tariff_lines "
        "WHERE heading_code='0106.19.40' AND raw_stat_no='11' AND raw_check_digit='1'"
    ).fetchone()
    if tiger and "B01" in (tiger[0] or "") and not (tiger[1] or "").strip():
        _r(r, PASS, "V3", "保育類動物範例（老虎）", "0106.19.40.11.1 輸入規定含 B01、輸出規定為空，符合預期")
    else:
        _r(r, FAIL, "V3", "保育類動物範例（老虎）", f"預期輸入規定含 B01、輸出規定為空，實際：{dict(tiger) if tiger else None}")

    elephant = con.execute(
        "SELECT export_reg_codes, import_reg_codes FROM tariff_lines "
        "WHERE heading_code='0106.19.40' AND raw_stat_no='15' AND raw_check_digit='7'"
    ).fetchone()
    if elephant and "B01" in (elephant[0] or "") and "B01" in (elephant[1] or ""):
        _r(r, PASS, "V4", "保育類動物範例（象）", "0106.19.40.15.7 輸入輸出規定皆含 B01，符合預期")
    else:
        _r(r, FAIL, "V4", "保育類動物範例（象）", f"預期輸入輸出皆含 B01，實際：{dict(elephant) if elephant else None}")

    gun = con.execute(
        "SELECT desc_zh, desc_en FROM regulation_codes WHERE direction='import' AND code='364'"
    ).fetchone()
    if gun and ("槍" in (gun[0] or "") or "gun" in (gun[1] or "").lower()):
        _r(r, PASS, "V5", "槍枝規定範例", "輸入規定代號 364 說明含「槍」/「gun」，符合預期")
    else:
        _r(r, FAIL, "V5", "槍枝規定範例", f"查無或說明不含槍枝字樣：{dict(gun) if gun else None}")

    for direction in ("import", "export"):
        cnt = con.execute("SELECT COUNT(*) FROM regulation_codes WHERE direction=?", (direction,)).fetchone()[0]
        blank = con.execute(
            "SELECT desc_zh, desc_en FROM regulation_codes WHERE direction=? AND code='(空白)'", (direction,)
        ).fetchone()
        if cnt > 50 and blank and ("免" in (blank[0] or "") or "permit" in (blank[1] or "").lower()):
            _r(r, PASS, "V6", f"附錄完整性（{direction}）", f"{cnt} 筆規定代號，含「(空白)＝免證」定義")
        else:
            _r(r, WARN if cnt > 0 else FAIL, "V6", f"附錄完整性（{direction}）",
               f"{cnt} 筆規定代號，(空白)定義={'有' if blank else '缺'}")

    orphan_count = 0
    for row in con.execute("SELECT import_reg_codes, export_reg_codes FROM tariff_lines"):
        for direction, codes in (("import", row[0]), ("export", row[1])):
            for c in (codes or "").split(","):
                c = c.strip()
                if not c or c == "(空白)":
                    continue
                hit = con.execute(
                    "SELECT 1 FROM regulation_codes WHERE direction=? AND code=?", (direction, c)
                ).fetchone()
                if not hit:
                    orphan_count += 1
    if orphan_count == 0:
        _r(r, PASS, "V7", "交叉參照完整性", "稅則列引用的規定代號都能在附錄找到定義")
    else:
        _r(r, WARN, "V7", "交叉參照完整性", f"{orphan_count} 個規定代號引用在附錄找不到對應定義，可能是解析誤差")

    bad_digits = con.execute(
        "SELECT COUNT(*) FROM tariff_lines WHERE length(ccc_code_digits) != 11 "
        "OR ccc_code_digits GLOB '*[^0-9]*'"
    ).fetchone()[0]
    if bad_digits == 0:
        _r(r, PASS, "V8", "schema 健全性", "所有 ccc_code_digits 皆為 11 碼純數字")
    else:
        _r(r, FAIL, "V8", "schema 健全性", f"{bad_digits} 筆 ccc_code_digits 不是 11 碼純數字")

    con.close()
    return r


def print_verify_report(results: list) -> bool:
    print("=" * 66)
    print("  稅則資料庫建置驗證")
    print("=" * 66)
    for c in results:
        print(f"  {ICON[c['level']]} [{c['level']}] {c['rule']}  {c['name']}")
        print(f"              {c['detail']}")
    n = {k: sum(1 for c in results if c["level"] == k) for k in (FAIL, WARN, PASS, SKIP)}
    print("-" * 66)
    print(f"  共 {len(results)} 項｜FAIL {n[FAIL]}｜WARN {n[WARN]}｜PASS {n[PASS]}｜SKIP {n[SKIP]}")
    print("=" * 66)
    return n[FAIL] == 0


# ---------- 主流程 ----------

def main():
    args = sys.argv[1:]
    if "--verify-only" in args:
        args.remove("--verify-only")
        db_path = DEFAULT_DB
        if "--db" in args:
            db_path = args[args.index("--db") + 1]
        if not os.path.exists(db_path):
            raise SystemExit(f"[build] --verify-only 但找不到資料庫：{db_path}")
        ok = print_verify_report(verify(db_path))
        sys.exit(0 if ok else 1)

    db_path = DEFAULT_DB
    if "--db" in args:
        db_path = args[args.index("--db") + 1]
    chunk_size = DEFAULT_CHUNK
    if "--chunk-size" in args:
        chunk_size = int(args[args.index("--chunk-size") + 1])

    positional = [a for a in args if not a.startswith("--")]
    # 過濾掉被 --db/--chunk-size 消耗掉的值
    skip_next = False
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--db", "--chunk-size"):
            i += 2
            continue
        if not a.startswith("--"):
            positional.append(a)
        i += 1

    upper_pdf = positional[0] if len(positional) > 0 else DEFAULT_UPPER
    lower_pdf = positional[1] if len(positional) > 1 else DEFAULT_LOWER

    for label, path in (("上冊", upper_pdf), ("下冊", lower_pdf)):
        if not os.path.exists(path):
            raise SystemExit(f"[build] 找不到{label} PDF：{path}")

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"[build] 上冊：{upper_pdf}")
    print(f"[build] 下冊：{lower_pdf}")
    print(f"[build] 輸出：{db_path}（chunk-size={chunk_size}）")

    upper_dims = preflight_volume(upper_pdf, "上冊", None)
    preflight_volume(lower_pdf, "下冊", upper_dims)

    reg_codes = []
    upper_state = BuildState("upper")
    classify_and_process(upper_pdf, "upper", chunk_size, upper_state, reg_codes)

    lower_state = BuildState("lower")
    classify_and_process(lower_pdf, "lower", chunk_size, lower_state, reg_codes)

    for st in (upper_state, lower_state):
        for w in st.warnings[:20]:
            print(w)
        if len(st.warnings) > 20:
            print(f"[build] {st.volume} 還有 {len(st.warnings) - 20} 則警告未列出")

    meta = {
        "schema_version": "1",
        "upper_pdf_path": upper_pdf, "upper_pdf_sha256": sha256_of(upper_pdf),
        "upper_pdf_pages": str(pdf_page_count(upper_pdf)),
        "lower_pdf_path": lower_pdf, "lower_pdf_sha256": sha256_of(lower_pdf),
        "lower_pdf_pages": str(pdf_page_count(lower_pdf)),
    }
    building_path = write_db(db_path, upper_state, lower_state, reg_codes, meta)

    print(f"[build] 建置完成（暫存於 {building_path}），開始驗證...")
    results = verify(building_path)
    ok = print_verify_report(results)

    con = sqlite3.connect(building_path)
    con.execute("INSERT OR REPLACE INTO build_meta (key, value) VALUES ('verify_status', ?)",
                ("ok" if ok else "warnings",))
    con.commit()
    con.close()

    if not ok:
        raise SystemExit(
            f"[build] 驗證發現 FAIL，保留 {building_path} 供檢查，"
            f"不會覆蓋既有的 {db_path}。"
        )

    os.replace(building_path, db_path)
    print(f"[build] 驗證通過，已寫入 {db_path}")
    sys.exit(0)


if __name__ == "__main__":
    main()
