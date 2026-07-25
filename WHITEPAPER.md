# Joining Mandates to Filings: A Reproducible Method for Measuring U.S. Congressional Reporting Compliance

## Abstract

Congress requires federal agencies to submit thousands of standing reports, on everything from drug control strategy to veterans' health care to the operations of the Glen Canyon Dam. Until recently, no public dataset showed which of those reports were actually being filed. The 2022 Congressionally Mandated Reports Act (CMRA) closed half of that gap by creating a public repository at the Government Publishing Office (GPO) where covered federal agencies deposit their submissions. The other half, the authoritative list of which reports are *required*, exists only in a 700-page Congressional document published as a PDF. The two datasets have never been formally joined.

This paper describes a reproducible pipeline that joins them. From a PDF of *House Document CDOC-119hdoc4* we extract 3,250 standing reporting mandates; from the GPO Congressionally Mandated Reports collection we ingest 1,057 filed submissions, with deposit dates from October 2023 onward. We match the two sides using parsed statutory citations, canonicalized agency names, title similarity, and a pair of independently-prompted large language models acting as adjudicators on borderline cases. Among mandates that are in scope — assigned to an entity CMRA actually covers, on a cadence that should have produced a filing in the window — 9.8 percent have at least one matching filing; a sensitivity table shows how each scope choice moves that figure. Of the matched filings, 77.5 percent were submitted on time per GPO's own marker, a rate that conditions on the self-selected population that files through this channel at all. The headline most plausibly understates real CMR-channel uptake — the matcher is deliberately conservative, and the GPO repository captures only a subset of the channels agencies use to file material to Congress — though we are explicit that this is a directional claim, not a measured bound. We document a systematic reviewer audit that surfaced and fixed twelve substantive defects during construction and nine more in subsequent independent review, publish all intermediate artifacts as line-delimited JSON, and release source code under permissive terms. The pipeline is designed to be re-run as both datasets grow, and to make the joining decisions transparent enough that any individual match can be inspected by a reader who is not the pipeline's author.

**Keywords:** government oversight, congressional reports, record linkage, statutory citation parsing, large language model adjudication, reproducible journalism, civic technology.

---

## 1. Introduction

### 1.1 What congressional reports are

When Congress passes a statute directing an agency to do something non-trivial (implement a new program, study a problem, oversee an industry), it commonly includes a clause requiring the agency to **report back**. Sometimes the report is one-time ("submit a study to Congress within 18 months of enactment"). More often it recurs ("submit an annual report on this program's operations"). Over decades this practice has produced several thousand standing reporting mandates spread across hundreds of agencies and tens of thousands of pages of statute.

Each mandate is, in effect, a small contract: Congress has told an agency to deliver a specific document on a specific cadence under a specific statutory authority. Whether those contracts are being honored is a basic oversight question. Until recently, it was also impossible to answer at scale, because nobody published the answers in one place.

### 1.2 Two halves of the same problem

To audit reporting compliance one needs two datasets:

- A **register of mandates**: what reports are required, by which agency, under what authority, on what cadence.
- A **record of filings**: what reports were actually submitted, and when.

Both halves now exist in public form, but they were produced for different purposes and they do not natively join.

The register lives in *House Document CDOC-119hdoc4*, "List of Reports Which It Is the Duty of Any Officer or Department to Make to Congress." The document is updated each Congress by the Clerk of the House. It is a 700-page printed report rendered to PDF. After extraction it contains roughly 3,250 mandate rows, each consisting of a reporting entity, a nature-of-report description, a statutory authority citation, and a "when expected" cadence clause.

The record of filings lives in the GPO Congressionally Mandated Reports collection (the *CMR* collection on govinfo.gov), created by the 2022 CMRA. CMRA requires covered Federal agencies (§2.1) to deposit copies of congressional reports into a public repository where they remain permanently accessible. The collection became operational in January 2024 and as of this writing contains about 1,057 packages, each representing one submitted report and exposed through a structured JSON API.

These two datasets are produced by different parts of the federal government, encoded in incompatible formats, and indexed on incompatible keys. The register identifies a mandate by free-text description and a hand-typed statutory citation. The CMR record identifies a filing by a GPO-assigned package ID, often (but not always) tagged with a numeric `requirement.number` that is GPO's own internal mandate ID and that does not appear anywhere in the House Document. To ask "did agency X file the report that statute Y requires?" one must first construct a join key that neither side publishes.

### 1.3 What this paper contributes

This paper describes a pipeline that constructs that join and publishes the results. The contribution is fourfold:

1. **A deterministic extractor** that turns the House Document PDF into structured records, defeating three specific pathologies that defeat conventional PDF table extractors.
2. **A matcher** that joins mandates to filings using parsed statutory citations as the primary signal and titles plus agency names as fallbacks.
3. **An adjudication layer** that routes borderline matches through two independently-prompted large language models, promoting to confident only on unanimous agreement.
4. **A reviewer-driven audit harness** that turned every hand-found error during construction into an automated systematic check, and that publishes a candid limitations document alongside the headline numbers. Two audit rounds — one during construction, one by independent reviewers after a complete draft existed — surfaced twenty-one substantive defects between them, every one of which is documented in §5.

All intermediate artifacts are written to disk as line-delimited JSON. Every match in the final dataset records which path produced it, so a reader can re-derive any number in the headline report from the underlying evidence. The full pipeline runs end-to-end in roughly twenty minutes of wall-clock time, the bulk of which is the LLM adjudication step.

### 1.4 What this paper does *not* claim

One scope caveat upfront. The headline number, that roughly one in ten in-scope mandates has at least one matched filing in the GPO window, measures *CMR-channel* compliance specifically. CMR is the channel CMRA created in 2022. It is not the only channel agencies use to submit material to Congress, and several major executive-branch entities (notably OMB, the U.S. Trade Representative, EPA, and the Department of Energy) deposit zero packages there despite plainly filing congressional materials through other routes: the President's Budget collection, the Federal Register, direct committee correspondence. Read the number as **an estimate of uptake of the specific channel CMRA established, one that most plausibly understates real compliance.** §8.1 and §10 return to this caveat and to the forces that cut the other way.

---

## 2. Background

### 2.1 The Congressionally Mandated Reports Act (CMRA)

CMRA — the Access to Congressionally Mandated Reports Act, enacted as part of the James M. Inhofe National Defense Authorization Act for Fiscal Year 2023 (Pub. L. 117-263, div. G, title LXXII, subtitle D, §§ 7241–7248, 136 Stat. 3677; amended by Pub. L. 118-172) — directs the Government Publishing Office to maintain a permanent, freely-accessible online repository of congressionally mandated reports. Each submitting agency designates a CMRA point of contact, and each covered report is deposited within 30 days of submission. The repository became operational in January 2024, with a trickle of deposits beginning in October 2023.

Two features of CMRA shape what is and is not measurable from the CMR collection alone:

- **Entity scope.** CMRA defines "Federal agency" by reference to 40 U.S.C. § 102, which covers executive agencies *and* establishments in the legislative and judicial branches — that is why filings from the Administrative Office of the U.S. Courts appear in the collection — except the Senate, the House of Representatives, and the Architect of the Capitol. The Act then excludes, by name, the Government Accountability Office and elements of the intelligence community (50 U.S.C. § 3003). The President, not being an "agency," is also outside the definition. Mandates assigned to these excluded entities can never lawfully appear in the CMR collection, so any CMR-channel compliance denominator must leave them out; a previous draft of this paper wrongly described the Act as covering only the executive branch and wrongly counted GAO and presidential mandates in the denominator.
- **No mandate registry.** CMRA created the filing channel but not the authoritative list of mandates against which filings would be measured. GPO assigns each submission a `requirement.number` when it can recognize the underlying mandate, but the `requirement.number` namespace is GPO's own internal register, not the canonical list of what Congress has required.

The 2022 act thus solved the publishing problem and surfaced the joining problem.

### 2.2 House Document CDOC-119hdoc4

The Clerk of the House publishes, once per Congress, a compilation titled *List of Reports Which It Is the Duty of Any Officer or Department to Make to Congress*. The 119th-Congress edition (CDOC-119hdoc4) is roughly 700 pages and lists every standing report Congress requires from any federal entity, with four columns per row: the reporting entity, the nature of the report, the statutory authority, and a clause describing when the report is expected.

The document is the closest thing in U.S. government publishing to an authoritative mandate register. Its limitations are real: it covers *standing* mandates and so omits some one-off statutory directives, and the cadence column is free-text English rather than structured. But it is the only public document of its kind. Pairing it with the CMR collection is the natural way to convert CMR's filing record into a compliance signal.

### 2.3 Why the join is hard

A naïve join on agency name and statute citation might be expected to work; in practice every key in both datasets has been written by humans, in inconsistent forms, over decades.

- **Agency names vary.** The same entity appears as "Department of Health and Human Services" in the House Doc, "HHS" or "Health and Human Services Department" in some GPO records, "FDA" in others, and "CMS" in yet others. Public agency lists exist (the Federal Register agency directory, OMB and Treasury agency codes, the SAM.gov hierarchy), but none crosswalks the specific free-text variants these two documents use, and none captures the House Doc's multi-agency bucket rows.
- **Citations vary.** A statutory authority might be written as `42 U.S.C. 282a(c)(1)(C)`, `42 U.S.C. § 282a`, `Pub. L. 103-43, Sec. 102`, or `107 Stat. 122`, each referring to the same provision but in three different citation systems (U.S. Code, Public Laws, Statutes at Large). Within a single citation system, subsection notation differs.
- **Report titles vary.** Congress writes mandates in statutory English ("Operations of the preceding and the projected year regarding the Grand Canyon Protection Act"); agencies title their filings in agency English ("Report to Congress on the Operations of Glen Canyon Dam Pursuant to the Grand Canyon Protection Act of 1992 Water Years 2021 to 2022"). Substantial token overlap is the exception, not the rule.
- **The PDF is adversarial to extractors.** The House Document's text layer is correct but laid out by a 90°-rotated text transform that defeats standard PDF table tools. The first task in the pipeline is recovering structured data from it without resorting to OCR.

The remainder of this paper describes how each of these problems was addressed and what the result looks like.

---

## 3. Data Sources

### 3.1 Mandate side: House Document CDOC-119hdoc4

The document is downloaded once and stored at `data/CDOC-119hdoc4.pdf`. Its table pages have a consistent four-column layout, but conventional PDF table extractors (Tabula, Camelot, naïve `pdfplumber` word grouping) all fail on it for three reasons:

1. **The text layer is correct but visually rotated.** A naïve full-page text extraction yields reversed strings (`SSERGNOC` for `CONGRESS`) because the underlying text matrix is rotated 90°. The characters are correct; their spatial layout misleads a tool that does not account for the rotation.
2. **Bold is simulated by double-stamping.** Each glyph in bold portions is drawn twice at a ~0.4-point offset, producing doubled characters that defeat word-grouping heuristics.
3. **There is no machine-readable grid.** The visual rule lines on the page are incomplete; column boundaries are implied by horizontal character position rather than by an explicit table structure.

After extraction the dataset contains 3,250 mandate rows.

### 3.2 Filing side: GPO Congressionally Mandated Reports collection

GPO exposes the CMR collection via govinfo.gov's standard API (`collection=CMR`). Each *package* in the collection represents one submitted report. We fetch the entire collection, caching the raw package summary JSON to `data/gpo/packages/<id>.json`. The fetcher is resumable; a partial run resumes without re-hitting the API. As of this writing the collection contains 1,057 packages. (Two `CMR-CR!-*` packages cannot be fetched under any documented URL encoding while the browser-facing pages work; this is a 0.2 percent data loss we accept and log.)

Each package carries three distinct dates, and conflating them produces apparent paradoxes. `dateIssued` is the report's own date and ranges back to 2021 — agencies deposited reports written years before the channel existed. `submittedToCongressDate` is when the report went to Congress. `submittedToGpoDate` is the deposit date into the repository and ranges from October 2023 (a soft-launch trickle before the January 2024 operational date) through the run date. All recency and overdue analysis in this paper uses the deposit date, `submittedToGpoDate`; this is why some "most recent filing" dates in §6 fall in late 2023.

Each package's JSON exposes, when present, a `requirement.number` that ties the submission to GPO's internal mandate register. Of the 1,057 packages, 207 (about 20 percent) carry a `requirement.number`. The remainder are the harder cases that motivate the rest of the pipeline.

We project the cached packages into two derived line-delimited JSON views:

- `submissions.jsonl`: one row per package (the actuals).
- `requirements.jsonl`: one row per unique `requirement.number`, merged across all packages that reference it.

The merge into `requirements.jsonl` uses **prefer-non-empty** logic: when one package referencing a requirement ID has empty `legalAuthority` or `references` fields and another has them populated, the populated fields win. (Without this, eleven matches were silently lost in early iterations because the first package encountered for a requirement ID happened to have empty fields.)

---

## 4. Method

### 4.1 Pipeline overview

The pipeline has seven stages, each producing a durable artifact that the next stage consumes. Any stage can be re-run in isolation given its predecessors' outputs. Figure 1 shows the data flow.

```
[Stage 1] PDF extraction       [Stage 2] GPO ingestion
data/cmra_extract.jsonl    data/gpo/{submissions,requirements}.jsonl
       |                            |
       +-------------+--------------+
                     |
                     v
         [Stage 3] Normalization
            (citations, agencies, text)
                     |
                     v
         [Stage 4] Deterministic matcher
            (Stages A / B1 / B2)
                     |
                     v
         [Stage 5] LLM adjudication
            (Claude + Gemini)
                     |
                     v
         [Stage 6] Consolidation + scope filter
                     |
                     v
         REPORT.md + final_matches.jsonl
                     |
                     v
         [Stage 7] Audit (systematic spot-checks)
                     |
                     v
         AUDIT.md
```

*Figure 1: Pipeline data flow. After the two ingestion stages, each box reads only durable artifacts from the stages above it, never the network; this makes every stage independently re-runnable.*

### 4.2 Stage 1: extracting the mandate register

The extractor (`extract.py`) is deterministic: given the same input PDF it produces the same output, with no LLM and no randomness. Its design follows three principles:

- **Work at the character level, in PDF coordinates.** Because the text matrix is rotated, word-grouping heuristics that depend on glyph proximity fail. The extractor operates on raw character positions and reconstructs rows and columns from those positions.
- **Derive column boundaries from the document's own rule lines.** The visible vertical rules, even where incomplete, are reliable enough to anchor column positions per page, so the extractor self-calibrates rather than relying on hard-coded coordinates.
- **Use a separate offline harness for accuracy validation.** A judge module compares each extracted row against the source PDF region visually, but does not feed back into extraction. This keeps the extraction path fully deterministic while still allowing systematic accuracy measurement.

The extractor's output schema is shown in Table 1.

| Field | Example |
|---|---|
| `reporting_entity` | Government Accountability Office |
| `nature_of_report` | Review of Congressional Award Foundation audit |
| `authority` | `2 U.S.C. 807(b); Pub. L. 96-114, title I, Sec. 107 (as amended …)` |
| `when_expected` | Not later than 180 days after the date on which the audit is received |

*Table 1: Per-row fields produced by Stage 1.*

A full architectural rationale for the extractor lives separately in `approach.md` and is the subject of its own write-up.

### 4.3 Stage 2: ingesting the filing register

The GPO fetcher (`gpo_fetch.py`) walks the CMR collection page by page, caches each package's summary JSON, and derives the two views described in §3.2. The fetcher throttles to roughly three requests per second, comfortably under data.gov's 1,000-per-hour limit.

### 4.4 Stage 3: normalization

Every match decision later in the pipeline depends on three normalizers in `normalize.py`.

#### 4.4.1 Statutory citation parsing

Statutory citations appear in three notations: U.S. Code (`2 U.S.C. 807(b)`), Public Laws (`Pub. L. 96-114, Sec. 107`), and Statutes at Large (`104 Stat. 2308`). The parser extracts structured tuples for each notation:

- `(usc_title, section)` for U.S. Code citations
- `(plaw_congress, plaw_number)` for Public Law citations
- `(stat_volume, stat_page)` for Statutes at Large citations

Two implementation details are worth noting because each one fixed a class of bugs.

First, U.S. Code section identifiers can contain **multi-letter suffixes and dash-numbered subsections**: `950cc`, `2349aa`, `286yy`, `2279aa-10`. An early regex captured only one trailing letter, so `7 U.S.C. 950cc(d)` was stored as `(7, "950c")`. Sixty-nine mandates were affected. The corrected regex (`(\d+[A-Za-z]*(?:-\d+)?)`) handles every variant we've seen.

Second, the parser runs against **both** the explicit `authority` field *and* the structured `references` array inside GPO package JSON. Many GPO requirements have an empty `legalAuthority` text field but a populated `references` array; lifting citations from the latter is what lets the matcher find them at all.

#### 4.4.2 Agency name canonicalization

The matcher uses agency match as a blocking key: two records can only match if their canonicalized agency names agree (modulo a few explicitly-multi-agency mandates). To collapse the many variants of each entity name into a canonical form, we maintain a hand-curated alias table (`_ALIASES` in `normalize.py`).

We deliberately do **not** use fuzzy entity matching here. Fuzzy matching produces many false joins across genuinely different agencies (e.g., conflating bureaus within different departments that happen to share substrings). A curated alias table is more work to maintain but never lies about cross-agency identity. When a new variant appears in the data, the audit process (§5) flags it and an alias is added; in this run, adding the abbreviation "AOUSC" for the Administrative Office of the U.S. Courts immediately recovered six matches.

#### 4.4.3 Text normalization for token similarity

Titles and nature-of-report strings are lowercased, stripped of boilerplate stopwords (federal-paperwork phrases such as "fiscal year", "report to congress", "annual report"), and tokenized for Jaccard similarity computation. The stopword list was extended several times during audit: the initial list permitted candidate matches whose only token overlap was generic paperwork vocabulary, and tightening it materially reduced the false-positive candidate pool.

### 4.5 Stage 4: deterministic matcher

`match.py` performs the join. It has three sub-stages with very different signal strength; understanding the gradient of trust between them is central to interpreting the results.

#### 4.5.1 Stage A: requirement-level matching (citation-anchored)

For each GPO `requirement.number`, Stage A finds House Doc mandates whose parsed citations overlap. When the same `(USC title, section)`, `(Public Law congress, number)`, or `(Stat volume, page)` tuple appears on both sides, the mandate and the requirement are linked. This is the strongest possible signal short of an explicit shared identifier: matches with two or more overlapping citations are essentially never wrong, and matches with one overlap plus an agency match are very rarely wrong.

Trust in Stage A is high but not unconditional. Two failure modes drove late-stage matcher changes:

- **GPO can mis-tag packages.** The Federal Maritime Commission Annual Report was tagged with `requirement.number` 319, which actually represents the President's emergency war-powers expenditures mandate. The matcher cheerfully attached FMC's report to that mandate. Fix: any Stage A match with title-vs-mandate Jaccard below 0.10 now routes through the LLM judge as a Stage A *validation* candidate before counting toward coverage. Of 109 such validation candidates in this run, 52 were confirmed (legitimate matches like Agency Financial Reports, where no token overlap is expected), 49 were stripped as mis-tags, and 8 were held conservatively. That stripped fraction — roughly 45 percent of the low-overlap subset of the *strongest* matching path — is the clearest evidence in the dataset that no path can be trusted unvalidated.
- **`requirement.number` can repeat within one package.** FMC's package listed requirement 319 three times, which without dedupe would have produced three identical attachments. Within-package dedupe handles this.

#### 4.5.2 Stage B1: package-level fallback via parsed references

When GPO has not applied a `requirement.number` tag, Stage B1 applies the same citation-intersection logic but using citations parsed from the package's `references` block. The signal is the same as Stage A, one level less canonical.

Two thresholding decisions are load-bearing here:

- **A single shared citation is not always enough.** A single Public Law number like Pub. L. 110-234 (the 2008 Farm Bill) contains hundreds of unrelated mandates; sharing that one citation alone is noise. Stage B1 requires two or more citation kinds, a USC or Statutes hit specifically, or a Public Law hit plus non-trivial title overlap.
- **Two citation kinds together is still not always enough.** The Postal Regulatory Commission's ratemaking mandate (M03135) was matching PRC's No FEAR Act Annual Report on the strength of two shared citations: both reports cite the Postal Accountability Act among their authorities. Stage B1 now requires title Jaccard at or above 0.15 alongside the citation match; otherwise the candidate routes to the judge.

Two further Stage B1 defects were found in the second audit round and are worth recording because both silently destroyed recall:

- **Multi-agency mandates could never pass B1's agency gate.** The House Doc lists government-wide reports (Agency Financial Reports, No FEAR Act reports) under "Multiple Executive Agencies and Departments," a name no individual filer's key can equal. B1's exact agency comparison therefore dropped citation-anchored matches like FTC's Agency Financial Report — shared statute, title Jaccard 0.06, no agency match, verdict `None`, gone without a trace. Multi-agency mandate rows are now treated as agency-compatible for *routing* (they go to the judge) but never auto-confirm.
- **Only the single top-scored mandate per package was ever considered.** A shared citation frequently maps to several House Doc rows — one per agency, or near-duplicate rows — and when the scoring put the wrong row first, the right runner-up never reached the judge. B1 now sends every qualifying mandate in the top three to adjudication.

#### 4.5.3 Stage B2: package-level fallback via title and agency

When no citation signal exists at all, Stage B2 falls back to token Jaccard between the GPO package title and the House Doc `nature_of_report`, blocked by canonicalized agency. This is the weakest deterministic signal, because mandate language and filing titles diverge in vocabulary even when they refer to the same report. Most Stage B2 outputs are written as candidates rather than as confident matches, with the judge making the final call.

A subtle bug discovered during audit: Stage B2 initially blocked by *single* agency only, which meant a Labor-filed report could not candidate-match a House Doc mandate listed under "Multiple Executive Agencies and Departments" even when both were obviously the same report (e.g., Labor's No FEAR Act Annual Report to mandate M02661). The fix allows multi-agency mandate buckets ("Multiple Executive Agencies", "Joint Responsibility") to always appear in the candidate set for otherwise-agency-blocked submissions. This recovered twelve-plus matches across the corpus.

#### 4.5.4 Stage outputs

The matcher writes two files: `matches.jsonl` (confident attachments) and `candidates.jsonl` (borderline cases for adjudication). The post-adjudication consolidation stage merges these into `final_matches.jsonl`.

### 4.6 Stage 5: large language model adjudication

The deterministic matcher's confident bucket is high-precision but low-recall. Its candidate bucket is the largest single source of potential additional matches. We adjudicate every candidate with **two independent large language models** (Anthropic's Claude and Google's Gemini), promoting to confident only when both judges return a verdict of *same*.

Three design choices need explaining.

**Why two judges — and what two judges do not buy.** A single LLM has unmeasurable failure modes. Running two independently gives us a cheap disagreement signal that flags exactly the cases where reasonable readers can differ. Both judges receive the same prompt and the same evidence (mandate text, GPO title, parsed citations on both sides, agency context) but no visibility into each other's verdicts. The honest caveat: because both judges share the prompt and evidence framing, their errors are *not* independent in the statistical sense. The clearest demonstration is in this very pipeline — the umbrella-mandate prompt defect described below fooled both judges identically until the prompt was fixed. Unanimity protects against idiosyncratic model error; it does not protect against shared framing error, and readers should weight it accordingly.

**Why "same" requires unanimity.** Disagreements and joint "unclear" verdicts are kept as candidates rather than promoted. The disagreement pool is the highest-information rows for any subsequent manual review: by construction, they are the rows where the two judges weighed the evidence differently. In a downstream investigative-journalism or oversight workflow, those are the rows worth a human's attention.

**Why the prompt distinguishes "is the same as" from "is covered by".** Some mandates are *umbrella* mandates. The Congressional Review Act (M02659) is the clearest case: every federal regulatory rule technically counts as a CRA submission. An early prompt let the judges call rule submissions "same" as the CRA umbrella, inflating the substantive match count by roughly 17 percent. The revised prompt asks explicitly: *do both records describe the same discrete mandate, or is one merely covered by the other as an instance of a broader category?* After re-judging, all eight over-promoted CRA matches were correctly rejected.

Verdict distribution in this run (387 candidates, 774 verdicts):

| Pair outcome | Candidates | Disposition |
|---|---:|---|
| Both judges said *same* | 163 | Promoted (111 new matches; 52 confirmations of existing Stage A attachments) |
| Both judges said *different* | 186 | Rejected (including 49 Stage A attachments stripped as GPO mis-tags) |
| Both judges said *unclear* | 4 | Kept as candidate |
| Judges disagreed | 34 | Kept as candidate |

*Table 2: LLM adjudication outcomes, counted at the candidate-pair level (each candidate receives one verdict from each of two judges). Promoted Stage A requirements expand to one match row per package that referenced the requirement, so the 111 promoted candidates yield 114 attachment rows in `final_matches.jsonl`.*

The judge harness is resumable by `(candidate_id, judge)` pair. One operational lesson from this run belongs in print: the judgment file does **not** invalidate itself when the candidate pool changes, and an earlier draft of this paper unknowingly reported verdict counts from a judgment file that was stale relative to the matcher that produced the candidates. Re-judging after any matcher change is now a hard step in the runbook. Wall-clock cost in this run was roughly fifteen minutes and a few U.S. dollars in API fees.

### 4.7 Stage 6: consolidation and scope filtering

`compare.py` reads the deterministic confident matches, applies the judges' verdicts, and produces the final outputs. Two consolidation decisions shape the headline numbers and need walking through.

#### 4.7.1 The honest denominator

The naïve coverage number is `133 / 3,250 = 4.1 percent` (both figures post-adjudication — an earlier draft compared a pre-adjudication numerator against a post-adjudication one, which is incoherent; numerators on both sides of any comparison in this paper are now drawn from the same post-judge artifacts). That naïve number is misleading for two distinct reasons, and the honest denominator applies two distinct filters.

**Filter one: cadence.** The full denominator includes mandates whose reporting cycle predates CMRA's start, many of which were satisfied through pre-CMR channels and whose next filing isn't due yet. The cadence classifier (`cadence.py`) parses each mandate's `when_expected` field into one of ten cadence labels: annual, quarterly, monthly, semiannual, biennial, triennial, one-time-with-deadline, on-demand, event-driven, and other. The in-scope filter keeps mandates that should have produced at least one filing within the GPO window:

- Recurring cadences (annual, quarterly, monthly, semiannual, biennial, triennial): in scope (1,022 mandates).
- One-time mandates with a deadline inside the window — at or after January 2024 *and* not later than the run year: in scope (115).
- On-demand mandates ("immediately upon", "promptly after receipt"): in scope (68).

Excluded: one-time mandates whose deadline passed before 2024 (697), one-time mandates not yet due — deadlines after the run date, which cannot be covered yet (23), event-driven mandates that may simply not have triggered (486), and the unclassifiable "other" bucket (839).

**Filter two: entity.** CMRA does not bind every entity the House Document lists (§2.1). Of the 1,205 cadence-in-scope mandates, 149 are assigned to entities the Act cannot reach: GAO (47, excluded by name), the President (89, not an "agency"), intelligence-community elements (10), and House/Senate/Architect-of-the-Capitol entities (3). Counting those as "uncovered" would penalize entities with no legal pathway into the collection, so the headline excludes them. (An earlier draft did count them, which both misstated the Act's coverage and deflated the result.)

This yields an in-scope denominator of **1,056 mandates** and a headline coverage figure of **103 / 1,056 = 9.8 percent**. With the exempt entities left in, the figure is 8.5 percent; the full sensitivity table in `REPORT.md` shows the coverage under every alternative scope choice, from 4.1 percent (naïve) to 10.1 percent (additionally excluding on-demand mandates).

Two judgment calls in the cadence filter deserve flags rather than burial. First, on-demand mandates are counted in scope even though their triggers are unobserved — epistemically the same situation as the excluded event-driven bucket; excluding them moves the headline to 10.1 percent, so the choice is visible but not decisive. Second, biennial and triennial mandates are counted in scope even though a particular mandate's cycle may not have come due inside a roughly 2.5-year window; excluding them yields 9.9 percent. The classifier is otherwise deliberately conservative: the "other" bucket contains some legitimate recurring mandates whose phrasings are not yet recognized (mostly things like "Within X days after the end of each session"), and we leave them out rather than risk over-counting. Each phrasing added to `_CADENCE_PATTERNS` in a future version will move mandates from "other" into scope.

#### 4.7.2 The two-tier overdue list

Cadence classification also drives an **overdue list**: mandates with a clear recurring cadence whose most recent matched filing is older than the cadence window. We split this list into two tiers because they carry very different evidentiary weight:

- **High-confidence overdue (39 mandates):** clear recurring cadence AND prior matched filings AND nothing recent. For these the matcher has *demonstrated* recall on this mandate at least once, so the current absence is the most defensible compliance signal in the dataset. It is still not proof: a recent filing could be missed if the agency changed its title format or GPO dropped the requirement tag, and a filing inside the statutory 30-day deposit window (plus GPO processing time) would not yet be visible. The freshness windows are padded well beyond each cadence to absorb most of this.
- **Lower-confidence overdue (992 mandates):** clear recurring cadence but no matched filings at all. These are contaminated by matcher recall problems (some are real non-compliance, others are real filings the matcher could not anchor) and we present them only as a secondary indicator.

#### 4.7.3 Procedural-compliance separation

The Congressional Review Act (M02659) is treated as an umbrella mandate (`is_umbrella: true` in `final_matches.jsonl`). Sixty-nine GPO packages are flagged as **procedural CRA compliance**: tagged with GPO requirement number 8070 (the CRA process), citing 5 U.S.C. 801 (the CRA statute) in their references block, or titled as a rule submission ("Final Rule on …"). In this corpus the title heuristic adds zero packages — every rule-titled package already carries a 5 U.S.C. 801 reference — but it stays as a guard against future packages with sparser metadata. CRA-procedural packages are reported separately from substantive matches, are not folded into coverage or the on-time rate, and deliberately remain in the orphan pool (they satisfy a filing process, not a discrete report mandate).

---

## 5. Validation: a reviewer-driven audit

Most pipelines end at consolidation. This one does not, because a recurring observation during construction was that **every spot-check by hand surfaced a defect.** The matcher was right on average, but manual spot-checks kept finding individual rows that were wrong in ways that, once understood, generalized to whole classes of error. So we formalized the spot-checks as a stage of the pipeline.

`audit.py` runs seven systematic checks against the consolidated artifacts and writes its findings to `compare_output/AUDIT.md`:

1. **Zero-coverage agency verification.** For each large entity with 0 percent in-scope coverage, the auditor independently verifies via five different fields (word-boundary regex on three agency-name fields, `governmentAuthor1` exact string, `organizationDisplayName`, GPO `docclass`, and the structured `agencyCodes` field) that no GPO package mentions that entity. This is what allows confident statements like "EPA, the Department of Energy, the Department of the Army, the Office of Management and Budget, and the Government Accountability Office have zero CMR-channel filings."
2. **Confident-match sampling, stratified by stage.** Random-seeded samples drawn separately from Stage A, Stage B1, Stage B2, and judge-promoted matches, so a reviewer can verify each matching path independently.
3. **LLM judge audit.** Verdict-shape summary plus stratified samples of *both agreed same*, *both agreed different*, and *disagreement* cases.
4. **Orphan failure-mode classification.** A stratified sample of orphan submissions per top-orphan agency, with each miss tagged by cause (`no_signal`, `has_refs_but_no_mandate_citation_overlap`, `weak_signal_below_threshold`, `near_miss_below_threshold`). The tag distribution tells future contributors where matcher improvements would yield the most recall.
5. **Near-duplicate mandate detection.** Identifies House Doc rows with identical entity + nature + authority, so reviewers can confirm filings have not attached to the wrong row.
6. **Cadence-flagged overdue verification.** Inspects the high-confidence overdue mandates (39 in this run) to confirm each truly has prior matched submissions and an unambiguous cadence.
7. **Cross-entity attachment verification.** Surfaces cases where a filing's agency differs from the matched mandate's listed entity (e.g., Coast Guard filings against DHS-listed mandates) and asks whether each is legitimate.

`AUDIT.md` opens with an executive summary in four buckets: what survived audit (safe to publish), findings that hold up (substantive conclusions), caveats a reader should know, and bugs the audit pass found and fixed. The last bucket is probably the most useful read for an outside reviewer. Most of the matcher's apparent complexity is the residue of a specific bug it now prevents, and reading the bug list is the fastest path to understanding why each stage looks the way it does.

Twelve substantive defects were identified and fixed during the construction-time audit. They fall into four categories:

- **Citation-parsing bugs** (multi-letter U.S.C. sections truncated, references-block citations not lifted into the search).
- **Matcher-thresholding bugs** (single-citation Stage B1 too permissive, two-citation Stage B1 still too permissive without title evidence, single-agency Stage B2 blocking unable to reach multi-agency mandate rows).
- **GPO-data-quality bugs the matcher did not defend against** (within-package requirement number repeats, GPO's own mis-tagging of packages, empty `requirement` records shadowing populated ones).
- **Prompt and counting bugs** (the umbrella-mandate "covered by is not the same as" distinction, CRA double-counting between substantive and procedural compliance).

A second audit round — two independent reviewers reading a complete draft of this paper against the artifacts, plus one reader question that doubled as a spot-check — found nine more, fixed in this revision:

1. **Ampersand canonicalization.** The agency normalizer stripped `&` as punctuation instead of normalizing it to "and," so the House Doc's "Centers for Medicare & Medicaid Services" could never key-match GPO's "Centers for Medicare and Medicaid Services."
2. **Stage B1's agency gate excluded all multi-agency mandates** (§4.5.2), silently dropping citation-anchored matches like agency AFRs.
3. **Stage B1 judged only the top-scored mandate per package** (§4.5.2), so correct runner-ups on shared citations never reached adjudication.
4. **Judge-promoted Stage A matches collapsed to one placeholder row** in `final_matches.jsonl` while the coverage view attached one row per package — the two artifacts disagreed by exactly the expansion difference. They now reconcile, and the pipeline warns if they ever diverge again.
5. **The naïve and in-scope coverage figures used numerators from different pipeline stages** (pre- vs. post-adjudication), making the draft's naïve numerator smaller than its in-scope numerator — an arithmetic impossibility for a subset.
6. **The headline denominator counted CMRA-exempt entities** (§4.7.1) — and the draft simultaneously misstated the Act's entity coverage, describing it as executive-branch-only when it in fact reaches legislative- and judicial-branch establishments.
7. **A stale judgment file.** Verdict counts were reported from a judgment file produced against an earlier candidate pool (§4.6).
8. **Misdocumented date semantics.** The draft claimed the ingestion window began in January 2024; the collection's deposit dates begin in October 2023, and packages carry three distinct date fields that the draft conflated (§3.2).
9. **Not-yet-due mandates in the denominator.** The scope filter admitted one-time mandates with any deadline at or after 2024 — including 23 with deadlines after the run date (out to 2046) that cannot possibly be covered yet. Surfaced by a reader asking the natural question "what is coverage among reports that newly came due?"; the window now has an upper bound.

One reviewer-reported defect did not survive verification, which is worth recording as evidence the process can also *clear* code: rule-titled packages were reported as escaping the CRA-procedural classifier, but every such package already carried a 5 U.S.C. 801 reference and was classified correctly; the reviewer had misread orphan-pool membership (deliberate, §4.7.3) as misclassification.

Each defect is documented in `AUDIT.md` with the spot-check that surfaced it. The pattern matters more than any individual fix: the audit harness is what kept the pipeline honest — and the second round demonstrates that the construction-time audit, run by the pipeline's author, was *not sufficient on its own*. Fresh eyes against the artifacts found accounting defects the original audit's checks were never designed to catch. We recommend that anyone replicating this work treat both audit forms — systematic in-pipeline checks and adversarial post-hoc review — as part of the method.

---

## 6. Results

### 6.1 Headline numbers

Against the in-scope denominator of 1,056 mandates — assigned to a CMRA-covered entity, on a cadence that should have produced at least one CMR filing during the window:

- **103 mandates (9.8 percent) have at least one matched filing.** (8.5 percent if CMRA-exempt entities are left in the denominator; the full sensitivity table is in `REPORT.md`.)
- **Brand-new obligations fare no better.** Among one-time mandates that came due inside the window (deadlines 2024–2025, fully elapsed), coverage is 5 of 76 — 6.6 percent, all five with 2024 deadlines and none from 2025. The hypothesis that agencies at least route *new* obligations through the new channel is not supported.
- **302 substantive mandate-to-submission attachments** survive the full pipeline (188 deterministic, 114 judge-promoted). This figure now reconciles exactly with `final_matches.jsonl` and the on-time denominator below.
- **69 CRA-related rule submissions** are tracked separately as procedural compliance.
- **On-time rate, among matched substantive filings: 77.5 percent** (234 on time, 68 late; every matched package carries GPO's `isOnTime` flag in this corpus), measured by GPO's own flag and conditioned on the self-selected population that files via CMR at all (§8.7).
- **39 high-confidence overdue mandates** (clear cadence, prior matched filings, no recent filing).
- **762 orphan filings** that the matcher could not attach to any specific mandate. Of these, 69 are CRA-procedural by design, leaving **693 true recall-loss orphans** — of which 21 come from entities with no House Doc rows at all (§8.5).

### 6.2 Coverage by reporting entity

Coverage varies sharply by entity. A representative slice (in-scope mandates with at least three rows in scope) is shown in Table 3.

| Entity | In-scope mandates | Covered | Coverage |
|---|---:|---:|---:|
| Office of National Drug Control Policy | 4 | 4 | 100% |
| Administrative Office of the U.S. Courts | 7 | 6 | 86% |
| Railroad Retirement Board | 5 | 4 | 80% |
| Coast Guard | 4 | 2 | 50% |
| Department of Labor | 13 | 4 | 31% |
| Multiple Executive Agencies and Departments | 40 | 12 | 30% |
| Department of Health and Human Services | 107 | 29 | 27% |
| Department of Veterans Affairs | 46 | 11 | 24% |
| Department of Agriculture | 29 | 4 | 14% |
| Department of Justice | 68 | 4 | 6% |
| Department of the Interior | 36 | 2 | 6% |
| Department of Defense | 64 | 0 | 0% |
| Department of Energy | 54 | 0 | 0% |
| Department of Transportation | 42 | 0 | 0% |
| Environmental Protection Agency | 23 | 0 | 0% |
| Department of Homeland Security | 22 | 0 | 0% |
| Office of Management and Budget | 16 | 0 | 0% |

*Table 3: CMR-channel coverage by reporting entity (selected rows; full table in REPORT.md). CMRA-exempt entities — GAO (47 in-scope mandates) and the President (89) chief among them — no longer appear here because they are outside the denominator; their absence from the collection is by law, not delinquency.*

A few patterns are visible. Small agencies with concentrated reporting portfolios (ONDCP, AOUSC, RRB) lead; AOUSC is notable as a *judicial-branch* establishment filing at 86 percent, direct evidence that CMRA's coverage extends beyond the executive branch. Government-wide mandates filed per-agency ("Multiple Executive Agencies") now sit at 30 percent after the second-round matcher fixes unblocked them. Large departments with many reporting obligations (HHS, VA) sit in the middle. Several cabinet-level entities sit at zero, and the audit verifies that this isn't a matcher artifact: they file no packages into the CMR collection at all.

### 6.3 The five-agency zero-filing finding

The most striking individual finding is that five major entities have **zero filings in the CMR collection**: the Department of Energy, the Department of the Army, EPA, OMB, and GAO. The zero is verified across five different metadata fields (three agency-name fields under word-boundary regex, `governmentAuthor1`, `organizationDisplayName`, GPO `docclass`, and `agencyCodes`). One honest limit on that verification: the five fields are attributes of the same GPO record, not independent sources — a filing deposited under an unexpected name (a contractor, a sub-bureau alias, a delegated filer) would evade all five together. Delegation is a particular caveat for presidential mandates, which agencies often prepare and file under their own names.

The zeros mean different things. GAO is excluded from CMRA by name; its zero is correct by design and it sits outside the headline denominator. The other four are entities CMRA covers and binds. The Department of Defense is the next-most-striking case: four total filings across the entire DoD family (Defense Department plus Army, Navy, Air Force, Marines) against 64 in-scope mandates, and only one of those filings carries a statutory citation the matcher could anchor.

The same pattern is sharpest across the **Executive Office of the President**, where some sub-entities participate actively (ONDCP with 22 packages, OSTP with 14, the National Science and Technology Council with 4, the U.S. Global Change Research Program with 1) and others do not at all (OMB with 0, the U.S. Trade Representative with 0, the President with 0). The pattern is unlikely to be coincidence and is worth investigating as a question about CMR uptake within the Executive Office.

### 6.4 The high-confidence overdue list

The 39 high-confidence overdue mandates (clear recurring cadence, prior matched filings, nothing recent) are the dataset's most defensible non-compliance signal — with the caveats of §4.7.2: demonstrated past recall is not a guarantee of present recall, and dates below are GPO *deposit* dates (§3.2), which is why several fall in the collection's pre-launch trickle in late 2023. Five illustrative entries:

- *Department of Veterans Affairs*: On-campus educational and vocational counseling (annual; last filing 2023-11-29).
- *Department of Veterans Affairs*: Information on student's progress submitted by educational institutions (annual; last 2023-11-30).
- *Department of Health and Human Services*: Social and economic conditions of American Indians, Native Hawaiians, and other Native American populations (annual; last 2023-12-07).
- *Interagency Autism Coordinating Committee*: Advances in autism spectrum disorder research (annual; last 2023-12-19).
- *Department of Justice*: Quarterly reporting on Attorney General referrals for failure to grant employment or reemployment rights (quarterly; last 2023-12-26).

Full details are in `compare_output/overdue_mandates.jsonl`.

---

## 7. The artifacts published

All intermediate and final artifacts are written under `data/` and `compare_output/` as line-delimited JSON (with `REPORT.md` and `AUDIT.md` as the human-readable headlines). Table 4 lists what each artifact answers.

| File | Answers |
|---|---|
| `data/cmra_extract.jsonl` | What does Congress require, by mandate? |
| `data/gpo/submissions.jsonl` | What did agencies file, by package? |
| `data/gpo/requirements.jsonl` | Which mandates has GPO assigned its own internal ID? |
| `compare_output/REPORT.md` | The headline numbers. |
| `compare_output/AUDIT.md` | What survived audit and what didn't. |
| `compare_output/final_matches.jsonl` | Every confident mandate-to-filing pair. |
| `compare_output/mandate_coverage.jsonl` | Per-mandate: which filings (if any) matched. |
| `compare_output/submission_coverage.jsonl` | Per-filing: which mandate (if any) matched. |
| `compare_output/in_scope_uncovered_mandates.jsonl` | The actionable non-compliance backlog. |
| `compare_output/overdue_mandates.jsonl` | Cadence-flagged overdue mandates, both tiers. |
| `compare_output/orphan_submissions.jsonl` | Filings the matcher could not attach. |
| `compare_output/candidates.jsonl` | Borderline matches sent to the judge. |
| `compare_output/match_judgments.jsonl` | Per-judge verdicts on every candidate. |

*Table 4: Pipeline artifacts. Every artifact is regenerable from the inputs in `data/` without re-fetching the network.*

---

## 8. Limitations

We aim for the headline numbers to be defensible, not for them to be final truth. A candid summary of the limits:

### 8.1 The headline most plausibly understates channel uptake — but the argument has two sides

The matcher is conservative by design, and two failure modes push measured coverage **down** relative to real CMR-channel compliance:

- **Recall loss in the orphan pool.** 693 GPO submissions in this dataset are non-CRA orphans: real filings for which the matcher could not find a citation overlap and whose title was too distant from any candidate mandate to clear the similarity threshold. Some of these are real matches the pipeline missed. Audit classification (§5) suggests the largest single category is filings with no citation signal at all, which improves only with embedding-based title similarity or richer agency aliasing.
- **Cadence-classification conservatism.** The mandates in the "other" cadence bucket are excluded from the in-scope denominator. Some are legitimately on-demand or event-driven and belong out. Some are recurring but use phrasings the classifier does not yet recognize. Each newly-recognized phrasing changes both the denominator and, usually, the covered count; the sensitivity table in `REPORT.md` shows the bound (coverage with the entire "other" bucket counted in the denominator).

The most useful way to bound the headline is a floor-and-ceiling argument, both published in `REPORT.md` and regenerated on every run:

- **Floor — distrust the LLMs entirely.** Counting only deterministic citation- and title-anchored matches, with every judge-promoted match discarded, in-scope coverage is 6.0 percent (63 of 1,056). The adjudication layer is responsible for the lift from 6.0 to 9.8; a reader who rejects LLM adjudication outright still inherits the floor.
- **Ceiling — assume the matcher is perfect.** Each entity can cover at most as many mandates as it deposited packages. Summing those per-entity caps, a *flawless* matcher could reach at most 39.1 percent in-scope coverage (413 of 1,056) given the packages that actually exist in the collection. The remaining sixty-plus points of shortfall are matcher-proof: those filings are not in the repository under any name. The low headline is, first and foremost, a fact about the collection — 1,057 packages against 3,250 standing mandates — not an artifact of conservative matching.

Two forces push in the *other* direction, and a fair reading weighs them:

- **Residual false positives.** Every match path has a measured or suspected error rate. The Stage A validation pass is the cautionary example: of the low-title-overlap Stage A attachments routed to the judge, roughly two in five turned out to be GPO mis-tags. Those were caught *because* they were routed; the matches that were never flagged for validation have no equivalent measured error rate, only the audit's stratified samples as a spot check. We do not publish a formal precision estimate, and until one exists, "lower bound" is a directional claim, not a measured one.
- **Denominator-exclusion choices.** Excluding hard-to-classify mandates from the denominator mechanically raises the coverage figure if those mandates are disproportionately unmatched — and unmatched mandates are exactly the ones likely to be hard to classify. The scope-sensitivity table exists so a skeptical reader can see how much each exclusion moves the number rather than taking our judgment on faith. Two specific judgment calls deserve flags: *on-demand* mandates ("immediately upon," "promptly after receipt") are counted in scope even though their triggers are unobserved — the same epistemic situation as the excluded event-driven bucket — and *biennial/triennial* mandates are counted in scope even though a given mandate's cycle may not have come due inside a roughly 2.5-year window. The sensitivity table quantifies both choices.

### 8.2 CMR is not the only filing channel

"Zero packages in the CMR collection" is not the same as "files no reports to Congress." OMB-issued reports very likely flow through the President's Budget collection, the Federal Register, or direct committee correspondence rather than CMR. The numbers in this paper measure CMR-channel compliance specifically, and any policy reading of them should account for that.

### 8.3 LLM adjudication has unmeasurable failure modes

We measure judge quality two ways: by running two judges independently (and treating disagreement as a non-promotion signal) and by sampling both *same* and *different* verdicts in the audit. The visible failure mode in the present run was over-rejection during the original CRA-era prompt, not over-acceptance, but the absence of evidence for over-acceptance is not evidence of its absence. Spot-check protocols in the audit are intended to surface it if present.

### 8.4 Extraction errors propagate silently

Any error introduced by Stage 1 extraction propagates into every downstream number. The pipeline's defense is a separate extraction-accuracy harness that compares each extracted row against the PDF source: across a 481-row sample judged by three independent LLMs (1,259 completed verdicts), 95.8 percent of row-judgments passed. The harness's full design is out of scope for this paper, but the residual ~4 percent row-issue rate should be read as noise floor under every downstream figure — and the harness shares the LLM-judge caveats of §8.3.

### 8.5 The mandate register itself is incomplete

CDOC-119hdoc4 covers *standing* mandates. Statutes sometimes also contain one-off reporting directives that never made it into a standing-mandates compilation. Filings against such directives, when they exist, will appear in the dataset as orphans because the denominator side does not include them. This is not hypothetical: several agencies that actively file into CMR — the Nuclear Regulatory Commission, the Federal Labor Relations Authority, the EEOC, the Election Assistance Commission, the Udall Foundation — have **no rows at all** in the House Document register, so every one of their filings is structurally an orphan. `REPORT.md` quantifies this register-absent orphan pool.

### 8.6 Matching thresholds were tuned on the corpus they are evaluated on

The title-similarity thresholds (0.10 for Stage A validation routing, 0.15 and 0.25 in Stage B) were set in response to specific failures observed in this corpus and then evaluated on the same corpus. That is the right way to bootstrap a v1 and the wrong way to claim generality: the thresholds encode this corpus's vocabulary, and the defects fixed are, by construction, the defects the audit happened to find. A held-out evaluation — or simply the next Congress's edition of the House Document — is the real test.

### 8.7 The on-time rate is GPO's flag, on a self-selected population

The on-time figure uses GPO's own `isOnTime` flag, which GPO computes against its internal due date; we do not independently verify either the flag or the due-date register behind it. More importantly, the rate conditions on mandates that have matched CMR filings at all — a small, self-selected population plausibly drawn from the most compliance-attentive agencies. It should be read as "among reports that reach this channel, most arrive on time," not as a government-wide timeliness estimate.

---

## 9. Related work

We are not aware of prior published work that performs this specific join. Five adjacent strands exist:

- **Transparency advocacy around mandated reports.** The multi-year campaign for what became CMRA — led most visibly by Demand Progress and Daniel Schuman, alongside the Congressional Data Coalition — documented the problem this paper measures and maintained informal inventories of mandated reports before the GPO channel existed. That work motivated the statute; this paper measures the statute's uptake.
- **GAO's mandate-tracking work.** The Government Accountability Office internally maintains lists of mandates against which it tracks compliance for its own oversight reports. These lists are not, to our knowledge, published as a structured dataset, and would in any case not have included CMR-side filings before 2024.
- **Legal citation parsing.** Open-source citation parsers exist, most notably Free Law Project's *eyecite*, which is engineered primarily for case-law citations in judicial opinions. We hand-rolled a three-notation statutory parser instead because the citation grammar needed here (U.S. Code with multi-letter and dash-numbered sections, Public Law numbers, Statutes at Large pages, in hand-typed Clerk's-office style) is narrow, and because the failure modes we hit (§4.4.1) were specific enough to want direct control. A future version could delegate to eyecite where coverage overlaps.
- **Civic-tech projects that index U.S. federal documents.** Projects like govinfo.gov itself, the Congressional Research Service's Public Edition reports, and academic citation-graph projects on the U.S. Code provide pieces of the necessary infrastructure (citation parsing, agency taxonomies, document indices) but do not perform the mandate-to-filing join.
- **Record-linkage methodology.** The approach here is an informal instance of the classic Fellegi–Sunter paradigm: block on the strongest structured key, score weaker signals, and route the indeterminate middle band to a (here, LLM) adjudicator — a pattern now studied in its own right in the LLM-as-judge literature, whose central caveats (prompt sensitivity, correlated errors across judges) we encountered directly (§4.6). The novelty is the specific datasets and the explicit audit process that surfaced and corrected matcher defects.

---

## 10. Discussion

The core methodological claim is that **the statutory citation is the unit of truth.** When the same parsed citation tuple appears on both sides of the join, the records are almost certainly the same report. Everything else in the pipeline (agency canonicalization, title similarity, LLM adjudication, audit) exists to handle cases where the citation signal is absent, ambiguous, or mis-applied. We made citations the foundation rather than starting from text similarity because text similarity has no natural stopping point and produces a long tail of plausible-but-wrong matches that are expensive to disprove.

The **audit-as-pipeline-stage** discipline is probably the most generalizable contribution. The numbers in `REPORT.md` would be quietly wrong without the audit pass: twenty-one substantive defects across two audit rounds, each fixing not one row but a class of rows, were caught only because every unusual-looking match was treated as a bug report rather than as a curiosity. The split between the rounds is itself informative — the construction-time audit caught matching and data-quality defects, while the post-hoc independent round caught accounting and framing defects the author was too close to see. Downstream work on similar joins should treat audit as a co-equal stage with extraction and matching, not as a post-hoc activity — and should budget for at least one adversarial read by someone who didn't build the pipeline.

Finally, the picture of reporting compliance the dataset paints is unflattering: under 10 percent CMR coverage among the mandates the Act actually reaches, and four major covered entities (the Department of Energy, the Department of the Army, EPA, and OMB) with zero filings. Those numbers are real, but they are real with respect to a specific channel still in its early years, and §8.1 is explicit that the figure is a conservative estimate rather than a measured bound — matcher recall losses push it down, residual false positives and denominator choices push it up, and the sensitivity table brackets the denominator choices at roughly 7 to 10 percent. The interesting follow-up question is not "is 9.8 percent correct" but "which agencies are accelerating their CMR uptake over time, and which are not, and why." That question becomes answerable as the dataset is re-run on a rolling basis. The pipeline was designed for that re-run.

---

## 11. Reproducibility

The full pipeline is released as source code. End-to-end reproduction requires:

- A `GOVINFO_API_KEY` (free, from api.data.gov).
- An `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` for the LLM judge stage.
- Python 3.12 and the `uv` package manager.

The five operative commands are:

```
uv run python gpo_fetch.py        # ~6 minutes, resumable
uv run python match.py            # ~5 seconds
uv run python match_judge.py      # ~15 minutes, a few dollars
uv run python compare.py          # seconds
uv run python audit.py            # seconds
```

(Stage 1, the PDF extraction, runs automatically on first invocation of `match.py` if `data/cmra_extract.jsonl` is absent.) `REPORT.md` and `AUDIT.md` are written to `compare_output/`. Total wall-clock time from a clean checkout, end to end, is roughly twenty minutes; total API cost is a few U.S. dollars.

One honesty note on the word "reproducible": stages 1–4 and 6–7 are deterministic — same inputs, same outputs, byte for byte. Stage 5 is not. LLM verdicts can vary across runs and will certainly vary across model versions, and the judgment file is therefore a *recorded* artifact (published with the dataset, resumable by `(candidate_id, judge)` key) rather than a re-derivable one. A reader can re-run the judges and should expect smallish verdict drift at the margins; every number downstream of judging inherits that caveat. The detailed operator runbook lives in `RUNBOOK.md` alongside this paper.

---

## 12. Conclusion

The Congressionally Mandated Reports Act created a public filing channel without creating a public mandate register. The mandate register exists separately, as a 700-page printed PDF. This paper describes a reproducible pipeline that joins the two: extracting structured mandates from the PDF, ingesting filings from the GPO collection, normalizing statutory citations and agency names, matching deterministically where the signal is strong, and adjudicating borderline cases with two independently-prompted large language models before promoting to confident. A reviewer-driven audit harness, run as a stage of the pipeline itself, identified and fixed twelve substantive defects during construction; a second, adversarial review round by independent readers found nine more, and both rounds are documented alongside the headline numbers in `AUDIT.md` and §5.

The resulting dataset shows that 9.8 percent of CMRA-in-scope mandates — those assigned to entities the Act covers, on cadences that should have filed — have at least one matched filing in the GPO repository's first roughly two and a half years. Several major covered entities have zero filings. Among matched filings, 77.5 percent were submitted on time, within the self-selected population that uses the channel. These numbers are best read as a conservative estimate of CMR-channel uptake specifically — most plausibly an understatement, bracketed by a published sensitivity analysis — and as a starting point for the more interesting longitudinal question that the pipeline is designed to answer: which agencies are converging on the CMR channel over time, and which are not.

All artifacts are public, all source code is released, and the deterministic stages of the pipeline reproduce end-to-end in under fifteen minutes of wall-clock time, with the LLM adjudication published as a recorded artifact. We hope the method is useful as a template for further work on public-record joining problems where the two halves of the dataset were produced by different parts of government and were never intended to be joined — and the two-round audit record is offered as evidence that on this kind of work, the author's own audit is necessary but not sufficient.
