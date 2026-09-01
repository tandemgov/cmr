"""Produce the human-readable comparison report from match.py outputs.

Reads compare_output/ and writes:
  compare_output/REPORT.md  — narrative summary, top findings, sample tables
  compare_output/final_matches.jsonl  — confident matches + judge-promoted candidates

If match_judgments.jsonl exists, promotes any candidate where both judges
returned "same" into the confident bucket and downgrades any "different" to
no-match. Single-judge "same" or disagreements stay as candidates.

Usage:
  uv run python compare.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import re

from cadence import classify, cmra_exempt_reason, freshness_days, in_cmra_window
from normalize import agency_key

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("compare")

OUT_DIR = REPO_ROOT / "compare_output"
SUMMARY_PATH = OUT_DIR / "match_summary.json"
MATCHES_PATH = OUT_DIR / "matches.jsonl"
CANDIDATES_PATH = OUT_DIR / "candidates.jsonl"
UNCOVERED_PATH = OUT_DIR / "uncovered_mandates.jsonl"
ORPHAN_PATH = OUT_DIR / "orphan_submissions.jsonl"
MANDATE_COV_PATH = OUT_DIR / "mandate_coverage.jsonl"
# match.py owns the coverage views; rewrite_views_post_judge edits them in place, so a second run strips attachments that never come back.
POST_JUDGE_STAMP = OUT_DIR / ".post_judge_stamp"
JUDGMENTS_PATH = OUT_DIR / "match_judgments.jsonl"

FINAL_MATCHES_PATH = OUT_DIR / "final_matches.jsonl"
OVERDUE_PATH = OUT_DIR / "overdue_mandates.jsonl"
REPORT_PATH = OUT_DIR / "REPORT.md"

TODAY = date.today()

# Mandates that are *procedural process* obligations rather than discrete
# reports — each compliance instance is implicit in another submission, not a
# standalone fulfillment of the mandate. They get attached to a huge number of
# submissions (via GPO's own requirement tag) which inflates raw match counts.
# We track these separately rather than excluding them, so the headline metrics
# reflect actual report-vs-mandate compliance, not procedural compliance.
UMBRELLA_MANDATE_IDS = {
    "M02659",  # Congressional Review Act — every agency rule submission is technically a fulfillment
}

# Title shapes that mark a package as a Congressional Review Act rule
# submission even when GPO applied no requirement#8070 tag and the package's
# references block omits 5 U.S.C. 801 (e.g. FMC's "Final Rule on Carrier
# Automated Tariffs", FEC's "Final Rules on Candidate Salaries").
_CRA_RULE_TITLE = re.compile(
    r"^\s*((interim|direct)\s+)?(final|proposed)\s+rules?\b", re.I)


def is_cra_procedural(sub: dict) -> bool:
    """A package satisfies the CRA filing *process* (not a discrete report
    mandate) if it is tagged with GPO requirement #8070, cites 5 U.S.C. 801,
    or is titled as a rule submission."""
    if "8070" in (sub.get("requirement_numbers") or []):
        return True
    refs = sub.get("references") or {}
    if any(tuple(x) == ("5", "801") for x in (refs.get("usc") or [])):
        return True
    return bool(_CRA_RULE_TITLE.match(sub.get("title") or ""))


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def consolidate_judgments(judgments: list[dict]) -> dict[str, str]:
    """For each candidate_id, decide the final verdict from the judges' votes:
      - both 'same' → 'same'
      - both 'different' → 'different'
      - all 'unclear' or empty → 'unclear'
      - disagreement → 'unclear'
      - fewer than two usable verdicts → 'unclear'
    """
    by_cand: dict[str, list[str]] = defaultdict(list)
    for j in judgments:
        v = j.get("verdict")
        if v:
            by_cand[j["candidate_id"]].append(v)

    out = {}
    for cid, verdicts in by_cand.items():
        # One judge is not a consensus. Without this, a panel degraded by an outage silently promotes on a single vote.
        if len(verdicts) < 2:
            out[cid] = "unclear"
            continue
        vs = set(verdicts)
        if vs == {"same"}:
            out[cid] = "same"
        elif vs == {"different"}:
            out[cid] = "different"
        elif vs == {"unclear"}:
            out[cid] = "unclear"
        else:
            # Mixed
            out[cid] = "unclear"
    return out


def write_final_matches(
    matches: list[dict],
    candidates: list[dict],
    judge_verdicts: dict[str, str],
    submissions: list[dict],
) -> tuple[int, int]:
    """Confident matches + judge-promoted candidates → final_matches.jsonl.

    One row per (mandate, package) attachment — judge-promoted Stage A
    requirements expand to one row per package that referenced the
    requirement, so this file's row count agrees with the attachment counts
    in mandate_coverage.jsonl (an earlier version wrote a single placeholder
    row per promoted requirement, which made the on-time denominator and the
    match-row count disagree by exactly the expansion difference).

    Returns (confident_count, judge_promoted_count, a_validation_rejected).
    """
    promoted = 0
    subs_by_req: dict[str, list[dict]] = defaultdict(list)
    for s in submissions:
        for rn in s.get("requirement_numbers", []):
            subs_by_req[rn].append(s)
    written: set[tuple[str, str]] = set()  # (mandate_id, package_id) dedupe
    # Build a set of judge-rejected (mandate_id, package_id) pairs from
    # Stage A validation candidates so we can strip them from final output.
    a_validation_rejected: set[tuple[str, str]] = set()
    a_validation_total = 0
    for c in candidates:
        if c.get("stage") == "A_validation":
            a_validation_total += 1
            cid = c["mandate_id"] + "|AV" + c["package_id"]
            v = judge_verdicts.get(cid)
            if v == "different":
                a_validation_rejected.add((c["mandate_id"], c["package_id"]))

    confident_written = 0
    with FINAL_MATCHES_PATH.open("w") as f:
        for m in matches:
            key = (m["mandate_id"], m.get("package_id") or "")
            if key in a_validation_rejected:
                continue  # GPO mis-tag; judge says "different"
            if key in written:
                continue
            written.add(key)
            row = dict(m)
            row["origin"] = "stage_match"
            row["is_umbrella"] = row["mandate_id"] in UMBRELLA_MANDATE_IDS
            f.write(json.dumps(row) + "\n")
            confident_written += 1

        for c in candidates:
            if c["stage"] == "A":
                cid = c["mandate_id"] + "|" + f"R{c['requirement_number']}"
            elif c["stage"] == "A_validation":
                continue  # validation candidates only used to strip attachments above
            else:
                cid = c["mandate_id"] + "|" + f"P{c['package_id']}"
            v = judge_verdicts.get(cid)
            if v == "same":
                m = c["mandate"]
                if c["stage"] == "A":
                    # One row per package that referenced this requirement —
                    # mirrors what rewrite_views_post_judge attaches.
                    promo_subs = subs_by_req.get(c["requirement_number"], [])
                    via = f"requirement#{c['requirement_number']} (judge-promoted)"
                else:
                    promo_subs = [c["submission"]]
                    via = "title+agency (judge-promoted)"
                for s in promo_subs:
                    key = (c["mandate_id"], s["package_id"])
                    if key in written:
                        continue
                    written.add(key)
                    row = {
                        "mandate_id": c["mandate_id"],
                        "reporting_entity": m["reporting_entity"],
                        "nature_of_report": m["nature_of_report"],
                        "authority": m["authority"],
                        "package_id": s["package_id"],
                        "title": s["title"],
                        "submitting_agency": s["government_author"],
                        "submitted_to_gpo_date": s["submitted_to_gpo_date"],
                        "is_on_time": s["is_on_time"],
                        "via": via,
                        "origin": "judge_promoted",
                        "is_umbrella": c["mandate_id"] in UMBRELLA_MANDATE_IDS,
                    }
                    f.write(json.dumps(row) + "\n")
                    promoted += 1

    return confident_written, promoted, a_validation_rejected


def _coverage_fingerprint() -> str:
    return hashlib.sha256(MANDATE_COV_PATH.read_bytes()).hexdigest()


def assert_views_are_pristine() -> None:
    """Refuse to post-judge views that a previous run already post-judged.

    The edits are destructive and verdict-dependent: a second pass under
    different verdicts silently drops attachments the first pass stripped.
    """
    if not (POST_JUDGE_STAMP.exists() and MANDATE_COV_PATH.exists()):
        return
    if POST_JUDGE_STAMP.read_text().strip() == _coverage_fingerprint():
        raise SystemExit(
            "compare_output/mandate_coverage.jsonl was already rewritten by a previous "
            "compare.py run. Re-running now would strip attachments permanently. "
            "Regenerate the views first:\n\n    uv run python pipeline/match.py\n"
        )


def rewrite_views_post_judge(
    rejected: set[tuple[str, str]],
    judge_promoted_stage_a: dict[str, str],
    judge_promoted_stage_b: list[dict],
    submissions: list[dict],
) -> int:
    """After Stage A validation, rewrite mandate_coverage.jsonl and
    uncovered_mandates.jsonl to:
      - drop submissions the judge rejected (rejected set)
      - attach submissions to judge-promoted Stage A mandates that the
        matcher considered "candidate" — those wouldn't otherwise get the
        attachments because write_views only walks confident Stage A
        requirement→mandate links.

    Returns the count of newly-added attachments via judge-promoted Stage A.
    """
    rows = []
    for line in MANDATE_COV_PATH.read_text().splitlines():
        if not line.strip(): continue
        m = json.loads(line)
        m["submissions"] = [s for s in m["submissions"]
                            if (m["mandate_id"], s["package_id"]) not in rejected]
        m["submission_count"] = len(m["submissions"])
        rows.append(m)

    by_mid = {m["mandate_id"]: m for m in rows}
    sub_by_pid = {s["package_id"]: s for s in submissions}
    added = 0
    for req_num, mid in judge_promoted_stage_a.items():
        target = by_mid.get(mid)
        if not target:
            continue
        existing_pids = {s["package_id"] for s in target["submissions"]}
        for sub in submissions:
            if req_num not in sub.get("requirement_numbers", []):
                continue
            if sub["package_id"] in existing_pids:
                continue
            target["submissions"].append({
                "package_id": sub["package_id"],
                "title": sub["title"],
                "submitting_agency": sub["government_author"],
                "submitted_to_gpo_date": sub["submitted_to_gpo_date"],
                "required_at_gpo_date": sub["required_at_gpo_date"],
                "is_on_time": sub["is_on_time"],
                "via": f"requirement#{req_num} (judge-promoted)",
            })
            existing_pids.add(sub["package_id"])
            added += 1
        target["submission_count"] = len(target["submissions"])

    # Stage B judge-promoted: a specific (mandate, package) pair where the
    # LLM judge said "same" on a Stage B candidate. Add to coverage too.
    for promo in judge_promoted_stage_b:
        target = by_mid.get(promo["mandate_id"])
        if not target:
            continue
        existing_pids = {s["package_id"] for s in target["submissions"]}
        if promo["package_id"] in existing_pids:
            continue
        sub = sub_by_pid.get(promo["package_id"])
        if not sub:
            continue
        target["submissions"].append({
            "package_id": sub["package_id"],
            "title": sub["title"],
            "submitting_agency": sub["government_author"],
            "submitted_to_gpo_date": sub["submitted_to_gpo_date"],
            "required_at_gpo_date": sub["required_at_gpo_date"],
            "is_on_time": sub["is_on_time"],
            "via": "title+agency (judge-promoted)",
        })
        target["submission_count"] = len(target["submissions"])
        added += 1

    with MANDATE_COV_PATH.open("w") as f:
        for m in rows:
            f.write(json.dumps(m) + "\n")
    with UNCOVERED_PATH.open("w") as f:
        for m in rows:
            if m["submission_count"] == 0:
                f.write(json.dumps({
                    "mandate_id": m["mandate_id"],
                    "reporting_entity": m["reporting_entity"],
                    "nature_of_report": m["nature_of_report"],
                    "authority": m["authority"],
                    "when_expected": m["when_expected"],
                }) + "\n")
    # Rewrite orphan_submissions.jsonl from the canonical post-judge set —
    # otherwise packages the judge promoted still show as orphans.
    attached_now: set[str] = set()
    for m in rows:
        for s in m["submissions"]:
            attached_now.add(s["package_id"])
    with (OUT_DIR / "orphan_submissions.jsonl").open("w") as f:
        for sub in submissions:
            if sub["package_id"] not in attached_now:
                f.write(json.dumps(sub) + "\n")

    if rejected:
        logger.info("Stripped %d judge-rejected Stage A attachments from coverage views", len(rejected))
    if added:
        logger.info("Added %d submissions via judge-promoted Stage A requirement matches", added)
    return added


def analyze_overdue(mandate_cov: list[dict]) -> list[dict]:
    """For mandates with a clear recurring cadence (annual/quarterly/etc),
    flag those whose most-recent matched submission is older than the
    cadence's freshness window — or missing entirely.

    Returns sorted-by-staleness list. Writes overdue_mandates.jsonl.
    Skips event-driven/one-time mandates where 'overdue' isn't meaningful.
    """
    overdue: list[dict] = []
    cadence_counts: Counter = Counter()
    for m in mandate_cov:
        cad = classify(m.get("when_expected", ""))
        cadence_counts[cad] += 1
        window = freshness_days(cad)
        if window is None:
            continue  # event_driven, one_time, other — can't compute overdue
        latest = None
        for s in m["submissions"]:
            d = s.get("submitted_to_gpo_date")
            if d:
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d").date()
                    if latest is None or dt > latest:
                        latest = dt
                except ValueError:
                    pass
        if latest is None:
            days_since = None
            is_overdue = True
            reason = "no matched submission"
        else:
            days_since = (TODAY - latest).days
            is_overdue = days_since > window
            reason = f"last submission {days_since}d ago (window {window}d)" if is_overdue else None
        if is_overdue:
            overdue.append({
                "mandate_id": m["mandate_id"],
                "reporting_entity": m["reporting_entity"],
                "nature_of_report": m["nature_of_report"],
                "when_expected": m["when_expected"],
                "cadence": cad,
                "freshness_window_days": window,
                "latest_submission": latest.isoformat() if latest else None,
                "days_since_latest": days_since,
                "reason": reason,
            })
    overdue.sort(key=lambda r: (r["days_since_latest"] is not None, -(r["days_since_latest"] or 10**9)))
    with OVERDUE_PATH.open("w") as f:
        for r in overdue:
            f.write(json.dumps(r) + "\n")
    logger.info("Cadence breakdown: %s", dict(cadence_counts))
    logger.info("Overdue mandates (recurring + no recent submission): %d", len(overdue))
    return overdue


def in_scope_breakdown(mandate_cov: list[dict]) -> dict:
    """Partition mandates by CMRA in-scope status and compute per-bucket
    coverage. The *headline* denominator additionally excludes mandates
    assigned to entities that CMRA does not reach (GAO, the intelligence
    community, the Senate/House/Architect of the Capitol, and the President)
    — those rows can never appear in the CMR collection by law, so counting
    them as uncovered would penalize entities the statute doesn't bind.

    Returns counts + per-entity in-scope coverage table. `in_scope_total` /
    `in_scope_covered` are the headline (CMRA-covered entities only);
    `in_scope_total_all_entities` / `in_scope_covered_all_entities` keep the
    no-entity-filter figures for comparison."""
    by_reason = Counter()
    exempt_by_reason = Counter()
    in_scope_total = 0
    in_scope_covered = 0
    in_scope_total_all = 0
    in_scope_covered_all = 0
    by_entity_inscope: dict[str, dict] = defaultdict(lambda: {"in_scope": 0, "covered": 0, "subs": 0, "exempt": None})
    in_scope_uncovered: list[dict] = []

    for m in mandate_cov:
        in_scope, reason = in_cmra_window(m.get("when_expected", ""))
        by_reason[reason] += 1
        if not in_scope:
            continue
        e = m["reporting_entity"]
        covered = m["submission_count"] > 0
        in_scope_total_all += 1
        in_scope_covered_all += int(covered)
        exempt = cmra_exempt_reason(e)
        if exempt:
            exempt_by_reason[exempt] += 1
            continue
        in_scope_total += 1
        by_entity_inscope[e]["in_scope"] += 1
        if covered:
            in_scope_covered += 1
            by_entity_inscope[e]["covered"] += 1
            by_entity_inscope[e]["subs"] += m["submission_count"]
        else:
            in_scope_uncovered.append({
                "mandate_id": m["mandate_id"],
                "reporting_entity": m["reporting_entity"],
                "nature_of_report": m["nature_of_report"],
                "when_expected": m["when_expected"],
                "in_scope_reason": reason,
            })

    return {
        "by_reason": dict(by_reason),
        "exempt_by_reason": dict(exempt_by_reason),
        "in_scope_total": in_scope_total,
        "in_scope_covered": in_scope_covered,
        "in_scope_pct": (100 * in_scope_covered / in_scope_total) if in_scope_total else 0.0,
        "in_scope_total_all_entities": in_scope_total_all,
        "in_scope_covered_all_entities": in_scope_covered_all,
        "by_entity_inscope": dict(by_entity_inscope),
        "in_scope_uncovered": in_scope_uncovered,
    }


def scope_sensitivity(mandate_cov: list[dict]) -> list[dict]:
    """Coverage under alternative scope-filter choices, so a reader can see
    how much each judgment call moves the headline number. Each variant is
    (label, covered, total, pct)."""
    rows = []
    for m in mandate_cov:
        cad = classify(m.get("when_expected", ""))
        in_scope, reason = in_cmra_window(m.get("when_expected", ""))
        rows.append({
            "covered": m["submission_count"] > 0,
            "cadence": cad,
            "in_scope": in_scope,
            "reason": reason,
            "exempt": cmra_exempt_reason(m["reporting_entity"]) is not None,
        })

    def tally(label: str, pred) -> dict:
        pop = [r for r in rows if pred(r)]
        cov = sum(1 for r in pop if r["covered"])
        return {"label": label, "covered": cov, "total": len(pop),
                "pct": (100 * cov / len(pop)) if pop else 0.0}

    return [
        tally("Headline: in-scope cadence, CMRA-covered entities",
              lambda r: r["in_scope"] and not r["exempt"]),
        tally("… also counting CMRA-exempt entities (GAO, IC, President, House/Senate/AoC)",
              lambda r: r["in_scope"]),
        tally("… excluding on-demand mandates (trigger unobserved)",
              lambda r: r["in_scope"] and not r["exempt"] and r["reason"] != "on_demand"),
        tally("… excluding biennial/triennial (cycle may not be due in window)",
              lambda r: r["in_scope"] and not r["exempt"] and r["cadence"] not in ("biennial", "triennial")),
        tally("… adding the unclassified-cadence ('other') bucket to the denominator",
              lambda r: (r["in_scope"] or r["reason"] == "other") and not r["exempt"]),
        tally("All mandates, CMRA-covered entities (no cadence filter)",
              lambda r: not r["exempt"]),
        tally("All mandates, all entities (naive)",
              lambda r: True),
    ]


def write_report(
    summary: dict,
    matches: list[dict],
    candidates: list[dict],
    uncovered: list[dict],
    orphans: list[dict],
    mandate_cov: list[dict],
    judge_verdicts: dict[str, str],
    promoted: int,
    overdue: list[dict],
    scope: dict,
) -> None:
    # Post-judge coverage over ALL mandates (the honest naive numerator —
    # the pre-judge summary["mandates_covered"] is smaller and must not be
    # compared against the post-judge in-scope numerator).
    covered_all = sum(1 for m in mandate_cov if m["submission_count"] > 0)

    # Per-entity coverage
    entity_stats: dict[str, dict] = defaultdict(lambda: {"mandates": 0, "covered": 0, "subs": 0})
    for m in mandate_cov:
        e = m["reporting_entity"]
        entity_stats[e]["mandates"] += 1
        if m["submission_count"] > 0:
            entity_stats[e]["covered"] += 1
            entity_stats[e]["subs"] += m["submission_count"]

    rows_with_subs = sorted(
        [(e, v) for e, v in entity_stats.items() if v["mandates"] >= 3],
        key=lambda x: (-x[1]["covered"] / x[1]["mandates"], -x[1]["mandates"]),
    )

    # On-time rate — derive from the final mandate_coverage view (which
    # includes Stage B and Stage A judge promotions AND has had Stage A
    # mis-tags stripped). Exclude umbrella-mandate attachments since each
    # rule submission technically meets CRA but isn't a discrete report.
    # Track attachments where GPO supplied no isOnTime flag — the rate's
    # denominator is only the flagged subset and readers should see how
    # many rows it omits.
    on_time = not_on_time = on_time_unknown = 0
    substantive_attachments = 0
    for m in mandate_cov:
        if m["mandate_id"] in UMBRELLA_MANDATE_IDS:
            continue
        for s in m["submissions"]:
            substantive_attachments += 1
            if s.get("is_on_time") == "true":
                on_time += 1
            elif s.get("is_on_time") == "false":
                not_on_time += 1
            else:
                on_time_unknown += 1

    # Consistency check: final_matches.jsonl rows should agree with the
    # attachment count in mandate_coverage — both are one row per
    # (mandate, package). If they drift, something double- or under-counted.
    final_matches_path = REPO_ROOT / "compare_output/final_matches.jsonl"
    final_rows = load_jsonl(final_matches_path)
    substantive_match_count = sum(1 for fm in final_rows if not fm.get("is_umbrella"))
    if substantive_match_count != substantive_attachments:
        logger.warning(
            "final_matches substantive rows (%d) != mandate_coverage substantive attachments (%d)",
            substantive_match_count, substantive_attachments,
        )

    sensitivity = scope_sensitivity(mandate_cov)

    # Near-duplicate mandate rows (identical entity+nature+authority) — these
    # inflate the denominator and can absorb each other's filings; report the
    # count so readers can weigh it.
    dup_key_counts = Counter(
        (m["reporting_entity"], m["nature_of_report"], m["authority"]) for m in mandate_cov
    )
    dup_rows = sum(c for c in dup_key_counts.values() if c > 1)
    dup_groups = sum(1 for c in dup_key_counts.values() if c > 1)

    # CRA procedural-compliance signal: unique GPO submissions satisfying the
    # CRA filing process (requirement#8070 tag, 5 U.S.C. 801 reference, or a
    # rule-shaped title). Not substantive mandate matches, but real
    # procedural compliance worth reporting.
    raw_subs = load_jsonl(REPO_ROOT / "data/gpo/submissions.jsonl")
    cra_packages: set[str] = {
        sub["package_id"] for sub in raw_subs if is_cra_procedural(sub)
    }
    cra_tagged_submissions = len(cra_packages)

    # Orphan agency rollup
    orphan_agency = Counter()
    for s in orphans:
        orphan_agency[s.get("government_author") or "(unknown)"] += 1

    lines: list[str] = []
    lines.append("# CMRA — Mandates × GPO Submissions Comparison\n")
    lines.append(
        "Joins the House Doc catalog of every standing Congressional reporting "
        "mandate ([CDOC-119hdoc4](../data/CDOC-119hdoc4.pdf), 3,250 mandates) against the GPO CMR repository of "
        "actual report submissions (1,057 packages since Jan 2024). "
        "See [`AUDIT.md`](AUDIT.md) for reviewer findings and known limitations.\n"
    )

    # ── Executive summary ───────────────────────────────────────────────────
    lines.append("## Executive summary\n")
    pct = scope["in_scope_pct"]
    on_time_pct = (100 * on_time / (on_time + not_on_time)) if (on_time + not_on_time) else 0.0
    pct_all = (100 * scope["in_scope_covered_all_entities"] / scope["in_scope_total_all_entities"]) if scope["in_scope_total_all_entities"] else 0.0
    exempt_in_scope = scope["in_scope_total_all_entities"] - scope["in_scope_total"]
    lines.append(
        f"- **{scope['in_scope_covered']} of {scope['in_scope_total']} ({pct:.1f}%) CMRA-in-scope mandates "
        f"have at least one matched submission.** ('In-scope' = mandates assigned to a CMRA-covered entity "
        f"that should have produced ≥1 filing inside the GPO window — recurring cadences, one-time deadlines "
        f"due within the window, on-demand.) Counting the {exempt_in_scope} in-scope mandates assigned to CMRA-exempt "
        f"entities (GAO, intelligence community, the President, House/Senate/Architect of the Capitol — "
        f"which cannot legally file into CMR), the figure is "
        f"{scope['in_scope_covered_all_entities']} of {scope['in_scope_total_all_entities']} ({pct_all:.1f}%)."
    )
    lines.append(f"- **{substantive_attachments} substantive mandate↔submission attachments** (deterministic + LLM-judge-promoted), plus {cra_tagged_submissions} CRA-related rule submissions tracked separately as procedural compliance.")
    flagged = on_time + not_on_time
    lines.append(f"- **On-time submission rate, among attachments where GPO supplies an `isOnTime` flag: {on_time_pct:.1f}%** ({on_time:,} on time / {not_on_time:,} late; {on_time_unknown:,} attachments carry no flag and are excluded from the rate). Note this conditions on the small, self-selected population of mandates with matched CMR filings — it is not a government-wide on-time rate.")
    if overdue:
        with_some = sum(1 for o in overdue if o.get('days_since_latest') is not None)
        lines.append(f"- **{with_some} highest-confidence overdue mandates** — clear recurring cadence, prior matched submissions, but no recent filing past the cadence window. (Plus {len(overdue) - with_some} more flagged but no prior matches, see `overdue_mandates.jsonl`.)")
    lines.append("- **Several major entities have zero submissions in the CMR repository** (verified across 5 metadata fields — see AUDIT.md §1): EPA, Department of Energy, Department of the Army, OMB, GAO. **This means \"not in the CMR collection\" — not necessarily \"files no reports to Congress.\"** GAO is excluded from CMRA by name (and is excluded from the headline denominator for that reason); the executive-branch zeros appear to reflect filing via other channels (Federal Register, direct committee correspondence, agency websites) rather than the GPO CMR workflow. The pattern is most striking across the Executive Office of the President: ONDCP / OSTP / NSTC participate in CMR; OMB and USTR do not.")
    lines.append("")
    lines.append("This document presents the matching results. `AUDIT.md` explains how the matcher works, the 12 bugs found and fixed during reviewer-driven audit, and what limitations remain.\n")

    lines.append("## How matching works (one paragraph)\n")
    lines.append(
        "GPO assigns each submission a `requirement.number` when it can — that's a stable mandate ID maintained "
        "by GPO. **Stage A** matches GPO requirements to House Doc mandates by intersecting parsed citations "
        "(USC / Public Law / Statutes at Large). **Stage B** handles packages where GPO didn't apply a "
        "requirement tag — matching by package `references` block (B1) or title+agency token jaccard (B2). "
        "Every Stage A attachment with low title overlap, every Stage B1 single-citation match, and every "
        "Stage B2 candidate goes through an **LLM judge** (Claude + Gemini) before counting as confident. "
        "The judge correctly distinguishes \"covered by\" (e.g. every Labor rule is technically a CRA filing) "
        "from \"is the same mandate as.\"\n"
    )

    lines.append("## Pipeline numbers (technical)\n")
    lines.append(f"- **House Doc mandates:** {summary['mandates']:,}")
    if dup_groups:
        lines.append(f"  - of which {dup_rows:,} rows fall in {dup_groups:,} groups of exact near-duplicates (identical entity + nature + authority); duplicates inflate the denominator and a filing may attach to either twin.")
    lines.append(f"- **GPO CMR submissions (since 2024-01-01):** {summary['submissions']:,}")
    lines.append(f"- **Unique GPO requirement records:** {summary['requirements_unique']:,}")
    lines.append("")
    lines.append("### Stage A — requirement-level matching (citation-anchored)\n")
    lines.append(f"- Confident: **{summary['stageA_confident_requirements']}** requirements matched")
    lines.append(f"- Candidate (needs review): **{summary['stageA_candidate_requirements']}**")
    lines.append(f"- No match: **{summary['stageA_no_match_requirements']}**")
    lines.append("")
    lines.append("### Stage B — package-level fallback (title + agency, for packages with no requirement metadata)\n")
    lines.append(f"- Confident: **{summary['stageB_confident_packages']}** packages matched")
    lines.append(f"- Candidate: **{summary['stageB_candidate_packages']}**")
    lines.append(f"- No match: **{summary['stageB_no_match_packages']}**")
    lines.append("")
    lines.append("### Coverage view\n")
    pct_covered = 100 * covered_all / summary["mandates"] if summary["mandates"] else 0
    lines.append(f"- **Raw coverage (post-judge):** {covered_all:,} of {summary['mandates']:,} mandates have at least one matched submission ({pct_covered:.1f}%) — but this denominator includes mandates whose reporting cycle predates CMRA and mandates assigned to CMRA-exempt entities, so it understates real channel compliance.")
    lines.append("")
    lines.append("#### CMRA-in-scope coverage (the honest denominator)\n")
    lines.append(
        "Restricting the denominator to mandates assigned to a CMRA-covered entity that **should** have "
        "produced at least one submission in the GPO window (Jan 2024 – today). This excludes one-time "
        "deadlines that already passed pre-2024, event-driven mandates that may never have triggered, "
        "'other' mandates whose cadence we couldn't classify, and mandates assigned to entities CMRA "
        "does not reach (GAO and the intelligence community are excluded by the Act itself; the Senate, "
        "House, and Architect of the Capitol fall outside 40 U.S.C. 102; the President is not an 'agency'). "
        "Legislative- and judicial-branch *establishments* — CBO, the Library of Congress, AOUSC, the "
        "Sentencing Commission — **are** CMRA-covered and stay in the denominator."
    )
    lines.append("")
    exempt_in_scope2 = scope["in_scope_total_all_entities"] - scope["in_scope_total"]
    lines.append(f"- **In-scope mandates (CMRA-covered entities):** {scope['in_scope_total']:,}")
    lines.append(f"  - Recurring (annual/quarterly/monthly/semiannual): {scope['by_reason'].get('recurring', 0):,} (before entity filter)")
    lines.append(f"  - One-time with deadline due inside the window:    {scope['by_reason'].get('one_time_in', 0):,} (before entity filter)")
    lines.append(f"  - On-demand (immediately / promptly):              {scope['by_reason'].get('on_demand', 0):,} (before entity filter)")
    lines.append(f"  - In-scope but CMRA-exempt entity (excluded):      {exempt_in_scope2:,} — {scope['exempt_by_reason']}")
    lines.append(f"- **Excluded from denominator by cadence:** {summary['mandates'] - scope['in_scope_total_all_entities']:,}")
    lines.append(f"  - One-time deadline already passed pre-2024:       {scope['by_reason'].get('one_time_out', 0):,}")
    lines.append(f"  - One-time deadline not yet due (post-run-date):   {scope['by_reason'].get('one_time_future', 0):,}")
    lines.append(f"  - Event-driven (only fires when conditions arise): {scope['by_reason'].get('event_driven', 0):,}")
    lines.append(f"  - Cadence unclear ('within X days of receipt' etc): {scope['by_reason'].get('other', 0):,}")
    lines.append("")
    lines.append(f"- **In-scope coverage (headline):** **{scope['in_scope_covered']:,} of {scope['in_scope_total']:,} ({scope['in_scope_pct']:.1f}%)**")
    lines.append(f"- **Confident match attachments (substantive — mandate ↔ submission):** {substantive_attachments:,}")
    if cra_tagged_submissions:
        lines.append(f"- **CRA-related rule submissions (procedural compliance, not substantive matches):** {cra_tagged_submissions:,} *— GPO submissions either tagged with requirement #8070 OR citing 5 U.S.C. 801 (the Congressional Review Act). The LLM judge correctly distinguished individual rules from the umbrella CRA mandate, so they're not counted as substantive matches. Reported for transparency about CRA filing activity.*")
    lines.append(f"- **Orphan submissions (GPO records not matched to any mandate):** {len(orphans):,}")
    lines.append("")
    lines.append(
        "> Caveat on the in-scope coverage: matcher recall losses (see the orphan_submissions count) push "
        "the number **down**; residual matcher false positives and denominator-exclusion choices push it "
        "**up**. The sensitivity table below quantifies the denominator choices; the audit's stratified "
        "samples are the check on false positives. Treat the number as an estimate of CMR-channel uptake "
        "that most plausibly understates it, not as a measured floor.\n"
    )

    # Robustness floor and ceiling for the headline:
    #  - floor: coverage counting ONLY deterministic (citation/title) matches,
    #    no LLM promotions — answers "what if you distrust the judges entirely"
    #  - ceiling: coverage if matching were PERFECT, limited only by how many
    #    packages each entity actually deposited — answers "is the low number
    #    the matcher's fault or the collection's"
    inscope_mids = set()
    ins_by_entity_n: Counter = Counter()
    for m in mandate_cov:
        if in_cmra_window(m.get("when_expected", ""))[0] and not cmra_exempt_reason(m["reporting_entity"]):
            inscope_mids.add(m["mandate_id"])
            ins_by_entity_n[m["reporting_entity"]] += 1
    det_mids = {r["mandate_id"] for r in final_rows
                if r.get("origin") == "stage_match" and not r.get("is_umbrella")}
    det_floor = len(det_mids & inscope_mids)
    pkg_by_key: Counter = Counter()
    for s in raw_subs:
        for k in {agency_key(s.get(f, "")) for f in ("government_author", "organization_full", "organization_display_name") if s.get(f)}:
            pkg_by_key[k] += 1
    MULTI_KEYS = {"multiple executive agencies and departments", "joint responsibility"}
    ceiling = sum(
        min(n, len(raw_subs) if agency_key(e) in MULTI_KEYS else pkg_by_key.get(agency_key(e), 0))
        for e, n in ins_by_entity_n.items()
    )
    n_ins = len(inscope_mids)
    lines.append("### Robustness floor and ceiling\n")
    lines.append(
        f"- **Floor (no LLM at all):** counting only deterministic citation/title matches, "
        f"{det_floor:,} of {n_ins:,} in-scope mandates are covered ({100*det_floor/n_ins:.1f}%). "
        f"The LLM adjudication layer lifts this to the headline; a reader who distrusts the judges "
        f"entirely still gets this floor."
    )
    lines.append(
        f"- **Ceiling (perfect matching):** even if every deposited package were matched flawlessly, "
        f"per-entity package counts cap in-scope coverage at {ceiling:,} of {n_ins:,} ({100*ceiling/n_ins:.1f}%). "
        f"Everything above that ceiling is missing from the *collection*, not missed by the *matcher* — "
        f"the gap between the ceiling and 100% is matcher-proof evidence of non-filing."
    )
    lines.append("")

    lines.append("### Scope-filter sensitivity\n")
    lines.append(
        "The headline number depends on judgment calls in the scope filter. "
        "Coverage under each alternative:\n"
    )
    lines.append("| Scope variant | Covered | Denominator | Coverage |")
    lines.append("|---|---:|---:|---:|")
    for v in sensitivity:
        lines.append(f"| {v['label']} | {v['covered']:,} | {v['total']:,} | {v['pct']:.1f}% |")
    lines.append("")

    if on_time + not_on_time:
        lines.append("### On-time submission rate (substantive matches, excluding umbrella)\n")
        lines.append(f"- On time: {on_time:,} ({100*on_time/(on_time+not_on_time):.1f}%)")
        lines.append(f"- Late:    {not_on_time:,}")
        lines.append(f"- No `isOnTime` flag (excluded from rate): {on_time_unknown:,}")
        lines.append("")
        lines.append("> Umbrella-mandate attachments (CRA) are excluded from this rate because each individual rule submission technically satisfies CRA — including them would inflate the denominator with rule-by-rule procedural compliance, not discrete reports. The flag is GPO's own computation against its internal due date; we do not independently verify it.")
        lines.append("")

    if judge_verdicts:
        promo_pct = 100 * promoted / len(candidates) if candidates else 0
        same = sum(1 for v in judge_verdicts.values() if v == "same")
        diff = sum(1 for v in judge_verdicts.values() if v == "different")
        unclear = sum(1 for v in judge_verdicts.values() if v == "unclear")
        lines.append("### LLM judge adjudication of fuzzy candidates\n")
        lines.append(f"- Same:      {same}")
        lines.append(f"- Different: {diff}")
        lines.append(f"- Unclear:   {unclear}")
        lines.append(f"- Promoted to confident: **{promoted}** ({promo_pct:.1f}% of candidates)")
        lines.append("")

    lines.append("## Coverage by reporting entity (CMRA-in-scope, ≥3 in-scope mandates)\n")
    scope_rows = [
        (e, v) for e, v in scope["by_entity_inscope"].items() if v["in_scope"] >= 3
    ]
    scope_rows.sort(key=lambda x: (-x[1]["covered"] / x[1]["in_scope"], -x[1]["in_scope"]))
    lines.append("| Entity | In-scope mandates | Covered | Coverage % | Total submissions |")
    lines.append("|---|---:|---:|---:|---:|")
    for e, v in scope_rows[:30]:
        pct = 100 * v["covered"] / v["in_scope"]
        lines.append(f"| {e} | {v['in_scope']} | {v['covered']} | {pct:.0f}% | {v['subs']} |")
    if len(scope_rows) > 30:
        lines.append(f"\n…and {len(scope_rows)-30} more entities.\n")
    lines.append("")

    # Overdue analysis section (cadence + recency)
    if overdue:
        overdue_by_entity = Counter(o["reporting_entity"] for o in overdue)
        with_some_sub = [o for o in overdue if o["days_since_latest"] is not None]
        no_sub_at_all = [o for o in overdue if o["days_since_latest"] is None]
        cadence_breakdown = Counter(o["cadence"] for o in overdue)
        lines.append("## Cadence-based overdue analysis\n")
        lines.append(
            "Mandates with a clear recurring cadence (annual/quarterly/monthly/semiannual) whose "
            "most recent matched submission is older than the cadence freshness window. "
            "These are the rows where 'no submission' is least likely a time-window artifact and "
            "most likely real non-compliance. See `overdue_mandates.jsonl` for the full list.\n"
        )
        lines.append(f"- Total flagged: **{len(overdue):,}** mandates")
        lines.append(f"  - With *some* matched submission (just stale): {len(with_some_sub):,}")
        lines.append(f"  - With no matched submission at all:           {len(no_sub_at_all):,}")
        lines.append(f"- Cadence breakdown: {dict(cadence_breakdown)}")
        lines.append("")
        lines.append("Top entities by overdue count (recurring mandates only):\n")
        lines.append("| Entity | Overdue mandates |")
        lines.append("|---|---:|")
        for e, n in overdue_by_entity.most_common(15):
            lines.append(f"| {e} | {n} |")
        lines.append("")
        if with_some_sub:
            lines.append("Sample of overdue mandates that *do* have prior submissions (clearer signal):\n")
            for o in with_some_sub[:5]:
                lines.append(
                    f"- **{o['reporting_entity']}** | {o['cadence']} | last submission "
                    f"{o['latest_submission']} ({o['days_since_latest']}d ago, window {o['freshness_window_days']}d)"
                )
                lines.append(f"  - *{o['nature_of_report'][:120]}*")
            lines.append("")

    lines.append("## Orphan submissions by agency (top 15)\n")
    lines.append(
        "Submissions that didn't match any specific mandate. We split into two columns: **CRA-related** "
        "(rules citing 5 USC 801 or tagged with requirement#8070 — these are procedural CRA filings, "
        "individually correct but don't satisfy a specific report mandate), and **Other** (true orphans — "
        "reports without a clearly-matching House Doc mandate, or matching mandates the matcher couldn't "
        "anchor by citation or title).\n"
    )
    cra_orphan_pids: set[str] = {s["package_id"] for s in orphans if is_cra_procedural(s)}
    orphan_split: dict[str, dict[str, int]] = defaultdict(lambda: {"cra": 0, "other": 0})
    for s in orphans:
        agency = s.get("government_author") or "(unknown)"
        bucket = "cra" if s["package_id"] in cra_orphan_pids else "other"
        orphan_split[agency][bucket] += 1
    lines.append("| Agency | CRA-related orphans | Other orphans | Total |")
    lines.append("|---|---:|---:|---:|")
    for agency, _n in orphan_agency.most_common(15):
        b = orphan_split[agency]
        lines.append(f"| {agency} | {b['cra']} | {b['other']} | {b['cra'] + b['other']} |")
    lines.append("")
    non_cra_orphans = len(orphans) - len(cra_orphan_pids)
    lines.append(f"Non-CRA orphans (the true recall-loss pool): **{non_cra_orphans:,}** of {len(orphans):,} total orphans.\n")

    # Orphans from entities that have no House Doc rows at all — these are a
    # register-completeness problem (the mandate side is missing), not a
    # matcher recall problem, and no matcher improvement can attach them.
    house_doc_keys = {agency_key(m["reporting_entity"]) for m in mandate_cov}
    register_absent = Counter()
    for s in orphans:
        if s["package_id"] in cra_orphan_pids:
            continue
        keys = {
            agency_key(s.get(f, ""))
            for f in ("government_author", "organization_full", "organization_display_name")
            if s.get(f)
        }
        if keys and not (keys & house_doc_keys):
            register_absent[s.get("government_author") or "(unknown)"] += 1
    if register_absent:
        total_ra = sum(register_absent.values())
        lines.append(
            f"Of the non-CRA orphans, **{total_ra}** come from entities with **no rows in the House Doc "
            f"register at all** — filings against mandates the register omits (register incompleteness, "
            f"not matcher recall loss):\n"
        )
        for agency, n in register_absent.most_common(10):
            lines.append(f"- {agency}: {n}")
        lines.append("")

    if judge_verdicts:
        lines.append("## How to read the candidate-judging pipeline\n")
        lines.append(
            "The deterministic matcher produces 'confident' attachments (high signal) and 'candidate' attachments "
            "(borderline). Every candidate goes to both Claude and Gemini for verdict. We promote to confident only "
            "when both judges agree the records describe the same mandate. Sample outcomes (random selection from each bucket):\n"
        )
        import random as _rand
        rng = _rand.Random(0)
        # Group candidates by judge verdict bucket
        by_bucket: dict[str, list] = defaultdict(list)
        cand_by_id: dict[str, dict] = {}
        for c in candidates:
            if c.get("stage") == "A":
                cid = c["mandate_id"] + "|R" + c["requirement_number"]
            elif c.get("stage") == "A_validation":
                cid = c["mandate_id"] + "|AV" + c["package_id"]
            else:
                cid = c["mandate_id"] + "|P" + c["package_id"]
            cand_by_id[cid] = c
            v = judge_verdicts.get(cid, "no_judgment")
            by_bucket[v].append((cid, c))

        for bucket, label in [
            ("same", "**`same` (promoted to confident)** — agencies are filing this report"),
            ("different", "**`different` (correctly rejected)** — citation/title shared but topically distinct"),
            ("unclear", "**`unclear` or `mixed` (held back)** — genuine reviewer judgment calls"),
        ]:
            rows = by_bucket.get(bucket, [])
            if bucket == "unclear":
                rows = rows + by_bucket.get("no_judgment", [])
            if not rows:
                continue
            rng.shuffle(rows)
            lines.append(f"### {label}: {len(by_bucket.get(bucket, []))} cases\n")
            for cid, c in rows[:3]:
                m = c["mandate"]
                lines.append(f"- **{c['mandate_id']}** | *{m['reporting_entity'][:40]}* | {m['nature_of_report'][:90]}")
                if c["stage"] == "A":
                    r = c["requirement"]
                    lines.append(f"  - GPO requirement: {r.get('nature','')[:90]}")
                elif c["stage"] == "A_validation":
                    s = c["submission"]
                    lines.append(f"  - GPO submission: *{s['title'][:90]}* (attached via GPO requirement-tag)")
                else:
                    s = c["submission"]
                    lines.append(f"  - GPO submission: *{s['title'][:90]}*")
            lines.append("")

    REPORT_PATH.write_text("\n".join(lines))
    logger.info("Wrote %s", REPORT_PATH)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not SUMMARY_PATH.exists():
        raise SystemExit("Missing match_summary.json — run match.py first")

    summary = json.loads(SUMMARY_PATH.read_text())
    matches = load_jsonl(MATCHES_PATH)
    candidates = load_jsonl(CANDIDATES_PATH)
    uncovered = load_jsonl(UNCOVERED_PATH)
    orphans = load_jsonl(ORPHAN_PATH)
    mandate_cov = load_jsonl(MANDATE_COV_PATH)
    judgments = load_jsonl(JUDGMENTS_PATH)
    judge_verdicts = consolidate_judgments(judgments)

    raw_submissions = load_jsonl(REPO_ROOT / "data/gpo/submissions.jsonl")
    confident, promoted, a_validation_rejected = write_final_matches(matches, candidates, judge_verdicts, raw_submissions)
    # Build map of judge-promoted Stage A requirement→mandate matches so we
    # can wire their submissions into mandate_coverage.
    judge_promoted_stage_a: dict[str, str] = {}
    for c in candidates:
        if c.get("stage") == "A":
            cid = c["mandate_id"] + "|R" + c["requirement_number"]
            if judge_verdicts.get(cid) == "same":
                judge_promoted_stage_a[c["requirement_number"]] = c["mandate_id"]
    # Build list of judge-promoted Stage B (package-level) matches as well
    judge_promoted_stage_b: list[dict] = []
    for c in candidates:
        if c.get("stage") == "B":
            cid = c["mandate_id"] + "|P" + c["package_id"]
            if judge_verdicts.get(cid) == "same":
                judge_promoted_stage_b.append({"mandate_id": c["mandate_id"], "package_id": c["package_id"]})

    assert_views_are_pristine()
    rewrite_views_post_judge(a_validation_rejected, judge_promoted_stage_a, judge_promoted_stage_b, raw_submissions)
    POST_JUDGE_STAMP.write_text(_coverage_fingerprint() + "\n")
    # Reload mandate_cov, uncovered, and orphans — rewrite_views_post_judge
    # updated all three files; the variables loaded before judge processing
    # are now stale.
    mandate_cov = load_jsonl(MANDATE_COV_PATH)
    uncovered = load_jsonl(UNCOVERED_PATH)
    orphans = load_jsonl(ORPHAN_PATH)
    overdue = analyze_overdue(mandate_cov)
    scope = in_scope_breakdown(mandate_cov)
    # Persist the in-scope uncovered list separately — it's the most useful
    # "what's actually missing" view, distinct from the raw uncovered list.
    with (OUT_DIR / "in_scope_uncovered_mandates.jsonl").open("w") as f:
        for r in scope["in_scope_uncovered"]:
            f.write(json.dumps(r) + "\n")
    write_report(summary, matches, candidates, uncovered, orphans, mandate_cov, judge_verdicts, promoted, overdue, scope)
    logger.info("CMRA-in-scope: %d/%d (%.1f%%)", scope["in_scope_covered"], scope["in_scope_total"], scope["in_scope_pct"])
    print(f"Wrote {REPORT_PATH}")
    print(f"  confident matches:        {confident}")
    print(f"  judge-promoted to final:  {promoted}")


if __name__ == "__main__":
    main()
