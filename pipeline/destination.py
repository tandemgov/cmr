"""Read a report's destination — chamber or committee — out of its resolved statutory text.

CMRA attaches the deposit obligation to chamber-directed reports at any statute age, but to committee-directed reports only post-Pub. L. 117-263.

The House Doc does not record destination; the statutory text does. See RUNBOOK section 13.

Tiers: `chamber` (obligation attaches at any age), `committee` (post-CMRA only), `congress` ("to Congress", nothing narrower), `unknown`.

Usage:
  uv run python pipeline/destination.py             # tier distribution
  uv run python pipeline/destination.py --sample 5  # worked examples per tier
  uv run python pipeline/destination.py --confirm   # re-judge the weak tiers (LLM, resumable)
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

USC_PROVISIONS_PATH = REPO_ROOT / "data/usc/provisions.jsonl"
PLAW_PROVISIONS_PATH = REPO_ROOT / "data/usc/plaw_provisions.jsonl"

# Anchor on the recipient of a delivery, not any mention of a body: a consulted committee is not a destination.
_DELIVERY_RE = re.compile(
    r"(?i)\b(?:submit|transmit|report|furnish|deliver|present|send|provide|available)\w*\b"
    r"[^.;]{0,60}?\bto\b"
)
# `notify the Congress` takes a direct object, so there is no "to" to anchor on.
_DIRECT_OBJECT_RE = re.compile(r"(?i)\b(?:notif|inform|advis|appris)\w*\b")
# How much text after "to" can still name the recipient, before the report's subject matter starts.
_RECIPIENT_WINDOW = 240

# Officers who receive on behalf of a whole chamber. Floor leaders count: they receive for the chambers, not a committee.
_CHAMBER_RE = re.compile(
    r"(?i)\b("
    r"speaker of the house"
    r"|president (?:pro tempore )?of the senate"
    r"|president pro tempore"
    r"|each house of (?:the )?congress"
    r"|both houses"
    r"|(?:majority|minority) leader"
    r"|clerk of the house"
    r"|secretary of the senate"
    # "committees and leadership" is the commonest mixed form; leadership carries the chamber prong.
    r"|(?:congressional\s+)?leadership"
    # Both chambers together. A whole phrase, since bare `the Senate` also opens "the Senate Committee on ...".
    r"|house of representatives and the senate"
    r"|senate and the house of representatives"
    r"|senate and house of representatives"
    r")\b"
)

# "Committees on Appropriations" is the dominant plural form.
_COMMITTEE_RE = re.compile(r"(?i)\bcommittees?\s+(?:on|of)\b|\bcongressional\s+\w*\s*committees?\b")

# Matched inside a span that already starts after "to", so the preposition must not be in the pattern.
_CONGRESS_RE = re.compile(r"(?i)\bcongress\b")

CHAMBER, COMMITTEE, CONGRESS, UNKNOWN = "chamber", "committee", "congress", "unknown"


def recipient_spans(text: str) -> list[str]:
    """Stretches where a recipient is named: after a delivery verb's ``to``, or after a direct-object verb."""
    text = text or ""
    spans = [text[m.end():m.end() + _RECIPIENT_WINDOW] for m in _DELIVERY_RE.finditer(text)]
    spans += [text[m.end():m.end() + _RECIPIENT_WINDOW] for m in _DIRECT_OBJECT_RE.finditer(text)]
    return spans


def _classify_span(span: str) -> str:
    """Classify one recipient span.

    A chamber officer anywhere carries the chamber prong. Otherwise the *first* named recipient governs.
    """
    if _CHAMBER_RE.search(span):
        return CHAMBER
    congress = _CONGRESS_RE.search(span)
    committee = _COMMITTEE_RE.search(span)
    if congress and committee:
        return CONGRESS if congress.start() < committee.start() else COMMITTEE
    if committee:
        return COMMITTEE
    if congress:
        return CONGRESS
    return UNKNOWN


def classify_destination(text: str) -> str:
    """Return the tier a single mandate's statutory text falls into.

    Aggregation runs chamber → congress → committee, deliberately conservative: a wrong `congress` only reaches the bracketed tier.
    """
    if not text or not text.strip():
        return UNKNOWN
    tiers = {_classify_span(s) for s in recipient_spans(text)}
    for tier in (CHAMBER, CONGRESS, COMMITTEE):
        if tier in tiers:
            return tier
    return UNKNOWN


def load_mandate_text() -> dict[str, str]:
    """Map mandate_id → all resolved statutory text, codified and uncodified.

    Provisions are concatenated because the destination may be stated in any one of them.
    """
    out: dict[str, list[str]] = {}
    for path, key in ((USC_PROVISIONS_PATH, "provisions"),
                      (PLAW_PROVISIONS_PATH, "plaw_provisions")):
        if not path.exists():
            raise SystemExit(f"{path} missing — run statute_fetch.py / plaw_fetch.py first")
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            chunks = [p.get("text") or "" for p in (row.get(key) or [])]
            if any(c.strip() for c in chunks):
                out.setdefault(row["mandate_id"], []).extend(chunks)
    return {mid: " ".join(c for c in chunks if c) for mid, chunks in out.items()}


def classify_all() -> dict[str, str]:
    text = load_mandate_text()
    return {mid: classify_destination(t) for mid, t in text.items()}


# Paid, non-reproducible verdicts, so they live in data/gold/ like the other adjudications.
CONFIRM_PATH = REPO_ROOT / "data/gold/destination.jsonl"

# The gate is 72% precise on `chamber`, the tier that decides the denominator, so a judge confirms it.
CONFIRM_TIERS = (CHAMBER, UNKNOWN)

JUDGE_SYSTEM = """You read U.S. statutory text and identify WHO a required report must be delivered to.

Answer with exactly one label:
- "chamber"   - delivered to a chamber or its presiding officer: the Speaker, the President or President pro tempore of the Senate, the majority/minority leaders, congressional leadership, the Clerk of the House, the Secretary of the Senate, "both Houses", "each House of Congress", or the House of Representatives and the Senate named together.
- "committee" - delivered ONLY to one or more congressional committees, and to no chamber officer.
- "congress"  - delivered to "Congress" as a body, with no narrower recipient named.
- "unknown"   - the text states no congressional delivery at all.

Judge only the RECIPIENT of a delivery. A body merely consulted, described, or cross-referenced is not a recipient. If both a chamber officer and a committee receive it, answer "chamber".

Reply with JSON only: {"destination": "<label>", "why": "<one sentence>"}"""


def _judge(client, model: str, body: str) -> str | None:
    resp = client.chat.completions.create(
        model=model, max_completion_tokens=2000,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": JUDGE_SYSTEM},
                  {"role": "user", "content": body[:6000]}],
    )
    raw = resp.choices[0].message.content or ""
    return json.loads(raw).get("destination")


def confirm(model: str, workers: int = 8) -> dict[str, int]:
    """Re-judge the gate's low-precision tiers, appending to CONFIRM_PATH.

    Resumable: a non-null verdict is skipped, so an outage self-heals on re-run instead of being recorded as settled.
    """
    from concurrent.futures import ThreadPoolExecutor
    from openai import OpenAI

    text = load_mandate_text()
    tiers = {mid: classify_destination(t) for mid, t in text.items()}
    done = set()
    if CONFIRM_PATH.exists():
        for line in CONFIRM_PATH.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("destination"):
                    done.add(row["mandate_id"])
    todo = [m for m, tier in tiers.items() if tier in CONFIRM_TIERS and m not in done]
    print(f"  gate tiers {'/'.join(CONFIRM_TIERS)}: {sum(1 for v in tiers.values() if v in CONFIRM_TIERS):,}"
          f"   already judged: {len(done):,}   to judge: {len(todo):,}")

    client = OpenAI()
    def run(mid):
        try:
            return {"mandate_id": mid, "gate": tiers[mid], "destination": _judge(client, model, text[mid]), "model": model}
        except Exception as e:
            return {"mandate_id": mid, "gate": tiers[mid], "destination": None, "model": model, "error": f"{type(e).__name__}: {e}"[:200]}

    tally: Counter[str] = Counter()
    CONFIRM_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIRM_PATH.open("a") as fh, ThreadPoolExecutor(max_workers=workers) as ex:
        for row in ex.map(run, todo):
            fh.write(json.dumps(row) + "\n")
            tally[row["destination"] or "FAILED"] += 1
    return dict(tally)


def final_destinations() -> dict[str, str]:
    """The gate's verdict, overridden by the judge wherever a confirmation exists."""
    out = classify_all()
    if CONFIRM_PATH.exists():
        for line in CONFIRM_PATH.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("destination"):
                    out[row["mandate_id"]] = row["destination"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=0, help="Show N worked examples per tier")
    ap.add_argument("--confirm", action="store_true", help="Re-judge the gate's low-precision tiers with an LLM")
    ap.add_argument("--model", default="gpt-5.6-terra")
    args = ap.parse_args()

    if args.confirm:
        for k, v in confirm(args.model).items():
            print(f"    {k:<12} {v:>6,}")
        final = Counter(final_destinations().values())
        print("\n  after confirmation:")
        for tier in (CHAMBER, COMMITTEE, CONGRESS, UNKNOWN):
            print(f"    {tier:<12} {final[tier]:>6,}")
        return

    text = load_mandate_text()
    tiers = {mid: classify_destination(t) for mid, t in text.items()}
    tally = Counter(tiers.values())

    total_mandates = sum(1 for _ in USC_PROVISIONS_PATH.read_text().splitlines() if _.strip())
    print(f"  mandates                 {total_mandates:>6,}")
    print(f"  with resolved text       {len(text):>6,}")
    for tier in (CHAMBER, COMMITTEE, CONGRESS, UNKNOWN):
        print(f"    {tier:<20} {tally[tier]:>6,}")
    print(f"  no resolved text         {total_mandates - len(text):>6,}")

    if args.sample:
        for tier in (CHAMBER, COMMITTEE, CONGRESS):
            print(f"\n--- {tier} ---")
            shown = 0
            for mid, t in text.items():
                if tiers[mid] != tier:
                    continue
                rx = {CHAMBER: _CHAMBER_RE, COMMITTEE: _COMMITTEE_RE, CONGRESS: _CONGRESS_RE}[tier]
                span = next(s for s in recipient_spans(t) if rx.search(s))
                print(f"  {mid}  …to {span[:130].strip()}…")
                shown += 1
                if shown >= args.sample:
                    break


if __name__ == "__main__":
    main()
