# CMRA Comparison — Reviewer Runbook

A reviewer's guide to the GPO/CMRA comparison pipeline that lives alongside
the deterministic House Doc extractor. Read top-to-bottom on first arrival;
later, jump straight to the section you need.

## 1. What this project does (in 30 seconds)

The House Document `CDOC-119hdoc4` lists **every standing report Congress
requires** from federal entities — 3,297 mandate rows after extraction.
The 2022 Congressionally Mandated Reports Act (CMRA) created a public GPO
repository where executive-branch agencies must file copies of those reports
on an ongoing basis — about 1,176 packages since Jan 2024.

This pipeline **joins those two datasets** so you can ask:

- *Did agency X actually file the report that statute Y requires?*
- *Which mandated reports are visibly overdue?*
- *Which GPO submissions don't tie back to a known mandate (orphans)?*
- *What's the on-time rate of CMRA-filed reports?*

## 2. Five-minute reproduction

Prereqs: a `GOVINFO_API_KEY` in `.env` (free at
[api.data.gov](https://api.data.gov/signup/)), and the existing
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` for the judge (Gemini's quota is exhausted; see §4).

```bash
# One-time GPO catalog fetch (~6 min, idempotent, resumable)
uv run python pipeline/gpo_fetch.py

# Match deterministically (~5s)
uv run python pipeline/match.py

# Adjudicate the fuzzy candidate bucket with two LLMs (~10 min, ~$2)
uv run python pipeline/match_judge.py --judges claude,openai --workers 8

# Produce the consolidated report
uv run python pipeline/compare.py

# Run the reviewer audit (systematic spot-checks)
uv run python pipeline/audit.py

# Read the headline summary + audit findings
open compare_output/REPORT.md compare_output/AUDIT.md
```

That is the v1 flow, and it is where this runbook originally stopped. The v2
matcher below addresses v1's recall problem — v1 leaves 835 of 1,176 filings
unmatched — and produces `scoped_compliance.json`, the source of the
compliance figures in `deck/slides.md`. The whitepaper predates it. Run it
after `compare.py`:

```bash
# v2 pass 1: filing-first LLM matching against the filer's House Doc slice
# (~1176 filings, resumable, LLM cost)
uv run python pipeline/match_v2.py

# v2 pass 2: corpus-wide rescue for pass-1 "none" verdicts (resumable)
uv run python pipeline/match_v2_pass2.py

# House Doc gaps: filings whose mandate is absent from the Clerk's list
uv run python pipeline/gap_report.py

# Compliance within CMRA's actual reach (obligation-screened denominator)
uv run python pipeline/scoped_compliance.py

open compare_output/housedoc_gaps.md compare_output/scoped_compliance.json
```

**Two things this block will not tell you itself.**

`compare.py` is **single-shot**. It rewrites coverage views that `match.py` owns, so a second run without regenerating first strips attachments permanently; `assert_views_are_pristine()` refuses rather than letting it happen. Re-running any part of this means re-running `match.py` first. See §9.

`scoped_compliance.py` now depends on §10 and §13. It imports `final_destinations()` from `destination.py`, which exits unless `data/usc/provisions.jsonl` and `data/usc/plaw_provisions.jsonl` exist, and silently falls back to raw regex tiers unless `data/gold/destination.jsonl` does. From a clean checkout, run `statute_fetch.py`, `plaw_fetch.py`, and `destination.py --confirm` before the last step or it aborts.

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
extraction/main.py           pipeline/gpo_fetch.py (~1176 pkgs)
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
                     │                          │  (Claude + GPT-5.6-terra)
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
| **A_validation** | A GPO requirement already tagged with a mandate, re-checked by the judge | **A demotion path, not a match path.** 119 of the current 444 candidates — the second-largest bucket. A `different` verdict *strips* an attachment the deterministic matcher had made. | The judge rejecting a correct GPO tag, silently removing a real match. |
| **Judge** | Claude + GPT-5.6-terra both vote "same" on the (mandate, GPO record) pair | **High when both agree.** Fewer than two usable verdicts, or any disagreement, resolves to `unclear` and stays a candidate. | Both LLMs charitably wrong about a near-miss (rare); the measured error runs the other way — see below. |

### Choosing the second judge

The panel was Claude + Gemini until Gemini's quota was exhausted mid-run. Its replacement was picked by measurement rather than by spec sheet: two candidates judged all 444 current candidates, scored against Claude, with `claude-opus-5` adjudicating a 60-row sample of the disagreements. The auditor is deliberately never a judge — it is the same model behind the frontier precision and recall audits, and a panel that includes its own auditor grades its own work.

| | agrees with Claude | `unclear` | errors | accuracy on contested rows |
|---|---|---|---|---|
| **`gpt-5.6-terra`** | **90.3%** | 3 | 0 | **60.0%** |
| `gpt-5.6-luna` | 87.8% | 12 | 0 | 38.3% |
| Claude (judge one) | — | 4 | 0 | 60.0% |

Terra ties Claude exactly on the contested subset, which is what a peer looks like: neither dominates, so their agreement carries information. Luna loses nearly two to one on the same rows and abstains four times as often, despite being billed as ahead of Opus 4.8 — the argument for baking off rather than reading benchmarks. Read the 60% as accuracy on the hardest rows by construction; overall agreement is 90.3%.

**The judge over-rejects, and now there is a number for it.** Where Claude says `different` and Terra says `same` — 30 adjudicated rows — the auditor splits **16 to 14**. Close to a coin flip. §8 item 4 suspected this; it is real, and about half as large as that item assumes. Pin the judge models: a floating alias makes verdicts non-reproducible, for the same reason the OLRC release point is pinned.

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
   (not the raw 4.5%) — that's the honest denominator. If anything looks off
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

   The ~200 "different" verdicts are the largest single decision the judge
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
   Currently VA 151, Coast Guard 128, Labor 40, ACF 35, PBGC 35 — note HHS
   appears only through sub-agencies and DHS only as the Coast Guard, which is
   itself the finding. For each, do 2–3 of these orphans
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
  matches 4 of the 8, and 2 of those land directly on recovered rows
  (M03162 hiring/vacancies, M03159 licensing status). One is the CRA
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
  (40 U.S.C. 102, as adopted by the Act) excludes GAO by name, so GAO's 241
  mandates can never appear in CMR. This is not a bug, and those mandates
  are excluded from the headline in-scope denominator (as are intelligence
  community elements, the President, and the Senate/House/Architect of the
  Capitol). Note the Act *does* cover legislative- and judicial-branch
  establishments generally — CBO, the Library of Congress, AOUSC — which is
  why AOUSC filings appear in the collection.
- **DoD has 6 submissions for 232 mandates.** This is real — DoD's CMRA
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
| `pipeline/match_judge.py` | LLM judge harness (Claude + GPT-5.6-terra) for fuzzy candidates |
| `pipeline/destination.py` | Reads chamber-vs-committee destination out of resolved statutory text — the obligation prong (see §13) |
| `pipeline/compare.py` | Consolidates judge verdicts into final outputs + writes `REPORT.md` |
| `pipeline/audit.py` | Comprehensive reviewer audit — produces `AUDIT.md` |
| `pipeline/match_v2.py` | v2 pass 1: filing-first LLM matching against the filer's House Doc slice |
| `pipeline/match_v2_pass2.py` | v2 pass 2: corpus-wide rescue pass for pass-1 "none" verdicts |
| `pipeline/gap_report.py` | House Doc gap clusters — filings with no mandate row anywhere |
| `pipeline/mandate_classify.py` | The US Code sweep for mandates the Clerk's list misses (see §12) |
| `pipeline/currency.py` | Sunset / repeal screen over swept mandates — writes `sweep_live.jsonl` |
| `pipeline/novelty.py` | Marks swept provisions already present in the Clerk's list |
| `pipeline/mandate_units.py` | Collapses swept provisions into mandates and writes the published list (see §12) |
| `pipeline/sweep_audit.py` | Frontier-model audits of the list: precision, and whether each mandate is counted once |
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
| `data/discovered/mandates.{jsonl,csv}` | Recurring congressional reporting mandates in the US Code that the Clerk's list lacks — tracked, a floor (§12) |
| `data/gold/adjudication_units_*.jsonl` | `sweep_audit.py` verdicts: counting (`_merge`) and precision (`_precision`) |
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

4. ~~**Audit the "different" LLM verdicts.**~~ **Measured — see §4.** On the 30
   adjudicated rows where Claude says `different` and Terra says `same`, the
   auditor splits 16–14. The judge does over-reject, by roughly half what this
   item assumed. What remains is acting on it: tightening the prompt to accept
   wording variation while keeping the "different statutory subsection" rule.

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
- **`compare.py` is not safely re-runnable.** `rewrite_views_post_judge` edits `mandate_coverage.jsonl` in place, but `match.py` owns that file. A second run under different verdicts cannot restore attachments the first run stripped — they are already off disk — and the drift surfaces only as a `final_matches substantive rows != mandate_coverage substantive attachments` warning that does not stop the run. `assert_views_are_pristine()` now refuses the second pass; when it fires, run `match.py` and then `compare.py`, in that order, every time.
- **Mandate IDs are positional.** `match.py` assigns `M{i:05d}` by line number in the extract, so any change to the extract or the candidate pool re-pairs them. `match_judge.py` resumes on `(candidate_id, judge)`, so stale entries do not collide — they simply stop matching, and you quietly re-pay for judgments you already have. After the September catalog refresh only 3 of the 387 two-judge candidates from the June run survived into the new 444.
- **Scripts no longer care about your working directory.** Every path is
  anchored to the repo root via `REPO_ROOT` in each module, so
  `python pipeline/match.py` behaves identically from anywhere. This was not
  true before the directory restructure; older shell history that `cd`s to
  the repo root first is harmless but no longer necessary.
- **API rate limits.** Data.gov is 1000 req/hour; the fetcher throttles to
  ~3/sec which is comfortably under. Anthropic and OpenAI have their own
  per-key limits — the judge uses 8 workers by default, lower if you see
  429s. OpenAI project keys additionally carry a per-project **model
  allowlist**: a model absent from it returns `403 model_not_found` naming
  the project, and edits in the dashboard take minutes to reach the API,
  arriving model by model rather than all at once.
- **GPT-5.x rejects `max_tokens`.** Use `max_completion_tokens`, and leave
  real headroom: reasoning tokens are drawn from that budget *before* any
  visible content, so a tight cap returns an empty string rather than an
  error — which `parse_json_loose` turns into a null verdict.

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

**Headline: 3,172 of 3,297 Clerk claims (96.2%) can have their statutory text
inspected.** Ask for coverage *of the Clerk's claims*, not of USC citations —
the latter is a flattering denominator that hides the uncodified third.

| Route | Mandates | Share |
|---|---|---|
| US Code (`statute_fetch.py`) | 2,261 | 68.6% |
| Public law (`plaw_fetch.py`) | 911 | 27.6% |
| **Total inspectable** | **3,172** | **96.2%** |

(The 99.3% quoted under §Traps is a different denominator: USC citations resolved *excluding* the 15 sections absent from current law, 2,286/2,301. Against all USC citations it is 98.3%.)
| Remaining | 125 | 3.8% |

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

The 125 that remain: **71** cite a title or division but no section
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

Measured on the full corpus: the same lexical probe fires on roughly **1–2%** of the
~541k *uncited* provisions — order **7,000** provisions that look like
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

| stage | mechanism | measured on | volume |
|---|---|---|---|
| 1. recipient gate | regex, `passes_gate()` | **≥70% recall** (frontier-audited floor) | 583,422 → **64,233** |
| 2. sweep | nemotron, thinking off | 95.6% recall / 82.6% prec **on gold** | — |
| 3. confirm | gemma-26B | 93.9% / 94.7% **on gold** | — |
| 4. currency | `currency.py` | — | drops 7.7% |
| **end-to-end** | | **88.1% precision, ≤70% recall** | |

Precision is frontier-audited on two stratified samples, 400 and 505 rows (§ Precision below); the figure quoted is the second, weighted back to the live frame. Recall is frontier-audited on 1,000 gate-rejected rows and is a **ceiling with a wide band** — 63–80% — because only stage-1 misses were sampled (§ Recall).
Recall is weighted over precision throughout, because a false positive is
rejected downstream while a false negative is invisible in a 583k-provision
corpus — which is exactly why recall needed measuring rather than asserting.

> **Correction.** Earlier revisions of this section claimed 99.1% gate recall
> and ~94.7% end-to-end. Both were wrong. The 99.1% was computed as 113/114 —
> a raw count across two strata whose populations differ by **14×** (37k
> gate-pass vs ~509k gate-fail). Size-weighted, the same gold data gives ~75%,
> and a proper stratified study gives a bounded ≤70.4% end-to-end (§Recall). **Never compute a rate across
> strata without weighting by stratum size.**

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

> **The host was reconfigured by 2026-09-16, and the table above describes the run, not the host.** Ports 8080, 8082 and 30000 now refuse connections, so the sweep and confirm models that produced `sweep_live.jsonl` are gone, and neither pass can be resumed or extended as-is. What answers: `gemma-4-E2B` on 8081, `granite-embedding` on 8084, and **`nemotron-3-super` (120B-A12B) on 8085**, direct, one slot, thinking off via the same knob. On gold it scores **92.5% recall / 92.5% precision** (196 TP, 16 FP, 16 FN), between the old sweep and confirm models, at **0.27 items/s** — about a fifth of the old sweep's rate, so a pass of any size is measured in days. It is registered in `ENDPOINTS` as `nemotron-super`.

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

### Optimizing JUDGE_SYSTEM with DSPy

`experiments/dspy_judge.py` asks whether MIPROv2 beats the handwritten
`JUDGE_SYSTEM`, holding everything else fixed: same nemotron endpoint, same
thinking-off setting, same gold rows truncated identically. Claude proposes
instruction candidates; nemotron only ever executes them. The optimizer is
*seeded* with the handwritten instruction, so the question is "can this be
improved", not "can the domain knowledge be rediscovered".

Gold is split once, stratified on (stratum, label), seed 0 — 185 train / 115
val / 167 test. Only the 167-row test slice is quoted below.

| arm | recall | precision | FN | FP |
|---|---|---|---|---|
| handwritten `JUDGE_SYSTEM` | 97.4% | 78.9% | 2 | 20 |
| DSPy zero-shot (same instruction) | 100.0% | 70.6% | 0 | 32 |
| MIPROv2 compiled | 100.0% | 80.2% | 0 | 19 |
| MIPROv2 compiled, hand-corrected | 100.0% | 77.0% | 0 | 23 |

**The objective decides the answer, so it is a flag.** `--fp-credit` sets the
partial credit a false positive earns; a false negative always scores zero. The
arms cross over at ~0.83. A first run at 0.4 rejected the recall-first operating
point for making exactly the trade this pipeline wants — a false positive costs
one confirm-pass call, a false negative sits unevaluated in a 583k-provision
corpus.

**But encoding that preference faithfully destroys the search signal.** At
`--fp-credit 0.9` MIPROv2 ran 13 trials and returned the seed program
unchanged, byte-identical instruction and zero demos: once false positives are
nearly free, every candidate reaching zero false negatives scores 98–99 and
there is no gradient left. The prompt worth keeping came from the
*precision-leaning* objective. Treat the dial as a search parameter that needs
tension in it, then rank survivors by the preference you actually hold.

**An optimizer will silently write a prompt that contradicts the schema.** The
winning instruction told the model to emit `recipient` values outside the enum
(`congressional_committee`, `not_congress`), dropped `semiannual` and `other`
from the cadence list, and replaced the empty-string convention with a
"not specified" sentinel. DSPy's adapter clamped all of it, which is why it
survived 13 trials of scoring — the metric only reads `is_mandate` and
`recipient == "congress"`. `judge_one` parses raw JSON with no schema and would
not have clamped anything. The corrections live in
`experiments/optimized_judge_prompt.txt`, checked in as readable text so the
diff is reviewable; they cost ~3 points of precision (4 rows, plausibly noise).

**Not yet adopted into the pipeline.** The margin over the handwritten prompt is
2 false negatives and 3 false positives out of 167 — real on a recall-first
reading, but small, and switching would invalidate the current sweep outputs and
every recall figure recorded above. `data/gold/mandate_gold.json` is the only
clean measurement available and it is 467 rows.

Wall-clock notes: a `light` compile is ~30 min against this host, and MIPROv2
needs the `optuna` extra (`uv sync --extra dspy`) or it raises only after
bootstrapping and instruction proposal have already been paid for.

#### Per-row forensics: what the optimizer actually bought

Aggregate confusion counts cannot tell a systematic recall hole from two
coin-flips near the boundary. `--slice`-wise per-row dumps
(`experiments/dspy_output/preds_*.jsonl`) can. Pooled over test + val, 129 gold
positives:

| arm | FN | FP |
|---|---|---|
| baseline (handwritten) | 3 | 30 |
| variant: xref clause | 4 | 28 |
| variant: approp clause | 3 | 31 |
| variant: both clauses | 3 | 28 |
| DSPy compiled (corrected) | **1** | 39 |

The disagreement on positives is strictly one-directional — the compiled prompt
catches 2 the handwritten one misses, and the handwritten one catches nothing
the compiled one misses. But 2–0 discordant pairs is McNemar's exact p = 0.5.
Direction is clean; magnitude is not there.

**Two hypotheses about the mechanism, both refuted.**

*"Appropriations language masks reporting duties."* `43 USC 1748` hides a
biennial submission to the Speaker and the President of the Senate behind an
opening appropriations clause, and both arms miss it. But of the 6 gold
positives whose text carries appropriations-authorization language, the baseline
catches 5 — the pattern does not predict misses, and a variant targeting that
clause changes no prediction on any of them. Nor is depth the explanation: the
reporting verb in `1748` sits at char 742, while caught positives run to a p90
of 1705.

*"The win is a threshold shift, reproducible by editing one clause."* Three
single-clause variants of `JUDGE_SYSTEM` (`experiments/make_variants.py`, which
asserts an exact one-clause diff so a reworded upstream prompt fails loudly)
recover **neither** catch. `xref` makes recall worse, trading a true positive
for three false positives. The compiled prompt is the only arm that moves
recall, so its advantage is not something to hand-replicate — which was the
load-bearing argument for not adopting it, and it is gone.

The three baseline misses are idiosyncratic rather than a class: a form-spec
sub-element (`22 USC 8003(g)(4)`), a conditional presidential notification
(`22 USC 8743(c)(1)(A)`), and the buried biennial submission above. A sharper
answer needs more adjudicated rows, not more prompt engineering — that is now
demonstrated rather than asserted.

#### A degrading host renders as a recall collapse, not an error

An unparsed response counts as negative, so an endpoint that fails mid-run
produces plausible-looking scores. One variant recorded 0.0% recall with 0 false
positives on 167/167 unparsed, and another 40.4% on 75/115; every prompt
replayed clean afterwards. `run_baseline` now refuses to save any score built on
more than 2% unparsed. When diagnosing this, note that a bare `curl` to the chat
endpoint reasons by default and returns empty `content` — a health check must
send `chat_template_kwargs.enable_thinking = false` or it reproduces the
symptom and misdiagnoses the cause.

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
| gap rows with a cite absent from the Clerk's list | 71 |
| unique USC sections cited | 60 |
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

### Precision — frontier-audited

Two audits, both adjudicating each part of the claim independently with `claude-opus-5` at medium effort. The first predates `iter_notes()` and so samples provisions only; the second was run after notes became candidates and splits by source.

**Audit 1 — 400 provisions, stratified by frequency** ($3.62, 400/400 parsed):

| Opus 5 confirms | | |
|---|---|---|
| is a congressional reporting duty | 389/400 | 97.2% ±1.6 |
| …and is recurring | 374/400 | 93.5% ±2.4 |
| …and is still in force | 362/400 | **90.5% ±2.9** |

Errors concentrate in the cheap metadata filters, not the hard judgment: 15 one-time duties mislabelled recurring, 12 lapsed, 11 not congressional. The `frequency` prompt in `JUDGE_SYSTEM` was tightened in response — a deadline is not a cadence, and recurrence needs explicit language.

**Audit 2 — 505 rows, 354 provisions and 151 notes** (538k in / 96k out tokens, 505/505 parsed).

Mind the frame. The sample was drawn from the rows `currency.py` had already classified `current` — 5,731 at the time, not the whole 6,067-row confirmed set — and notes were **deliberately over-sampled**, 29.9% of the sample against 7.4% of that frame, because the point was to characterise a new stratum rather than re-measure the whole. The raw sample column therefore estimates nothing on its own. Weight the strata back by their size in the frame (5,308 provisions, 423 notes), the same move §Recall makes:

| Opus 5 confirms | provisions | notes | raw sample | **frame, weighted** | rows |
|---|---|---|---|---|---|
| is a congressional reporting duty | 349/354 (98.6%) | 135/151 (89.4%) | 95.8% | **97.9% ±1.2** | ~5,611 |
| …and is recurring | 333/354 (94.1%) | 129/151 (85.4%) | 91.5% | **93.4% ±2.3** | ~5,354 |
| …and is still in force | 317/354 (89.5%) | **69/151 (45.7%)** | 76.4% | **86.3% ±3.0** | ~4,947 |

The provision column reproduces audit 1 within noise on all three rows, which licenses reading the rest as a stratum effect rather than drift. Read the weighted column: the raw 76.4% describes the sample's own composition and overstates the damage by about ten points.

#### Notes describe repealed duties, and the note says so

**54% of the notes reaching `current` were not in force**, against 10.5% of provisions (both cumulative: a congressional report, recurring, and still in force). The mechanism is structural rather than a tuning gap. A statutory note often exists *because* the underlying section was repealed — it is the editorial trace of a dead duty, written in the past tense — and the judge reads a description of a duty as a duty.

The evidence is legible in the note itself, and `currency.py` was missing all of it: every one of the 82 dead notes in the sample scored `current`. `_REVIEW_RE` looks for `repealed effective`, and repeal notes say *"Repealed by Pub. L. 93-…"* instead.

Widening that is a precision problem, so the pattern was chosen by measurement against the 151 adjudicated notes rather than by eye:

| candidate | catches (of 82 dead) | kills (of 69 live) |
|---|---|---|
| `repealed\s+(?:by\|effective)` | 1 | 0 |
| **bare `repeal`** | **41** | **0** |
| `repeal\|terminat\|omitted` | 53 | 10 |

Bare `repeal` is the keeper. The wider pattern buys 12 more catches for 10 live notes and is rejected. **The test is note-scoped**: on provisions bare `repeal` catches 0 of 37 dead and costs a live row, because a provision that mentions repeal is usually repealing something else.

Applied, it moves 130 rows from `current` to `expired` and drops 41 of the 505 audited rows — **41 of which the frontier model independently calls dead, for zero false positives on the sample.** The frame's numbers after the screen:

| | before | after |
|---|---|---|
| rows classified `current` | 5,731 | **5,601** |
| notes among them | 423 (7.4%) | 293 (5.2%) |
| notes still in force | 45.7% | **62.7%** |
| weighted in force | 86.3% ±3.0 | **88.1% ±3.1** |

Notes remain the weaker stratum — 62.7% against provisions' 89.5% — so this recovers roughly half the note error mass, not all of it. The residue needs either a pattern that survives the live-note test above or a judge pass over note text specifically; **note-sourced mandates are still the least trustworthy rows in the set.**

This is the sweep-path form of a hazard already recorded under §Note-citation selection, where `19 U.S.C. 2703 note` resolved to a termination notice rather than the live report it meant. Notes that announce the death of a duty are a recurring trap in this corpus, in both paths.

Verdicts with reasoning: `data/gold/adjudication.jsonl` (audit 1), `data/gold/adjudication_v2.jsonl` (audit 2). Neither file has a generating script checked in; both were produced ad hoc, so reproducing either means rebuilding the harness.

### Recall — frontier-audited

Recall cannot be measured by sampling rejects uniformly: with ~0.4% of the
519k rejects being misses, a 400-row uniform sample finds one or two. Sample
**enriched strata** instead and weight back by stratum size.

| stratum | size | sampled | recurring congressional report | est. missed |
|---|---|---|---|---|
| duty verb + delivery verb, no recipient token | 37,576 | 400 | 22 (5.5%) | **~2,066** |
| unusual recipient (`both Houses`, `majority leader`, …) | 834 | 400 | 6 | ~12 |
| residual | 480,779 | 200 | **0** | ~0 |

**~2,079 missed.** Cost: $5.98.

Turning that into a recall figure needs care, and the original `≈ 72%` was loose in two ways.

*The denominator moved.* It was 5,282 when this was written, a count from the run whose note figures the endpoint bug voided. The current `current` count, after the repeal screen, is **5,601**.

*Found and missed were not the same quantity.* `missed` counts provisions the frontier model calls genuine recurring congressional reports, so the denominator has to be genuine ones too — not the raw keep count, which §Precision puts at 88.1% in force. Precision-adjusting gives **~4,934 true positives**, and:

| | found | recall |
|---|---|---|
| raw keep count | 5,601 | 72.9% |
| **precision-adjusted** | **~4,934** | **≤70%** |

*It is a ceiling, not a point.* The ~2,079 were sampled from **gate-rejected** provisions only, so they are the misses of stage 1. Stages 2–4 discard true positives too, and none of them are counted here. Writing it out: true positives total ≥ 4,934 + 2,079, so end-to-end recall ≤ 4,934 / 7,013 = **70.4%** — and the gate's own recall is ≥ 70.4%, since at least 4,934 true positives got through it.

**Quote the band, not the point.** The estimate rests on 22 positives in one 400-row sample, so its 95% interval is ~1,240–2,920 missed — **a ceiling anywhere from 63% to 80%.** Anything finer than "roughly two-thirds to four-fifths, at best" reads precision the sample does not carry. Narrowing it means sampling that stratum harder, which is the same job as fixing it.

The residual `0/200` is the load-bearing result — it shows the misses
concentrate in one identifiable stratum rather than being smeared across half
a million provisions, which is what makes the estimate trustworthy *and*
points at the fix: run the whole `duty_no_recipient` stratum through the
judge rather than trying to patch the recipient regex.

### Two structural recall holes found by tests

**Statutory notes were never candidates.** `iter_provisions` yields only
addressable USLM nodes and `_text_of` strips `<notes>`, so the sweep was blind
to 40,699 notes — 9,833 of which name a congressional recipient. This is not a
tuning gap: 497 of the Clerk's own 3,297 mandates resolve to notes.
`iter_notes()` closes it, synthesising `<section_id>/note/<n>` identifiers
(stable per release point, but **not** USLM addresses — don't feed them back
to a USLM lookup).

**The gate rejected plural committee names.** `committee on` did not match
"the Committees on Appropriations" — the dominant form in appropriations law.
Now `committees?\s+(?:on|of)`, plus `joint committee`, `congressional budget
office`, `clerk of the house`, and `secretary of the senate`. Caught by a unit
test, not by inspection; practical impact was smaller than feared (113
provisions) because chapeau context usually supplied another matching token.

### Counting mandates, not provisions

**Every count above is a count of USLM nodes, and nodes are not duties.** Of the 5,601 `current` rows, 3,122 have a flagged ancestor that is also in the set. The chapeau repair causes it: a short section is emitted whole *and* each subsection is emitted with the section's lead-in prepended, and "shall submit … a report that provides the following: (1) … (11)" becomes eleven children that each carry the obligation. Counting sections errs the other way, since one section can hold several reports (15 U.S.C. 3721(m) and (n)). Do not quote 5,601, or the precision-adjusted ~4,934, as a number of mandates.

```bash
uv run python pipeline/mandate_units.py     # writes data/discovered/mandates.{jsonl,csv}
```

`mandate_units.py` merges only on evidence that two rows are one duty:

1. A node folds into its nearest flagged ancestor when both name the same reporting entity (prefix-tolerant) and cadence.
2. Siblings under one parent fold together when they share entity and cadence *and* their common text states the obligation, meaning the duty lives in the shared lead-in. Siblings that share only a heading stay apart.

It also drops what the confirm model labelled `one-time` (86) or `unknown` (20). The novelty scope filtered on the *sweep* model's cadence, and the two models disagree often enough to let those through.

| | |
|---|---|
| `current` rows | 5,601 |
| kept (recurring or event-driven) | 5,495 |
| **mandates** | **2,553** — 2,417 recurring, 136 event-driven |
| sections | 2,028 |

**The count is audited in both directions** (`sweep_audit.py units`, `claude-opus-5`, 150 verdicts in `data/gold/adjudication_units_merge.jsonl`):

| sample | question | result | extrapolated |
|---|---|---|---|
| 75 merged units | one duty? | 66 yes; 9 hold 2–3 | +0.147 duties × 1,320 merged units ≈ **+194** |
| 75 same-section pairs | two duties? | 36 yes; 29 are one; 10 hold 3+ | 38.7% of the 525 surplus units ≈ **−203** |

The errors cancel to within about ten mandates, so 2,553 stands; the samples are too small to claim better than a few percent. The undercount side is inflated a little: the auditor counted *any* duty, and the extra duties inside merged units are often one-time advance notifications rather than recurring reports. The residual echo that remains is almost all cadence disagreement between parent and child, e.g. `10 U.S.C. 10216(c)` "annual" vs `(c)(2)` "event-driven" for one budget-justification duty.

### Precision of the list

Audited on the list itself, not on swept rows (`sweep_audit.py precision`, `claude-opus-5` at medium effort, 300 mandates, 150 per source, verdicts in `data/gold/adjudication_units_precision.jsonl`). Each claim is cumulative, and the frame column weights the strata back to their share of the 2,553:

| Opus 5 confirms | provisions (2,308) | notes (245) | **frame, weighted** |
|---|---|---|---|
| is a congressional reporting duty | 147/150 | 148/150 | **98.1% ±2.0** |
| …and is recurring | 145/150 | 148/150 | **96.9% ±2.6** |
| …and is still in force | 139/150 (92.7%) | **115/150 (76.7%)** | **91.1% ±3.8** |

That is roughly **2,330 mandates** that are real, recurring and live. It is higher than the row-level 88.1%, but the frames differ (mandates, not rows) and the samples do not isolate a cause. The one attributable change is the recurring claim: after dropping confirm-model one-time rows, only 2 of 300 fail it. Total spend for both audits was ~$5.80.

**What remains is lapse, and it lives in notes.** 33 of the 35 note failures are duties that ended, against 6 of 11 provision failures. A notes-only pattern is visible in the verdicts and was not applied: **12 of the 33 dead notes cite the Federal Reports Elimination and Sunset Act** (`Pub. L. 104-66` or "May 15, 2000"), and every audited note matching that pattern is dead (12/12). It matches 16 notes in the list and 4 provisions. It was picked from this sample, so like the repeal screen it needs a held-out check before it becomes a `currency.py` rule. Dropping the 12 matches from the sample would put notes at 115/138 (83%).

### The published list

`data/discovered/mandates.jsonl` and `.csv` are tracked, unlike everything else under `data/usc/`. One record per mandate: `citation`, `url` (uscode.house.gov), `reporting_entity`, `cadence`, `deadline`, `source` (`provision` or `note`), an 800-character excerpt, and `members`, the swept nodes it absorbed.

**It is a floor on the US Code, not a list of everything the Clerk misses.** Three limits bound it:

- **Recall ≤70%** (63–80% band, §Recall). About 2,000 more mandates sit in gate-rejected text, and the next subsection says how to get them.
- **Codified law and its notes only.** Session law that was neither codified nor printed as a note is not swept. About 30% of the Clerk's own rows are uncodified, so this gap is not small.
- **Notes are the least reliable rows.** They are 9.6% of mandates but only 5.2% of rows, because a note never echoes into subsections. Per §Precision of the list, 23% of notes describe a lapsed duty, against 7% of provisions.

The two confirm-pass nulls from `sweep_confirmed.jsonl` are not in the list and cannot be retried until a confirm model is back on the host (§Endpoints).

### The recall pass that was not run

The fix §Recall points at is to judge the gate-rejected text that states a duty and a delivery. The original stratum filter (37,576 items) was never checked in. Its size does pin it to the current reject set: 37,576 + 834 + 480,779 = 519,189, exactly what `passes_gate` rejects today. The closest reconstruction is `_MODAL_RE` plus a whole-word delivery verb (`submit|submits|transmit|transmits|report|reports|notify|notifies|furnish|furnishes|deliver|delivers`), which yields **42,311** items with 11.4M tokens of text.

It was costed and deferred on 2026-09-16: ~44 hours on `nemotron-super` at 0.27/s, or roughly $25–29 (Haiku 4.5), $50–57 (Sonnet 5), or $125–145 (Opus 5, low effort) through the API before any confirm pass. At the audit's 5.5% hit rate, expect ~2,300 flagged provisions, which collapse to fewer mandates for the reason above. Whichever judge runs it must be scored on `mandate_gold.json` first, and its hits must go through `novelty.py`, `currency.py`, `mandate_units.py` and a precision audit as a separate stratum. Judged by a different model, they are not the same population as the rows above.

## 13. Reading destination out of the statute

CMRA's deposit obligation has two prongs: **chamber-directed** reports are covered at any statute age, **committee-directed** ones only under statutes enacted on or after Pub. L. 117-263. Destination therefore decides whether a pre-CMRA mandate is obligated at all.

`scoped_compliance.py` used to answer "we can't tell", and excluded 2,921 of 3,297 mandates on that basis — every compliance figure rested on 3% of the corpus. Its reason was sound but scoped to the wrong artifact: *the House Doc table* does not record destination. The **statute** does, and §10 had already resolved 96.2% of the Clerk's mandates to their operative text. Nothing joined the two.

```bash
uv run python pipeline/destination.py              # GATE tiers only (pre-confirmation)
uv run python pipeline/destination.py --sample 5   # worked examples per tier
uv run python pipeline/destination.py --confirm    # re-judge the weak tiers (LLM, resumable)
```

### Recipients, not mentions

The load-bearing distinction is between a body that *receives* a report and one merely named nearby. `43 USC`-style drafting routinely says "after consultation with the appropriate committees … shall submit to the Congress" — congress-directed, with committees appearing only as consultees. A first version matched the bare word `committees` and read that backwards, scoring **16% precision** on the committee tier.

`_DELIVERY_RE` anchors on a delivery verb and takes the 240 characters after its `to` as the recipient span; `_DIRECT_OBJECT_RE` covers `notify the Congress`, which has no `to` to anchor on. Within a span a chamber officer wins outright; otherwise the *first* named recipient governs, so "to Congress, including the intelligence committees" is congress-directed.

### The gate is not the verdict

Audited against `claude-opus-5` on 87 mandates, the regex alone reached 96% on `congress` and 83% on `committee` — but **72% on `chamber`**, which is the only tier that moves a row into the denominator. Trusting it there would have put roughly one bad row in four into a published figure.

So it is a gate, in the same shape as the sweep's: it narrows 3,172 mandates to the 525 sitting in its two weak tiers (`chamber` and `unknown`), and `gpt-5.6-terra` re-judges those. Verdicts append to `data/gold/destination.jsonl` and a null is retryable, so an outage self-heals. Re-audited after confirmation, on 85 rows:

| tier | n | precision |
|---|---|---|
| **chamber** | 40 | **97.5%** |
| congress | 20 | 95.0% |
| committee | 15 | 100.0% |
| unknown | 10 | 90.0% |
| **overall** | **85** | **96.5%** |

Corpus after confirmation: **208 chamber, 61 committee, 2,724 congress, 179 unknown**, and 125 mandates with no resolved text at all — those can never leave the upper bracket. Note the gate's own distribution differs (260/23/2,624/265); the bare command prints that, and only `--confirm` reports the numbers above.

**Both audits above were ad hoc and left no artifact.** `data/gold/destination.jsonl` holds the 525 `gpt-5.6-terra` confirmations, not the `claude-opus-5` adjudications that scored them, so the two precision tables cannot be re-checked without re-running. Same gap §12 records for its own adjudications.

### What it buys, and what it doesn't

The defensible denominator grows from 110 to 139, and compliance falls from 15.5% to **14.4%**. A bigger denominator against a near-static numerator always lowers the rate; that is the honest direction, not a regression.

The larger gain is that the reading is now measured. `congress` is the modal tier by a wide margin — 2,724 mandates say "to Congress" and name nobody narrower — so whether *that* counts as chamber-directed is the question the whole answer turns on. It now has its own bracket instead of hiding inside the upper one's blanket assumption:

| reading | covered | rate |
|---|---|---|
| strict — chamber-directed or post-CMRA | 20/139 | **14.4%** |
| middle — plus "to Congress" | 164/972 | **16.9%** |
| upper — all in-window covered-entity mandates | 176/1071 | **16.4%** |

The deck's claim is that the answer holds under every reading. It does, and the band is now narrow and evidenced rather than assumed.

**Do not read the +29 as the ceiling of this idea, or as its floor.** Most chamber-directed pre-CMRA mandates are then removed by the entity or cadence screens, so the denominator grew 26%, not the order of magnitude the tier counts suggest. The remaining lever is the `congress` tier, and that is a legal-interpretation question, not an engineering one.

**Stale figure to watch:** the upper-bracket comment in `scoped_compliance.py` cites "1,056 of 1,057 actual deposits are chamber-received" as empirical support. That is the June catalog; the catalog is now 1,176. The argument is unaffected but the number wants recomputing.
