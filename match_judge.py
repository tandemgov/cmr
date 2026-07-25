"""LLM judge for fuzzy match candidates produced by match.py.

Reads compare_output/candidates.jsonl (rows that the deterministic matcher
flagged as plausible-but-uncertain), and asks Claude + Gemini whether each
(mandate, GPO record) pair refers to the same Congressional reporting mandate.

Resumable: writes compare_output/match_judgments.jsonl incrementally and
skips (candidate_id, judge) pairs already present.

Env vars (set the ones for judges you enable):
  ANTHROPIC_API_KEY
  GEMINI_API_KEY (or GOOGLE_API_KEY)

Optional model overrides:
  ANTHROPIC_MODEL  (default: claude-sonnet-4-5)
  GEMINI_MODEL     (default: gemini-2.5-flash)

Usage:
  uv run python match_judge.py                        # judge all candidates
  uv run python match_judge.py --limit 50             # cap the workload
  uv run python match_judge.py --judges claude         # subset of judges
  uv run python match_judge.py --dry-run               # show plan, no calls
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("match_judge")

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

OUT_DIR = Path("compare_output")
CANDIDATES_PATH = OUT_DIR / "candidates.jsonl"
JUDGMENTS_PATH = OUT_DIR / "match_judgments.jsonl"


SYSTEM_PROMPT = """You decide whether two records describe the SAME Congressional reporting mandate.

You will see:
  A) A row from the official House Document listing every report Congress requires.
  B) Either (i) a GPO "requirement" record (the deduplicated mandate as GPO tracks it)
            or (ii) a GPO package submission (an actual report filed by an agency).

Compare them on substance, not formatting. The same mandate is often cited with
slightly different abbreviations ("Pub. L." vs "Public Law"), missing/extra
parenthetical sections, or slightly reworded "nature" descriptions. Different
mandates may share an authority statute but cover different reports.

CRITICAL — "covered by" is NOT "is the same as":
A specific report can be *covered by* an umbrella process requirement without
*being* that same mandate. Two important examples to reject as "different":

  1. Congressional Review Act (5 U.S.C. 801; Pub. L. 104-121, sec. 251):
     this is a procedural requirement that every agency rule must be submitted
     to Congress for review. ANY specific final rule, interim rule, or
     rulemaking notice (e.g. "Retirement Security Rule", "Final Rule on
     Inflation Adjustment", "Worker Walkaround Designation Process") is
     COVERED BY the CRA, but it is NOT the same mandate as the CRA itself.
     A House Doc mandate that says "Congressional review of agency rulemaking"
     is the umbrella obligation, not any specific rule. → "different".

  2. Any other umbrella "[procedure] must be reported" requirement vs a
     specific report that happens to use that procedure. The umbrella
     describes a recurring class of obligations; the specific report is one
     instance of compliance, not the same mandate.

Decision rules:
- "same": both records clearly describe the same specific reporting obligation
  — same underlying statutory subsection, same subject of the report, same
  recurring cadence. Year-over-year editions of the same report (e.g. "2022
  Conservation Reserve Report" vs "2023 Conservation Reserve Report") count
  as "same". Different agencies submitting their own copy of a multi-agency
  annual report (e.g. each agency's CFO Act Agency Financial Report) also
  count as "same".
- "different": (a) different reports under the same statute, (b) different
  statutory subsections, (c) a specific rule/report vs an umbrella procedural
  requirement that covers it, (d) topically related but distinct mandates.
- "unclear": you genuinely cannot decide from the information given.

Respond ONLY with valid JSON (no markdown fences, no commentary):
{
  "verdict": "same" | "different" | "unclear",
  "reasoning": "one short paragraph, max 400 chars"
}
"""


def parse_json_loose(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
        if t.startswith("json"):
            t = t[4:].lstrip()
    return json.loads(t)


def build_user_text(cand: dict) -> str:
    m = cand["mandate"]
    parts = [
        "A) HOUSE DOCUMENT MANDATE",
        f"   reporting_entity: {m.get('reporting_entity','')!r}",
        f"   nature_of_report: {m.get('nature_of_report','')!r}",
        f"   authority:        {m.get('authority','')!r}",
        f"   when_expected:    {m.get('when_expected','')!r}",
        "",
    ]
    if cand["stage"] == "A":
        r = cand["requirement"]
        parts += [
            "B) GPO REQUIREMENT RECORD",
            f"   submitting_agency: {r.get('submitting_agency_canonical','')!r}",
            f"   nature:            {r.get('nature','')!r}",
            f"   legal_authority:   {r.get('legal_authority','')!r}",
            f"   frequency:         {r.get('frequency','')!r}",
        ]
    elif cand["stage"] == "A_validation":
        # Stage A attached this submission to the mandate via GPO's own
        # requirement-number tag, but the title doesn't share tokens with
        # the mandate. Ask the judge to confirm GPO's tag was correct.
        s = cand["submission"]
        parts += [
            "B) GPO PACKAGE SUBMISSION (attached via GPO's requirement-number tag — possibly mis-tagged)",
            f"   title:             {s.get('title','')!r}",
            f"   submitting_agency: {s.get('government_author','')!r}",
            f"   organization:      {s.get('organization_full','')!r}",
            f"   submitted:         {s.get('submitted_to_gpo_date','')!r}",
        ]
    else:
        s = cand["submission"]
        parts += [
            "B) GPO PACKAGE SUBMISSION (no requirement metadata)",
            f"   title:             {s.get('title','')!r}",
            f"   submitting_agency: {s.get('government_author','')!r}",
            f"   organization:      {s.get('organization_full','')!r}",
            f"   submitted:         {s.get('submitted_to_gpo_date','')!r}",
        ]
    parts += [
        "",
        "Judge whether A and B describe the SAME reporting mandate.",
    ]
    return "\n".join(parts)


def candidate_id(cand: dict) -> str:
    """Stable ID per candidate, derived from mandate_id + GPO key."""
    if cand["stage"] == "A":
        key = cand["mandate_id"] + "|" + f"R{cand['requirement_number']}"
    elif cand["stage"] == "A_validation":
        # Different namespace from Stage B so a same (mandate,package) pair
        # being judged under both stages doesn't collide.
        key = cand["mandate_id"] + "|" + f"AV{cand['package_id']}"
    else:
        key = cand["mandate_id"] + "|" + f"P{cand['package_id']}"
    return key


def call_anthropic(cand: dict) -> dict:
    from anthropic import Anthropic
    client = Anthropic()
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=500,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": build_user_text(cand)}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return parse_json_loose(text)


def call_gemini(cand: dict) -> dict:
    from google import genai
    from google.genai import types
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[build_user_text(cand)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=1500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return parse_json_loose(resp.text)


JUDGES = {
    "claude": (call_anthropic, lambda: ANTHROPIC_MODEL, "ANTHROPIC_API_KEY"),
    "gemini": (call_gemini, lambda: GEMINI_MODEL, ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
}


def have_key(env_spec) -> bool:
    if isinstance(env_spec, tuple):
        return any(os.environ.get(k) for k in env_spec)
    return bool(os.environ.get(env_spec))


def judge_one(judge_name: str, cand: dict) -> dict:
    fn, model_fn, _ = JUDGES[judge_name]
    t0 = time.time()
    cid = candidate_id(cand)
    try:
        result = fn(cand)
        return {
            "candidate_id": cid,
            "mandate_id": cand["mandate_id"],
            "stage": cand["stage"],
            "judge": judge_name,
            "model": model_fn(),
            "verdict": result.get("verdict"),
            "reasoning": result.get("reasoning"),
            "error": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        logger.warning("Judge %s failed for %s: %s", judge_name, cid, e)
        return {
            "candidate_id": cid,
            "mandate_id": cand["mandate_id"],
            "stage": cand["stage"],
            "judge": judge_name,
            "model": model_fn(),
            "verdict": None,
            "reasoning": None,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }


def load_existing(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                done.add((rec["candidate_id"], rec["judge"]))
            except Exception:
                pass
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--judges", default="claude,gemini", help="Comma-separated subset of: " + ",".join(JUDGES))
    ap.add_argument("--limit", type=int, default=None, help="Cap number of candidates to judge")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    for j in judges:
        if j not in JUDGES:
            sys.exit(f"Unknown judge: {j}")
        if not have_key(JUDGES[j][2]):
            sys.exit(f"No API key for judge {j}")

    if not CANDIDATES_PATH.exists():
        sys.exit(f"No candidates file at {CANDIDATES_PATH}; run match.py first")

    candidates = [json.loads(l) for l in CANDIDATES_PATH.read_text().splitlines() if l.strip()]
    if args.limit:
        candidates = candidates[: args.limit]
    done = load_existing(JUDGMENTS_PATH)

    tasks: list[tuple[str, dict]] = []
    for c in candidates:
        cid = candidate_id(c)
        for j in judges:
            if (cid, j) not in done:
                tasks.append((j, c))

    logger.info(
        "Candidates: %d  judges: %s  tasks: %d  already done: %d",
        len(candidates), judges, len(tasks), len(done),
    )

    if args.dry_run:
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_f = JUDGMENTS_PATH.open("a")
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(judge_one, j, c): (j, candidate_id(c)) for j, c in tasks}
            for fut in as_completed(futs):
                rec = fut.result()
                out_f.write(json.dumps(rec) + "\n")
                out_f.flush()
                logger.info("[%s] %s → %s", rec["judge"], rec["candidate_id"], rec["verdict"] or rec["error"])
    finally:
        out_f.close()


if __name__ == "__main__":
    main()
