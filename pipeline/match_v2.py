"""v2 matcher: filing-first.

v1 (match.py) only compares a GPO filing against House Doc mandates that
share a parsed citation key or clear an agency+jaccard gate — most filings
are never substantively compared to anything (824/1057 orphans).

v2 inverts that: each GPO filing is the query. We assemble the House Doc
slice for the filing's agency (plus multi-agency rows like No FEAR / AFR /
OMWI), hand the whole slice to an LLM in one call, and ask which mandate —
if any — the filing satisfies. Deterministic signals (citation overlap,
title jaccard) are computed *after* the pick as corroboration, not used as
gates.

"none" is a finding, not a failure: every CMR-collection filing is by
definition congressionally mandated, so a filing with no House Doc row is
evidence the House Doc list is incomplete (or the agency slice is wrong).

Resumable: writes compare_output/v2_matches.jsonl incrementally and skips
(package_id, model) pairs already present.

Usage:
  uv run python match_v2.py --orphans-only --sample 25 --seed 42   # pilot
  uv run python match_v2.py                                        # full run
  uv run python match_v2.py --dry-run --orphans-only --sample 25
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from match import ensure_extract, load_jsonl, SUBS_PATH
from normalize import (
    agency_key,
    citation_overlap,
    jaccard,
    parse_citations,
    split_parent_subagency,
    token_set,
)

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv()

logger = logging.getLogger("match_v2")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

OUT_DIR = REPO_ROOT / "compare_output"
V2_MATCHES_PATH = OUT_DIR / "v2_matches.jsonl"
SUBMISSION_COV_PATH = OUT_DIR / "submission_coverage.jsonl"

# Mandates listed under these House Doc entities are filed by *each* agency
# under its own name, so they belong in every filing's slice.
MULTI_AGENCY_KEYS = {"multiple executive agencies and departments", "joint responsibility"}

# Keep prompts bounded: longest slice today is HHS (~227 rows ≈ 15k tokens),
# which is fine; the cap is a guard against pathological growth.
MAX_SLICE = 400

SYSTEM_PROMPT = """You match a federal agency report (filed with GPO under the Access to \
Congressionally Mandated Reports Act) to the congressional reporting mandate it satisfies, \
chosen from the official House Document list of mandates for that agency.

You will see:
  A) The FILING: title, submitting agency, dates.
  B) The MANDATE LIST: every House Doc mandate row for this agency, plus
     government-wide mandates listed under "Multiple Executive Agencies"
     (e.g. No FEAR Act, Agency Financial Reports, OMWI reports) that each
     agency files under its own name.

Pick the mandate row the filing satisfies, or null if none of the listed
mandates corresponds. Filings are congressionally mandated by definition,
but the House Doc list may genuinely be missing their mandate — answer null
when that is so; do NOT force a pick.

CRITICAL — "covered by" is NOT "satisfies":
  1. Congressional Review Act (5 U.S.C. 801): a procedural requirement that
     every agency rule be submitted to Congress. A specific final rule or
     rulemaking notice is COVERED BY the CRA but does not satisfy a House Doc
     row that just says "Congressional review of agency rulemaking" — unless
     the House Doc row IS about CRA rule submissions and the filing is such a
     submission. When the filing is one specific rule and the row is the
     umbrella procedure, answer null rather than matching the umbrella.
  2. Same for any umbrella "[class of thing] must be reported" row vs. one
     specific instance of that class.

What DOES match:
  - Year-over-year editions of the same recurring report.
  - A filing whose title restates the mandate's subject even with different
    wording, abbreviations, or citation formats.
  - Popular-name statute references ("Agricultural Act of 2014" = Pub. L.
    113-79): resolve them from your knowledge when picking.

If two mandate rows are near-duplicates and both fit, pick the best and list
the other under "alternates".

Respond ONLY with valid JSON (no markdown fences):
{
  "match": "M01234" | null,
  "alternates": ["M05678"],
  "confidence": "high" | "medium" | "low",
  "reasoning": "one short paragraph, max 400 chars"
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Slice construction
# ─────────────────────────────────────────────────────────────────────────────


def submission_agency_keys(sub: dict) -> set[str]:
    """All comparison keys for a filing's agency, including subagency parts
    (GPO writes 'Department of X - Subagency'; the House Doc sometimes lists
    the subagency as its own entity, e.g. National Institutes of Health)."""
    keys: set[str] = set()
    for f in ("government_author", "organization_full", "organization_display_name"):
        name = sub.get(f)
        if not name:
            continue
        keys.add(agency_key(name))
        parent, subagency = split_parent_subagency(name)
        if subagency:
            keys.add(agency_key(subagency))
    keys.discard("")
    return keys


def build_agency_index(mandates: list[dict]) -> dict[str, list[int]]:
    by_agency: dict[str, list[int]] = defaultdict(list)
    for i, m in enumerate(mandates):
        by_agency[agency_key(m["reporting_entity"])].append(i)
    return by_agency


def slice_for(sub: dict, mandates: list[dict], by_agency: dict[str, list[int]]) -> tuple[list[int], list[int]]:
    """Return (agency_mandate_idxs, multi_agency_idxs) for a filing."""
    keys = submission_agency_keys(sub)
    own: list[int] = []
    for k in keys:
        own.extend(by_agency.get(k, ()))
    multi: list[int] = []
    for k in MULTI_AGENCY_KEYS:
        multi.extend(by_agency.get(k, ()))
    own = list(dict.fromkeys(own))[:MAX_SLICE]
    return own, multi


def _mandate_line(m: dict) -> str:
    nature = (m.get("nature_of_report") or "").strip()[:400]
    authority = (m.get("authority") or "").strip()[:300]
    when = (m.get("when_expected") or "").strip()[:120]
    return f"  {m['mandate_id']}: {nature} | authority: {authority} | due: {when}"


def build_user_text(sub: dict, mandates: list[dict], own: list[int], multi: list[int]) -> str:
    parts = [
        "A) FILING",
        f"   title:    {sub['title']!r}",
        f"   agency:   {sub.get('government_author','')!r} / {sub.get('organization_full','')!r}",
        f"   issued:   {sub.get('date_issued','')}",
        "",
        f"B) MANDATE LIST for this agency ({len(own)} rows)",
    ]
    parts += [_mandate_line(mandates[i]) for i in own] or ["  (none listed for this agency)"]
    parts += ["", f"GOVERNMENT-WIDE mandates, filed by each agency under its own name ({len(multi)} rows)"]
    parts += [_mandate_line(mandates[i]) for i in multi]
    parts += ["", "Which mandate does the filing satisfy? Respond with the JSON shape only."]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# LLM call + corroboration
# ─────────────────────────────────────────────────────────────────────────────


def parse_json_loose(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
        if t.startswith("json"):
            t = t[4:].lstrip()
    # Tolerate trailing commentary after the JSON object ("Extra data" errors).
    obj, _ = json.JSONDecoder().raw_decode(t)
    return obj


class ModelRefusal(Exception):
    """API-level refusal (stop_reason='refusal') — e.g. biosecurity-adjacent
    filing titles like APHIS plant-pest action plans. Deterministic; recorded
    as a 'refused' verdict rather than retried."""


def call_anthropic(user_text: str) -> dict:
    from anthropic import Anthropic, RateLimitError
    client = Anthropic()
    for attempt in range(5):
        try:
            return _call_anthropic_once(client, user_text)
        except RateLimitError:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s, 40s


def _call_anthropic_once(client, user_text: str) -> dict:
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=600,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    if resp.stop_reason == "refusal":
        raise ModelRefusal()
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return parse_json_loose(text)


def corroborate(sub: dict, mandate: dict) -> dict:
    """Deterministic post-hoc signals on a picked (filing, mandate) pair.
    v1 used these as gates; v2 records them so review can sort by weakness."""
    title_cits = parse_citations(sub.get("title", ""))
    m_cits = parse_citations(mandate.get("authority", ""))
    refs = sub.get("references") or {}
    sub_cits = {
        "usc":  title_cits["usc"]  | {tuple(x) for x in refs.get("usc",  [])},
        "plaw": title_cits["plaw"] | {tuple(x) for x in refs.get("plaw", [])},
        "stat": title_cits["stat"] | {tuple(x) for x in refs.get("stat", [])},
    }
    overlap = citation_overlap(sub_cits, m_cits)
    return {
        "citation_overlap": overlap,
        "citation_kinds_matched": sum(1 for v in overlap.values() if v > 0),
        "title_jaccard": round(jaccard(token_set(sub.get("title", "")), token_set(mandate.get("nature_of_report", ""))), 3),
        "agency_match": agency_key(mandate.get("reporting_entity", "")) in submission_agency_keys(sub),
    }


def match_one(sub: dict, mandates: list[dict], mandate_by_id: dict[str, dict],
              own: list[int], multi: list[int]) -> dict:
    pid = sub["package_id"]
    t0 = time.time()
    base = {
        "package_id": pid,
        "title": sub["title"],
        "agency": sub.get("government_author", ""),
        "model": ANTHROPIC_MODEL,
        "slice_size": len(own),
    }
    try:
        result = call_anthropic(build_user_text(sub, mandates, own, multi))
        mid = result.get("match")
    except ModelRefusal:
        return {
            **base,
            "verdict": "refused", "mandate_id": None, "mandate_entity": None,
            "mandate_nature": None, "alternates": [], "confidence": None,
            "reasoning": None, "corroboration": None, "error": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        logger.warning("v2 match failed for %s: %s", pid, e)
        return {
            **base,
            "verdict": None, "mandate_id": None, "mandate_entity": None,
            "mandate_nature": None, "alternates": [], "confidence": None,
            "reasoning": None, "corroboration": None,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }
    try:
        picked = mandate_by_id.get(mid) if mid else None
        if mid and not picked:
            raise ValueError(f"model returned unknown mandate_id {mid!r}")
        return {
            **base,
            "verdict": "matched" if picked else "none",
            "mandate_id": mid,
            "mandate_entity": picked["reporting_entity"] if picked else None,
            "mandate_nature": picked["nature_of_report"] if picked else None,
            "alternates": [a for a in result.get("alternates") or [] if a in mandate_by_id],
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
            "corroboration": corroborate(sub, picked) if picked else None,
            "error": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        logger.warning("v2 match failed for %s: %s", pid, e)
        return {
            **base,
            "verdict": None, "mandate_id": None, "mandate_entity": None,
            "mandate_nature": None, "alternates": [], "confidence": None,
            "reasoning": None, "corroboration": None,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def load_existing(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("error") is None:
                    done.add((rec["package_id"], rec["model"]))
            except Exception:
                pass
    return done


def load_orphan_ids() -> set[str]:
    if not SUBMISSION_COV_PATH.exists():
        sys.exit(f"{SUBMISSION_COV_PATH} missing; run match.py first (needed for --orphans-only)")
    return {
        r["package_id"]
        for r in load_jsonl(SUBMISSION_COV_PATH)
        if not r["matched_mandate_id"]
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orphans-only", action="store_true", help="only filings v1 left unmatched")
    ap.add_argument("--sample", type=int, default=None, help="random sample of N filings")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set")

    mandates = ensure_extract()
    mandate_by_id = {m["mandate_id"]: m for m in mandates}
    submissions = load_jsonl(SUBS_PATH)
    by_agency = build_agency_index(mandates)

    if args.orphans_only:
        orphans = load_orphan_ids()
        submissions = [s for s in submissions if s["package_id"] in orphans]
        logger.info("Restricted to %d v1-orphan filings", len(submissions))
    if args.sample:
        rng = random.Random(args.seed)
        submissions = rng.sample(submissions, min(args.sample, len(submissions)))
    if args.limit:
        submissions = submissions[: args.limit]

    done = load_existing(V2_MATCHES_PATH)
    todo = [s for s in submissions if (s["package_id"], ANTHROPIC_MODEL) not in done]
    logger.info("Filings: %d  already done: %d  to match: %d  model: %s",
                len(submissions), len(submissions) - len(todo), len(todo), ANTHROPIC_MODEL)

    if args.dry_run:
        for s in todo[:10]:
            own, multi = slice_for(s, mandates, by_agency)
            logger.info("  %s [%s] slice=%d multi=%d %r",
                        s["package_id"], s.get("government_author", ""), len(own), len(multi), s["title"][:70])
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_f = V2_MATCHES_PATH.open("a")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {}
            for s in todo:
                own, multi = slice_for(s, mandates, by_agency)
                futs[pool.submit(match_one, s, mandates, mandate_by_id, own, multi)] = s["package_id"]
            for fut in as_completed(futs):
                rec = fut.result()
                out_f.write(json.dumps(rec) + "\n")
                out_f.flush()
                logger.info("%s → %s %s", rec["package_id"], rec["verdict"] or rec["error"],
                            rec["mandate_id"] or "")
    finally:
        out_f.close()


if __name__ == "__main__":
    main()
