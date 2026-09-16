"""House Doc gap report: GPO filings whose mandate is absent from the House Document.

Input: compare_output/v2_pass2.jsonl (filings that survived BOTH v2 passes as
"none" — no plausible House Doc row agency-scoped or corpus-wide) plus
compare_output/v2_matches.jsonl (for entity-level gaps: filers whose agency
has zero House Doc rows at all).

Every filing in GPO's Congressionally Mandated Reports collection is, by
definition, congressionally mandated. So a filing with no House Doc row is
evidence the Clerk's list is missing that mandate. This script organizes that
evidence into distinct (agency, report) clusters and classifies each by how
strongly the absence is verified:

Citations here are DETERMINISTIC ONLY: parsed from the filing's own title
and GPO's `references` metadata. LLM-recalled statutes are deliberately
excluded — a 2026-06 OpenLaws sample found only 1 of 8 model-recalled cites
carried a report-to-Congress mandate at the cited section (models recall a
program's lead/definitions section, not the reporting-duty section).

  stated_cite_absent_from_housedoc — the filing itself states/carries a
      citation and it appears NOWHERE in any House Doc authority string.
      Strongest class: both ends of the chain are agency-asserted.
  stated_cite_present_in_housedoc — the filing's stated statute IS in the
      House Doc somewhere. Either a missed match (different subsection) or
      a Doc row that mis-describes the report. Needs review.
  no_stated_cite — neither the title nor GPO metadata carries a parseable
      citation. The gap evidence is the two-pass no-match alone; the true
      authority lives in the filing's transmittal letter (unread by this
      pipeline so far).

Outputs:
  compare_output/housedoc_gaps.jsonl — one row per gap cluster
  compare_output/housedoc_gaps.md    — human-readable summary

Usage:
  uv run python gap_report.py
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from match import ensure_extract
from normalize import parse_citations

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

OUT_DIR = REPO_ROOT / "compare_output"
PASS1_PATH = OUT_DIR / "v2_matches.jsonl"
PASS2_PATH = OUT_DIR / "v2_pass2.jsonl"
GAPS_JSONL = OUT_DIR / "housedoc_gaps.jsonl"
GAPS_MD = OUT_DIR / "housedoc_gaps.md"


def _latest_by_pid(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["package_id"]] = r
    return out


def _stem(title: str) -> str:
    """Collapse year-over-year editions of the same report to one cluster key."""
    t = re.sub(r"(fiscal year|fy|cy)?\s*20\d\d(\s*[-–]\s*20\d\d)?", "", title.lower())
    t = re.sub(r"[^a-z ]", " ", t)
    return " ".join(t.split()[:8])


def _stated_cites(sub: dict) -> dict[str, set[tuple]]:
    """Citations the filing itself carries: parsed from its GPO title plus
    GPO's structured `references` metadata. Never from model recall."""
    cits = parse_citations(sub.get("title", ""))
    refs = sub.get("references") or {}
    for kind in ("usc", "plaw", "stat"):
        cits[kind] |= {tuple(x) for x in refs.get(kind, [])}
    return cits


def main() -> None:
    mandates = ensure_extract()
    housedoc_cits: dict[str, set[tuple]] = {"usc": set(), "plaw": set(), "stat": set()}
    for m in mandates:
        c = parse_citations(m.get("authority", ""))
        for kind in housedoc_cits:
            housedoc_cits[kind] |= c[kind]

    pass1 = _latest_by_pid(PASS1_PATH)
    pass2 = _latest_by_pid(PASS2_PATH)

    # Gap filings: "none" in pass 2 (which only saw pass-1 nones).
    gap_filings = [r for r in pass2.values() if r["verdict"] == "none"]

    # Entity-level gaps: filer agencies with zero House Doc rows (pass-1 slice).
    zero_slice_agencies = sorted({
        pass1[r["package_id"]]["agency"]
        for r in gap_filings
        if r["package_id"] in pass1 and pass1[r["package_id"]].get("slice_size") == 0
    })

    # Cluster year-over-year editions of the same report.
    clusters: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in gap_filings:
        clusters[(r["agency"], _stem(r["title"]))].append(r)

    from match import load_jsonl, SUBS_PATH
    subs = {s["package_id"]: s for s in load_jsonl(SUBS_PATH)}

    rows = []
    for (agency, stem), filings in sorted(clusters.items()):
        # Deterministic cites only: union across the cluster's filings of
        # title-parsed + GPO-references citations.
        stated: dict[str, set[tuple]] = {"usc": set(), "plaw": set(), "stat": set()}
        for f in filings:
            c = _stated_cites(subs[f["package_id"]])
            for kind in stated:
                stated[kind] |= c[kind]
        all_stated = [(kind, cite) for kind in ("usc", "plaw", "stat") for cite in sorted(stated[kind])]
        if not all_stated:
            verification = "no_stated_cite"
        elif any(cite in housedoc_cits[kind] for kind, cite in all_stated):
            verification = "stated_cite_present_in_housedoc"
        else:
            verification = "stated_cite_absent_from_housedoc"
        rows.append({
            "agency": agency,
            "report": filings[0]["title"],
            "cluster_stem": stem,
            "filing_count": len(filings),
            "package_ids": sorted(f["package_id"] for f in filings),
            "stated_cites": [[kind, *cite] for kind, cite in all_stated],
            "verification": verification,
            # USC cite paired with a Pub. L. cite: when the USC section's
            # codified text carries no reporting duty, the mandate likely
            # lives in an uncodified session-law provision printed as a
            # statutory note (2026-06 OpenLaws sample: both "No" rows out of
            # 8 had this shape — 46 USC 8103 + PL 109-241, 5 USC 605 +
            # PL 104-121). Check the note, not just the section text.
            "uncodified_note_candidate": bool(stated["usc"] and stated["plaw"]),
            "agency_has_zero_housedoc_rows": agency in zero_slice_agencies,
            "model_reasoning": filings[0].get("reasoning"),
        })

    rows.sort(key=lambda r: (r["verification"], -r["filing_count"], r["agency"]))
    with GAPS_JSONL.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    by_v = defaultdict(list)
    for r in rows:
        by_v[r["verification"]].append(r)

    md = [
        "# House Document gap report",
        "",
        "GPO filings in the Congressionally Mandated Reports collection whose mandate",
        "has no row in the House Document (CDOC-119hdoc4), after two matching passes:",
        "agency-scoped (match_v2.py) and corpus-wide with entity mismatch allowed",
        "(match_v2_pass2.py). Regenerate with `uv run python gap_report.py`.",
        "",
        f"- **{sum(len(v) for v in by_v.values())} distinct (agency, report) gap clusters** "
        f"covering {sum(r['filing_count'] for r in rows)} filings",
        f"- **{len(by_v['stated_cite_absent_from_housedoc'])} with a filing-stated cite absent** from every House Doc authority",
        f"- **{len(by_v['stated_cite_present_in_housedoc'])} need review**: the filing's stated statute IS in the House Doc "
        "(missed match or mis-described row)",
        f"- **{len(by_v['no_stated_cite'])} with no stated cite**: gap evidence is the two-pass no-match alone; "
        "true authority is in the filing's transmittal letter (not yet read)",
        "",
        "> Citations are deterministic only: parsed from the filing's GPO title and",
        "> GPO `references` metadata. LLM-recalled statutes are excluded by design",
        "> (a 2026-06 sample found 7 of 8 recalled cites pointed at the wrong section).",
        "> Stated cites validate well: a 2026-06 OpenLaws read of 8 sampled sections",
        "> found 6 carry an explicit report-to-Congress mandate in codified text; the",
        "> 2 that don't both pair the USC cite with a Pub. L. cite, consistent with",
        "> the mandate living in an uncodified session-law note (marked † below).",
        "",
        "## Reporting entities with zero House Doc rows",
        "",
    ]
    md += [f"- {a}" for a in zero_slice_agencies] or ["- (none)"]

    sections = [
        ("stated_cite_absent_from_housedoc", "Filing-stated cite absent from House Doc"),
        ("stated_cite_present_in_housedoc", "Filing-stated statute present in House Doc — needs review"),
        ("no_stated_cite", "No stated cite (two-pass no-match only)"),
    ]
    for key, title in sections:
        md += ["", f"## {title} ({len(by_v[key])})", ""]
        md += ["| filings | agency | report | stated cites |",
               "|---|---|---|---|"]
        for r in by_v[key]:
            cites = "; ".join(
                f"{c[1]} U.S.C. {c[2]}" if c[0] == "usc"
                else (f"Pub. L. {c[1]}-{c[2]}" if c[0] == "plaw" else f"{c[1]} Stat. {c[2]}")
                for c in r["stated_cites"][:4]
            )
            marker = " †" if r["uncodified_note_candidate"] else ""
            md.append(
                f"| {r['filing_count']} | {r['agency']} | {r['report'][:90]} | {cites[:90]}{marker} |"
            )

    GAPS_MD.write_text("\n".join(md) + "\n")
    print(f"{len(rows)} gap clusters → {GAPS_JSONL} and {GAPS_MD}")
    for key, title in sections:
        print(f"  {title}: {len(by_v[key])}")
    print(f"  zero-row entities: {len(zero_slice_agencies)}")


if __name__ == "__main__":
    main()
