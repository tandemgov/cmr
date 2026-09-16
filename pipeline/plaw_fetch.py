"""Resolve uncodified mandates to public-law text via govinfo's PLAW collection.

``statute_fetch.py`` covers mandates whose authority is codified in the US Code.
It cannot reach the ~31% of House Doc rows that cite only session law: those
provisions were never codified, so no US Code text states their duty.

Those mandates are disproportionately *recent* — the 117th Congress alone
accounts for ~200 of them — which makes them the part of the Clerk's list you
most want to read and currently cannot.

Two hard boundaries, both verified against the API rather than assumed:

- **govinfo PLAW begins at the 104th Congress (1995).** ``PLAW-103publ1`` and
  earlier 404. Older public laws exist only in the Statutes at Large (STATUTE)
  collection as scanned PDFs, and are reported here as ``out_of_collection``.
- **USLM XML exists only for recent congresses** (113th onward at time of
  writing). Older packages offer HTML/text only, so section extraction falls
  back to parsing ``SEC. <n>.`` headers.

A tempting shortcut that does NOT work: looking these provisions up among the
US Code's statutory notes. Measured across the corpus, 93% of the gap rows name
a section but only 5.6% of them appear in any note credit — uncodified law in
this population is genuinely absent from the Code.

Outputs:
  data/usc/plaw/cache/PLAW-*.{xml,htm}   — raw packages
  data/usc/plaw_provisions.jsonl         — one row per gap mandate, with text

Env vars:
  GOVINFO_API_KEY   (required; same key gpo_fetch.py uses)

Usage:
  uv run python pipeline/plaw_fetch.py                # fetch + resolve
  uv run python pipeline/plaw_fetch.py --resolve-only
  uv run python pipeline/plaw_fetch.py --limit 25     # pilot run
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

from authority_parse import PlawSectionRef, load_mandates, parse_plaw_sections

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv()

logger = logging.getLogger("plaw_fetch")

API_BASE = "https://api.govinfo.gov"
OUT_DIR = REPO_ROOT / "data/usc"
CACHE_DIR = OUT_DIR / "plaw/cache"
PROVISIONS_PATH = OUT_DIR / "plaw_provisions.jsonl"
CODIFIED_PATH = OUT_DIR / "provisions.jsonl"

_WS_RE = re.compile(r"\s+")


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag.

    PLAW and the US Code use *different* USLM namespaces —
    ``http://schemas.gpo.gov/xml/uslm`` (GPO, root element ``pLaw``) versus
    ``http://xml.house.gov/schemas/uslm/1.0`` (OLRC) — and GPO has changed its
    URI before. Matching on local names keeps this working across both.
    """
    return tag.rsplit("}", 1)[-1]

# govinfo's PLAW collection starts here; see module docstring.
EARLIEST_PLAW_CONGRESS = 104


def _http_get(url: str, api_key: str, max_retries: int = 5) -> bytes | None:
    """GET with rate-limit-aware backoff. Returns None on 404.

    Mirrors gpo_fetch._http_get_json's retry discipline; a 404 is an expected
    outcome here (not every public law is in the collection), so it is reported
    rather than raised.
    """
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}{urllib.parse.urlencode({'api_key': api_key})}"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "cmra-compare/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining is not None and int(remaining) < 20:
                    logger.warning("Rate limit low (%s remaining); slowing down", remaining)
                    time.sleep(2.0)
                return resp.read()
        except urllib.error.HTTPError as e:
            # 404 = package absent from the collection. 400 = this *format* is
            # not offered for this package — govinfo returns Bad Request, not
            # Not Found, when you ask for USLM on a pre-113th law. Both mean
            # "not available here", so the caller can try the next format.
            if e.code in (400, 404):
                return None
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "60"))
                logger.warning("429; sleeping %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError as e:
            logger.warning("URLError %s; backing off", e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def fetch_package(package_id: str, api_key: str) -> tuple[Path, str] | None:
    """Fetch one public law, preferring USLM XML over HTML. Idempotent.

    Returns ``(path, kind)`` where kind is ``"uslm"`` or ``"htm"``, or None if
    the package is absent from the collection.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ext, kind in (("xml", "uslm"), ("htm", "htm")):
        p = CACHE_DIR / f"{package_id}.{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p, kind

    for endpoint, ext, kind in (("uslm", "xml", "uslm"), ("htm", "htm", "htm")):
        body = _http_get(f"{API_BASE}/packages/{package_id}/{endpoint}", api_key)
        if body:
            p = CACHE_DIR / f"{package_id}.{ext}"
            p.write_bytes(body)
            return p, kind
    return None


def _text_of(elem: ET.Element) -> str:
    return _WS_RE.sub(" ", "".join(elem.itertext())).strip()


def extract_section_uslm(path: Path, section: str) -> str | None:
    """Pull one section's text from a public law's USLM XML.

    ``<num value="1001">SEC. 1001.</num>`` — the ``value`` attribute is the
    clean section number; the element text carries the printed form.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    want = section.strip().lower()
    for sec in root.iter():
        if _local(sec.tag) != "section":
            continue
        num_el = next((c for c in sec if _local(c.tag) == "num"), None)
        if num_el is None:
            continue
        num = (num_el.get("value") or _text_of(num_el)).strip().lower()
        num = num.rstrip(".").removeprefix("sec.").removeprefix("section").strip(" .§")
        if num == want:
            return _text_of(sec)
    return None


# "SEC. 1095. COMMISSION ON..." — the enrolled-bill section header. Anchored to
# line start so cross-references inside body text ("under section 1095") don't
# match.
def _sec_header(section: str) -> re.Pattern:
    return re.compile(rf"^\s*SEC\.?\s*{re.escape(section)}\.\s", re.MULTILINE | re.IGNORECASE)


_ANY_SEC_HEADER = re.compile(r"^\s*SEC\.?\s*\d+[A-Za-z]?\.\s", re.MULTILINE | re.IGNORECASE)
# Enrolled-bill sidenotes: "<<NOTE: 10 USC 1144 note.>>", "<<NOTE: Deadline.>>".
# Printing apparatus, not statutory text. govinfo serves these HTML-escaped
# inside <pre>, so they only become matchable after unescaping — see the
# strip/unescape/strip order in extract_section_text.
_SIDENOTE_RE = re.compile(r"<<\s*NOTE:.*?>>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_section_text(path: Path, section: str) -> str | None:
    """Pull one section from a public law's HTML/text rendering.

    Runs from the section's own ``SEC. n.`` header to the next section header,
    which is the only structure these renderings provide.

    A public law states each section number at least twice: once in the table
    of contents and once at the section itself. The TOC entry is immediately
    followed by the next TOC entry, so it yields a one-line chunk. Taking the
    *longest* chunk across all matches picks the body over the TOC without
    having to locate where the TOC ends.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    # Order matters: real markup goes first, then entities are decoded, and only
    # then are sidenotes matchable — they arrive as `&lt;&lt;NOTE:...&gt;&gt;`.
    body = _SIDENOTE_RE.sub(" ", html.unescape(_TAG_RE.sub("", raw)))
    best = ""
    for m in _sec_header(section).finditer(body):
        nxt = _ANY_SEC_HEADER.search(body, m.end())
        chunk = body[m.start(): nxt.start() if nxt else len(body)]
        if len(chunk) > len(best):
            best = chunk
    return _WS_RE.sub(" ", best).strip() or None


def gap_mandates(codified_path: Path = CODIFIED_PATH) -> list[dict]:
    """Mandates that ``statute_fetch.py`` could not resolve to US Code text."""
    if not codified_path.exists():
        raise SystemExit(
            f"{codified_path} not found — run `uv run python pipeline/statute_fetch.py` first"
        )
    rows = [json.loads(l) for l in codified_path.read_text().splitlines() if l.strip()]
    return [r for r in rows if not any(p.get("resolved") for p in r["provisions"])]


def resolve(limit: int | None = None, fetch: bool = True) -> list[dict]:
    api_key = os.environ.get("GOVINFO_API_KEY", "")
    if fetch and not api_key:
        sys.exit("GOVINFO_API_KEY not set in environment (.env)")

    gaps = gap_mandates()
    if limit:
        gaps = gaps[:limit]
    logger.info("Gap mandates to resolve: %d", len(gaps))

    targets: dict[str, PlawSectionRef] = {}
    for r in gaps:
        for ref in parse_plaw_sections(r["authority_raw"]):
            if ref.in_plaw_collection:
                targets.setdefault(ref.package_id, ref)
    logger.info("Unique PLAW packages to fetch: %d", len(targets))

    fetched: dict[str, tuple[Path, str]] = {}
    if fetch:
        for i, pid in enumerate(sorted(targets), 1):
            try:
                got = fetch_package(pid, api_key)
            except Exception as e:
                logger.error("Failed %s: %s", pid, e)
                continue
            if got:
                fetched[pid] = got
            if i % 25 == 0 or i == len(targets):
                logger.info("Fetched %d/%d", i, len(targets))
            time.sleep(0.35)
    else:
        for pid in targets:
            for ext, kind in (("xml", "uslm"), ("htm", "htm")):
                p = CACHE_DIR / f"{pid}.{ext}"
                if p.exists():
                    fetched[pid] = (p, kind)
                    break

    out = []
    for r in gaps:
        provisions = []
        for ref in parse_plaw_sections(r["authority_raw"]):
            prov = {
                "cite": ref.cite(),
                "package_id": ref.package_id,
                "section": ref.section,
                "resolved": False,
                "resolved_at": "",
                "text": "",
            }
            if not ref.in_plaw_collection:
                prov["resolved_at"] = "out_of_collection"
            elif ref.package_id not in fetched:
                prov["resolved_at"] = "package_missing"
            elif not ref.section:
                prov["resolved_at"] = "section_unspecified"
            else:
                path, kind = fetched[ref.package_id]
                text = (extract_section_uslm(path, ref.section) if kind == "uslm"
                        else extract_section_text(path, ref.section))
                if text:
                    prov.update(resolved=True, resolved_at=f"section_{kind}", text=text)
                else:
                    prov["resolved_at"] = "section_not_found"
            provisions.append(prov)
        out.append({
            "mandate_id": r["mandate_id"],
            "reporting_entity": r["reporting_entity"],
            "nature_of_report": r["nature_of_report"],
            "when_expected": r["when_expected"],
            "authority_raw": r["authority_raw"],
            "plaw_provisions": provisions,
        })
    return out


def summarize(rows: list[dict]) -> dict:
    resolved = sum(1 for r in rows if any(p["resolved"] for p in r["plaw_provisions"]))
    reasons: dict[str, int] = {}
    for r in rows:
        if any(p["resolved"] for p in r["plaw_provisions"]):
            continue
        why = r["plaw_provisions"][0]["resolved_at"] if r["plaw_provisions"] else "no_plaw_cite"
        reasons[why] = reasons.get(why, 0) + 1
    return {
        "gap_mandates": len(rows),
        "resolved_to_public_law_text": resolved,
        "unresolved": len(rows) - resolved,
        **{f"  unresolved: {k}": v for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resolve-only", action="store_true", help="Use cached packages; no network")
    ap.add_argument("--limit", type=int, help="Only process the first N gap mandates (pilot)")
    ap.add_argument("-o", "--out", type=Path, default=PROVISIONS_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    rows = resolve(limit=args.limit, fetch=not args.resolve_only)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"\nWrote {len(rows)} rows → {args.out}")

    stats = summarize(rows)
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k:<{width}}  {v:>5,}")


if __name__ == "__main__":
    main()
