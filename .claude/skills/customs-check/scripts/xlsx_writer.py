"""
最小可用的 XLSX 寫出工具，只用標準函式庫（zipfile + xml），不依賴
openpyxl 之類的第三方套件——本專案的環境限制是「僅使用標準函式庫」
（見 CLAUDE.md）。

XLSX 本質上是一個依 OOXML 規格組成的 zip 檔；這裡只實作到「多分頁、
每格文字或數字」這個最小可用集合，不做樣式、公式、凍結窗格等——
這支工具的用途是把 staging 層的資料整理成方便人工檢視/交接的活頁簿，
不是報表排版引擎。

用法：
    from xlsx_writer import write_xlsx
    write_xlsx(path, [
        ("Notes", ["note"], [{"note": "..."}]),
        ("Shipment", ["case_id", "invoice_no", ...], [{"case_id": "...", ...}]),
        ...
    ])
"""
import zipfile
from xml.sax.saxutils import escape

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheet_overrides}
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
{sheet_entries}
</sheets>
</workbook>
"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rel_entries}
</Relationships>
"""


def _col_letter(idx: int) -> str:
    """0-indexed 欄位序號 → Excel 欄位字母（A, B, ..., Z, AA, ...）。"""
    idx += 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_xml(ref: str, value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        # bool 是 int 的子類別，要先擋掉，否則會被當數字寫出 0/1
        text = "TRUE" if value else "FALSE"
        return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _row_xml(row_idx: int, values: list) -> str:
    cells = "".join(_cell_xml(f"{_col_letter(i)}{row_idx}", v) for i, v in enumerate(values))
    return f'<row r="{row_idx}">{cells}</row>'


def _sheet_xml(fieldnames: list, rows: list) -> str:
    body = [_row_xml(1, fieldnames)]
    for i, row in enumerate(rows, start=2):
        body.append(_row_xml(i, [row.get(f, "") for f in fieldnames]))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>')


def write_xlsx(path, sheets: list):
    """sheets: [(sheet_name, fieldnames, rows), ...]，rows 是 dict 清單。
    只用第一列當表頭；缺欄位或值是 None/"" 的儲存格留空，不補值。"""
    sheet_overrides, sheet_entries, rel_entries = [], [], []
    sheet_files = {}
    for i, (name, fieldnames, rows) in enumerate(sheets, start=1):
        sheet_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        sheet_entries.append(f'<sheet name="{escape(name[:31])}" sheetId="{i}" r:id="rId{i}"/>')
        rel_entries.append(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>')
        sheet_files[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml(fieldnames, rows)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   _CONTENT_TYPES.format(sheet_overrides="\n".join(sheet_overrides)))
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheet_entries="\n".join(sheet_entries)))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS.format(rel_entries="\n".join(rel_entries)))
        for name, xml in sheet_files.items():
            z.writestr(name, xml)
