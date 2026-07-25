"""Parse House Doc / GPO ``authority`` strings into resolvable statutory references.

This is the *addressing* layer for statutory text. It differs from
``normalize.parse_citations()`` on purpose:

- ``normalize`` keeps only ``(title, section)`` and deliberately discards the
  subsection path, because the matcher needs House Doc and GPO cites to compare
  equal even when one side cites subparts and the other doesn't.
- This module keeps the **full subsection path**, because the operative
  reporting requirement usually lives in a specific subsection — often not even
  the one cited. 70.7% of the House Doc's USC cites carry a subsection path
  (depth up to 5), and 98.9% of those resolve to an exact USLM node.

Use ``normalize`` for matching. Use this for resolving to text.

The output ``uslm_id`` is a USLM identifier as published by the Office of the
Law Revision Counsel, e.g. ``/us/usc/t2/s807/c/3``.

Usage:
  uv run python pipeline/authority_parse.py                 # parse the House Doc extract
  uv run python pipeline/authority_parse.py --stats         # coverage summary only
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXTRACT_PATH = REPO_ROOT / "data/cmra_extract.jsonl"
OUT_PATH = REPO_ROOT / "data/usc/authorities.jsonl"

# USLM writes suffixed section numbers with an EN DASH (U+2013): `/us/usc/t12/s635a–5`.
# The House Doc writes them with an ASCII hyphen: `12 U.S.C. 635a-5`. Failing to
# fold these together drops resolution from 99.3% to 91.1% and makes ~200 live
# citations look like repealed law. Fold every dash variant to ASCII hyphen on
# both sides of the lookup.
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})


def fold_dashes(s: str) -> str:
    """Normalize every Unicode dash variant to ASCII hyphen."""
    return (s or "").translate(_DASHES)


# Section numbers seen in the corpus: `807`, `950cc`, `2349aa`, `2279aa-10`,
# `300gg-111`, `4370m-10`, `2394-1a`, `300j-3d`. So: digits, optional letters,
# optionally followed by a dash then digits then optional letters.
_SECTION = r"\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?"

_USC_RE = re.compile(
    r"\b(\d+[A-Za-z]?)\s*U\.?\s*S\.?\s*C\.?\s*"
    r"(?P<app>App\.\s*)?"
    r"(?:§+\s*)?"
    rf"(?P<section>{_SECTION})"
    r"(?P<subs>(?:\([a-zA-Z0-9]{1,4}\))*)"
    r"(?P<note>\s+note)?",
    re.IGNORECASE,
)

# Accepts "Pub. L. 116-260" / "Pub L 116-260" (House Doc), "Public Law 116-260"
# (GPO), and "P.L. 116-260" (common elsewhere, e.g. bill text).
_PLAW_RE = re.compile(
    r"\bP(?:ub(?:lic)?)?(?:\.|\s)*\s*L(?:aw)?(?:\.|\s)*\s*(\d+)\s*-\s*(\d+)",
    re.IGNORECASE,
)

_STAT_RE = re.compile(r"\b(\d+)\s*Stat\.?\s*(\d+)", re.IGNORECASE)

_SUBPART_RE = re.compile(r"\(([a-zA-Z0-9]{1,4})\)")


@dataclasses.dataclass(frozen=True)
class UscRef:
    title: str
    section: str
    subsections: tuple[str, ...]
    is_note: bool
    is_appendix: bool

    @property
    def uslm_id(self) -> str:
        """USLM identifier for the cited node, e.g. ``/us/usc/t2/s807/c/3``.

        Subsection case is preserved: USLM levels are case-significant, so
        ``(a)`` (subsection) and ``(A)`` (subparagraph) are different depths and
        ``/a/2/A/iii`` must not be folded to ``/a/2/a/iii``. Resolution against
        the index is separately case-insensitive, which absorbs sloppy casing in
        the source citation without corrupting the identifier we publish.

        Notes are not addressable nodes in the USLM section tree, so a note
        reference addresses its parent section; ``is_note`` carries the rest.
        """
        base = f"/us/usc/t{self.title.lower()}/s{self.section.lower()}"
        if self.is_note:
            return base
        return base + "".join(f"/{s}" for s in self.subsections)

    @property
    def section_id(self) -> str:
        """USLM identifier for the enclosing section, ignoring the subsection path."""
        return f"/us/usc/t{self.title.lower()}/s{self.section.lower()}"

    def cite(self) -> str:
        subs = "".join(f"({s})" for s in self.subsections)
        note = " note" if self.is_note else ""
        return f"{self.title} U.S.C. {self.section}{subs}{note}"


@dataclasses.dataclass(frozen=True)
class PlawRef:
    congress: int
    number: int

    @property
    def package_id(self) -> str:
        """govinfo PLAW package id, e.g. ``PLAW-117publ263``."""
        return f"PLAW-{self.congress}publ{self.number}"

    def cite(self) -> str:
        return f"Pub. L. {self.congress}-{self.number}"


@dataclasses.dataclass(frozen=True)
class PlawSectionRef:
    """A section *within* a public law, e.g. ``Pub. L. 117-81, Sec. 1095``.

    This is the addressing unit for uncodified mandates. ~30% of House Doc rows
    never made it into the US Code, so the only text that states their duty is
    the section of the enacted public law itself.
    """

    congress: int
    number: int
    section: str | None

    @property
    def package_id(self) -> str:
        return f"PLAW-{self.congress}publ{self.number}"

    @property
    def in_plaw_collection(self) -> bool:
        """govinfo's PLAW collection begins at the 104th Congress (1995).

        Earlier public laws are only in the Statutes at Large (STATUTE)
        collection, as scanned PDFs.
        """
        return self.congress >= 104

    def cite(self) -> str:
        base = f"Pub. L. {self.congress}-{self.number}"
        return f"{base}, Sec. {self.section}" if self.section else base


@dataclasses.dataclass(frozen=True)
class StatRef:
    volume: int
    page: int

    def cite(self) -> str:
        return f"{self.volume} Stat. {self.page}"


@dataclasses.dataclass
class Authority:
    raw: str
    usc: list[UscRef]
    plaw: list[PlawRef]
    stat: list[StatRef]

    @property
    def is_codified(self) -> bool:
        """True when at least one USC reference is present.

        ~30% of House Doc rows carry no USC cite at all — those mandates are
        uncodified and live only in session law, so they must be resolved
        through PLAW / Statutes at Large instead.
        """
        return bool(self.usc)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "usc": [{**dataclasses.asdict(u), "subsections": list(u.subsections), "uslm_id": u.uslm_id} for u in self.usc],
            "plaw": [dataclasses.asdict(p) for p in self.plaw],
            "stat": [dataclasses.asdict(s) for s in self.stat],
        }


def parse_authority(text: str) -> Authority:
    """Parse one authority string into structured, resolvable references.

    Order is preserved and duplicates are dropped, so ``usc[0]`` is the
    primary cite — the House Doc consistently leads with the codified cite.
    """
    if not text:
        return Authority(raw=text or "", usc=[], plaw=[], stat=[])

    folded = fold_dashes(text)

    usc: list[UscRef] = []
    for m in _USC_RE.finditer(folded):
        ref = UscRef(
            title=m.group(1),
            section=m.group("section"),
            subsections=tuple(_SUBPART_RE.findall(m.group("subs") or "")),
            is_note=bool(m.group("note")),
            is_appendix=bool(m.group("app")),
        )
        if ref not in usc:
            usc.append(ref)

    plaw: list[PlawRef] = []
    for c, n in _PLAW_RE.findall(folded):
        ref = PlawRef(congress=int(c), number=int(n))
        if ref not in plaw:
            plaw.append(ref)

    stat: list[StatRef] = []
    for v, p in _STAT_RE.findall(folded):
        ref = StatRef(volume=int(v), page=int(p))
        if ref not in stat:
            stat.append(ref)

    return Authority(raw=text, usc=usc, plaw=plaw, stat=stat)


_SEC_IN_PLAW_RE = re.compile(r"(?:Sec\.|§+)\s*(\d+[A-Za-z]?)", re.IGNORECASE)


def parse_plaw_sections(text: str) -> list[PlawSectionRef]:
    """Pair each Pub. L. in an authority string with the section that follows it.

    Authority strings chain a base law and its amendments —
    ``Pub. L. 101-510, Sec. 502(a)(1) (as added by Pub. L. 112-260, Sec. 301(f))``
    — so a section number belongs to the *nearest preceding* public law, not to
    the first one. Each Pub. L.'s span runs until the next Pub. L. begins.

    Returns refs in citation order; the first is the operative provision and
    later ones are the amending laws.
    """
    if not text:
        return []
    folded = fold_dashes(text)
    hits = list(_PLAW_RE.finditer(folded))
    out: list[PlawSectionRef] = []
    for i, m in enumerate(hits):
        span_end = hits[i + 1].start() if i + 1 < len(hits) else len(folded)
        window = folded[m.end():span_end]
        sec = _SEC_IN_PLAW_RE.search(window)
        ref = PlawSectionRef(
            congress=int(m.group(1)),
            number=int(m.group(2)),
            section=sec.group(1) if sec else None,
        )
        if ref not in out:
            out.append(ref)
    return out


def load_mandates(path: Path = EXTRACT_PATH) -> list[dict]:
    """Load the House Doc extract, assigning the same ``M#####`` ids the matcher uses.

    Mandate ids are line indexes into the extract — any change to extraction
    shifts every id, so they are only stable against a fixed extract file.
    """
    if not path.exists():
        raise SystemExit(
            f"{path} not found — rebuild it first with `uv run python pipeline/match.py`"
        )
    out = []
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        row["mandate_id"] = f"M{i:05d}"
        out.append(row)
    return out


def parse_all(mandates: list[dict]) -> list[dict]:
    """Attach a parsed authority to every mandate row."""
    out = []
    for m in mandates:
        auth = parse_authority(m.get("authority", ""))
        out.append({
            "mandate_id": m["mandate_id"],
            "reporting_entity": m.get("reporting_entity", ""),
            "nature_of_report": m.get("nature_of_report", ""),
            "when_expected": m.get("when_expected", ""),
            "authority": auth.to_dict(),
        })
    return out


def summarize(parsed: list[dict]) -> dict:
    """Coverage stats — the numbers that justify the whole approach."""
    n = len(parsed)
    with_usc = sum(1 for p in parsed if p["authority"]["usc"])
    with_subs = sum(1 for p in parsed if any(u["subsections"] for u in p["authority"]["usc"]))
    with_plaw = sum(1 for p in parsed if p["authority"]["plaw"])
    with_stat = sum(1 for p in parsed if p["authority"]["stat"])
    notes = sum(1 for p in parsed if any(u["is_note"] for u in p["authority"]["usc"]))
    uncodified = [p for p in parsed if not p["authority"]["usc"]]
    modern_uncodified = sum(
        1 for p in uncodified
        if p["authority"]["plaw"] and max(x["congress"] for x in p["authority"]["plaw"]) >= 104
    )
    uniq_usc = {u["uslm_id"] for p in parsed for u in p["authority"]["usc"]}
    uniq_sections = {(u["title"], u["section"]) for p in parsed for u in p["authority"]["usc"]}
    return {
        "mandates": n,
        "with_usc": with_usc,
        "with_usc_subsection_path": with_subs,
        "with_plaw": with_plaw,
        "with_stat": with_stat,
        "usc_note_refs": notes,
        "uncodified": len(uncodified),
        "uncodified_reachable_via_plaw": modern_uncodified,
        "uncodified_needs_statutes_at_large": len(uncodified) - modern_uncodified,
        "unique_usc_nodes": len(uniq_usc),
        "unique_usc_sections": len(uniq_sections),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="Print coverage summary without writing output")
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    parsed = parse_all(load_mandates())
    stats = summarize(parsed)

    if not args.stats:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as fh:
            for p in parsed:
                fh.write(json.dumps(p) + "\n")
        print(f"Wrote {len(parsed)} parsed authorities → {args.out}")

    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k:<{width}}  {v:>6,}")


if __name__ == "__main__":
    main()
