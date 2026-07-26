# CMRA Comparison — Reviewer Runbook

A reviewer's guide to the GPO/CMRA comparison pipeline that lives alongside
the deterministic House Doc extractor. Read top-to-bottom on first arrival;
later, jump straight to the section you need.

## 1. What this project does (in 30 seconds)

The House Document `CDOC-119hdoc4` lists **every standing report Congress
requires** from federal entities — 3,297 mandate rows after extraction.
The 2022 Congressionally Mandated Reports Act (CMRA) created a public GPO
repository where executive-branch agencies must file copies of those reports
on an ongoing basis — about 1,057 packages since Jan 2024.

This pipeline **joins those two datasets** so you can ask:

- *Did agency X actually file the report that statute Y requires?*
- *Which mandated reports are visibly overdue?*
- *Which GPO submissions don't tie back to a known mandate (orphans)?*
- *What's the on-time rate of CMRA-filed reports?*

## 2. Five-minute reproduction

Prereqs: a `GOVINFO_API_KEY` in `.env` (free at
[api.data.gov](https://api.data.gov/signup/)), and the existing
`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` for the judge.

```bash
# One-time GPO catalog fetch (~6 min, idempotent, resumable)
uv run python pipeline/gpo_fetch.py

# Match deterministically (~5s)
uv run python pipeline/match.py

# Adjudicate the fuzzy candidate bucket with two LLMs (~10 min, ~$2)
uv run python pipeline/match_judge.py --judges claude,gemini --workers 8

# Produce the consolidated report
uv run python pipeline/compare.py

# Run the reviewer audit (systematic spot-checks)
uv run python pipeline/audit.py

# Read the headline summary + audit findings
open compare_output/REPORT.md compare_output/AUDIT.md
```

That is the v1 flow, and it is where this runbook originally stopped. The v2
matcher below addresses v1's recall problem — v1 leaves 824 of 1,057 filings
unmatched — and produces `scoped_compliance.json`, the source of the
compliance figures in `deck/slides.md`. The whitepaper predates it. Run it
after `compare.py`:

```bash
# v2 pass 1: filing-first LLM matching against the filer's House Doc slice
# (~1057 filings, resumable, LLM cost)
uv run python pipeline/match_v2.py

# v2 pass 2: corpus-wide rescue for pass-1 "none" verdicts (resumable)
uv run python pipeline/match_v2_pass2.py

# House Doc gaps: filings whose mandate is absent from the Clerk's list
uv run python pipeline/gap_report.py

# Compliance within CMRA's actual reach (obligation-screened denominator)
uv run python pipeline/scoped_compliance.py

open compare_output/housedoc_gaps.md compare_output/scoped_compliance.json
```

Pilot first if you are changing the matcher — `match_v2.py` takes
`--orphans-only --sample 25 --seed 42` for a cheap representative run, and
`--dry-run` to see the prompts without spending anything.

All intermediate files land under `data/gpo/` and `compare_output/` and are
gitignored. If you only want to rebuild the views without re-fetching,
`gpo_fetch.py --derive-only` is the move.

## 3. Pipeline architecture (one diagram)

```
data/CDOC-119hdoc4.pdf       govinfo.gov API (collection=CMR)
        │                            │
        ▼                            ▼
extraction/main.py           pipeline/gpo_fetch.py (~1057 pkgs)
        │                            │
        ▼                            ▼
data/cmra_extract.jsonl      data/gpo/packages/*.json
   (3,297 mandates)                  │
        │                            ▼ (derive)
        │                  data/gpo/submissions.jsonl  (one per pkg)
        │                  data/gpo/requirements.jsonl (deduped by req#)
        │                            │
        └────────────┬───────────────┘
                     ▼
                  match.py
   Stage A:  requirement record  ↔  mandate  (citation-anchored)
   Stage B1: package `references` ↔  mandate (citation-anchored fallback)
   Stage B2: package title+agency ↔  mandate (jaccard fallback)
                     │
                     ▼
       compare_output/candidates.jsonl  →  match_judge.py
                     │                          │  (Claude + Gemini)
                     │                          ▼
                     │           compare_output/match_judgments.jsonl
                     │                          │
                     └──────────┬───────────────┘
                                ▼
                             compare.py
                                │
                                ▼
                    compare_output/REPORT.md
                    compare_output/final_matches.jsonl
                    compare_output/overdue_mandates.jsonl
                    compare_output/{mandate,submission}_coverage.jsonl
                                │
   ─────────────────────────────┼─────────────────────────────  v2
                                ▼
                          match_v2.py  (pass 1)
   Each GPO filing is the query; the filer's House Doc slice plus
   government-wide rows goes to the LLM in one call. Deterministic
   signals corroborate the pick rather than gating it.
                                │
                                ▼
                    compare_output/v2_matches.jsonl
                                │  (verdict == "none")
                                ▼
                        match_v2_pass2.py  (pass 2)
   Re-asks each "none" against a corpus-wide top-K candidate set ranked
   by citation overlap + title similarity. Entity mismatch allowed, so
   cross-entity misattribution in the House Doc is recoverable.
                                │
                                ▼
                    compare_output/v2_pass2.jsonl
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
           gap_report.py                scoped_compliance.py
   Filings with no House Doc row      Compliance within CMRA's actual
   at all — evidence the Clerk's      reach: post-CMRA statute, covered
   list is incomplete. Deterministic  entity, deposit due in window.
   citations only.                    Reads v1 + both v2 passes.
                 │                             │
                 ▼                             ▼
   compare_output/housedoc_gaps.md   compare_output/scoped_compliance.json
   compare_output/housedoc_gaps.jsonl        (quoted in deck/slides.md)
                    compare_output/orphan_submissions.jsonl
```

## 4. The three matching paths — and how much to trust each

| Stage | Key signal | Trust | Typical failure |
|---|---|---|---|
| **A** | `requirement.legalAuthority` ↔ House Doc `authority` via parsed USC/PLAW/Stat citations | **High.** Matches are essentially deterministic; spot-check anyway. | Requirement record exists but is empty (handled by `references` fallback). |
| **B1** | Package `references` (parsed citations) ↔ mandate | **Medium-high.** Same signal as A, just one level less canonical (per-package vs per-requirement). | Wrong subsection of the same statute; multiple mandates citing the same authority. |
| **B2** | Token jaccard of GPO `title` vs House Doc `nature_of_report`, blocked by canonical agency | **Low without a judge.** The two corpora phrase the same report very differently. | Reasonable-looking title matches a topically-related but different mandate. |
| **Judge** | Claude+Gemini both vote "same" on the (mandate, GPO record) pair | **High when both agree.** Disagreements/unclear stay as candidates. | Both LLMs being charitably wrong about a near-miss (rare; we saw the inverse — over-rejecting on wording). |

The **citation signal is the foundation.** When the same `(USC title, section)`
or `(Public Law congress, number)` or `(Stat volume, page)` appears in both
records, that's the strongest possible link short of an explicit ID match. A
match with two of those overlapping is essentially never wrong; one overlap +
agency match is very rarely wrong.

## 5. What to spot-check (in priority order)

**Start with `compare_output/AUDIT.md`** — it's the consolidated findings
document, generated by `uv run python pipeline/audit.py`. Top of that file is an
executive summary of what survived audit, what holds up as a real finding,
what caveats matter, and what bugs the audit pass fixed. Use that document
as the main reviewer hand-off; the steps below give you the manual spot-
checks that an audit run can't do for you.

1. **`compare_output/REPORT.md` — first 60 lines.** Get the headline numbers
   and on-time rate. Pay attention to the **CMRA-in-scope coverage** number
   (not the raw 2.8%) — that's the honest denominator. If anything looks off
   (e.g., GAO has nonzero coverage), stop and investigate before reading
   further.

2. **`compare_output/final_matches.jsonl` — 25 random rows.** Use this:

   ```bash
   shuf -n 25 compare_output/final_matches.jsonl | jq -c '{mandate_id, reporting_entity, nature_of_report, title, via, origin}'
   ```

   Look for: rows where `origin: judge_promoted` actually look correct (the
   judge's main risk is being charitably right about a wrong match). Rows
   where `via` is `title+agency` (judge-promoted) are the highest-risk class.

3. **`compare_output/match_judgments.jsonl` — sample of "different" verdicts.**

   ```bash
   jq -c 'select(.verdict=="different")' compare_output/match_judgments.jsonl | shuf -n 15
   ```

   The 175+ "different" verdicts are the largest single decision the judge
   makes. If the judge is over-rejecting (saying "different" for actual same
   reports), you'll under-count coverage. Cross-reference 10–15 of these
   against the source — if more than 1–2 are clearly wrong rejections, the
   judge prompt needs a tweak.

4. **`compare_output/overdue_mandates.jsonl` — entries with `days_since_latest != null`.**
   These are the *most reliable* overdue signal — the mandate clearly has a
   recurring cadence AND we've successfully matched submissions to it before
   AND none recently. The much larger "no submission at all" bucket is
   contaminated by matcher recall problems and can't stand alone.

5. **`compare_output/orphan_submissions.jsonl` — top 5 agencies by count.**
   Looking at HHS, VA, DHS specifically. For each, do 2–3 of these orphans
   *clearly* correspond to a House Doc mandate that we just didn't catch?
   If yes, that's the highest-leverage place to invest matcher improvements.

## 6. Known limitations & where the seams are

- **The extractor's output changed once, additively, and it mattered.** An
  early version of `extraction/extract.py` produced 3,250 mandate rows; the
  current one produces 3,297. The difference is purely additive — all 3,250
  original rows survive unchanged and 47 previously-missed rows were
  recovered, none lost (verified 2026-07-25 by running both versions against
  the same PDF and diffing on entity + nature + authority). Of the 47, 24 are
  Department of Energy and 14 are Nuclear Regulatory Commission.

  The NRC rows are the cautionary tale. Before they were recovered, the NRC
  had no rows at all in the extract, so all 7 of its GPO filings were
  structural orphans — unmatchable by construction, and easy to read as an
  agency that files without any mandate behind it. With the rows present, v2
  matches 3 of the 7, and 2 of those land directly on recovered rows
  (M03162 hiring/vacancies, M03159 licensing status). The third is the CRA
  umbrella row, which is procedural rather than substantive. Four remain
  genuinely unmatched.

  The lesson for anyone re-running this: an entity with zero extract rows is
  a claim about the extractor at least as much as a claim about the House
  Document. Check the former before publishing the latter. Any analysis
  computed against a 3,250-row extract predates this fix.

- **Page classification is stable, but only against a stable input.** The
  classifier yields 436 data pages, 430 rotated and 6 upright (20, 183, 184,
  186, 420, 421), and `tests/test_extraction_invariants.py` asserts those
  numbers exactly. A one-off deviation was observed on 2026-07-25 — a seventh
  upright page appearing while a rotated one dropped — and traced to the
  source PDF being edited while the suite ran, not to the extractor. If these
  assertions fail, check `git status` on `data/` before suspecting the code.

- **GAO submits zero CMRA reports.** CMRA's "Federal agency" definition
  (40 U.S.C. 102, as adopted by the Act) excludes GAO by name, so GAO's 233
  mandates can never appear in CMR. This is not a bug, and those mandates
  are excluded from the headline in-scope denominator (as are intelligence
  community elements, the President, and the Senate/House/Architect of the
  Capitol). Note the Act *does* cover legislative- and judicial-branch
  establishments generally — CBO, the Library of Congress, AOUSC — which is
  why AOUSC filings appear in the collection.
- **DoD has 4 submissions for 232 mandates.** This is real — DoD's CMRA
  filing is essentially negligible and that's not a matcher artifact.
- **The "Multiple Executive Agencies and Departments" requirements** (e.g.
  No FEAR Act #12302) have one GPO requirement record but apply to dozens
  of agencies, each of which has its own House Doc row. The matcher gates
  submission→mandate attachment by agency match, but spot-check that, e.g.,
  EPA's No FEAR submission is attached to EPA's row and not also to NRC's.
- **The CMR collection's deposit dates start October 2023** (a soft-launch
  trickle before the January 2024 operational date), and packages carry
  three distinct dates: `dateIssued` (the report's own date, back to 2021),
  `submittedToCongressDate`, and `submittedToGpoDate` (the deposit date —
  what the overdue analysis uses). The raw coverage number is misleading
  because the denominator includes mandates whose reporting cycle predates
  CMRA and mandates assigned to CMRA-exempt entities. The "CMRA-in-scope"
  view restricts the denominator to CMRA-covered entities whose mandates
  should have produced ≥1 submission inside the window (recurring cadences
  + one-time deadlines ≥ 2024 + on-demand). Use *that* number when
  communicating compliance; REPORT.md's sensitivity table shows how each
  scope choice moves it.
- The classifier for in-scope still misses some legitimate recurring
  cadences hidden in the "other" bucket (mostly phrasings like "Within X
  days after the end of each session"). Expanding `_CADENCE_PATTERNS` in
  `cadence.py` will lift more mandates into scope.
- **Agency canonicalization is a hand-curated alias table** in
  `normalize.py` (`_ALIASES`). When a new variant appears, add it there.
  The matcher will only collapse names it's been told to collapse — there's
  no fuzzy entity matching by design (that would create many false joins
  across genuinely-different agencies).
- **Two `CMR-CR!-*` packages** can't be fetched (govinfo API rejects them
  under any encoding; the browser-facing page works). Acceptable 0.2% loss.
- **Empty `requirement` blocks** (no nature/authority text but a number) are
  handled by lifting structured citations from the package `references`
  block during derivation. If a package has *neither* `requirement` data
  nor parsed `references`, it can only match via Stage B2 (title), which is
  weak — these are over-represented in the orphan pool.

## 7. File map

| Path | Role |
|---|---|
| `extraction/main.py`, `extraction/extract.py`, `extraction/schema.py` | House Doc PDF → structured mandates (the existing pipeline) |
| `pipeline/gpo_fetch.py` | govinfo CMR catalog fetch + JSONL derivation |
| `pipeline/normalize.py` | Citation parser (USC/PLAW/Stat), agency canonicalizer, text normalizer |
| `pipeline/authority_parse.py` | Citation parser that *keeps* the subsection path — the addressing layer for statutory text (see §10) |
| `pipeline/statute_fetch.py` | Fetches the US Code as USLM XML from OLRC release points; resolves each mandate's citation to its operative text |
| `pipeline/plaw_fetch.py` | Resolves the *uncodified* mandates against govinfo's PLAW collection (public-law text) |
| `pipeline/cadence.py` | "When-expected" string → cadence label + freshness window |
| `pipeline/match.py` | The v1 matcher (Stage A / B1 / B2) — outputs candidates + confident matches |
| `pipeline/match_judge.py` | LLM judge harness (Claude + Gemini) for fuzzy candidates |
| `pipeline/compare.py` | Consolidates judge verdicts into final outputs + writes `REPORT.md` |
| `pipeline/audit.py` | Comprehensive reviewer audit — produces `AUDIT.md` |
| `pipeline/match_v2.py` | v2 pass 1: filing-first LLM matching against the filer's House Doc slice |
| `pipeline/match_v2_pass2.py` | v2 pass 2: corpus-wide rescue pass for pass-1 "none" verdicts |
| `pipeline/gap_report.py` | House Doc gap clusters — filings with no mandate row anywhere |
| `pipeline/scoped_compliance.py` | Obligation-screened compliance rate (the defensible denominator) |
| `extraction/judge.py`, `extraction/verify.py`, `extraction/verify_report.py` | The pre-existing extraction-accuracy harness (unrelated to the comparison flow) |
| `data/CDOC-119hdoc4.pdf` | Source House Doc |
| `data/cmra_extract.jsonl` | Cached House Doc extract (rebuilt on demand) |
| `data/gpo/packages/*.json` | Raw GPO package summaries (gitignored) |
| `data/gpo/submissions.jsonl` | One row per GPO package |
| `data/gpo/requirements.jsonl` | One row per unique requirement number |
| `data/usc/cache/xml_uscAll@*.zip` | OLRC release-point archive (~108 MB, gitignored) |
| `data/usc/xml/usc*.xml` | Extracted USLM XML, one file per title (~665 MB, gitignored) |
| `data/usc/provisions.jsonl` | Every mandate with its citation resolved to statutory text |
| `data/usc/plaw/cache/PLAW-*.{xml,htm}` | Raw public-law packages (gitignored) |
| `data/usc/plaw_provisions.jsonl` | The uncodified mandates, resolved to public-law text |
| `compare_output/REPORT.md` | Human-readable narrative summary |
| `compare_output/AUDIT.md` | Reviewer audit document (executive summary + 7 systematic checks) |
| `compare_output/final_matches.jsonl` | The authoritative confident-match set (deterministic + judge-promoted) |
| `compare_output/mandate_coverage.jsonl` | Every mandate with every matched submission |
| `compare_output/submission_coverage.jsonl` | Every GPO package with its matched mandate (if any) |
| `compare_output/uncovered_mandates.jsonl` | Mandates with no matched submission |
| `compare_output/orphan_submissions.jsonl` | Submissions with no matched mandate |
| `compare_output/overdue_mandates.jsonl` | Cadence-flagged overdue mandates |
| `compare_output/in_scope_uncovered_mandates.jsonl` | Mandates that *should* have been filed in the GPO window but weren't — the actionable non-compliance backlog |
| `compare_output/candidates.jsonl` | Fuzzy candidates fed to the judge |
| `compare_output/match_judgments.jsonl` | Per-judge verdicts on each candidate |
| `compare_output/match_summary.json` | Numeric summary of the run |
| `compare_output/v2_matches.jsonl` | v2 pass-1 verdicts, one row per (package, model); resumable append log |
| `compare_output/v2_pass2.jsonl` | v2 pass-2 verdicts on pass-1 "none" filings |
| `compare_output/housedoc_gaps.jsonl` | Gap clusters, classified by strength of the absence evidence |
| `compare_output/housedoc_gaps.md` | Human-readable gap summary |
| `compare_output/scoped_compliance.json` | Per-entity compliance within the obligation-screened denominator |

## 8. Where to push next (ranked by leverage)

1. **Mine the filings that survive both v2 passes as "none".** The original
   version of this item pointed at v1's ~800 orphan submissions; `match_v2.py`
   and `match_v2_pass2.py` were built to attack exactly that, so the remaining
   pool is much smaller and much more interesting. For each survivor, ask
   which signal should have caught it — missing alias? Subagency split? Title
   wording too different? — or whether the House Doc genuinely lacks the row,
   which is what `gap_report.py` exists to classify.

2. **Expand `_ALIASES` in `normalize.py`** based on (1). Every alias added
   automatically improves agency blocking and routinely recovers tens of
   matches.

3. **Strengthen Stage B2** with embedding-based title similarity (e.g.
   `sentence-transformers/all-MiniLM-L6-v2`) instead of token jaccard. This
   would let "Specialty Crops Report 2024" match "How Secretary addressed
   each recommendation of the specialty crops committee" without the LLM
   needing to step in.

4. **Audit the "different" LLM verdicts.** If the judge is over-rejecting,
   tightening the prompt to be more accepting of wording variation (while
   keeping its "different statutory subsection" rule) would automatically
   promote a chunk of currently-rejected candidates.

5. **Cadence analyzer needs more vocabulary.** The current regex misses
   "between X and Y after the end of fiscal year" and "within Z days of"
   patterns. Adding those to `cadence.py` would expand the trustworthy
   overdue list.

6. **Surface the `isOnTime` field per entity.** GPO marks each submission as
   on-time or late. A per-agency on-time-rate breakdown for matched
   submissions is publishable without further matcher work.

## 9. Gotchas

- **There is no `cmra` console command.** Every entry point is invoked as
  `uv run python <dir>/<script>.py`. A `[project.scripts]` entry used to
  exist but never worked — uv does not install scripts for unpackaged
  projects — so it was removed rather than left as a trap.
- **`VIRTUAL_ENV`** may be stale (`/Users/.../Coding/...` vs
  `/Users/.../Sync/...`). Prefix shell commands with `unset VIRTUAL_ENV`
  if uv warns.
- **`gpo_fetch.py` is resumable** but if you change the derivation logic
  (the `requirements.jsonl` schema, etc.), use `--derive-only` to rebuild
  the JSONLs from cached package JSONs without re-hitting the API.
- **`match_judge.py` is resumable** by `(candidate_id, judge)` pair. If you
  change the candidate pool (e.g. the matcher's thresholds), delete
  `compare_output/match_judgments.jsonl` first.
- **The v2 passes are resumable too**, and carry the same trap in a costlier
  form. `match_v2.py` skips `(package_id, model)` pairs already in
  `v2_matches.jsonl`; `match_v2_pass2.py` appends to `v2_pass2.jsonl`. Both
  files are append logs where the *last* record for a package wins, so a
  resumed run after a matcher change silently mixes old and new verdicts.
  Change the prompt, the agency slice, or the candidate ranking and you must
  delete the corresponding file before re-running — not just re-run it.
- **Scripts no longer care about your working directory.** Every path is
  anchored to the repo root via `REPO_ROOT` in each module, so
  `python pipeline/match.py` behaves identically from anywhere. This was not
  true before the directory restructure; older shell history that `cd`s to
  the repo root first is harmless but no longer necessary.
- **API rate limits.** Data.gov is 1000 req/hour; the fetcher throttles to
  ~3/sec which is comfortably under. Anthropic and Gemini have their own
  per-key limits — the judge uses 8 workers by default, lower if you see
  429s.

## 10. Resolving mandates to statutory text

The House Doc tells you a mandate exists and cites its authority. It does not
tell you what the law *says*. `authority_parse.py` + `statute_fetch.py` close
that gap: they turn each `authority` string into a USLM address and pull the
operative provision.

```bash
# One-time corpus fetch (~108 MB zip → ~665 MB XML, idempotent)
uv run python pipeline/statute_fetch.py --fetch-only

# Resolve every mandate's citation to text (~45s, fully offline)
uv run python pipeline/statute_fetch.py --resolve-only

# Citation coverage without touching the corpus
uv run python pipeline/authority_parse.py --stats
```

The US Code only reaches the codified ~69%. `plaw_fetch.py` covers the rest:

```bash
# Uncodified mandates → public-law text (~281 packages, ~8 min, resumable)
uv run python pipeline/plaw_fetch.py

uv run python pipeline/plaw_fetch.py --limit 30    # pilot first
uv run python pipeline/plaw_fetch.py --resolve-only
```

**Headline: 3,171 of 3,297 Clerk claims (96.2%) can have their statutory text
inspected.** Ask for coverage *of the Clerk's claims*, not of USC citations —
the latter is a flattering denominator that hides the uncodified third.

| Route | Mandates | Share |
|---|---|---|
| US Code (`statute_fetch.py`) | 2,261 | 68.6% |
| Public law (`plaw_fetch.py`) | 910 | 27.6% |
| **Total inspectable** | **3,171** | **96.2%** |
| Remaining | 126 | 3.8% |

Codified detail, against release point **PL 119-102**:

| | |
|---|---|
| Rows with a USC cite | 2,301 (69.8%) |
| Citations resolved | 2,261 / 2,301 (98.3%) |
| — at the exact subsection node | 1,746 |
| — at a statutory note | 497 |
| — at section level (subsection path stale/malformed) | 18 |
| Unresolved: section absent from current law | 15 |
| Unresolved: note cited but section has no operative notes | 25 |
| Rows with no USC cite (uncodified) | 996 (30.2%) |

The 126 that remain: **71** cite a title or division but no section
(`Pub. L. 94-59, title III`), so there is no section to extract; **53** predate
the 104th Congress and are outside PLAW entirely; **1** is a section the
extractor could not locate.

### Why OLRC, not govinfo

The Office of the Law Revision Counsel publishes a **release point** after each
public law, so it tracks current law within days. govinfo's `/bulkdata/USCODE/`
carries *annual edition* snapshots that can lag by a year, and its
`/bulkdata/json/USCODE` listing endpoint 404s. `discover_release_point()`
scrapes the current one; pin an older one with `--release 119-102`.

### Traps

- **En dashes.** USLM writes suffixed section numbers with U+2013
  (`/us/usc/t12/s635a–5`); the House Doc uses an ASCII hyphen. Fold them
  (`authority_parse.fold_dashes`) or resolution drops from 99.3% to 91.1% and
  ~200 live citations masquerade as repealed law.
- **Subsection case is significant.** `(a)` is a subsection and `(A)` a
  subparagraph — different depths. `uslm_id` preserves case; only the *lookup*
  is case-insensitive.
- **`... note` cites point at uncodified law, not the section.** 523 mandates
  cite a note. Returning the parent section's body hands you the wrong statute
  entirely — `10 U.S.C. 2687 note` is a BRAC reporting mandate, while § 2687
  itself is the base-closure prohibition. The resolver substitutes the
  section's statutory notes and marks `resolved_at: "note"`.
- **Notes are mostly drafting apparatus.** Amendment history, effective dates
  and "References in Text" swamp the operative text. `_APPARATUS_TOPICS` is a
  *deny* list, so an unrecognized USLM topic is kept rather than silently
  dropped.
- **Uncodified law is not in the US Code's statutory notes.** The obvious
  shortcut — look the uncodified provisions up among the notes, which cite
  their source Pub. L. — was measured and fails: 93% of gap rows name a
  section, but only 5.6% appear in any note credit. Fetch PLAW instead.
- **PLAW and the US Code use different USLM namespaces.** GPO serves
  `http://schemas.gpo.gov/xml/uslm` (root `pLaw`); OLRC serves
  `http://xml.house.gov/schemas/uslm/1.0`. Match on local tag names.
- **govinfo returns 400, not 404, for an unavailable *format*.** Asking for
  USLM on a pre-113th public law is a Bad Request; treat 400 and 404 alike
  when probing formats, or the HTML fallback never fires.
- **Public laws state each section number twice** — once in the table of
  contents, once at the section. Take the longest match, or you extract a
  one-line TOC entry.
- **The 15 unresolved sections are a finding, not a bug.** They are absent from
  current law — mostly title 50 Atomic Energy Defense sections (2525, 2566,
  2587, 2590, 2602, 2704, 2750, 2751, 2757) plus `43 U.S.C. 300gg-111`, which
  the House Doc prints as title 42 on p.215 and title 43 on p.218. Mandates
  whose statutory basis no longer exists are worth surfacing on their own.

### What this unlocks

`provisions.jsonl` is a labelled corpus: ~2,261 provisions known to create a
congressional reporting duty, each paired with the Clerk's own `when_expected`
and `reporting_entity`. Two immediate uses:

1. **Citation audit.** A crude `shall (submit|transmit|…) … to Congress` probe
   fires on only ~78% of resolved provisions. The rest are mis-scoped cites
   (the cited subsection isn't the operative one — `2 U.S.C. 807(b)` is the
   Board's audit report, while the GAO mandate is 807(c)(3)), or duties phrased
   in ways the probe misses. Triaging that set validates the Clerk's citations.
2. **Classifier training data** — see §11 before trusting the negatives.

## 11. Caveat on training a mandate classifier

It is tempting to treat "cited by the House Doc" as positive and every other
US Code provision as negative. **The negatives are not clean.** This project
exists because the Clerk's list is incomplete; a classifier trained that way
learns its blind spots.

Measured on the full corpus: the same lexical probe fires on **1.79%** of the
~404k *uncited* provisions — roughly **7,200** provisions that look like
congressional reporting mandates but appear nowhere in the House Doc, against a
list of 3,297. That number is a mixture of genuine omissions, agency-to-agency
and public-facing reports, and expired one-offs. Separating those three is the
actual work, and it should be done with an LLM judge over a sample before any
classifier is fit. Treat ~7,200 as an upper bound on House Doc incompleteness,
not an estimate of it.

## 12. Finding mandates the Clerk's list misses

§11 warns against training a classifier on "uncited = negative". `mandate_classify.py`
avoids that by never fitting a model: it gates candidates deterministically,
judges each with a local open-weight LLM, and measures the judge against an
adjudicated gold set.

```bash
# All four judge models live on their own ports; :4000 is the wrong one (below)
uv run python pipeline/mandate_classify.py --pilot --negatives 8000 --judge-n 300
```

### Measured performance

| stage | mechanism | recall | volume |
|---|---|---|---|
| 1. recipient gate | regex, `passes_gate()` | **99.1%** | 546,625 → **37,141** (6.8%) |
| 2. sweep | nemotron, thinking off | **95.6%** (prec 82.6%) | — |
| 3. confirm | gemma-26B | 93.9% (prec **94.7%**) | — |

End-to-end recall ≈ 94.7%; the sweep is 8–10 h of local compute. Recall is
weighted over precision throughout, because a false positive is rejected
downstream while a false negative is invisible in a 546k-provision corpus.

Ground truth is `data/gold/mandate_gold.json` — 467 rows (212 positive), Claude-
adjudicated, stratified over known Clerk mandates plus gate-passing and
gate-failing provisions. It cost real API spend; reuse it rather than rebuild it.

### The chapeau split — the largest single lever

USLM puts the modal in the parent (`MACPAC shall—`) and the duty verb in the
enumerated child (`(D) ... submit a report to Congress`), so node-level
extraction hands the judge text with the obligation removed. `iter_provisions`
prepends ancestor num/heading/chapeau. Effect on gold:

| model | no context | with context |
|---|---|---|
| nemotron | 82.5% recall | **95.6%** |
| gemma-26B | 66.7% recall | **93.9%** |

Precision held or improved in both cases, so the worry that inherited headings
("Reports to Congress") would cause over-flagging did not materialise. An
earlier n=78 test showed no effect and was simply underpowered — do not read a
null result off a sample this task can't support.

### Endpoints

| model | port | server | reasoning knob |
|---|---|---|---|
| nemotron-30B-A3B | 8082 | llama.cpp | `chat_template_kwargs.enable_thinking` |
| gemma-4-26B-A4B | 8080 | llama.cpp | same |
| gemma-4-E2B | 8081 | llama.cpp | same |
| gpt-oss-20b | 30000 | SGLang | `reasoning_effort` (needs thinking **on**) |

**Do not send chat to the `:4000` proxy.** It silently drops
`chat_template_kwargs`, so every request reasons: 146 completion tokens /
2636 ms via the proxy versus 2 / 19 ms direct, and 0.19/s versus 1.26/s on the
real judge. Embeddings on `:4000` are fine (~121/s).

`n_ctx` is 2048 on both gemmas (16384 on nemotron), and llama.cpp charges
`max_tokens` against it — so a long provision plus a large `max_tokens`
silently truncates the input. `Endpoint.body()` budgets this. With thinking on,
`max_tokens` must be ≥600 or reasoning consumes the whole allowance and
`content` returns empty with a normal `finish_reason`.

### Things that were tried and rejected

- **Embedding similarity as the gate.** `granite-embedding` cosine to known
  mandates reaches only AUC 0.71–0.76 on hard negatives, discarding almost
  nothing at usable recall. Legal prose is stylistically uniform; embeddings
  capture topic, not deontic structure. The regex gate is better *and*
  deterministic.
- **More concurrency.** The host saturates: 8, 16 and 32 workers all measured
  ~0.19 items/sec. Throughput came from switching reasoning off, not parallelism.
- **Easy negatives in benchmarks.** Excluding anything mentioning Congress gave
  every model 0/16 false positives — a number that measures nothing. Hard
  negatives (name Congress, no reporting duty) are the only useful control.

### Note-citation selection

A busy section carries many statutory notes, so returning all of them buries
the one cited. `select_notes()` matches the authority's Pub. L. against each
note's credit line (`Pub. L. 115–232, ... 132 Stat. 2257, provided that:`),
falling back to the Statutes at Large cite. **486 of 497 note citations now
resolve to a specific note**; the remaining 11 are reported as
`resolved_at: "note_unmatched"` rather than silently looking exact.

This mattered: `49 U.S.C. 47101 note` previously returned "Runway Length in
Alaska" instead of the "Runway Safety" mandate it means, and `19 U.S.C. 2703
note` returned a *termination* notice rather than the live rum-remedial-measures
report — which briefly looked like evidence the Clerk lists a repealed mandate.
It was not; it was this bug.

### External validation against the gap filings

The strongest available check does not rely on the classifier agreeing with
itself. Take the `housedoc_gaps.jsonl` rows whose filing states a citation that
appears nowhere in the Clerk's list, resolve that citation to statutory text,
and ask the judge independently:

| | |
|---|---|
| gap rows with a cite absent from the Clerk's list | 68 |
| unique USC sections cited | 56 |
| resolved to statutory text | 48 |
| **independently confirm a congressional reporting duty** | **43 (90%)** |

Three independent sources agree: an agency filed the report, the statute says
one is owed, and the Clerk's list does not have it.

Note the measurement trap. `iter_provisions` applies `max_len` to a node's own
text, so a long section is not emitted whole — it is represented by its
subsections. Asking only for the exact section id therefore finds 25 of 56 and
understates the result; ask the section *or any of its subsections*. An earlier
pass that silently substituted descendant text for unresolved sections produced
a misleading 28/48.
