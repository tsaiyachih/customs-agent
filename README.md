# customs-agent

A prototype that turns customs clearance documents (commercial invoice,
packing list, bill of lading) into structured data, cross-checks them with
deterministic rules, and produces a draft exception report plus an ERP
staging/import file for human review. **It does not file customs
declarations automatically, and it is not tied to any specific ERP.**

Full architecture, every script's responsibilities, all generated files, the
complete rule list, and an honest list of known gaps: **[docs/OVERVIEW.md](docs/OVERVIEW.md)**
(Traditional Chinese).

## How it works

```
PDF(s) for one shipment (merged, or split into separate files)
    │  extract.py / run_case.py — rule-based text extraction (regex, no LLM)
    ▼
Structured JSON (invoice / packing list / transport document / parties)
    │  checker.py     — cross-document consistency (R0–R16, I1–I3)
    │  validate.py    — single-field format/range sanity (Decimal-exact)
    │  extract_crosscheck.py — free-text fields vs. a second, independent
    │                    model reading (only for vessel/port/party-name/
    │                    item-description style fields; see below)
    ▼
export_erp.py — READY / REVIEW_REQUIRED / BLOCKED + draft ERP staging files
    (only READY produces an "import-ready" CSV; everything else needs a human)
```

## Why this exists

Before filing, someone has to manually check that an invoice, packing list,
and bill of lading agree on numbers, dates, weights, and container counts —
fields scattered across several pages, often off by a small amount that's
easy to miss under time pressure. Mistakes usually surface only after the
container has sailed or arrived at port, and by then the fix is a detention
fee, a re-export, or a fine — not a five-minute edit. This project turns
that manual cross-check into a deterministic, repeatable procedure, and adds
an extraction step in front of it so the numbers being checked came from the
documents rather than someone re-typing them.

## Quick start

**Requirements**
- Python 3.11+ — the core scripts use only the standard library (see
  `requirements.txt`; the one dependency listed, `reportlab`, is only needed
  to regenerate the synthetic test PDFs in `fixtures/`, not to run anything
  against them).
- [poppler](https://poppler.freedesktop.org/) — provides the `pdftotext`
  command the extraction layer shells out to. This is a system package, not
  a pip package:
  - macOS: `brew install poppler`
  - Ubuntu/Debian: `sudo apt-get install poppler-utils`
- `tariff.db` (Taiwan customs tariff schedule lookup) is **optional**, not
  required — a fresh clone won't have it (it's a ~10MB derived database,
  intentionally excluded from version control). Without it, extraction,
  cross-document checking, and ERP-readiness decisions all work exactly the
  same; only the tariff-candidate lookup gracefully returns no candidates
  instead of erroring. See `docs/OVERVIEW.md` (§7) for how to rebuild it if
  you have the source tariff PDFs.

**Run it against a bundled example** (`fixtures/A_clean.pdf`, synthetic data):

```bash
python3 .claude/skills/customs-check/scripts/extract.py \
    fixtures/A_clean.pdf /tmp/example.json
python3 .claude/skills/customs-check/scripts/checker.py \
    /tmp/example.json --direction import
```

What you'll actually see (captured from a real, from-scratch run — nothing
edited out):

```
==================================================================
  報關文件交叉核對報表
==================================================================
  來源檔案      A_clean.pdf（text_layer_rules）
  申報方向      進口   運輸單據 B/L
  Invoice       NKS-2608-114   AUG.12.2026
  貿易條件      CIF   54,600.00 USD   運費 未載明 
  貨物          1,200 PCS   原產地 Japan
  Packing List  NKS-2608-114   3 櫃 / 1,200 件 / 16,440.00 KGS
------------------------------------------------------------------
  ✓ [PASS] R0  抽取完整性
              6 項核心欄位齊備
  ✓ [PASS] R1  文件編號一致
              Invoice=NKS-2608-114｜Packing List=NKS-2608-114
  ...  (R2–R12, R14, R15 all PASS — trimmed here for length)
  ! [WARN] R13 稅則號列
              供應商標示 HS 848180（出口國申報，僅為候選值）；台灣進口須申報 11 碼 CCC，後段仍須自行歸列並負申報責任。
  · [SKIP] R16 稅則規定代號複查
              未提供 confirmed_ccc_code（此欄位僅由人工確認後填入，非抽取產生）
------------------------------------------------------------------
  共 17 項｜不符 0｜待確認 1｜通過 15｜略過 1
  ※ 本報表為草稿輔助。送件前仍須報關人員逐項覆核，責任不因本工具而移轉。
==================================================================
```

(The report itself is in Traditional Chinese — that's the working language
of the tool, not a translation gap; see `docs/OVERVIEW.md` if you want the
architecture explained in the same language.)

To go further and produce the ERP staging files:

```bash
python3 .claude/skills/customs-check/scripts/export_erp.py \
    /tmp/example.json --direction import --outdir /tmp/example_out
```

There's also a multi-file case format (`run_case.py` instead of
`extract.py`, for when the invoice/packing-list/bill-of-lading are three
separate PDFs) — try it against `fixtures/E_multiitem/`. And the full
regression suite:

```bash
python3 tests/run_tests.py
python3 tests/test_validate.py
python3 tests/test_extract_crosscheck.py
python3 tests/test_export_erp.py
```

All four pass on a clean checkout with no `tariff.db` and no `pip install`
— this was verified end-to-end in a fresh `git clone`, not assumed.

## Architecture: where the deterministic line is drawn

This is the part of the design that matters most, so it gets its own
section even though the rest of the detail lives in `docs/OVERVIEW.md`.

**Every "are these two values the same?" decision is made by plain Python,
never by an LLM.** An LLM's numeric judgment is occasionally wrong and
doesn't know it's wrong — ask the same model to compare the same two
numbers twice and it isn't guaranteed to give the same answer both times.
That's disqualifying for a domain with legal filing consequences, so the
system is built around a hard boundary:

- **`checker.py`** — the only component with authority to decide whether
  values *match*: cross-document consistency (do the invoice, packing list,
  and B/L agree?) and per-line-item arithmetic. Pure, deterministic Python.
- **`validate.py`** — whether a single field's value is *plausible* on its
  own (Decimal-exact arithmetic, known currency/unit vocabularies, date
  ranges, CCC-code format). Also pure Python; doesn't look at other
  documents.
- **`extract_crosscheck.py`** — for the handful of free-text fields that
  have no format or arithmetic to check against (vessel name, port name,
  company names, item descriptions), an LLM is allowed to *read* a second,
  independent candidate value from the source text — but whether that
  candidate agrees with the rule-based extraction is still decided by plain
  string comparison in Python, not by the model asserting agreement. The
  model never overwrites a field; a human resolves any disagreement.

Rule-based extraction is the default because it's free, deterministic, and
fully reproducible — the same PDF always produces the same output, and a
wrong output can be traced to an exact line of regex. The model only gets
involved (a) as a fallback when rule-based extraction fails outright
(scanned pages, a garbled table), and (b) as a second opinion on fields that
have no other way to be checked. It never gets to be the sole arbiter of
whether a number is correct.

## Known limitations (unedited)

- **Two independently-wrong readings still look "consistent."**
  `extract_crosscheck.py` can only catch *disagreement* between the
  rule-based extraction and the model's reading. If the source document
  itself has a typo, or both extraction paths happen to misread the same
  field the same way, they agree — silently — and no issue is raised. A
  clean crosscheck result means "two independent methods produced the same
  value," not "this value is correct."
- **Re-running `run_case.py` on a case directory discards any human
  decisions already recorded for it** (confirmed tariff codes, resolved
  crosscheck disagreements) — it rebuilds the case JSON from the source
  documents from scratch rather than merging into what's already there.
  This is a known, accepted limitation, not a bug that's been overlooked.
- **`validate.py` and `extract_crosscheck.py` are only wired into the
  multi-file case path (`run_case.py`)** — the single merged-PDF path
  (`extract.py`) doesn't call either yet, so the two input paths currently
  produce different levels of scrutiny for otherwise-identical documents.
- **All automated tests run against synthetic, generator-produced
  documents** (`tests/make_fixtures.py`), covering roughly eight layout
  variants plus three reconstructed historical bugs — there is no
  large-sample or real-document accuracy measurement, and none of the
  metrics currently available (a 3-case regression catch rate, deterministic
  rule-violation counts, dual-path disagreement rate) are a substitute for
  one. See `docs/OVERVIEW.md` §7 for the full, unedited breakdown, including
  what it would take to get an actual accuracy number.

Full detail on every script, every generated file, the complete rule list
(R0–R16, I1–I3, and all 24 `case_issues` codes across the three validation
layers), and more known gaps: **[docs/OVERVIEW.md](docs/OVERVIEW.md)**.

## License

MIT — see [LICENSE](LICENSE).
