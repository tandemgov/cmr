# cmra — Who is complying with the Congressionally Mandated Reports Act?

Two subsystems, run in sequence.

**`extraction/` — the House Doc extractor.** Extracts structured rows from
*"List of Reports Which It Is the Duty of Any Officer or Department to Make to
Congress"* (House Document [CDOC-119hdoc4](data/CDOC-119hdoc4.pdf)) into
clean, typed records — 3,297 mandates. Deterministic: no LLM, no randomness.

**`pipeline/` — the GPO comparison.** Joins those mandates against the ~1,176
packages in GPO's Congressionally Mandated Reports collection, to ask which
required reports were actually filed, which are visibly overdue, and which
filings tie back to no known mandate at all. (The fetcher's `--since` is a
*last-modified* filter, not an issue date: 337 of the 1,176 were issued before
2024, and deposits began October 2023.)

The same directory also resolves each mandate's citation to the **actual
statutory text** — **96.2% of the Clerk's 3,297 claims**, via the current
Office of the Law Revision Counsel release point for codified law and
govinfo's PLAW collection for the uncodified third — so you can ask what a
mandate's authority really says rather than trusting the citation. See
[RUNBOOK §10](docs/RUNBOOK.md).

Start with **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — it covers the method, how
to reproduce the comparison, and the known seams. The rest of this file is
mostly the extractor; the Layout table at the bottom maps both halves.

## The extractor

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
uv run python extraction/main.py data/CDOC-119hdoc4.pdf

# Other formats and an output file
uv run python extraction/main.py data/CDOC-119hdoc4.pdf --format csv  -o reports.csv
uv run python extraction/main.py data/CDOC-119hdoc4.pdf --format json -o reports.json
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

> **Provenance.** This audit ran 2026-05-21, against the 3,250-row extract that
> preceded the current 3,297. That change was purely additive — all 3,250 rows
> survive unchanged (RUNBOOK §6) — so the result still stands for the rows it
> covered, but the 47 recovered rows were never judged. The raw verdicts live in
> `verify_output/`, which is gitignored, so a reader cannot check them without
> re-running the harness.

> Note on judges: for this citation-dense task, **Claude Sonnet 4.5** and
> **Gemini 2.5 Flash** were reliable (~98–99% agreement with ground truth);
> **gpt-4o-mini** was not (88%, with incoherent reasoning) and should not be
> used as a serious judge here.

## Tests

```bash
uv run pytest
```

That runs the whole suite — **284 tests** across ten modules. The extractor's
own share is `tests/test_extract.py` (21) plus `tests/test_extraction_invariants.py`
(7): exact-value checks against a hand-verified 46-row fixture (`tests/test_doc.pdf`),
structural invariants, and regression tests against the full PDF (every authority
must close with `)`; no single-word orphan rows).

## Layout

| Path | Purpose |
|---|---|
| `extraction/extract.py` | The deterministic extraction pipeline (core) |
| `extraction/schema.py` | The `Report` Pydantic model |
| `extraction/main.py` | CLI entry point (stdout / `--format` / `-o`) |
| `extraction/verify.py` | Page-tracked extraction + seeded sampling for verification |
| `extraction/verify_report.py` | Renders an HTML report with PDF page images for manual QA |
| `extraction/judge.py` | LLM-as-judge harness (Claude / Gemini / OpenAI) |
| `pipeline/` | The GPO/CMRA comparison — see [docs/RUNBOOK.md](docs/RUNBOOK.md) §7 for a per-module map |
| `pipeline/authority_parse.py` | Citation parser retaining the subsection path — see [RUNBOOK §10](docs/RUNBOOK.md) |
| `pipeline/statute_fetch.py` | US Code fetch + citation→statutory-text resolution |
| `pipeline/plaw_fetch.py` | Public-law text for the uncodified mandates |
| `pipeline/mandate_classify.py` | Finds reporting mandates the Clerk's list misses — see [RUNBOOK §12](docs/RUNBOOK.md) |
| `pipeline/destination.py` | Reads chamber-vs-committee destination from statutory text — see [RUNBOOK §13](docs/RUNBOOK.md) |
| `pipeline/scoped_compliance.py` | The obligation-screened compliance denominator and its brackets |
| `data/gold/mandate_gold.json` | 467-row adjudicated gold set for judging the judges |
| `experiments/dspy_judge.py` | Does an optimizer beat the handwritten judge prompt? — see [RUNBOOK §12](docs/RUNBOOK.md) |
| `docs/approach.md` | Technical design document (extractor) |
| `docs/RUNBOOK.md` | Reviewer runbook for the comparison pipeline |
| `deck/` | `slides.md` (Slidev) and the images it references |
| `data/` | Source PDF, plus `data/gold/` (adjudicated verdict sets) |
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
