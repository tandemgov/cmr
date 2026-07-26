"""Decide whether a discovered provision is already on the Clerk's list.

The sweep flags provisions that state a congressional reporting duty. Most of
the value is in the ones the House Document does *not* already carry — but
deciding that is harder than it looks, because the Clerk cites the same duty in
two different ways:

- **codified**: ``42 U.S.C. 300gg-111(a)(2)``
- **uncodified**: ``Pub. L. 116-260, div. BB, title I, Sec. 102`` with no USC cite at all

~30% of House Doc rows are the second kind. Matching only on USC sections
therefore counts a listed mandate as novel whenever the Clerk cited its public
law instead — which inflates any "how much is missing" estimate.

This module matches on both keys. A provision's enclosing section carries a
USLM ``<sourceCredit>`` (``(Pub. L. 89–670, § 9(c), Oct. 15, 1966, 80 Stat.
944.)``) naming the public law and section that enacted it, which is exactly
the key an uncodified House Doc row cites.

Verdicts, strongest evidence first:

- ``listed_usc``   — the section is cited by a House Doc authority
- ``listed_plaw``  — its enacting (public law, section) is cited
- ``listed_stat``  — its Statutes at Large page is cited (weak: a page can
  carry several provisions, so this is a *possible* match, not a certain one)
- ``novel``        — no key matches

Usage:
  uv run python pipeline/novelty.py                      # annotate the sweep
  uv run python pipeline/novelty.py --build-credits      # rebuild the index
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from authority_parse import fold_dashes, parse_authority, parse_plaw_sections

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("novelty")

USC_XML_DIR = REPO_ROOT / "data/usc/xml"
EXTRACT_PATH = REPO_ROOT / "data/cmra_extract.jsonl"
CREDITS_PATH = REPO_ROOT / "data/usc/source_credits.json"
VERDICTS_PATH = REPO_ROOT / "data/usc/sweep_verdicts.jsonl"
CANDIDATES_PATH = REPO_ROOT / "data/usc/sweep_candidates.jsonl"
OUT_PATH = REPO_ROOT / "data/usc/sweep_novelty.jsonl"

_NS = "{http://xml.house.gov/schemas/uslm/1.0}"
_WS_RE = re.compile(r"\s+")

# Source credits pair a public law with the section that enacted the provision:
# "(Pub. L. 89-670, § 9(c), Oct. 15, 1966, 80 Stat. 944.)". Dashes are folded
# first, so the en dashes USLM prints match the hyphens the House Doc uses.
#
# A section number belongs to the *nearest preceding* public law, so each law's
# span runs to the next one — the same rule authority_parse.parse_plaw_sections
# applies. Scanning a fixed window instead breaks on the abbreviations credits
# are full of: "Pub. L. 116-260, div. BB, title I, § 102" truncates at "div."
# and loses the section entirely.
_CREDIT_PLAW_RE = re.compile(r"Pub\.\s*L\.\s*(\d+)-(\d+)")
_CREDIT_SEC_RE = re.compile(r"§+\s*(\d+[A-Za-z]?)")
_STAT_RE = re.compile(r"\b(\d+)\s*Stat\.\s*(\d+)")


def _text(elem: ET.Element) -> str:
    return _WS_RE.sub(" ", "".join(elem.itertext())).strip()


def build_source_credits(out: Path = CREDITS_PATH) -> int:
    """Index every section's source credit, so provisions can be keyed by enactment."""
    out.parent.mkdir(parents=True, exist_ok=True)
    credits: dict[str, str] = {}
    for f in sorted(USC_XML_DIR.glob("usc*.xml")):
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for sec in root.iter(f"{_NS}section"):
            ident = sec.get("identifier")
            sc = sec.find(f"{_NS}sourceCredit")
            if ident and sc is not None:
                credits[ident] = _text(sc)
        logger.info("%-14s credits %6d", f.name, len(credits))
    out.write_text(json.dumps(credits))
    logger.info("Wrote %d source credits → %s", len(credits), out)
    return len(credits)


def credit_keys(credit: str) -> tuple[set[tuple[int, int, str]], set[tuple[int, int]]]:
    """Extract ``{(congress, number, section)}`` and ``{(volume, page)}`` from a credit."""
    folded = fold_dashes(credit or "")
    hits = list(_CREDIT_PLAW_RE.finditer(folded))
    plaw: set[tuple[int, int, str]] = set()
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(folded)
        for sec in _CREDIT_SEC_RE.findall(folded[m.end():end]):
            plaw.add((int(m.group(1)), int(m.group(2)), sec))
    stat = {(int(v), int(p)) for v, p in _STAT_RE.findall(folded)}
    return plaw, stat


def clerk_keys(path: Path = EXTRACT_PATH) -> dict[str, set]:
    """Every key the House Document cites, in all three citation forms."""
    usc: set[str] = set()
    plaw: set[tuple[int, int, str]] = set()
    stat: set[tuple[int, int]] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        authority = json.loads(line)["authority"]
        parsed = parse_authority(authority)
        for u in parsed.usc:
            usc.add(u.section_id)
        for s in parsed.stat:
            stat.add((s.volume, s.page))
        for ref in parse_plaw_sections(authority):
            if ref.section:
                plaw.add((ref.congress, ref.number, ref.section))
    return {"usc": usc, "plaw": plaw, "stat": stat}


def section_of(uslm_id: str) -> str | None:
    """The enclosing section id for any addressable node."""
    m = re.match(r"(/us/usc/t[0-9A-Za-z]+/s[0-9A-Za-z\-–]+)", uslm_id or "")
    return m.group(1) if m else None


def classify(uslm_id: str, credits: dict[str, str], keys: dict[str, set]) -> str:
    """Return the strongest evidence that this provision is already listed."""
    sec = section_of(uslm_id)
    if not sec:
        return "novel"
    if sec in keys["usc"]:
        return "listed_usc"
    plaw, stat = credit_keys(credits.get(sec, ""))
    if plaw & keys["plaw"]:
        return "listed_plaw"
    if stat & keys["stat"]:
        return "listed_stat"
    return "novel"


def annotate(verdicts: Path = VERDICTS_PATH, out: Path = OUT_PATH) -> dict[str, int]:
    if not CREDITS_PATH.exists():
        raise SystemExit(f"{CREDITS_PATH} missing — run with --build-credits first")
    credits = json.loads(CREDITS_PATH.read_text())
    keys = clerk_keys()
    logger.info("Clerk keys: %d USC sections, %d (plaw, sec), %d Stat",
                len(keys["usc"]), len(keys["plaw"]), len(keys["stat"]))

    rows = [json.loads(l) for l in verdicts.read_text().splitlines() if l.strip()]
    flagged = [r for r in rows if r.get("is_mandate")]
    tally: Counter[str] = Counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        for r in flagged:
            verdict = classify(r["uslm_id"], credits, keys)
            tally[verdict] += 1
            recurring = (r.get("verdict") or {}).get("frequency", "")
            fh.write(json.dumps({
                "uslm_id": r["uslm_id"],
                "novelty": verdict,
                "frequency": recurring,
                "standing": recurring not in ("one-time", "unknown", ""),
                "verdict": r.get("verdict"),
            }) + "\n")
    logger.info("Wrote %d annotated rows → %s", len(flagged), out)
    return {"judged": len(rows), "flagged": len(flagged), **tally}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-credits", action="store_true", help="Rebuild the source-credit index")
    ap.add_argument("--verdicts", type=Path, default=VERDICTS_PATH)
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.build_credits or not CREDITS_PATH.exists():
        build_source_credits()

    stats = annotate(args.verdicts, args.out)
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k:<{width}}  {v:>6,}")
    novel = stats.get("novel", 0)
    flagged = stats.get("flagged", 1)
    print(f"\n  novel share of flagged: {novel / flagged * 100:.1f}%")


if __name__ == "__main__":
    main()
