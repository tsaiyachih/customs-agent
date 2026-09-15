"""
產生測試用文件組（合成資料，非真實客戶文件）。

三組：
  A  clean      — 完全一致，應全數通過
  B  faulty     — 植入多項錯誤，用來確認每條規則抓得到
  C  altlayout  — 版面與 A/B 不同，用來測試解析器的泛化能力

用法：
    python3 make_fixtures.py <輸出目錄>
"""

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4
MONO, BOLD = "Courier", "Courier-Bold"


def page(c, lines, size=9):
    y = H - 55
    for ln in lines:
        if ln.startswith("§"):
            c.setFont(BOLD, size + 2)
            c.drawString(60, y, ln[1:])
            c.setFont(MONO, size)
        else:
            c.setFont(MONO, size)
            c.drawString(60, y, ln)
        y -= 13
    c.showPage()


# ---------------------------------------------------------------- A: clean
A_INV = [
    "§NIKKO SEIKI CO., LTD.",
    "2-14-8 Shinkawa, Chuo-ku, Tokyo 104-0033, Japan",
    "",
    "§COMMERCIAL INVOICE",
    "",
    "INVOICE NO.: NKS-2608-114                       Date: AUG.12.2026",
    "MESSRS: UNI-LEAVES TRADING LTD.",
    "        11F.-3, No.48, Sec.2, Chongqing N. Rd., Taipei, Taiwan",
    "",
    "Ship by: FCL/SEA          From  YOKOHAMA, JAPAN",
    "Goods of origin: Japan    to    KAOHSIUNG, TAIWAN",
    "",
    "No.  Description                    Q'ty   Unit  Unit Price   Amount (USD)",
    "-" * 76,
    " 1   PNEUMATIC VALVE UNIT           1200   PCS       45.50      54,600.00",
    "     Model NV-220S, stainless body",
    "     HS Code: 848180",
    "",
    "     Terms of Delivery: CIF KAOHSIUNG",
    "-" * 76,
    "     Total:                         1200   PCS                 54,600.00",
]

A_PL = [
    "§NIKKO SEIKI CO., LTD.",
    "",
    "§Packing List",
    "",
    "MESSRS: UNI-LEAVES TRADING LTD.        INVOICE NO.: NKS-2608-114",
    "                                       Date: AUG.12.2026",
    "",
    "Ship by: FCL/SEA                       Total Package     3 CNTR",
    "Goods of origin: Japan",
    "",
    "  Description                  Q'ty  UNIT     N.W.(KGS)   G.W.(KGS)",
    "-" * 76,
    "  CNTR NO.:TEMU1234567",
    "  VALVE UNIT-400PKG             400   PCS       5200.00     5480.00",
    "",
    "  CNTR NO.:MSKU7654321",
    "  VALVE UNIT-400PKG             400   PCS       5150.00     5430.00",
    "",
    "  CNTR NO.:TCNU4455667",
    "  VALVE UNIT-400PKG             400   PCS       5250.00     5530.00",
    "-" * 76,
    "  Total : 1200 PKG                              15,600.00   16,440.00",
    "",
    "Shipping Mark : UNI-LEAVES / KHH / NO.1-1200",
]

A_BL = [
    "§OCEAN NETWORK EXPRESS",
    "",
    "§BILL OF LADING",
    "",
    "B/L NO.: ONEYTYOKA123456",
    "Shipper   : NIKKO SEIKI CO., LTD.",
    "Consignee : UNI-LEAVES TRADING LTD.",
    "",
    "Vessel : ONE HARBOUR V. 025E",
    "Port of Loading   : YOKOHAMA, JAPAN",
    "Port of Discharge : KAOHSIUNG, TAIWAN",
    "",
    "Container Nos.: TEMU1234567 / MSKU7654321 / TCNU4455667",
    "",
    "Description of Goods : PNEUMATIC VALVE UNIT",
    "Total : 1,200 PKGS",
    "GROSS WEIGHT : 16,440.00 KGS",
    "MEASUREMENT  : 58.400 CBM",
]

# ------------------------------------------------------------- B: faulty
B_INV = [
    "§SHANGHAI HENGDA IMP & EXP CO., LTD.",
    "No.1508 Pudong Ave., Shanghai, China",
    "",
    "§COMMERCIAL INVOICE",
    "",
    "INVOICE NO.: SHG-2608-77                        Date: AUG.05.2026",
    "MESSRS: UNI-LEAVES TRADING LTD.",
    "",
    "Ship by: FCL/SEA          From  SHANGHAI, CHINA",
    "Goods of origin: China    to    KAOHSIUNG, TAIWAN",
    "",
    "No.  Description                    Q'ty   Unit  Unit Price   Amount (CNY)",
    "-" * 76,
    " 1   GALVANIZED STEEL COIL          1000   PCS       32.50      33,500.00",
    "     Thickness 0.28mm, zinc coated",
    "",
    "     Terms of Delivery: EXW SHANGHAI",
    "-" * 76,
    "",
    "Freight Charge : 3,150.00 USD",
]

B_PL = [
    "§SHANGHAI HENGDA IMP & EXP CO., LTD.",
    "",
    "§Packing List",
    "",
    "MESSRS: UNI-LEAVES TRADING LTD.        INVOICE NO.: SHG-2608-7",
    "                                       Date: AUG.06.2026",
    "",
    "Ship by: FCL/SEA                       Total Package     4 CNTR",
    "Goods of origin: China",
    "",
    "  Description                  Q'ty  UNIT     N.W.(KGS)   G.W.(KGS)",
    "-" * 76,
    "  CNTR NO.:CSLU3344556",
    "  STEEL COIL-300PKG             300   PCS       4100.00     4260.00",
    "",
    "  CNTR NO.:CSNU9988776",
    "  STEEL COIL-300PKG             300   PCS       4050.00     3990.00",
    "",
    "  CNTR NO.:ABCD12345",
    "  STEEL COIL-300PKG             300   PCS       3850.00     4010.00",
    "-" * 76,
    "  Total : 950 PKG                               12,500.00   12,760.00",
    "",
    "Shipping Mark : N/M",
]

B_BL = [
    "§COSCO SHIPPING LINES",
    "",
    "§BILL OF LADING",
    "",
    "B/L NO.: COSU6712345670",
    "Shipper   : SHANGHAI HENGDA IMP & EXP CO., LTD.",
    "Consignee : UNI-LEAVES TRADING LTD.",
    "",
    "Vessel : COSCO PRIDE V. 118W",
    "Port of Loading   : SHANGHAI, CHINA",
    "Port of Discharge : KAOHSIUNG, TAIWAN",
    "",
    "Container Nos.: CSLU3344556 / CSNU9988776 / TGHU5566778",
    "",
    "Description of Goods : GALVANIZED STEEL COIL",
    "Total : 900 PKGS",
    "GROSS WEIGHT : 12,260.00 KGS",
]

# --------------------------------------------------------- C: alt layout
C_INV = [
    "§EUROTRADE B.V.",
    "Havenstraat 22, 3011 Rotterdam, The Netherlands",
    "",
    "§INVOICE",
    "",
    "Our reference .............. ETV/2026/0844",
    "Invoice date ............... AUG.20.2026",
    "Consignee .................. UNI-LEAVES TRADING LTD., Taipei",
    "Country of origin .......... Netherlands",
    "Delivery terms ............. CIF Kaohsiung",
    "",
    "Line   Article        Pieces      Price/pc      Line total (EUR)",
    "-" * 72,
    "0001   PUMP HOUSING      800          62,40           49.920,00",
    "-" * 72,
    "                                     TOTAL           49.920,00",
]

C_PL = [
    "§EUROTRADE B.V.",
    "",
    "§PACKING SPECIFICATION",
    "",
    "Reference .................. ETV/2026/0844",
    "Date ....................... AUG.20.2026",
    "Number of units ............ 2 containers",
    "",
    "Unit 1  ..  Container  MRKU 664 4550",
    "            Pieces 400   Net 6.150,00 kg   Gross 6.480,00 kg",
    "",
    "Unit 2  ..  Container  MRKU 771 2233",
    "            Pieces 400   Net 6.200,00 kg   Gross 6.530,00 kg",
    "-" * 72,
    "Grand total ...  800 pieces / 800 colli / net 12.350,00 / gross 13.010,00",
]


def build(outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    sets = {
        "A_clean.pdf": [A_INV, A_PL, A_BL],
        "B_faulty.pdf": [B_INV, B_PL, B_BL],
        "C_altlayout.pdf": [C_INV, C_PL],
        "D_export.pdf": [D_INV, D_PL, D_SO],
    }
    for name, pages in sets.items():
        c = canvas.Canvas(str(outdir / name), pagesize=A4)
        for p in pages:
            page(c, p)
        c.save()
        print(f"[fixtures] {outdir / name}")
    build_case_dirs(outdir)
    build_multiitem_case(outdir)


E_INV = [
    "§MEKONG DELTA OILS CO., LTD.",
    "45 Le Loi Street, Can Tho City, Viet Nam",
    "",
    "§COMMERCIAL INVOICE",
    "",
    "INVOICE NO.: MDO-2609-005                       Date: SEP.10.2026",
    "MESSRS: UNI-LEAVES TRADING LTD., Taipei, Taiwan",
    "",
    "Ship by: FCL/SEA          From  CAT LAI PORT, VIET NAM",
    "Goods of origin: Vietnam  to    KAOHSIUNG, TAIWAN",
    "",
    "No.  Description                    Q'ty   Unit  Unit Price   Amount (USD)",
    "-" * 76,
    " 1   SESAME OIL                     2000   KGS       3.2000      6,400.00",
    "     HS Code: 151550",
    "",
    " 2   PEANUT OIL                     1500   KGS       2.8000      4,200.00",
    "-" * 76,
    "  3                                 500   KGS       5.0000      2,500.00",
    "     HS Code: 151620",
    "-" * 76,
    "     Terms of Delivery: CIF KAOHSIUNG",
    "-" * 76,
    "     Total:                        4000   KGS                13,100.00",
]

E_PL = [
    "§MEKONG DELTA OILS CO., LTD.",
    "",
    "§Packing List",
    "",
    "MESSRS: UNI-LEAVES TRADING LTD.        INVOICE NO.: MDO-2609-005",
    "                                       Date: SEP.10.2026",
    "",
    "Ship by: FCL/SEA                       Total Package     1 CNTR",
    "Goods of origin: Vietnam",
    "",
    "  Description                  Q'ty UNIT      N.W.(KGS)   G.W.(KGS)",
    "-" * 76,
    "  CNTR NO.:TCLU7788990",
    "  MIXED EDIBLE OILS-90PKG      500   KGS       3900.00     4200.00",
    "-" * 76,
    "  Total : 90 PKG                                3,900.00    4,200.00",
    "",
    "Shipping Mark : N/M",
]

E_BL = [
    "§PACIFIC OCEAN LINE",
    "",
    "§BILL OF LADING",
    "",
    "B/L NO.: POLVN490312233",
    "Shipper   : MEKONG DELTA OILS CO., LTD.",
    "Consignee : UNI-LEAVES TRADING LTD.",
    "",
    "Vessel : PACIFIC STAR V. 212N",
    "Port of Loading   : CAT LAI PORT, VIET NAM",
    "Port of Discharge : KAOHSIUNG, TAIWAN",
    "",
    "Container Nos.: TCLU7788990",
    "",
    "Total : 90 PKGS",
    "GROSS WEIGHT : 4,200.00 KGS",
]


def build_multiitem_case(outdir: Path):
    """E_multiitem/：乾淨（無浮水印雜訊）的合成多品項案例，三個獨立檔案。
    3 個品項：品項 1 有供應商 HS、品項 2 沒有、品項 3 刻意讓品名抓不到
    （測 I2）；用來穩定測試多品項 staging／稅則候選／I1-I3，跟真實但
    含浮水印雜訊的 fixtures/GD-2609-003（見 tests/EXPECTED.md）分工不同：
    這組保證乾淨可預測，GD-2609-003 展示真實文件的抽取挑戰。"""
    case_dir = outdir / "E_multiitem"
    case_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "commercial_invoice.pdf": [E_INV],
        "packing_list.pdf": [E_PL],
        "bill_of_lading.pdf": [E_BL],
    }
    for name, pages in files.items():
        c = canvas.Canvas(str(case_dir / name), pagesize=A4)
        for p in pages:
            page(c, p)
        c.save()
        print(f"[fixtures] {case_dir / name}")


def build_case_dirs(outdir: Path):
    """A_case/：跟 A_clean.pdf 完全同樣的內容，但存成三個獨立檔案而不是
    一份三頁 PDF——用來證明 run_case.py（分離檔案）跟 extract.py（合併
    PDF）這兩條輸入路徑，對同樣的內容會得到同樣的核對結果，不是分開
    維護的兩套邏輯。"""
    case_dir = outdir / "A_case"
    case_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "commercial_invoice.pdf": [A_INV],
        "packing_list.pdf": [A_PL],
        "bill_of_lading.pdf": [A_BL],
    }
    for name, pages in files.items():
        c = canvas.Canvas(str(case_dir / name), pagesize=A4)
        for p in pages:
            page(c, p)
        c.save()
        print(f"[fixtures] {case_dir / name}")




# ------------------------------------------------- D: export (TW -> JP)
D_INV = [
    "§UNI-LEAVES TRADING LTD.",
    "11F.-3, No.48, Sec.2, Chongqing N. Rd., Taipei 103608, Taiwan",
    "",
    "§COMMERCIAL INVOICE",
    "",
    "INVOICE NO.: ULT-2608-039                       Date: AUG.22.2026",
    "MESSRS: NIKKO SEIKI CO., LTD., Tokyo, Japan",
    "",
    "Ship by: FCL/SEA          From  KAOHSIUNG, TAIWAN",
    "Goods of origin: Taiwan   to    YOKOHAMA, JAPAN",
    "",
    "No.  Description                    Q'ty   Unit  Unit Price   Amount (USD)",
    "-" * 76,
    " 1   MACHINED ALUMINIUM BRACKET     2000   PCS       18.75      37,500.00",
    "     HS Code: 761699",
    "",
    "     Terms of Delivery: CIF YOKOHAMA",
    "-" * 76,
    "     Total:                         2000   PCS                 37,500.00",
]

D_PL = [
    "§UNI-LEAVES TRADING LTD.",
    "",
    "§Packing List",
    "",
    "MESSRS: NIKKO SEIKI CO., LTD.          INVOICE NO.: ULT-2608-039",
    "                                       Date: AUG.22.2026",
    "",
    "Ship by: FCL/SEA                       Total Package     2 CNTR",
    "Goods of origin: Taiwan",
    "",
    "  Description                  Q'ty  UNIT     N.W.(KGS)   G.W.(KGS)",
    "-" * 76,
    "  CNTR NO.:WHLU2233445",
    "  ALU BRACKET-500PKG           1000   PCS       8400.00     8760.00",
    "",
    "  CNTR NO.:WHLU6677889",
    "  ALU BRACKET-500PKG           1000   PCS       8350.00     8710.00",
    "-" * 76,
    "  Total : 1000 PKG                              16,750.00   17,470.00",
    "",
    "Shipping Mark : NIKKO / TYO / NO.1-1000",
]

D_SO = [
    "§WAN HAI LINES LTD.",
    "",
    "§SHIPPING ORDER",
    "",
    "S/O NO.: W42733",
    "Shipper   : UNI-LEAVES TRADING LTD.",
    "Consignee : NIKKO SEIKI CO., LTD.",
    "",
    "Vessel : WAN HAI 322 V. 041N",
    "Port of Loading   : KAOHSIUNG, TAIWAN",
    "Port of Discharge : YOKOHAMA, JAPAN",
    "Closing Time : AUG.26.2026 17:00",
    "",
    "Container Nos.: WHLU2233445 / WHLU6677889",
    "",
    "Total : 1,000 PKGS",
    "GROSS WEIGHT : 17,470.00 KGS",
]


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/fixtures"))
