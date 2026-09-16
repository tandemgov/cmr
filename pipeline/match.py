"""Match House Doc mandates to GPO CMR requirements/submissions.

Two stages:

Stage A — requirement-level (the primary path):
  For each GPO `requirement` record, find the best-matching House Doc mandate
  by intersecting structured citations (USC, PLAW, Stat) and then scoring by
  nature/jaccard + agency canonical match. Cheap blocking on citation keys
  keeps this tractable.

Stage B — submission-level fallback (for packages with empty requirement[]):
  Match the package directly to a mandate by (agency, title→nature similarity).

Outputs to compare_output/:
  matches.jsonl              — confident matches (mandate ↔ requirement_or_package)
  candidates.jsonl           — fuzzy candidates (for LLM judge + manual review)
  uncovered_mandates.jsonl   — House Doc rows with no match
  orphan_submissions.jsonl   — GPO packages with no match
  mandate_coverage.jsonl     — every mandate + every matched package_id
  submission_coverage.jsonl  — every package + its best mandate match
  match_summary.json         — counts for the run

Usage:
  uv run python match.py                  # full run, writes compare_output/
  uv run python match.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from normalize import (
    agency_key,
    canonicalize_agency,
    citation_overlap,
    jaccard,
    parse_citations,
    token_set,
)

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("match")

# How many qualifying Stage B1 mandates per package go to the LLM judge.
# A single shared citation frequently maps to multiple House Doc rows (one
# per agency, or near-duplicates); top-1 silently dropped correct runner-ups.
B1_TOP_K = 3

# Inputs
EXTRACT_PATH = REPO_ROOT / "data/cmra_extract.jsonl"  # cached House Doc extract
PDF_PATH = REPO_ROOT / "data/CDOC-119hdoc4.pdf"
REQS_PATH = REPO_ROOT / "data/gpo/requirements.jsonl"
SUBS_PATH = REPO_ROOT / "data/gpo/submissions.jsonl"
PKG_DIR = REPO_ROOT / "data/gpo/packages"

# Outputs
OUT_DIR = REPO_ROOT / "compare_output"
MATCHES_PATH = OUT_DIR / "matches.jsonl"
CANDIDATES_PATH = OUT_DIR / "candidates.jsonl"
UNCOVERED_PATH = OUT_DIR / "uncovered_mandates.jsonl"
ORPHAN_PATH = OUT_DIR / "orphan_submissions.jsonl"
MANDATE_COV_PATH = OUT_DIR / "mandate_coverage.jsonl"
SUBMISSION_COV_PATH = OUT_DIR / "submission_coverage.jsonl"
SUMMARY_PATH = OUT_DIR / "match_summary.json"

# ─────────────────────────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────────────────────────


def ensure_extract() -> list[dict]:
    """Run extraction/main.py to (re)build the House Doc extract cache if missing."""
    if not EXTRACT_PATH.exists():
        EXTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Building House Doc extract → %s", EXTRACT_PATH)
        with open(EXTRACT_PATH, "w") as out:
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "extraction" / "main.py"), str(PDF_PATH)],
                check=True,
                stdout=out,
            )
    mandates = []
    for i, line in enumerate(EXTRACT_PATH.read_text().splitlines()):
        if not line.strip():
            continue
        d = json.loads(line)
        d["mandate_id"] = f"M{i:05d}"  # synthetic stable index
        mandates.append(d)
    logger.info("Loaded %d mandates", len(mandates))
    return mandates


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Stage A: requirement-level match
# ─────────────────────────────────────────────────────────────────────────────


def build_mandate_index(mandates: list[dict]) -> dict:
    """Build blocking indexes from House Doc mandates so we can look up
    candidates by citation without scanning all 3,250 rows per requirement."""
    by_usc = defaultdict(list)    # (title, section) -> [mandate_idx]
    by_plaw = defaultdict(list)   # (congress, number) -> [mandate_idx]
    by_stat = defaultdict(list)   # (volume, page) -> [mandate_idx]
    parsed = []
    for i, m in enumerate(mandates):
        cits = parse_citations(m.get("authority", ""))
        parsed.append(cits)
        for k in cits["usc"]:
            by_usc[k].append(i)
        for k in cits["plaw"]:
            by_plaw[k].append(i)
        for k in cits["stat"]:
            by_stat[k].append(i)
    return {"usc": by_usc, "plaw": by_plaw, "stat": by_stat, "parsed": parsed}


def find_candidates(
    req_cits: dict[str, set],
    idx: dict,
) -> set[int]:
    """Return the small set of mandate indices that share at least one
    structured citation with this requirement."""
    out: set[int] = set()
    for k in req_cits["usc"]:
        out.update(idx["usc"].get(k, ()))
    for k in req_cits["plaw"]:
        out.update(idx["plaw"].get(k, ()))
    for k in req_cits["stat"]:
        out.update(idx["stat"].get(k, ()))
    return out


def score_requirement_vs_mandate(req: dict, mandate: dict, req_cits, mandate_cits) -> dict:
    """Score a single (requirement, mandate) pair.

    Returns dict with per-signal scores and a verdict bucket:
      'confident' | 'candidate' | 'reject'
    """
    overlap = citation_overlap(req_cits, mandate_cits)
    cit_kinds = sum(1 for v in overlap.values() if v > 0)

    req_nature = req.get("nature", "")
    m_nature = mandate.get("nature_of_report", "")
    n_jacc = jaccard(token_set(req_nature), token_set(m_nature))

    req_agency = canonicalize_agency(req.get("submitting_agency_canonical", ""))
    # Try every observed agency variant against the mandate's entity. If the
    # requirement is "Multiple Executive Agencies and Departments" the
    # canonical name on the requirement won't match anything specific — fall
    # back to seeing if the mandate's entity matches any agency we've seen
    # submit under this requirement.
    m_agency_key = agency_key(mandate.get("reporting_entity", ""))
    seen_keys = {agency_key(a) for a in req.get("submitting_agencies_seen", [])}
    agency_match = (
        m_agency_key == agency_key(req_agency)
        or m_agency_key in seen_keys
        or req_agency == "Multiple Executive Agencies and Departments"
    )

    # Bucketing. Special case: when the requirement's `nature` is empty
    # (15 of 73 — GPO records the requirement number on the package but not
    # the human-readable nature text), nature_jaccard is structurally 0 and
    # would always reject. In that case rely on citation + agency alone.
    nature_empty = not req_nature.strip()

    if nature_empty:
        if cit_kinds >= 2 and agency_match:
            verdict = "confident"
        elif cit_kinds >= 1 and agency_match:
            verdict = "candidate"
        else:
            verdict = "reject"
    elif cit_kinds >= 2 and n_jacc >= 0.4:
        verdict = "confident"
    elif cit_kinds >= 1 and n_jacc >= 0.4:
        verdict = "confident" if agency_match else "candidate"
    elif cit_kinds >= 1 and n_jacc >= 0.2:
        verdict = "candidate"
    elif cit_kinds == 0 and n_jacc >= 0.7:
        verdict = "candidate"
    else:
        verdict = "reject"

    return {
        "verdict": verdict,
        "citation_overlap": overlap,
        "citation_kinds_matched": cit_kinds,
        "nature_jaccard": round(n_jacc, 3),
        "agency_match": agency_match,
    }


def match_requirements(
    mandates: list[dict],
    requirements: list[dict],
    idx: dict,
) -> tuple[dict, list]:
    """Run Stage A. Returns:
      req_to_mandate: {requirement_number: {best mandate match info}}
      candidates: list of (requirement, mandate, score) for the candidate bucket
    """
    req_to_mandate: dict[str, dict] = {}
    candidates: list[dict] = []

    for req in requirements:
        req_cits = parse_citations(req.get("legal_authority", ""))
        # Fallback: if the requirement's authority TEXT is empty/unparseable,
        # use the structured `references` accumulated from its packages.
        if not (req_cits["usc"] or req_cits["plaw"] or req_cits["stat"]):
            refs = req.get("references") or {}
            req_cits = {
                "usc":  {tuple(x) for x in refs.get("usc",  [])},
                "plaw": {tuple(x) for x in refs.get("plaw", [])},
                "stat": {tuple(x) for x in refs.get("stat", [])},
            }
        cand_idxs = find_candidates(req_cits, idx)
        if not cand_idxs:
            req_to_mandate[req["requirement_number"]] = {
                "best": None,
                "verdict": "no_match",
                "reason": "no citation overlap with any mandate",
            }
            continue

        scored = []
        for mi in cand_idxs:
            m = mandates[mi]
            s = score_requirement_vs_mandate(req, m, req_cits, idx["parsed"][mi])
            s["mandate_id"] = m["mandate_id"]
            s["mandate_idx"] = mi
            scored.append(s)

        scored.sort(
            key=lambda s: (
                s["verdict"] != "confident",
                s["verdict"] != "candidate",
                -s["citation_kinds_matched"],
                -s["nature_jaccard"],
            )
        )
        best = scored[0]

        if best["verdict"] == "confident":
            req_to_mandate[req["requirement_number"]] = {
                "best": best,
                "verdict": "confident",
                "reason": _score_reason(best),
            }
        elif best["verdict"] == "candidate":
            req_to_mandate[req["requirement_number"]] = {
                "best": best,
                "verdict": "candidate",
                "reason": _score_reason(best),
            }
            candidates.append({
                "requirement_number": req["requirement_number"],
                "requirement": req,
                "mandate_id": mandates[best["mandate_idx"]]["mandate_id"],
                "mandate": mandates[best["mandate_idx"]],
                "score": best,
                "stage": "A",
            })
        else:
            req_to_mandate[req["requirement_number"]] = {
                "best": None,
                "verdict": "no_match",
                "reason": "best candidate rejected: "+_score_reason(best),
            }

    return req_to_mandate, candidates


def _score_reason(s: dict) -> str:
    parts = []
    o = s["citation_overlap"]
    if o["usc"]: parts.append(f"USC×{o['usc']}")
    if o["plaw"]: parts.append(f"PLAW×{o['plaw']}")
    if o["stat"]: parts.append(f"Stat×{o['stat']}")
    parts.append(f"nature={s['nature_jaccard']}")
    parts.append(f"agency={'✓' if s['agency_match'] else '✗'}")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Stage B: submission-level fallback for packages without a requirement
# ─────────────────────────────────────────────────────────────────────────────


def _refs_to_sets(refs: dict) -> dict[str, set[tuple]]:
    """Lift JSON-serializable refs (lists of lists) into the {usc, plaw, stat}
    set-of-tuples shape that citation_overlap() expects."""
    return {
        "usc":  {tuple(x) for x in refs.get("usc",  [])},
        "plaw": {tuple(x) for x in refs.get("plaw", [])},
        "stat": {tuple(x) for x in refs.get("stat", [])},
    }


def match_orphan_packages(
    mandates: list[dict],
    submissions: list[dict],
    idx: dict,
    no_match_reqs: set[str] | None = None,
) -> tuple[dict, list]:
    """For packages whose summary had no requirement[], match in two passes:

    B1 — citation-anchored (preferred): the package summary's `references`
         block carries parsed USC/PLAW/Stat. Use the same citation-overlap +
         agency check as Stage A.

    B2 — title+agency fallback: when no references available, score by
         token jaccard between GPO title and House Doc nature_of_report,
         blocked by canonical agency.

    Returns:
      pkg_to_mandate: {package_id: {verdict, best mandate match, stage_path}}
      candidates: list of (package, mandate, score) for human/judge review
    """
    by_agency: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(mandates):
        by_agency[agency_key(m["reporting_entity"])].append(i)

    # Multi-agency mandates (e.g. CFO Act AFR, No FEAR, OMWI) are *listed*
    # under "Multiple Executive Agencies and Departments" in the House Doc,
    # but each individual agency files them under its own name. So when
    # blocking Stage B2 by agency, we ALSO include all multi-agency mandates
    # as candidates — otherwise Labor's No FEAR submission can't find the
    # No FEAR mandate (M02661, listed under "Multiple Executive Agencies").
    MULTI_AGENCY_KEYS = {"multiple executive agencies and departments", "joint responsibility"}
    multi_agency_mandate_idxs = []
    for k in MULTI_AGENCY_KEYS:
        multi_agency_mandate_idxs.extend(by_agency.get(k, []))
    multi_agency_set = set(multi_agency_mandate_idxs)

    pkg_to_mandate: dict[str, dict] = {}
    candidates: list[dict] = []
    no_match_reqs = no_match_reqs or set()

    for sub in submissions:
        # If the package has requirement numbers AND any of them resolved to
        # a confident Stage A mandate match, skip Stage B for this package.
        # If ALL its requirements landed in the "no_match" bucket (e.g. empty
        # GPO requirement records like #12857), fall through to Stage B so
        # we can try title+agency matching.
        if sub["requirement_numbers"]:
            if any(rn not in no_match_reqs for rn in sub["requirement_numbers"]):
                continue
        pid = sub["package_id"]
        sub_agency_keys = {
            agency_key(sub.get(f, ""))
            for f in ("government_author", "organization_full", "organization_display_name")
            if sub.get(f)
        }
        agency_candidates = []
        for k in sub_agency_keys:
            agency_candidates.extend(by_agency.get(k, ()))
        # Always also consider multi-agency mandates (No FEAR, OMWI, AFR, etc.)
        agency_candidates.extend(multi_agency_mandate_idxs)
        agency_candidates = list(dict.fromkeys(agency_candidates))  # dedupe

        # ── B1: citation-anchored ────────────────────────────────────────────
        refs = _refs_to_sets(sub.get("references") or {})
        if any(refs.values()):
            cand_idxs = find_candidates(refs, idx)
            # Prefer agency-matching subset
            agency_set = set(agency_candidates)
            agency_hits = [mi for mi in cand_idxs if mi in agency_set]
            search = agency_hits or list(cand_idxs)
            if search:
                scored = []
                for mi in search:
                    m = mandates[mi]
                    overlap = citation_overlap(refs, idx["parsed"][mi])
                    kinds = sum(1 for v in overlap.values() if v > 0)
                    # Specificity-weighted citation strength: USC sections
                    # uniquely identify a topic; Stat citations are pretty
                    # specific too; PLAW alone is *noisy* (e.g. the 2008
                    # Farm Bill = Pub. L. 110-234 contains many unrelated
                    # mandates, so two reports both citing it may have
                    # nothing else in common).
                    only_plaw = overlap["plaw"] > 0 and overlap["usc"] == 0 and overlap["stat"] == 0
                    n_jacc = jaccard(token_set(sub["title"]), token_set(m["nature_of_report"]))
                    a_match = agency_key(m["reporting_entity"]) in sub_agency_keys
                    scored.append({
                        "mandate_idx": mi,
                        "mandate_id": m["mandate_id"],
                        "citation_overlap": overlap,
                        "citation_kinds_matched": kinds,
                        "only_plaw": only_plaw,
                        "title_jaccard": round(n_jacc, 3),
                        "agency_match": a_match,
                        # Mandates filed under "Multiple Executive Agencies" /
                        # "Joint Responsibility" can never pass the exact
                        # agency-key comparison above, but each individual
                        # agency files them under its own name (AFR, No FEAR,
                        # OMWI). Treat them as agency-compatible for *routing*
                        # purposes only — they never auto-confirm.
                        "multi_agency_mandate": mi in multi_agency_set,
                    })
                scored.sort(key=lambda s: (-s["citation_kinds_matched"], -int(not s["only_plaw"]), -int(s["agency_match"] or s["multi_agency_mandate"]), -s["title_jaccard"]))

                # Tightened thresholds: "PLAW only" is too weak by itself
                # because of big-bill collisions; require either ≥2 citation
                # kinds, or a USC/Stat hit, or 1 citation + nontrivial title
                # overlap before a candidate is even worth the judge's time.
                # And ≥2 citation kinds alone is NOT auto-confident if title
                # jaccard is near-zero — a third statute can bridge two
                # genuinely-different reports (the Postal Accountability Act
                # bridges PRC ratemaking and PRC's No FEAR submission). Route
                # those to the judge instead.
                # Single-citation matches (e.g. just USC) are *not* auto-confident,
                # because the same statute often covers multiple distinct
                # mandates (e.g. 21 USC 1706 covers both HIDTA budget submissions
                # and HIDTA methamphetamine reports — different subsections).
                # Only ≥2 citation kinds + agency match + non-trivial title
                # qualifies for confident; everything else routes to the judge.
                def _b1_verdict(s: dict) -> str | None:
                    agencyish = s["agency_match"] or s["multi_agency_mandate"]
                    if s["citation_kinds_matched"] >= 2 and s["agency_match"]:
                        return "confident" if s["title_jaccard"] >= 0.15 else "candidate"
                    if s["citation_kinds_matched"] >= 2 and s["multi_agency_mandate"]:
                        return "candidate"  # cross-agency rows never auto-confirm
                    if s["citation_kinds_matched"] >= 1 and agencyish and not s["only_plaw"]:
                        return "candidate"  # always judge-validate single USC/Stat citation matches
                    if s["only_plaw"] and agencyish and s["title_jaccard"] >= 0.25:
                        return "candidate"
                    if s["citation_kinds_matched"] >= 1 and not s["only_plaw"] and s["title_jaccard"] >= 0.15:
                        return "candidate"
                    return None

                # The same citation often maps to several mandate rows (one
                # per agency, or near-duplicate rows); judging only the single
                # top-scored mandate silently dropped the right runner-up.
                # Send every qualifying mandate in the top-K to the judge.
                top = [(s, _b1_verdict(s)) for s in scored[:B1_TOP_K]]
                top = [(s, v) for s, v in top if v]
                if top:
                    best, verdict = top[0]
                    rec = {"verdict": verdict, "best": best,
                           "reason": f"B1 cit_kinds={best['citation_kinds_matched']} agency={'✓' if best['agency_match'] else '✗'} title={best['title_jaccard']}",
                           "stage_path": "B1"}
                    pkg_to_mandate[pid] = rec
                    if verdict == "candidate":
                        seen_mids: set[str] = set()
                        for s_k, v_k in top:
                            if v_k != "candidate" or s_k["mandate_id"] in seen_mids:
                                continue
                            seen_mids.add(s_k["mandate_id"])
                            candidates.append({
                                "package_id": pid, "submission": sub,
                                "mandate_id": mandates[s_k["mandate_idx"]]["mandate_id"],
                                "mandate": mandates[s_k["mandate_idx"]],
                                "score": s_k, "stage": "B",
                            })
                    continue

        # ── B2: title+agency fallback ───────────────────────────────────────
        if not agency_candidates:
            pkg_to_mandate[pid] = {"verdict": "no_match", "reason": "B2 no agency match", "stage_path": "B2"}
            continue

        title_tokens = token_set(sub["title"])
        scored = []
        for mi in agency_candidates:
            m = mandates[mi]
            j = jaccard(title_tokens, token_set(m["nature_of_report"]))
            scored.append({"mandate_idx": mi, "mandate_id": m["mandate_id"], "title_jaccard": round(j, 3)})
        scored.sort(key=lambda s: -s["title_jaccard"])
        best = scored[0]

        # Stage B2 is the weakest signal — no citations, just agency+title
        # token jaccard. Boilerplate words ("fiscal year", "report") are
        # filtered by normalize._STOPWORDS but we still want a non-trivial
        # token overlap. 0.25 minimum cuts agency-boilerplate-only matches
        # while preserving genuine year-over-year same-report pairs.
        if best["title_jaccard"] >= 0.5:
            verdict = "confident"
        elif best["title_jaccard"] >= 0.25:
            verdict = "candidate"
        else:
            verdict = "no_match"

        rec = {"verdict": verdict, "best": best,
               "reason": f"B2 title_jaccard={best['title_jaccard']} (agency-blocked)",
               "stage_path": "B2"}
        pkg_to_mandate[pid] = rec
        if verdict == "candidate":
            candidates.append({
                "package_id": pid, "submission": sub,
                "mandate_id": mandates[best["mandate_idx"]]["mandate_id"],
                "mandate": mandates[best["mandate_idx"]],
                "score": best, "stage": "B",
            })

    return pkg_to_mandate, candidates


# ─────────────────────────────────────────────────────────────────────────────
# Roll up to bidirectional views
# ─────────────────────────────────────────────────────────────────────────────


def write_views(
    mandates: list[dict],
    submissions: list[dict],
    requirements: list[dict],
    req_to_mandate: dict,
    pkg_to_mandate_b: dict,
    idx: dict,
) -> dict:
    """Produce the final output files and return a summary dict."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build mandate → [submissions] using Stage A path:
    # mandate ← requirement_number ← packages that referenced that requirement
    # For multi-agency requirements (one mandate authority cited across many
    # agencies), we also need to gate by agency to avoid attaching e.g. EPA's
    # No FEAR submission to NRC's No FEAR mandate row.
    req_lookup = {r["requirement_number"]: r for r in requirements}
    # Index ALL confident matches per requirement (not just best one) so that
    # if a requirement legitimately covers many parallel mandate rows (one per
    # agency), each agency's submission can find its own row.
    # NOTE: must apply the same `references` fallback as match_requirements()
    # — otherwise requirements with empty legal_authority text (like req#8237
    # SSI) get zero attachments even when they have parsed references.
    req_to_all_mandates: dict[str, list[int]] = defaultdict(list)
    for req in requirements:
        req_cits = parse_citations(req.get("legal_authority", ""))
        if not (req_cits["usc"] or req_cits["plaw"] or req_cits["stat"]):
            refs = req.get("references") or {}
            req_cits = {
                "usc":  {tuple(x) for x in refs.get("usc",  [])},
                "plaw": {tuple(x) for x in refs.get("plaw", [])},
                "stat": {tuple(x) for x in refs.get("stat", [])},
            }
        for mi in find_candidates(req_cits, idx):
            m = mandates[mi]
            s = score_requirement_vs_mandate(req, m, req_cits, idx["parsed"][mi])
            if s["verdict"] == "confident":
                req_to_all_mandates[req["requirement_number"]].append(mi)

    # GPO sometimes mis-tags packages with the wrong requirement number
    # (e.g. CMR-FMC1-00195455 — an FMC Annual Report — is tagged with req#319,
    # which is about Presidential emergency war powers expenditures). My
    # matcher trusted those tags blindly. Now: for any Stage A submission
    # attachment whose title doesn't share at least *some* tokens with the
    # mandate's nature, emit a "Stage A validation" candidate so the LLM
    # judge can confirm or reject the GPO-tag-based attachment.
    STAGE_A_VALIDATION_THRESHOLD = 0.10

    mandate_to_subs: dict[str, list[dict]] = defaultdict(list)
    stage_a_validation_candidates: list[dict] = []
    for sub in submissions:
        sub_agency_key = agency_key(sub.get("government_author") or sub.get("organization_full") or "")
        sub_title_tokens = token_set(sub.get("title", ""))
        for rn in sub["requirement_numbers"]:
            cand_idxs = req_to_all_mandates.get(rn, [])
            # Prefer mandates whose entity matches this submission's agency.
            agency_matches = [
                mi for mi in cand_idxs if agency_key(mandates[mi]["reporting_entity"]) == sub_agency_key
            ]
            attach_to = agency_matches if agency_matches else cand_idxs[:1]  # fall back to first if no agency hit
            for mi in attach_to:
                m = mandates[mi]
                title_jacc = jaccard(sub_title_tokens, token_set(m.get("nature_of_report", "")))
                attachment = {
                    "package_id": sub["package_id"],
                    "title": sub["title"],
                    "submitting_agency": sub["government_author"],
                    "submitted_to_gpo_date": sub["submitted_to_gpo_date"],
                    "required_at_gpo_date": sub["required_at_gpo_date"],
                    "is_on_time": sub["is_on_time"],
                    "via": f"requirement#{rn}",
                    "title_jaccard": round(title_jacc, 3),
                }
                if title_jacc < STAGE_A_VALIDATION_THRESHOLD:
                    attachment["needs_validation"] = True
                    stage_a_validation_candidates.append({
                        "package_id": sub["package_id"], "submission": sub,
                        "mandate_id": m["mandate_id"], "mandate": m,
                        "score": {"title_jaccard": round(title_jacc, 3), "via": f"requirement#{rn}"},
                        "stage": "A_validation",
                    })
                mandate_to_subs[m["mandate_id"]].append(attachment)
    # Stage B path
    for pid, match in pkg_to_mandate_b.items():
        if match["verdict"] == "confident":
            sub = next(s for s in submissions if s["package_id"] == pid)
            mandate_to_subs[match["best"]["mandate_id"]].append({
                "package_id": pid,
                "title": sub["title"],
                "submitting_agency": sub["government_author"],
                "submitted_to_gpo_date": sub["submitted_to_gpo_date"],
                "required_at_gpo_date": sub["required_at_gpo_date"],
                "is_on_time": sub["is_on_time"],
                "via": "title+agency (no requirement#)",
            })

    # Submission → mandate (best confident match)
    sub_to_mandate: dict[str, dict] = {}
    for sub in submissions:
        pid = sub["package_id"]
        sub_agency_key = agency_key(sub.get("government_author") or sub.get("organization_full") or "")
        mid = None
        via = None
        for rn in sub["requirement_numbers"]:
            cand_idxs = req_to_all_mandates.get(rn, [])
            if not cand_idxs:
                continue
            agency_hits = [mi for mi in cand_idxs if agency_key(mandates[mi]["reporting_entity"]) == sub_agency_key]
            pick = agency_hits[0] if agency_hits else cand_idxs[0]
            mid = mandates[pick]["mandate_id"]
            via = f"requirement#{rn}"
            break
        if mid is None:
            m = pkg_to_mandate_b.get(pid)
            if m and m["verdict"] == "confident":
                mid = m["best"]["mandate_id"]
                via = "title+agency"
        sub_to_mandate[pid] = {"mandate_id": mid, "via": via}

    # Confident matches: emit as flat (mandate, submission, score) rows.
    confident_count = 0
    with open(MATCHES_PATH, "w") as f:
        for mid, subs in mandate_to_subs.items():
            m_idx = int(mid[1:])
            m = mandates[m_idx]
            for s in subs:
                f.write(json.dumps({
                    "mandate_id": mid,
                    "reporting_entity": m["reporting_entity"],
                    "nature_of_report": m["nature_of_report"],
                    "authority": m["authority"],
                    "package_id": s["package_id"],
                    "title": s["title"],
                    "submitting_agency": s["submitting_agency"],
                    "submitted_to_gpo_date": s["submitted_to_gpo_date"],
                    "is_on_time": s["is_on_time"],
                    "via": s["via"],
                }) + "\n")
                confident_count += 1

    # Uncovered mandates (no confident submission).
    uncovered = []
    for m in mandates:
        if not mandate_to_subs.get(m["mandate_id"]):
            uncovered.append(m)
    with open(UNCOVERED_PATH, "w") as f:
        for m in uncovered:
            f.write(json.dumps(m) + "\n")

    # Orphan submissions (no confident mandate).
    orphans = [s for s in submissions if sub_to_mandate[s["package_id"]]["mandate_id"] is None]
    with open(ORPHAN_PATH, "w") as f:
        for s in orphans:
            f.write(json.dumps(s) + "\n")

    # Mandate coverage view
    with open(MANDATE_COV_PATH, "w") as f:
        for m in mandates:
            subs = mandate_to_subs.get(m["mandate_id"], [])
            f.write(json.dumps({
                "mandate_id": m["mandate_id"],
                "reporting_entity": m["reporting_entity"],
                "nature_of_report": m["nature_of_report"],
                "authority": m["authority"],
                "when_expected": m["when_expected"],
                "submission_count": len(subs),
                "submissions": subs,
            }) + "\n")

    # Submission coverage view
    with open(SUBMISSION_COV_PATH, "w") as f:
        for sub in submissions:
            entry = sub_to_mandate[sub["package_id"]]
            mid = entry["mandate_id"]
            m = mandates[int(mid[1:])] if mid else None
            f.write(json.dumps({
                "package_id": sub["package_id"],
                "title": sub["title"],
                "submitting_agency": sub["government_author"],
                "matched_mandate_id": mid,
                "matched_mandate_entity": m["reporting_entity"] if m else None,
                "matched_mandate_nature": m["nature_of_report"] if m else None,
                "match_via": entry["via"],
            }) + "\n")

    # Per-entity rollup
    by_entity: dict[str, dict] = defaultdict(lambda: {"mandates": 0, "with_submission": 0, "total_submissions": 0})
    for m in mandates:
        e = m["reporting_entity"]
        by_entity[e]["mandates"] += 1
        subs = mandate_to_subs.get(m["mandate_id"], [])
        if subs:
            by_entity[e]["with_submission"] += 1
            by_entity[e]["total_submissions"] += len(subs)

    summary = {
        "mandates": len(mandates),
        "submissions": len(submissions),
        "requirements_unique": len(requirements),
        "stageA_confident_requirements": sum(1 for v in req_to_mandate.values() if v["verdict"] == "confident"),
        "stageA_candidate_requirements": sum(1 for v in req_to_mandate.values() if v["verdict"] == "candidate"),
        "stageA_no_match_requirements": sum(1 for v in req_to_mandate.values() if v["verdict"] == "no_match"),
        "stageB_confident_packages": sum(1 for v in pkg_to_mandate_b.values() if v["verdict"] == "confident"),
        "stageB_candidate_packages": sum(1 for v in pkg_to_mandate_b.values() if v["verdict"] == "candidate"),
        "stageB_no_match_packages": sum(1 for v in pkg_to_mandate_b.values() if v["verdict"] == "no_match"),
        "confident_match_rows": confident_count,
        "mandates_covered": sum(1 for m in mandates if mandate_to_subs.get(m["mandate_id"])),
        "mandates_uncovered": len(uncovered),
        "orphan_submissions": len(orphans),
        "top_entities_by_coverage": sorted(
            [(e, v["with_submission"], v["mandates"]) for e, v in by_entity.items() if v["mandates"] >= 5],
            key=lambda x: -x[1] / x[2] if x[2] else 0,
        )[:10],
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    summary["_stage_a_validation_candidates"] = stage_a_validation_candidates
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    mandates = ensure_extract()
    requirements = load_jsonl(REQS_PATH)
    submissions = load_jsonl(SUBS_PATH)
    logger.info(
        "Loaded %d mandates, %d unique requirements, %d submissions",
        len(mandates), len(requirements), len(submissions),
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    idx = build_mandate_index(mandates)
    req_to_mandate, cand_A = match_requirements(mandates, requirements, idx)
    # Pass the set of requirements that landed in "no_match" so that Stage B
    # picks up their packages too (instead of leaving them orphaned).
    no_match_reqs = {rn for rn, v in req_to_mandate.items() if v["verdict"] == "no_match"}
    pkg_to_mandate_b, cand_B = match_orphan_packages(mandates, submissions, idx, no_match_reqs=no_match_reqs)

    summary = write_views(mandates, submissions, requirements, req_to_mandate, pkg_to_mandate_b, idx)

    # Combine all candidate types into one file for the LLM judge to consume.
    cand_A_validation = summary.pop("_stage_a_validation_candidates", [])
    with open(CANDIDATES_PATH, "w") as f:
        for c in cand_A + cand_B + cand_A_validation:
            f.write(json.dumps(c) + "\n")
    logger.info(
        "Candidates: A=%d B=%d A_validation=%d → %d total",
        len(cand_A), len(cand_B), len(cand_A_validation), len(cand_A) + len(cand_B) + len(cand_A_validation),
    )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
