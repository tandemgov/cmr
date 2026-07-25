---
theme: seriph
background: '#f8fafc'
title: Evaluating CMRA Compliance
info: |
  4-minute version. Full detail in WHITEPAPER.md.
class: text-center
highlighter: shiki
transition: slide-left
mdc: true
---

# Who is complying with the CMRA?

### The Access to Congressionally Mandated Reports Act promised every mandated report in one public repository. We measured whether that's happening.

---
layout: image-right
image: ./assets/gpo-cmr.png
backgroundSize: contain
---

# 1 · The question

The CMRA (Pub. L. 117-263, §§ 7241–7248) requires agencies to deposit congressionally
mandated reports with GPO, which publishes them on GovInfo (shown right). Deposits
began **October 2023**.

<v-click>

<div class="mt-6">
<div class="text-3xl font-bold">3,297 mandates</div>
<div class="text-sm opacity-90">what's owed · the Clerk's "House Doc," a 476-page PDF</div>
</div>
<div class="mt-4">
<div class="text-3xl font-bold">1,057 filings</div>
<div class="text-sm opacity-90">what's delivered · GPO's CMR collection, JSON API</div>
</div>

</v-click>

<v-click>

<div class="mt-6">
Built by different parts of government. <b>No shared key.</b>
</div>

</v-click>

---
layout: image-right
image: ./assets/housedoc-page.png
backgroundSize: contain
---

# 2 · Parse the House Doc

It's not a gentle PDF. Text rendered 90° rotated (`SSERGNOC`), bold simulated by
double-stamped glyphs, dot-leader noise, no table grid.

Deterministic extraction recovers **3,297 mandates**: entity, nature, statutory
authority, due date.

<v-click>

<div class="mt-6 p-3 bg-red-500/10 border border-red-500/40 rounded text-sm">
<b>The PDF fought back.</b> Six pages were typeset upright, invisible to a rotation-based
parser. They hid 47 mandates, including the <b>entire Nuclear Regulatory Commission</b>.
Parse failures masquerade as compliance findings.
</div>

</v-click>

---

# 3 · Match filings to mandates

**Take 1: citations as the anchor.** Parse USC, Pub. L., and Stat. cites from both sides;
intersect; LLM judges adjudicate the borderline pile. This works well when both sides cite
the same statute the same way, and it matched **22%** of filings. But structured citation
metadata is still sparse in these early deposits, so most filings never got compared to
anything at all.

<v-click>

**Take 2: invert the direction.** Each filing becomes the query. *"Here is every House Doc
mandate for your agency. Which one are you?"* The LLM proposes, deterministic signals
corroborate, and a corpus-wide second pass allows cross-entity matches (the Doc lists
CMS's report under SSA, USDA's under Interior).

</v-click>

<v-click>

<div class="mt-8 text-center text-xl">
<b>22% → 62%</b> of filings matched (659 of 1,057)<br/>
<span class="text-base">mandates with at least one deposit: <b>92 → 266</b></span>
</div>

</v-click>

---

# Three real rows

<div class="text-sm mt-1 mb-4">GPO filing on the left, House Doc mandate on the right. All three are live records.</div>

<div class="grid grid-cols-[5fr_3fr_5fr] gap-3 items-center text-sm">

<div class="p-2 bg-gray-500/10 rounded text-left">
<b>FY 2023 Office of Minority and Women Inclusion Annual Report</b><br/>
<span class="opacity-90">National Credit Union Administration</span>
</div>
<div class="text-center text-green-600 dark:text-green-400">
✓ matched<br/><span class="opacity-90">all 3 citation systems agree</span>
</div>
<div class="p-2 rounded text-left border border-green-600/40">
<b>M02714</b> · Actions taken by the Office of Minority and Women Inclusion<br/>
<span class="opacity-90">Multiple Executive Agencies and Departments</span>
</div>

<div v-click class="p-2 bg-gray-500/10 rounded text-left">
<b>CY 2020 Competitive Acquisition Ombudsman Report to Congress</b><br/>
<span class="opacity-90">Health and Human Services (CMS)</span>
</div>
<div v-click class="text-center text-green-600 dark:text-green-400">
✓ matched<br/><span class="opacity-90">same statute, listed under the <b>wrong agency</b></span>
</div>
<div v-click class="p-2 rounded text-left border border-green-600/40">
<b>M03212</b> · Activities of Competitive Acquisition Programs<br/>
<span class="opacity-90">listed under: Social Security Administration</span>
</div>

<div v-click class="p-2 bg-gray-500/10 rounded text-left">
<b>Report on the Condition of Education 2024</b><br/>
<span class="opacity-90">Department of Education · 20 U.S.C. 9545</span>
</div>
<div v-click class="text-center text-red-500 dark:text-red-400">
✗ no match<br/><span class="opacity-90">cite absent from all 3,297 rows</span>
</div>
<div v-click class="p-2 rounded text-left border border-dashed border-red-500/50 opacity-90">
<i>no row anywhere in the House Doc</i>
</div>

</div>

<v-click>

<img src="./assets/housedoc-ssa-rows.png" class="mt-4 rounded border border-gray-500/40 bg-white" />

<div class="text-sm mt-2 text-center">
Row 2's House Doc page, verbatim: §1847(f) is Medicare competitive bidding, run by CMS.<br/>
The row sits under SSA because the statute is the <i>Social Security Act</i>.
</div>

</v-click>

---

# 4 · The matches are imperfect. That's fine.

Where a statutory cite comes from determines whether it's evidence:

<div class="mt-4 text-sm">

| Provenance | Spot-check accuracy* | Use |
|---|---|---|
| **LLM recall** ("what statute mandates this?") | 1 of 8 | banned from the pipeline |
| **Filing-stated** (parsed from title + GPO metadata) | 6 of 8 carry verbatim *"shall submit to the Committee"* text | corroboration |

</div>

<div class="text-sm mt-2">*sections read in full against current U.S. Code text</div>

<v-clicks>

- Every LLM match ships with deterministic corroboration (citation overlap, token overlap, agency match), so reviewers sort by weakness
- The deterministic-only floor is published alongside every full-pipeline number
- Imperfect matching is a usable baseline, because both findings below survive at either end of it

</v-clicks>

---

# 5 · Finding one: adoption is still early

CMRA's scope is genuinely subtle (OMB M-23-17). Chamber-directed reports are covered at
any statute age; committee-directed reports only under newer statutes; several committees
are exempt entirely. Reasonable people can read the same mandate differently.

<v-click>

So we bracketed it: strict reading, maximal reading, and everything between. Under every
one, the answer is the same.

<div class="mt-6 text-center text-lg">
<b>Most mandates with a plausible deposit obligation<br/>have nothing in the repository yet.</b>
</div>

</v-click>

<v-click>

<div class="mt-6 text-sm">

- The encouraging part: the pipeline works. Nearly every deposit arrived exactly the way
  the Act envisioned, and agencies are even depositing reports beyond strict obligation
- The repository is young (deposits began October 2023), and the scope ambiguity is real:
  some of the shortfall is uncertainty, not unwillingness
- Full per-entity detail is published in <code>scoped_compliance.json</code> for anyone
  who wants to work the list

</div>

</v-click>

---

# 6 · Finding two: the House Doc has gaps too

**395 filings (37%) match nothing in the House Doc, after both passes.** Each is, by
definition, congressionally mandated: it's in the CMR collection. They collapse to
**298 distinct candidate gaps**, in three evidence tiers:

<v-clicks>

- **68 verified strongest**: the filing's own stated cite is absent from the entire Doc,
  and 6 of 8 spot-checked sections carry explicit committee-submission duties. Includes a
  contiguous band of Coast Guard reporting statutes (14 U.S.C. 5111–5113, 719, 903, 1155)
  and Education's flagship **Condition of Education** (20 U.S.C. 9545)
- **4 entities with zero rows anywhere**: EEOC, FLRA, Election Assistance Commission,
  Udall Foundation
- **The rest pending**: 58 are possible missed matches under listed statutes; 172 await
  authority extraction from their transmittal letters

</v-clicks>

<v-click>

<div class="mt-4 text-center">
The deposits audit the list. The list audits the deposits.<br/><b>Neither official record is ground truth.</b>
</div>

</v-click>

---

# 7 · Next steps: go to the source

Both findings are bounded by metadata quality. The fixes share one move:
**read the primary documents.**

<v-clicks>

- **Statutes as source data.** The mandate universe shouldn't be a typeset PDF. Derive it
  from the U.S. Code and session laws directly: destination (chamber vs. committee),
  cadence, and duty-holder are all in statute text we currently can't see
- **Transmittal letters as ground truth.** Every filed report states its own authority on
  page one, and GPO has the PDFs. Extracting those closes the 172 cite-less gap clusters
  and turns match corroboration into verification
- **Adversarial verification** of the 426 LLM matches, weakest corroboration first

</v-clicks>

---
layout: center
class: text-center
---

# What we can say today

<div class="mt-6 text-lg leading-relaxed">
The repository works, and most of what Congress is owed isn't in it yet.<br/>
The official mandate list is missing reports agencies actually file.<br/>
<b>Both gaps are closable.</b>
</div>

<v-click>

<div class="mt-10 text-sm opacity-75">To answer definitively, we need two things:</div>

<div class="mt-3 flex items-center justify-center gap-4">
<div class="px-4 py-2 border border-gray-500/40 rounded">a complete list of mandated reports</div>
<div class="text-2xl opacity-60">+</div>
<div class="px-4 py-2 border border-gray-500/40 rounded">a key to join it to what's filed</div>
</div>

</v-click>

<v-click>

<div class="mt-10 text-xl font-bold">That's the project.</div>

</v-click>
