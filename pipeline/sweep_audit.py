"""Frontier-model audits of the discovered list: is each mandate real, and is it counted once?

Verdicts land in `data/gold/` with model and usage, so every reported rate recomputes from disk — RUNBOOK section 12.

Env vars:
  ANTHROPIC_API_KEY

Usage:
  uv run python pipeline/sweep_audit.py precision --n 300
  uv run python pipeline/sweep_audit.py units --n 150
  uv run python pipeline/sweep_audit.py report
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

from mandate_units import section_of

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("sweep_audit")

MANDATES_PATH = REPO_ROOT / "data/discovered/mandates.jsonl"
PRECISION_PATH = REPO_ROOT / "data/gold/adjudication_units_precision.jsonl"
UNITS_PATH = REPO_ROOT / "data/gold/adjudication_units_merge.jsonl"

MODEL = "claude-opus-5"
EFFORT = "medium"
MAX_CHARS = 12000

PRECISION_SYSTEM = """You audit an automated search of the United States Code for REPORTING REQUIREMENTS DIRECTED TO CONGRESS.

You receive one provision of statutory text (sometimes with its enclosing section's heading and lead-in prepended) and what the search extracted from it. Judge the TEXT, independently of the extraction. Answer three questions:

1. is_congressional_report — does the text obligate some federal officer or entity to deliver a report, study, notification, certification, plan, or similar information to Congress, a chamber, a committee, or a congressional officer? Congress merely benefitting from, requesting, or being consulted about something is not enough.
2. is_recurring — if so, does the duty recur? Explicit repetition ("annually", "each fiscal year", "every 2 years", "thereafter") or a repeating trigger counts. A single deadline with nothing saying it repeats is one-time.
3. still_in_force — as far as the text itself shows, is the duty still operative as of 2026? Answer false when a stated end date or final reporting year has passed, when the text describes a repealed or terminated duty, or when it concerns a body or program the text itself shows has ended. Answer true when nothing in the text indicates it has lapsed."""

PRECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_congressional_report": {"type": "boolean"},
        "is_recurring": {"type": "boolean"},
        "still_in_force": {"type": "boolean"},
        "recipient": {"type": "string"},
        "frequency": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["is_congressional_report", "is_recurring", "still_in_force",
                 "recipient", "frequency", "reasoning"],
    "additionalProperties": False,
}

UNITS_SYSTEM = """You check how an automated search COUNTS congressional reporting requirements in the United States Code.

You receive two or more provisions of statutory text from one section. Each may repeat the section heading and lead-in. Decide how many DISTINCT reporting duties to Congress they state between them.

A distinct duty is a separate report, notification, or submission that someone must produce. Count as ONE duty:
- a report and the list of contents it must include
- one duty stated in a parent provision and repeated in its subdivisions
- one duty with a first deadline and a recurrence

Count as SEPARATE duties reports with different subjects, different reporting entities, or independent deadlines, even when they sit side by side in the same section. Provisions that state no duty to Congress at all contribute zero."""

UNITS_SCHEMA = {
    "type": "object",
    "properties": {
        "distinct_duties": {"type": "integer"},
        "reasoning": {"type": "string"},
    },
    "required": ["distinct_duties", "reasoning"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


def call(system: str, user: str, schema: dict) -> tuple[dict | None, dict]:
    """One structured adjudication; a failure returns None so the next run retries it rather than scoring it negative."""
    import anthropic  # noqa: PLC0415 — only the audit needs the SDK

    client = anthropic.Anthropic(max_retries=4)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system,
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        logger.warning("call failed: %s", e)
        return None, {}
    usage = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
    if resp.stop_reason != "end_turn":
        logger.warning("stop_reason=%s", resp.stop_reason)
        return None, usage
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text), usage
    except json.JSONDecodeError:
        return None, usage


def _run(jobs: list[dict], out: Path, workers: int) -> int:
    """Adjudicate jobs not already on disk; append each as it lands."""
    done = {r["key"] for r in _load(out) if r.get("verdict") is not None}
    todo = [j for j in jobs if j["key"] not in done]
    logger.info("%d jobs | %d already adjudicated | %d to run", len(jobs), len(jobs) - len(todo), len(todo))
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(out, "a") as fh, cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(call, j["system"], j["user"], j["schema"]): j for j in todo}
        for f in cf.as_completed(futs):
            j = futs[f]
            verdict, usage = f.result()
            rec = {k: v for k, v in j.items() if k not in ("system", "user", "schema")}
            rec.update({"model": MODEL, "effort": EFFORT, "verdict": verdict, "usage": usage})
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            written += 1
            if written % 25 == 0:
                logger.info("%d/%d adjudicated", written, len(todo))
    return written


def _load(path: Path) -> list[dict]:
    """Last record per key: a retried job is appended again, never rewritten."""
    if not path.exists():
        return []
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                rows[r["key"]] = r
            except (json.JSONDecodeError, KeyError):
                continue
    return list(rows.values())


def load_mandates(path: Path = MANDATES_PATH) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Precision
# ─────────────────────────────────────────────────────────────────────────────


def stratum(m: dict) -> str:
    """Notes and provisions differ sharply in precision — RUNBOOK §12."""
    return m["source"]


def precision_jobs(mandates: list[dict], candidates: dict[str, str], n: int, seed: int = 29) -> list[dict]:
    """Equal allocation per stratum, weighted back by `precision_report`; full text, since the published excerpt is capped."""
    by: dict[str, list[dict]] = defaultdict(list)
    for m in mandates:
        by[stratum(m)].append(m)
    rng = random.Random(seed)
    per = max(1, n // len(by))
    jobs = []
    for s, rows in sorted(by.items()):
        for m in rng.sample(rows, min(per, len(rows))):
            extracted = {k: m[k] for k in ("reporting_entity", "cadence", "deadline")}
            jobs.append({
                "key": m["uslm_id"], "stratum": s, "citation": m["citation"],
                "system": PRECISION_SYSTEM, "schema": PRECISION_SCHEMA,
                "user": f"Citation: {m['citation']}\n\nExtracted: {json.dumps(extracted)}\n\n"
                        f"Text:\n{candidates.get(m['uslm_id'], m['text'])[:MAX_CHARS]}",
            })
    return jobs


def precision_report(mandates: list[dict], rows: list[dict]) -> dict:
    """Per-stratum rates, and the frame rate weighted by stratum size."""
    size = Counter(stratum(m) for m in mandates)
    live = {m["uslm_id"] for m in mandates}
    rows = [r for r in rows if r.get("verdict") and r["key"] in live]
    out: dict = {"strata": {}, "weighted": {}}
    claims = ("is_congressional_report", "is_recurring", "still_in_force")
    for s in sorted(size):
        rs = [r for r in rows if r["stratum"] == s]
        cum = []
        ok = [True] * len(rs)
        for c in claims:
            ok = [o and r["verdict"][c] for o, r in zip(ok, rs)]
            cum.append(sum(ok))
        out["strata"][s] = {"size": size[s], "n": len(rs), **dict(zip(claims, cum))}
    total = sum(size.values())
    for i, c in enumerate(claims):
        est, var = 0.0, 0.0
        for s, d in out["strata"].items():
            if d["n"] == 0:
                continue
            w = d["size"] / total
            p = d[c] / d["n"]
            est += w * p
            var += w * w * p * (1 - p) / d["n"]
        out["weighted"][c] = {"rate": est, "ci95": 1.96 * math.sqrt(var)}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Unit counting
# ─────────────────────────────────────────────────────────────────────────────


def units_jobs(mandates: list[dict], candidates: dict[str, str], n: int, seed: int = 31) -> list[dict]:
    """Half merged units (root plus up to four members), half same-section pairs the builder kept apart."""
    rng = random.Random(seed)
    merged = [m for m in mandates if len(m["members"]) > 1]
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for m in mandates:
        by_sec[section_of(m["uslm_id"])].append(m)
    pairs = [tuple(rng.sample(v, 2)) for v in by_sec.values() if len(v) > 1]

    def render(ids: list[str]) -> str:
        return "\n\n".join(f"[{i + 1}] {u}\n{candidates.get(u, '')[:MAX_CHARS // len(ids)]}"
                           for i, u in enumerate(ids))

    jobs = []
    for m in rng.sample(merged, min(n // 2, len(merged))):
        ids = [m["uslm_id"]] + [u for u in m["members"] if u != m["uslm_id"]][:4]
        jobs.append({"key": f"merged:{m['uslm_id']}", "kind": "merged", "builder_count": 1,
                     "members": len(m["members"]), "system": UNITS_SYSTEM, "schema": UNITS_SCHEMA,
                     "user": render(ids)})
    for a, b in rng.sample(pairs, min(n - len(jobs), len(pairs))):
        ids = sorted([a["uslm_id"], b["uslm_id"]])
        jobs.append({"key": f"pair:{ids[0]}|{ids[1]}", "kind": "pair", "builder_count": 2,
                     "system": UNITS_SYSTEM, "schema": UNITS_SCHEMA, "user": render(ids)})
    return jobs


def units_report(rows: list[dict]) -> dict:
    out = {}
    for kind in ("merged", "pair"):
        rs = [r for r in rows if r.get("kind") == kind and r.get("verdict")]
        c = Counter(min(r["verdict"]["distinct_duties"], 3) for r in rs)
        out[kind] = {"n": len(rs), "by_count": dict(sorted(c.items()))}
    return out


def _candidates() -> dict[str, str]:
    path = REPO_ROOT / "data/usc/sweep_candidates.jsonl"
    return {json.loads(l)["uslm_id"]: json.loads(l)["text"] for l in path.read_text().splitlines() if l.strip()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audit", choices=("precision", "units", "report"))
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--mandates", type=Path, default=MANDATES_PATH)
    ap.add_argument("--dry-run", action="store_true", help="Build the sample and print its size; call nothing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(REPO_ROOT / ".env")
    mandates = load_mandates(args.mandates)

    if args.audit == "precision":
        jobs = precision_jobs(mandates, _candidates(), args.n)
        print(Counter(j["stratum"] for j in jobs))
        if not args.dry_run:
            _run(jobs, PRECISION_PATH, args.workers)
    elif args.audit == "units":
        jobs = units_jobs(mandates, _candidates(), args.n)
        print(Counter(j["kind"] for j in jobs))
        if not args.dry_run:
            _run(jobs, UNITS_PATH, args.workers)
    else:
        print(json.dumps(precision_report(mandates, _load(PRECISION_PATH)), indent=2))
        print(json.dumps(units_report(_load(UNITS_PATH)), indent=2))
        usage = [r.get("usage") or {} for r in _load(PRECISION_PATH) + _load(UNITS_PATH)]
        tin, tout = sum(u.get("in", 0) for u in usage), sum(u.get("out", 0) for u in usage)
        print(f"tokens in {tin:,} out {tout:,}  ≈ ${tin * 5e-6 + tout * 25e-6:.2f} at {MODEL} list price")


if __name__ == "__main__":
    main()
