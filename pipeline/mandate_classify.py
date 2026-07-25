"""Find congressional reporting mandates in statutory text.

The House Doc lists 3,297 mandates and is known to be incomplete — that
incompleteness is what this repo measures. This module attacks the inverse
question: *given statutory text, is there a reporting mandate here?*

**Deliberately no fitted classifier.** Training one requires labelling every
uncited provision negative, which bakes the Clerk's blind spots into the model.
Instead this retrieves by similarity to known mandates and lets an LLM judge
each candidate, so nothing is assumed and every decision stays inspectable. A
fitted model becomes reasonable *after* there are judge-verified labels.

Pipeline:

1. **Retrieve.** Embed known mandates and candidate provisions; score each
   candidate by max cosine similarity to any known mandate.
2. **Judge.** A local open-weight model rules on the top candidates.
3. **Calibrate.** Frontier models judge a stratified sample, measuring the
   local judge rather than trusting it. This repo's extraction audit found
   gpt-4o-mini wrong 12% of the time with incoherent reasoning; an unmeasured
   judge is not evidence.

``--pilot`` runs the honest version of step 1+2 on a subset: it holds out a
slice of *known* mandates, buries them among presumed negatives, and measures
whether the pipeline rediscovers them. Ground truth is known, so recall and
precision are real numbers rather than impressions.

Env vars:
  LOCAL_LLM_HOST   OpenAI-compatible endpoint (embeddings + chat)

Usage:
  uv run python pipeline/mandate_classify.py --pilot
  uv run python pipeline/mandate_classify.py --pilot --negatives 20000 --judge-n 400
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import os
import random
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv()

logger = logging.getLogger("mandate_classify")

USC_XML_DIR = REPO_ROOT / "data/usc/xml"
PROVISIONS_PATH = REPO_ROOT / "data/usc/provisions.jsonl"
PLAW_PROVISIONS_PATH = REPO_ROOT / "data/usc/plaw_provisions.jsonl"
OUT_DIR = REPO_ROOT / "data/usc"

EMBED_MODEL = "granite-embedding"
JUDGE_MODEL = "gemma-4-26b"

# The embedding endpoint enforces a per-request *token* ceiling, not a row
# count: batches of 64 full-length provisions 500, while 128 truncated to 512
# characters succeed. Truncate first, then batch.
EMBED_CHARS = 512
EMBED_BATCH = 128
EMBED_WORKERS = 8

_NS = "{http://xml.house.gov/schemas/uslm/1.0}"
_ADDRESSABLE = {
    f"{_NS}section", f"{_NS}subsection", f"{_NS}paragraph",
    f"{_NS}subparagraph", f"{_NS}clause",
}
_WS_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Local model client
# ─────────────────────────────────────────────────────────────────────────────


def _host() -> str:
    h = os.environ.get("LOCAL_LLM_HOST")
    if not h:
        sys.exit("LOCAL_LLM_HOST not set in environment (.env)")
    return h.rstrip("/")


def _post(path: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{_host()}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batched and parallelised against the local host."""
    batches = [
        [t[:EMBED_CHARS] for t in texts[i:i + EMBED_BATCH]]
        for i in range(0, len(texts), EMBED_BATCH)
    ]

    def one(batch: list[str]) -> list[list[float]]:
        for attempt in range(3):
            try:
                d = _post("/v1/embeddings", {"model": EMBED_MODEL, "input": batch})
                return [r["embedding"] for r in d["data"]]
            except urllib.error.HTTPError:
                if attempt == 2:
                    raise
                # Halve the batch and retry: a long row can push one request
                # over the token ceiling even at EMBED_CHARS truncation.
                mid = max(1, len(batch) // 2)
                return one(batch[:mid]) + one(batch[mid:])
        raise RuntimeError("unreachable")

    out: list[list[float]] = []
    with cf.ThreadPoolExecutor(EMBED_WORKERS) as ex:
        for i, vecs in enumerate(ex.map(one, batches), 1):
            out.extend(vecs)
            if i % 20 == 0 or i == len(batches):
                logger.info("embedded %d/%d batches", i, len(batches))
    return out


JUDGE_SYSTEM = """You decide whether a provision of federal statutory text creates a REPORTING REQUIREMENT DIRECTED TO CONGRESS.

Answer YES only if the text obligates some federal officer, agency, commission, or entity to deliver a report, study, notification, certification, plan, or other information TO Congress — that includes a chamber, a committee, a subcommittee, the Speaker, the President pro tempore, or the Comptroller General acting for Congress.

Answer NO if:
- the recipient is not Congress (an agency head, a court, a State, the public, a registry)
- the text merely defines terms, authorizes appropriations, grants rulemaking authority, or establishes a body without requiring it to report to Congress
- it only cross-references a reporting duty created elsewhere

Reply with STRICT JSON and nothing else:
{"is_mandate": true|false, "recipient": "congress"|"agency"|"public"|"other"|"none", "reporting_entity": "<who must report, or empty>", "deadline": "<deadline or trigger, or empty>", "frequency": "<one-time|annual|biennial|quarterly|event-driven|other|unknown>", "confidence": "high"|"medium"|"low"}"""


def _extract_json(s: str) -> dict | None:
    m = re.search(r"\{.*\}", s or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def judge_one(text: str, model: str = JUDGE_MODEL) -> dict | None:
    """Ask the local model to rule on one provision.

    ``max_tokens`` is generous on purpose: gemma-4-26b is a reasoning model that
    emits chain-of-thought into ``reasoning_content`` and the answer into
    ``content``. Budget too little and the reasoning consumes the whole
    allowance, returning empty content with a valid-looking finish_reason.
    """
    try:
        d = _post("/v1/chat/completions", {
            "model": model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": text[:6000]},
            ],
            "max_tokens": 1200,
            "temperature": 0,
        }, timeout=300)
    except Exception as e:
        logger.debug("judge failed: %s", e)
        return None
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    return _extract_json(msg.get("content") or "")


def judge_many(texts: list[str], workers: int = 8, model: str = JUDGE_MODEL) -> list[dict | None]:
    out: list[dict | None] = [None] * len(texts)
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(judge_one, t, model): i for i, t in enumerate(texts)}
        done = 0
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
            done += 1
            if done % 25 == 0 or done == len(texts):
                logger.info("judged %d/%d", done, len(texts))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Corpora
# ─────────────────────────────────────────────────────────────────────────────


def _text_of(elem: ET.Element) -> str:
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        if e.tag == f"{_NS}notes":
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
            if c.tag in _ADDRESSABLE or c.tag in (f"{_NS}num", f"{_NS}heading", f"{_NS}content"):
                parts.append(" ")
            if c.tail:
                parts.append(c.tail)

    walk(elem)
    return _WS_RE.sub(" ", "".join(parts)).strip()


def load_known_mandates() -> list[dict]:
    """Gold positives: mandates whose statutory text we resolved.

    Draws from both routes — codified US Code provisions and, for the
    uncodified ~28%, public-law text.
    """
    out = []
    if PROVISIONS_PATH.exists():
        for line in PROVISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            for p in r["provisions"]:
                if p.get("resolved") and p.get("text"):
                    out.append({
                        "mandate_id": r["mandate_id"], "source": "usc",
                        "uslm_id": p.get("uslm_id", ""), "text": p["text"],
                        "reporting_entity": r["reporting_entity"],
                        "when_expected": r["when_expected"],
                    })
                    break
    if PLAW_PROVISIONS_PATH.exists():
        for line in PLAW_PROVISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            for p in r["plaw_provisions"]:
                if p.get("resolved") and p.get("text"):
                    out.append({
                        "mandate_id": r["mandate_id"], "source": "plaw",
                        "uslm_id": p.get("package_id", ""), "text": p["text"],
                        "reporting_entity": r["reporting_entity"],
                        "when_expected": r["when_expected"],
                    })
                    break
    return out


def sample_uncited_provisions(exclude: set[str], n: int, seed: int = 7) -> list[dict]:
    """Randomly sample US Code provisions that no House Doc mandate cites.

    These are *presumed* negatives, not verified ones — the whole premise of
    this repo is that the Clerk's list misses real mandates, so some fraction
    of this pool is positive. That is exactly what the judge is for.
    """
    rng = random.Random(seed)
    pool: list[dict] = []
    files = sorted(USC_XML_DIR.glob("usc*.xml"))
    if not files:
        raise SystemExit(f"{USC_XML_DIR} is empty — run pipeline/statute_fetch.py --fetch-only")
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for e in root.iter():
            if e.tag not in _ADDRESSABLE:
                continue
            ident = e.get("identifier")
            if not ident or ident in exclude:
                continue
            # Reservoir-sample so memory stays flat over a 665 MB corpus.
            t = _text_of(e)
            if not (120 < len(t) < 4000):
                continue
            row = {"uslm_id": ident, "text": t}
            if len(pool) < n:
                pool.append(row)
            else:
                j = rng.randrange(len(pool) + 1)
                if j < n:
                    pool[j] = row
        logger.info("scanned %s (pool %d)", f.name, len(pool))
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────


def max_similarity(cands: list[list[float]], known: list[list[float]]) -> list[float]:
    """Max cosine similarity of each candidate to any known mandate.

    Vectors are unit-normalised, so cosine is a plain dot product and the whole
    comparison is one matrix multiply. Done row-by-row in Python this is ~17
    billion operations for a realistic pilot; numpy makes it seconds. Candidates
    are chunked so peak memory stays bounded by ``chunk × len(known)`` rather
    than the full pairwise matrix.
    """
    import numpy as np

    k = np.asarray(known, dtype=np.float32)
    k /= np.linalg.norm(k, axis=1, keepdims=True).clip(min=1e-12)
    c = np.asarray(cands, dtype=np.float32)
    c /= np.linalg.norm(c, axis=1, keepdims=True).clip(min=1e-12)

    out = np.empty(len(c), dtype=np.float32)
    chunk = 2048
    for i in range(0, len(c), chunk):
        out[i:i + chunk] = (c[i:i + chunk] @ k.T).max(axis=1)
    return out.tolist()


def recall_at_k(ranked_labels: list[int], n_positives: int) -> dict[int, float]:
    """Share of held-out positives appearing in the top-k of a ranked list."""
    out = {}
    for k in (100, 250, 500, 1000, 2500, 5000):
        if k > len(ranked_labels):
            break
        out[k] = sum(ranked_labels[:k]) / n_positives if n_positives else 0.0
    return out


def run_pilot(args) -> dict:
    """Hold out known mandates, bury them among presumed negatives, measure recovery.

    This is the honest version of the question "does this work?": the held-out
    rows are mandates the Clerk *did* list and the retriever was never shown,
    so their rank among presumed negatives is a real recall measurement.
    """
    rng = random.Random(args.seed)

    known_all = load_known_mandates()
    if not known_all:
        raise SystemExit("No resolved provisions — run statute_fetch.py and plaw_fetch.py first")
    rng.shuffle(known_all)
    n_hold = int(len(known_all) * args.holdout)
    holdout, known = known_all[:n_hold], known_all[n_hold:]
    logger.info("known mandates: %d  (reference %d / held out %d)", len(known_all), len(known), len(holdout))

    exclude = {k["uslm_id"] for k in known_all if k["uslm_id"]}
    negatives = sample_uncited_provisions(exclude, args.negatives, seed=args.seed)
    logger.info("presumed-negative pool: %d", len(negatives))

    logger.info("embedding reference mandates…")
    known_vecs = embed([k["text"] for k in known])
    logger.info("embedding candidates…")
    cands = [{"label": 1, **h} for h in holdout] + [{"label": 0, **n} for n in negatives]
    rng.shuffle(cands)
    cand_vecs = embed([c["text"] for c in cands])

    logger.info("scoring similarity…")
    sims = max_similarity(cand_vecs, known_vecs)
    for c, s in zip(cands, sims):
        c["similarity"] = s
    cands.sort(key=lambda c: -c["similarity"])

    labels = [c["label"] for c in cands]
    rec = recall_at_k(labels, len(holdout))

    # Judge the top slice, plus a random tail slice as a control — if the judge
    # says yes just as often at the bottom of the ranking, retrieval adds nothing.
    top = cands[:args.judge_n]
    tail = rng.sample(cands[len(cands) // 2:], min(args.judge_n // 3, len(cands) // 2))
    logger.info("judging %d top + %d control candidates…", len(top), len(tail))
    top_v = judge_many([c["text"] for c in top])
    tail_v = judge_many([c["text"] for c in tail])

    def score(cand_slice, verdicts):
        ok = [(c, v) for c, v in zip(cand_slice, verdicts) if v]
        yes = [(c, v) for c, v in ok if v.get("is_mandate") and v.get("recipient") == "congress"]
        held = [c for c, _ in ok if c["label"] == 1]
        held_yes = [c for c, v in yes if c["label"] == 1]
        return {
            "n": len(cand_slice), "parsed": len(ok),
            "judged_mandate": len(yes),
            "held_out_positives_present": len(held),
            "held_out_positives_confirmed": len(held_yes),
            "recall_on_held_out": (len(held_yes) / len(held)) if held else None,
            "flag_rate": (len(yes) / len(ok)) if ok else None,
        }

    results = {
        "known_mandates": len(known_all),
        "reference_set": len(known),
        "held_out": len(holdout),
        "negative_pool": len(negatives),
        "retrieval_recall_at_k": rec,
        "top_slice": score(top, top_v),
        "control_slice": score(tail, tail_v),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        **results,
        "top_examples": [
            {k: c[k] for k in ("uslm_id", "similarity", "label")} | {"verdict": v, "text": c["text"][:300]}
            for c, v in list(zip(top, top_v))[:40]
        ],
    }, indent=1))

    print(f"\nKnown mandates {len(known_all)}  →  reference {len(known)} / held out {len(holdout)}")
    print(f"Presumed-negative pool: {len(negatives)}\n")
    print("RETRIEVAL — share of held-out mandates in the top k:")
    for k, v in rec.items():
        print(f"  recall@{k:<5} {v*100:5.1f}%")
    print("\nJUDGE:")
    for name, s in (("top slice", results["top_slice"]), ("control (random, lower half)", results["control_slice"])):
        fr = s["flag_rate"]
        rc = s["recall_on_held_out"]
        print(f"  {name}:")
        print(f"    parsed {s['parsed']}/{s['n']}   flagged as congressional mandate: {s['judged_mandate']}"
              f"  ({fr*100:.1f}%)" if fr is not None else "")
        print(f"    held-out positives present {s['held_out_positives_present']}, "
              f"confirmed {s['held_out_positives_confirmed']}"
              + (f"  (recall {rc*100:.1f}%)" if rc is not None else ""))
    print(f"\nWrote {args.out}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pilot", action="store_true", help="Held-out validation on a subset")
    ap.add_argument("--negatives", type=int, default=8000, help="Presumed-negative pool size")
    ap.add_argument("--holdout", type=float, default=0.2, help="Fraction of known mandates held out")
    ap.add_argument("--judge-n", type=int, default=300, help="Candidates to send to the local judge")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-o", "--out", type=Path, default=OUT_DIR / "pilot_results.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.pilot:
        sys.exit("Only --pilot is implemented; run with --pilot")

    run_pilot(args)


if __name__ == "__main__":
    main()
