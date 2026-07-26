"""Fetch the US Code as USLM XML and resolve mandate citations to statutory text.

Source of truth is the Office of the Law Revision Counsel's **release points**,
not govinfo's bulkdata mirror:

- OLRC publishes a new release point after each public law, so it tracks current
  law within days. ``https://uscode.house.gov/download/releasepoints/...``
- govinfo's ``/bulkdata/USCODE/`` carries *annual edition* snapshots that lag by
  up to a year, and its ``/bulkdata/json/USCODE`` listing endpoint 404s.

One fetch (~108 MB zipped, ~665 MB extracted, 58 titles) makes every subsequent
resolution offline and deterministic.

Outputs:
  data/usc/cache/xml_uscAll@<C>-<L>.zip   — the raw release-point archive
  data/usc/xml/usc*.xml                   — extracted USLM per title
  data/usc/provisions.jsonl               — one row per mandate, with resolved text

Usage:
  uv run python pipeline/statute_fetch.py                 # fetch (if needed) + resolve
  uv run python pipeline/statute_fetch.py --fetch-only
  uv run python pipeline/statute_fetch.py --resolve-only
  uv run python pipeline/statute_fetch.py --release 119-102   # pin a release point
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

from authority_parse import fold_dashes, load_mandates, parse_authority

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("statute_fetch")

DOWNLOAD_PAGE = "https://uscode.house.gov/download/download.shtml"
RELEASE_BASE = "https://uscode.house.gov/download/releasepoints/us/pl"

OUT_DIR = REPO_ROOT / "data/usc"
CACHE_DIR = OUT_DIR / "cache"
XML_DIR = OUT_DIR / "xml"
PROVISIONS_PATH = OUT_DIR / "provisions.jsonl"
RELEASE_STAMP = OUT_DIR / "release_point.json"

USLM_NS = "http://xml.house.gov/schemas/uslm/1.0"
_NS = f"{{{USLM_NS}}}"

# Nodes that can be the target of a citation's subsection path.
_ADDRESSABLE = {
    f"{_NS}section", f"{_NS}subsection", f"{_NS}paragraph",
    f"{_NS}subparagraph", f"{_NS}clause", f"{_NS}subclause",
    f"{_NS}item", f"{_NS}subitem", f"{_NS}subsubitem",
}

_WS_RE = re.compile(r"\s+")

# Block-level USLM elements. Their boundaries are not marked by whitespace in
# the source, so flattening naively welds them together — a subsection renders
# as "...audit resultsNot later than May 15...", corrupting the tokens on
# either side of every heading. Inline elements (ref, b, i, sup) must NOT be
# separated, or citations inside a sentence gain spurious spaces.
_BLOCK_TAGS = frozenset(
    f"{_NS}{t}" for t in (
        "num", "heading", "subheading", "chapeau", "content", "p",
        "continuation", "proviso", "section", "subsection", "paragraph",
        "subparagraph", "clause", "subclause", "item", "subitem", "subsubitem",
        "quotedContent", "note", "notes",
    )
)


def _text_of(elem: ET.Element, include_notes: bool = False) -> str:
    """Flatten an element to text, separating block-level boundaries.

    ``notes`` subtrees are excluded by default. A USLM section's ``<notes>``
    block holds amendment history, editorial notes and credits — including it
    would bury the operative text under decades of "1996—Subsec. (b) amended"
    boilerplate and wreck any downstream classification.
    """
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        if not include_notes and e.tag == f"{_NS}notes":
            return
        if e.text:
            parts.append(e.text)
        for child in e:
            walk(child)
            if child.tag in _BLOCK_TAGS:
                parts.append(" ")
            if child.tail:
                parts.append(child.tail)

    walk(elem)
    return _WS_RE.sub(" ", "".join(parts)).strip()


# USLM note topics that are drafting apparatus rather than operative law:
# amendment history, effective dates, prior provisions, editorial cross-refs.
# Uncodified reporting mandates live in `miscellaneous` notes (and occasionally
# untagged ones), so this is a deny list — unrecognized topics are kept.
_APPARATUS_TOPICS = frozenset({
    "amendments", "editorialNotes", "credits", "codification",
    "historicalAndRevision", "referencesInText", "priorProvisions",
    "effectiveDate", "effectiveDateOfAmendment", "prospectiveAmendment",
    "shortTitle", "shortTitleOfAmendment", "changeOfName", "derivation",
    "dispositionOfSections", "similarProvisions", "enacting", "repeals",
    "savings", "separability", "retroactiveDate", "terminationDate",
    "constitutionality", "transferOfFunctions", "reorganizationPlan",
})


def _statutory_notes(section: ET.Element) -> list[dict]:
    """Return the section's operative statutory notes.

    A citation of the form ``42 U.S.C. 300gg-118 note`` points at an uncodified
    session-law provision printed as a note under that section. Those mandates
    are invisible in codified text, so they have to be pulled from here.

    Drafting apparatus is excluded: without it, "10 U.S.C. 2687 note" (a BRAC
    reporting mandate) comes back as "References in Text — The National
    Environmental Policy Act of 1969, referred to in subsec. (f)...".
    """
    notes_el = section.find(f"{_NS}notes")
    if notes_el is None:
        return []
    out = []
    for note in notes_el.findall(f"{_NS}note"):
        topic = note.get("topic") or ""
        if topic in _APPARATUS_TOPICS:
            continue
        # Pure dividers — "Editorial Notes", "Statutory Notes and Related
        # Subsidiaries" — carry a heading and no content.
        if note.get("role") == "crossHeading":
            continue
        text = _text_of(note, include_notes=True)
        if len(text) < 40:
            continue
        out.append({"topic": topic, "role": note.get("role") or "", "text": text})
    return out


def discover_release_point() -> tuple[int, int]:
    """Scrape OLRC for the current release point, returning ``(congress, law)``."""
    with urllib.request.urlopen(DOWNLOAD_PAGE, timeout=60) as resp:
        html = resp.read().decode("utf-8", "replace")
    hits = re.findall(r"releasepoints/us/pl/(\d+)/(\d+)", html)
    if not hits:
        raise RuntimeError(f"No release point found on {DOWNLOAD_PAGE}")
    congress, law = max(((int(c), int(l)) for c, l in hits))
    logger.info("Current OLRC release point: Public Law %d-%d", congress, law)
    return congress, law


def download_release(congress: int, law: int) -> Path:
    """Download the all-titles USLM archive for a release point. Idempotent."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"xml_uscAll@{congress}-{law}.zip"
    dest = CACHE_DIR / name
    if dest.exists() and zipfile.is_zipfile(dest):
        logger.info("Release archive already cached: %s", dest)
        return dest

    url = f"{RELEASE_BASE}/{congress}/{law}/{name}"
    logger.info("Downloading %s", url)
    tmp = dest.with_suffix(".zip.part")
    req = urllib.request.Request(url, headers={"User-Agent": "cmra-compare/0.1"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded file is not a zip: {url}")
    tmp.rename(dest)
    logger.info("Saved %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest


def extract_release(archive: Path, congress: int, law: int) -> Path:
    """Unzip the release archive into ``data/usc/xml/``. Idempotent."""
    stamp = {"congress": congress, "law": law, "archive": archive.name}
    if RELEASE_STAMP.exists() and json.loads(RELEASE_STAMP.read_text()) == stamp and XML_DIR.exists():
        logger.info("XML already extracted for PL %d-%d", congress, law)
        return XML_DIR
    XML_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Extracting %s → %s", archive.name, XML_DIR)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(XML_DIR)
    RELEASE_STAMP.parent.mkdir(parents=True, exist_ok=True)
    RELEASE_STAMP.write_text(json.dumps(stamp, indent=2))
    return XML_DIR


def _title_file(title: str, appendix: bool = False) -> Path | None:
    """Map a USC title to its USLM file.

    Files are zero-padded (``usc02.xml``). Appendix titles carry a letter
    suffix whose case is inconsistent upstream — ``usc05A.xml`` and
    ``usc11a.xml`` both exist — so try both.
    """
    digits = re.sub(r"[^0-9]", "", title)
    if not digits:
        return None
    want = f"usc{int(digits):02d}" + ("a" if appendix else "") + ".xml"
    # Match against real directory entries rather than probing paths: on a
    # case-insensitive filesystem (macOS) `usc11A.xml`.exists() is true for a
    # file actually named `usc11a.xml`, which would return a path whose case
    # doesn't exist anywhere else.
    for entry in sorted(XML_DIR.glob("usc*.xml")):
        if entry.name.lower() == want:
            return entry
    return None


def build_title_index(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Index one title's USLM file, returning ``(exact, folded)`` maps.

    Every addressable node gets an entry so a citation's full subsection path
    resolves directly. Both maps are dash-folded (USLM's en dashes never match
    the House Doc's hyphens otherwise); the second is additionally lowercased,
    to be consulted only when an exact-case lookup misses. Keeping them separate
    matters because USLM levels are case-significant — ``(a)`` and ``(A)`` are
    different depths — so a lowercase-only index can in principle collide two
    distinct provisions.
    """
    exact: dict[str, dict] = {}
    folded: dict[str, dict] = {}
    root = ET.parse(path).getroot()
    for section in root.iter(f"{_NS}section"):
        sec_id = section.get("identifier")
        if not sec_id:
            continue
        sec_heading = section.find(f"{_NS}heading")
        sec_heading_text = _text_of(sec_heading) if sec_heading is not None else ""
        notes = _statutory_notes(section)
        for node in section.iter():
            if node.tag not in _ADDRESSABLE:
                continue
            nid = node.get("identifier")
            if not nid:
                continue
            heading = node.find(f"{_NS}heading")
            entry = {
                "uslm_id": nid,
                "section_id": sec_id,
                "section_heading": sec_heading_text,
                "heading": _text_of(heading) if heading is not None else "",
                "text": _text_of(node),
                "statutory_notes": notes if node is section else [],
            }
            key = fold_dashes(nid)
            exact[key] = entry
            folded.setdefault(key.lower(), entry)
    return exact, folded


def select_notes(notes: list[dict], plaw: list, stat: list) -> tuple[list[dict], str]:
    """Pick the statutory note the citation actually points at.

    A busy section carries many notes, so returning all of them buries the cited
    provision — ``49 U.S.C. 47101 note`` came back as "Runway Length in Alaska"
    (Pub. L. 118-63, 2024) instead of the runway-safety-alert mandate it means.

    Every note opens with a credit naming its source (``Pub. L. 115–232, div. B,
    title XXVII, §§ 2702, 2703, Aug. 13, 2018, 132 Stat. 2257``) and the
    authority string names the same public law, so match on that. Statutes at
    Large volume/page is the fallback, since a few credits carry only that.

    Returns ``(notes, how)``; ``how`` records which signal fired so the
    imprecise cases stay visible instead of silently looking exact.
    """
    if not notes:
        return [], "none"

    def matches(text: str, needles: list[str]) -> bool:
        folded = fold_dashes(text)
        return any(n in folded for n in needles)

    pl_needles = [f"Pub. L. {p.congress}-{p.number}" for p in plaw]
    if pl_needles:
        hits = [n for n in notes if matches(n["text"], pl_needles)]
        if hits:
            return hits, "plaw_credit"

    st_needles = [f"{s.volume} Stat. {s.page}" for s in stat]
    if st_needles:
        hits = [n for n in notes if matches(n["text"], st_needles)]
        if hits:
            return hits, "stat_credit"

    # Nothing matched: keep every note rather than guess, but say so.
    return notes, "unmatched_all"


def resolve_mandates(mandates: list[dict]) -> list[dict]:
    """Resolve every mandate's USC citations to statutory text.

    Titles are parsed one at a time and discarded, so peak memory stays near a
    single title rather than the full 665 MB corpus.
    """
    parsed = [(m, parse_authority(m.get("authority", ""))) for m in mandates]

    # Keyed by (title, is_appendix): 50 U.S.C. and 50 U.S.C. App. are different
    # titles that live in different files.
    by_title: dict[tuple[str, bool], set[str]] = defaultdict(set)
    for _, auth in parsed:
        for ref in auth.usc:
            key = (ref.title.lower(), ref.is_appendix)
            by_title[key].add(ref.uslm_id)
            # The enclosing section is looked up too, so a citation carrying a
            # subsection path that no longer exists still resolves one level up
            # instead of coming back empty.
            by_title[key].add(ref.section_id)

    resolved: dict[str, dict] = {}
    for (title, appendix) in sorted(by_title, key=lambda k: (int(re.sub(r"[^0-9]", "", k[0]) or 0), k[1])):
        refs = by_title[(title, appendix)]
        path = _title_file(title, appendix)
        label = f"{title}{' App.' if appendix else ''}"
        if path is None:
            logger.warning("No USLM file for title %s (%d refs)", label, len(refs))
            continue
        exact, folded = build_title_index(path)
        hits = 0
        for uslm_id in refs:
            entry = exact.get(uslm_id) or folded.get(uslm_id.lower())
            if entry:
                resolved[uslm_id] = entry
                hits += 1
        logger.info("title %-8s  %4d/%4d refs resolved", label, hits, len(refs))

    out = []
    for m, auth in parsed:
        provisions = []
        for ref in auth.usc:
            entry = resolved.get(ref.uslm_id)
            # A subsection path that doesn't exist still resolves usefully at the
            # section level — the citation is stale or mis-scoped, not unfindable.
            fallback = resolved.get(ref.section_id) if entry is None else None
            hit = entry or fallback
            prov = {
                "cite": ref.cite(),
                "uslm_id": ref.uslm_id,
                "is_note": ref.is_note,
                "resolved": hit is not None,
                "resolved_at": "exact" if entry else ("section" if fallback else "none"),
                **({k: v for k, v in (hit or {}).items() if k != "uslm_id"}),
            }
            # A `... note` citation points at an uncodified provision printed as
            # a note, NOT at the section it hangs under. Returning the section's
            # body would hand downstream consumers the wrong law entirely — e.g.
            # "10 U.S.C. 2687 note" (a BRAC reporting mandate) would come back as
            # the text of § 2687 itself.
            if ref.is_note and hit:
                notes, how = select_notes(hit.get("statutory_notes") or [], auth.plaw, auth.stat)
                prov["text"] = "\n\n".join(n["text"] for n in notes)
                prov["heading"] = f"Statutory notes to {hit.get('section_heading', '')}".strip()
                prov["note_selection"] = how
                prov["note_count"] = len(notes)
                # "unmatched_all" means we could not tell which note was meant and
                # returned every one; that text is low-precision, not exact.
                prov["resolved_at"] = {
                    "none": "note_absent",
                    "unmatched_all": "note_unmatched",
                }.get(how, "note")
                prov["resolved"] = bool(notes)
            provisions.append(prov)
        out.append({
            "mandate_id": m["mandate_id"],
            "reporting_entity": m.get("reporting_entity", ""),
            "nature_of_report": m.get("nature_of_report", ""),
            "when_expected": m.get("when_expected", ""),
            "authority_raw": m.get("authority", ""),
            "is_codified": auth.is_codified,
            "plaw": [p.cite() for p in auth.plaw],
            "stat": [s.cite() for s in auth.stat],
            "provisions": provisions,
        })
    return out


def summarize(rows: list[dict]) -> dict:
    cited = [r for r in rows if r["provisions"]]
    all_p = [p for r in rows for p in r["provisions"]]

    def at(kind: str) -> int:
        return sum(1 for p in all_p if p["resolved_at"] == kind)

    return {
        "mandates": len(rows),
        "uncodified_no_usc_cite": len(rows) - len(cited),
        "with_usc_cite": len(cited),
        "citations_total": len(all_p),
        "citations_resolved": sum(1 for p in all_p if p["resolved"]),
        "  at_exact_node": at("exact"),
        "  at_statutory_note (matched)": at("note"),
        "  at_statutory_note (unmatched)": at("note_unmatched"),
        "  at_section_fallback": at("section"),
        "unresolved_section_missing": at("none"),
        "unresolved_note_absent": at("note_absent"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-only", action="store_true", help="Download + extract, skip resolution")
    ap.add_argument("--resolve-only", action="store_true", help="Resolve using already-extracted XML")
    ap.add_argument("--release", help="Pin a release point, e.g. 119-102 (default: current)")
    ap.add_argument("-o", "--out", type=Path, default=PROVISIONS_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.resolve_only:
        if args.release:
            congress, law = (int(x) for x in args.release.split("-", 1))
        else:
            congress, law = discover_release_point()
        extract_release(download_release(congress, law), congress, law)

    if args.fetch_only:
        return

    if not XML_DIR.exists():
        raise SystemExit(f"{XML_DIR} not found — run without --resolve-only first")

    rows = resolve_mandates(load_mandates())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"\nWrote {len(rows)} mandates → {args.out}")

    stats = summarize(rows)
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k:<{width}}  {v:>6,}")


if __name__ == "__main__":
    main()
