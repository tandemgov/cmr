"""Decide whether a discovered reporting mandate is still live.

A provision can state a perfectly good reporting duty and still be a dead
letter. Three ways that happens, all present in the sweep's output:

- **Sunset reached.** ``22 U.S.C. 7555`` requires a report "annually thereafter
  through 2010"; ``22 U.S.C. 8808`` runs "through 2016".
- **Temporary body.** ``30 U.S.C. 804`` establishes the *Interim* Compliance
  Panel under the 1969 Coal Mine Health and Safety Act.
- **Programme wound down.** ``12 U.S.C. 5226`` requires annual audited TARP
  financials; TARP has ended, though the section still reads as current law.

The classifier that produced the sweep filters one-time duties, but nothing
there catches a *recurring* duty whose recurrence has stopped. That is the last
systematic error class in the pipeline, so it is separated out rather than
folded into the headline.

Only the first is decidable from the text alone, and this module says so: it
reports ``expired`` where a terminal year has passed, ``review`` where the text
carries temporary/termination language whose current effect cannot be read off
the page, and ``current`` otherwise. ``review`` is a queue for a human, not a
verdict.

Usage:
  uv run python pipeline/currency.py                    # annotate the confirmed set
  uv run python pipeline/currency.py --year 2026
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("currency")

CANDIDATES_PATH = REPO_ROOT / "data/usc/sweep_candidates.jsonl"
CONFIRMED_PATH = REPO_ROOT / "data/usc/sweep_confirmed.jsonl"
NOVELTY_PATH = REPO_ROOT / "data/usc/sweep_novelty.jsonl"
OUT_PATH = REPO_ROOT / "data/usc/sweep_live.jsonl"

# "annually thereafter through 2010", "for each of fiscal years 2019 through
# 2023", "until December 31, 2015". The year is what matters; the preposition
# only establishes that it is an end bound rather than a start.
_SUNSET_RE = re.compile(
    r"(?i)\b(?:through|thereafter through|until|ending(?:\s+(?:on|in))?|no later than the end of)\s+"
    r"(?:the\s+)?(?:end\s+of\s+)?(?:fiscal\s+year\s+|calendar\s+year\s+|FY\s*)?"
    r"(?:\w+\s+\d{1,2},?\s+)?(19\d{2}|20\d{2})\b"
)

# Language that makes currency doubtful without settling it. "Interim" is
# included because a body named Interim is usually transitional, but plenty of
# statutes use it for a report *type* ("interim and final reports"), so this is
# a review queue rather than a verdict.
_REVIEW_RE = re.compile(
    r"(?i)\b("
    # Bodies are usually named "Interim <something> Panel", so allow words
    # between: the real case is 30 U.S.C. 804's "Interim Compliance Panel".
    r"interim\s+(?:\w+\s+){0,3}(?:panel|committee|commission|board|authority|council|corporation)"
    r"|shall\s+(?:cease|terminate|expire)"
    r"|terminat(?:es|ion)\s+(?:on|of\s+(?:this|the))"
    r"|no\s+longer\s+(?:be\s+)?(?:required|in\s+effect|applicable)"
    r"|sunset"
    r"|repealed\s+effective"
    r")\b"
)


def terminal_year(text: str) -> int | None:
    """The latest end-bound year stated in the text, if any.

    The latest is taken because a provision often names several — an original
    sunset and an extension — and the extension governs.
    """
    years = [int(y) for y in _SUNSET_RE.findall(text or "")]
    return max(years) if years else None


# A note often exists because its section was repealed, so it describes a dead duty. Notes only, and bare by measurement — RUNBOOK section 12.
_REPEAL_NOTE_RE = re.compile(r"(?i)repeal")


def classify(text: str, year: int, is_note: bool = False) -> tuple[str, str]:
    """Return ``(status, reason)`` for one provision.

    ``expired`` — a stated end bound has passed, or a note announces a repeal.
    ``review``  — temporary or termination language; undecidable from the text.
    ``current`` — no signal that the duty has lapsed.

    ``is_note`` gates the repeal test: sound on notes, harmful on provisions — RUNBOOK section 12.
    """
    end = terminal_year(text)
    if end is not None and end < year:
        return "expired", f"stated end bound {end} has passed"
    if is_note:
        m = _REPEAL_NOTE_RE.search(text or "")
        if m:
            return "expired", f"note announces a repeal: {m.group(0)!r}"
    m = _REVIEW_RE.search(text or "")
    if m:
        return "review", f"temporary/termination language: {m.group(0)!r}"
    return "current", ""


def annotate(year: int, confirmed: Path = CONFIRMED_PATH,
             candidates: Path = CANDIDATES_PATH, novelty: Path = NOVELTY_PATH,
             out: Path = OUT_PATH) -> dict[str, int]:
    for p in (confirmed, candidates):
        if not p.exists():
            raise SystemExit(f"{p} missing — run the sweep and confirm passes first")

    text = {}
    for line in candidates.read_text().splitlines():
        if line.strip():
            c = json.loads(line)
            text[c["uslm_id"]] = c["text"]

    freq = {}
    if novelty.exists():
        for line in novelty.read_text().splitlines():
            if line.strip():
                n = json.loads(line)
                freq[n["uslm_id"]] = n.get("frequency", "")

    # Append-only log: a retried row appears twice, so keep the last per id.
    seen: dict[str, dict] = {}
    for line in confirmed.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            seen[row["uslm_id"]] = row
    kept = [r for r in seen.values() if r.get("is_mandate")]

    tally: Counter[str] = Counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        for r in kept:
            uslm_id = r["uslm_id"]
            status, reason = classify(text.get(uslm_id, ""), year,
                                      is_note="/note/" in uslm_id)
            tally[status] += 1
            fh.write(json.dumps({
                "uslm_id": r["uslm_id"],
                "currency": status,
                "reason": reason,
                "frequency": freq.get(r["uslm_id"], ""),
                "verdict": r.get("verdict"),
            }) + "\n")
    logger.info("Wrote %d rows → %s", len(kept), out)
    return {"confirmed": len(kept), **tally}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, required=True,
                    help="Current year; provisions whose end bound precedes it are expired")
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    stats = annotate(args.year, out=args.out)
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k:<{width}}  {v:>6,}")
    live = stats.get("current", 0)
    print(f"\n  live recurring mandates not on the Clerk's list: {live:,}")


if __name__ == "__main__":
    main()
