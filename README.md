# cmra — Deterministic extraction of government PDF report tables

Extracts structured rows from *"List of Reports Which It Is the Duty of Any
Officer or Department to Make to Congress"* (House Document
[CDOC-119hdoc4](data/CDOC-119hdoc4.pdf)) into clean, typed records.

Each row is extracted into four fields:

| Field | Example |
|---|---|
| `reporting_entity` | Government Accountability Office |
| `nature_of_report` | Review of Congressional Award Foundation audit |
| `authority` | 2 U.S.C. 807(b); Pub. L. 96-114, title I, Sec. 107 (as amended by Pub. L. 101-525, Sec. 8); (104 Stat. 2308) |
| `when_expected` | Not later than 180 days after the date on which the audit is received |

## Why this is hard

The source PDF has three properties that defeat conventional extraction:

1. **The text layer is correct but garbled by rendering artifacts** — text is
   drawn through a 90°-rotated text matrix, so naive extraction yields reversed
   strings (`SSERGNOC`) and bold is simulated by double-stamping each glyph.
2. **OCR would only add noise** — the embedded text is already correct; OCRing
   the raster would trade known, systematic distortions for unpredictable ones.
3. **There is no machine-readable grid** — column boundaries are implied by
   spatial position, not cell borders, so Tabula/Camelot find no table.

The approach is **deterministic first**: every step that can be rule-based is.
See [approach.md](docs/approach.md) for the full pipeline design.

## Install

Uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# Extract to stdout (JSON Lines, one record per row)
uv run cmra data/CDOC-119hdoc4.pdf

# Other formats and an output file
uv run cmra data/CDOC-119hdoc4.pdf --format csv  -o reports.csv
uv run cmra data/CDOC-119hdoc4.pdf --format json -o reports.json
```

The full document extracts to **3,297 rows**.

## Accuracy

Validated by an LLM-as-judge audit: a random sample of rows is rendered against
the source page and judged by three independent vision models (Claude, Gemini,
OpenAI), with every disagreement adjudicated by hand against the PDF.

- **Sample:** 300 of 3,297 rows (9.1%), three judges
- **Confirmed extraction errors:** 0
- **Result:** 300/300 correct on the three within-page fields
  (`nature_of_report`, `authority`, `when_expected`)
- **≥99% field-level accuracy at 95% confidence** (one-sided rule-of-three:
  `1 − 0.05^(1/300) ≈ 0.99%` upper bound on the error rate)

Every judge "failure" was a false positive — most commonly a judge "correcting"
a typo that exists verbatim in the source document (e.g. `"the the"`,
`"Biannnually"`, `"recieved"`), or hallucinating a citation difference. The
extractor reproduces the source faithfully, typos included.

> Note on judges: for this citation-dense task, **Claude Sonnet 4.5** and
> **Gemini 2.5 Flash** were reliable (~98–99% agreement with ground truth);
> **gpt-4o-mini** was not (88%, with incoherent reasoning) and should not be
> used as a serious judge here.

## Tests

```bash
uv run pytest
```

21 tests: exact-value checks against a hand-verified 46-row fixture
(`tests/test_doc.pdf`), structural invariants, and regression tests against the
full PDF (every authority must close with `)`; no single-word orphan rows).

## Layout

| Path | Purpose |
|---|---|
| `extraction/extract.py` | The deterministic extraction pipeline (core) |
| `extraction/schema.py` | The `Report` Pydantic model |
| `extraction/main.py` | CLI entry point (`cmra`) |
| `extraction/verify.py` | Page-tracked extraction + seeded sampling for verification |
| `extraction/verify_report.py` | Renders an HTML report with PDF page images for manual QA |
| `extraction/judge.py` | LLM-as-judge harness (Claude / Gemini / OpenAI) |
| `approach.md` | Technical design document |
| `data/` | Source PDF |
| `tests/` | Test suite + fixtures |

### Validation harness

```bash
# Render a manual-QA HTML report (writes verify_output/, git-ignored)
uv run python extraction/verify_report.py

# Run the LLM-judge audit (needs API keys in .env; see judge.py header)
uv run python extraction/judge.py --samples 300 --judges claude,gemini
```

`verify_output/` (rendered images, HTML report, raw judge verdicts) is
generated and git-ignored — everything in it is reproducible from the two
commands above.
