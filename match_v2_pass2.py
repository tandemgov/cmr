"""v2 pass 2: corpus-wide rescue pass for filings judged "none" in pass 1.

Pass 1 (match_v2.py) scopes each filing to its own agency's House Doc slice
(plus government-wide rows). That misses two real failure modes observed in
the pilot:

  1. Cross-entity misattribution — the House Doc lists the mandate under a
     different entity (e.g. CMS's Competitive Acquisition Ombudsman report
     listed under "Social Security Administration").
  2. Needle-in-haystack recall — the right row was in the prompt but lost
     among 145 government-wide rows (e.g. NTSB's FAIR Act inventory vs
     M02698 "Annual lists of government activities not inherently
     governmental in nature").

Pass 2 re-asks each "none" with a much smaller, corpus-wide candidate set:
the top-K mandates from ALL entities ranked by citation overlap + token
similarity against the filing title. Entity mismatch is explicitly allowed.

For filings that remain "none", the model names the statute it believes
mandates the filing (`likely_authority`) — input for the House-Doc-gap
analysis.

Resumable: writes compare_output/v2_pass2.jsonl incrementally.

Usage:
  uv run python match_v2_pass2.py                 # all pass-1 nones
  uv run python match_v2_pass2.py --limit 20
  uv run python match_v2_pass2.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from match import ensure_extract, load_jsonl, SUBS_PATH
from match_v2 import (
    ANTHROPIC_MODEL,
    ModelRefusal,
    call_anthropic as _pass1_call,  # noqa: F401  (kept for parity; we use our own)
    corroborate,
    parse_json_loose,
)
from normalize import jaccard, parse_citations, token_set

load_dotenv()

logger = logging.getLogger("match_v2_pass2")

OUT_DIR = Path("compare_output")
PASS1_PATH = OUT_DIR / "v2_matches.jsonl"
PASS2_PATH = OUT_DIR / "v2_pass2.jsonl"

TOP_K = 20

SYSTEM_PROMPT = """You match a federal agency report (filed with GPO under the Access to \
Congressionally Mandated Reports Act) to the congressional reporting mandate it satisfies.

A first-pass review against the filing agency's own section of the official
House Document mandate list found NO match. You now see a second-chance
candidate set drawn from the ENTIRE House Document across ALL agencies,
selected by citation and text similarity.

IMPORTANT — entity mismatch does NOT disqualify a match. The House Document
sometimes lists a mandate under a parent department, a sibling component, or
even the wrong entity outright. If the candidate's subject, statute, and
cadence fit the filing, it is a match even if the listed entity differs from
the filer.

Still reject ("null") when:
  - the candidate is an umbrella procedural requirement and the filing is one
    specific instance of it (e.g. a specific rule vs the Congressional Review
    Act umbrella);
  - the candidate shares a statute but covers a genuinely different report;
  - the topical overlap is superficial.

If no candidate matches AND the filing's own title states its statutory
authority (e.g. "...As Required by Section 5104 of the Agricultural Act of
2014"), copy that stated authority into "likely_authority". Do NOT supply
an authority from your own knowledge — if the title doesn't state one, use
null. A guessed citation is worse than no citation.

Respond ONLY with valid JSON (no markdown fences):
{
  "match": "M01234" | null,
  "confidence": "high" | "medium" | "low",
  "reasoning": "one short paragraph, max 400 chars",
  "likely_authority": "statute name/citation" | null
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Corpus-wide candidate ranking
# ─────────────────────────────────────────────────────────────────────────────


def build_corpus_index(mandates: list[dict]) -> list[dict]:
    """Precompute tokens + citations per mandate."""
    out = []
    for m in mandates:
        out.append({
            "tokens": token_set(m.get("nature_of_report", "")),
            "cits": parse_citations(m.get("authority", "")),
        })
    return out


def rank_candidates(sub: dict, mandates: list[dict], idx: list[dict], k: int = TOP_K) -> list[int]:
    """Top-k mandate indices across the whole corpus by citation + token score."""
    title_tokens = token_set(sub.get("title", ""))
    refs = sub.get("references") or {}
    sub_cits = parse_citations(sub.get("title", ""))
    for kind in ("usc", "plaw", "stat"):
        sub_cits[kind] |= {tuple(x) for x in refs.get(kind, [])}

    scored = []
    for i, mi in enumerate(idx):
        cit_hits = sum(len(sub_cits[kind] & mi["cits"][kind]) for kind in ("usc", "plaw", "stat"))
        inter = len(title_tokens & mi["tokens"])
        if not cit_hits and inter < 2:
            continue
        denom = min(len(title_tokens), len(mi["tokens"])) or 1
        overlap_coef = inter / denom
        score = cit_hits * 2.0 + overlap_coef + jaccard(title_tokens, mi["tokens"])
        scored.append((score, i))
    scored.sort(key=lambda t: -t[0])
    return [i for _, i in scored[:k]]


def build_user_text(sub: dict, mandates: list[dict], cand_idxs: list[int]) -> str:
    parts = [
        "A) FILING",
        f"   title:    {sub['title']!r}",
        f"   agency:   {sub.get('government_author','')!r} / {sub.get('organization_full','')!r}",
        f"   issued:   {sub.get('date_issued','')}",
        "",
        f"B) CANDIDATE MANDATES from the full House Document, any agency ({len(cand_idxs)} rows)",
    ]
    for i in cand_idxs:
        m = mandates[i]
        nature = (m.get("nature_of_report") or "").strip()[:400]
        authority = (m.get("authority") or "").strip()[:300]
        when = (m.get("when_expected") or "").strip()[:120]
        parts.append(f"  {m['mandate_id']} [{m['reporting_entity']}]: {nature} | authority: {authority} | due: {when}")
    parts += ["", "Does the filing satisfy any candidate? Respond with the JSON shape only."]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# LLM call (same backoff/refusal handling as pass 1)
# ─────────────────────────────────────────────────────────────────────────────


def call_model(user_text: str) -> dict:
    from anthropic import Anthropic, RateLimitError
    client = Anthropic()
    for attempt in range(5):
        try:
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
        except RateLimitError:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt * 5)


def judge_one(sub: dict, mandates: list[dict], mandate_by_id: dict[str, dict],
              cand_idxs: list[int]) -> dict:
    pid = sub["package_id"]
    t0 = time.time()
    base = {
        "package_id": pid,
        "title": sub["title"],
        "agency": sub.get("government_author", ""),
        "model": ANTHROPIC_MODEL,
        "stage": "pass2",
        "candidates": len(cand_idxs),
    }
    if not cand_idxs:
        return {**base, "verdict": "none", "mandate_id": None, "mandate_entity": None,
                "mandate_nature": None, "confidence": None,
                "reasoning": "no corpus-wide candidates above similarity floor",
                "likely_authority": None, "corroboration": None, "error": None,
                "latency_ms": 0}
    try:
        result = call_model(build_user_text(sub, mandates, cand_idxs))
    except ModelRefusal:
        return {**base, "verdict": "refused", "mandate_id": None, "mandate_entity": None,
                "mandate_nature": None, "confidence": None, "reasoning": None,
                "likely_authority": None, "corroboration": None, "error": None,
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        logger.warning("pass2 failed for %s: %s", pid, e)
        return {**base, "verdict": None, "mandate_id": None, "mandate_entity": None,
                "mandate_nature": None, "confidence": None, "reasoning": None,
                "likely_authority": None, "corroboration": None,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.time() - t0) * 1000)}

    mid = result.get("match")
    picked = mandate_by_id.get(mid) if mid else None
    return {
        **base,
        "verdict": "matched" if picked else "none",
        "mandate_id": mid if picked else None,
        "mandate_entity": picked["reporting_entity"] if picked else None,
        "mandate_nature": picked["nature_of_report"] if picked else None,
        "confidence": result.get("confidence"),
        "reasoning": result.get("reasoning"),
        "likely_authority": result.get("likely_authority"),
        "corroboration": corroborate(sub, picked) if picked else None,
        "error": None,
        "latency_ms": int((time.time() - t0) * 1000),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def load_pass1_nones() -> list[str]:
    if not PASS1_PATH.exists():
        sys.exit(f"{PASS1_PATH} missing; run match_v2.py first")
    latest: dict[str, dict] = {}
    for line in PASS1_PATH.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            latest[r["package_id"]] = r
    return [pid for pid, r in latest.items() if r["verdict"] == "none"]


def load_existing(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
            if rec.get("error") is None:
                done.add(rec["package_id"])
        except Exception:
            pass
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set")

    mandates = ensure_extract()
    mandate_by_id = {m["mandate_id"]: m for m in mandates}
    submissions = {s["package_id"]: s for s in load_jsonl(SUBS_PATH)}
    idx = build_corpus_index(mandates)

    none_pids = load_pass1_nones()
    done = load_existing(PASS2_PATH)
    todo = [p for p in none_pids if p not in done]
    if args.limit:
        todo = todo[: args.limit]
    logger.info("pass-1 nones: %d  done: %d  to judge: %d", len(none_pids), len(done), len(todo))

    if args.dry_run:
        for pid in todo[:10]:
            sub = submissions[pid]
            cands = rank_candidates(sub, mandates, idx)
            tops = [mandates[i]["mandate_id"] for i in cands[:5]]
            logger.info("  %s %r → %d candidates %s", pid, sub["title"][:55], len(cands), tops)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_f = PASS2_PATH.open("a")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {}
            for pid in todo:
                sub = submissions[pid]
                cands = rank_candidates(sub, mandates, idx)
                futs[pool.submit(judge_one, sub, mandates, mandate_by_id, cands)] = pid
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
