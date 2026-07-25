"""Fetch the GPO Congressionally Mandated Reports (CMR) catalog from govinfo.gov.

Pulls every package's summary JSON into a local cache, then derives two views:

- ``data/gpo/packages/CMR-*.json``  — raw summary per package
- ``data/gpo/submissions.jsonl``    — one row per package (flat, joinable)
- ``data/gpo/requirements.jsonl``   — one row per unique requirement.number

Idempotent: rerunning skips packages already in the cache. Honors rate-limit
headers and backs off on 429s. Resumable across multiple runs.

Env vars:
  GOVINFO_API_KEY   (required; get one at https://api.data.gov/signup/)

Usage:
  uv run python gpo_fetch.py                        # full fetch (resumable)
  uv run python gpo_fetch.py --since 2024-01-01     # change start date
  uv run python gpo_fetch.py --derive-only          # rebuild views from cache
  uv run python gpo_fetch.py --refresh PACKAGE_ID   # force re-fetch one
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("gpo_fetch")

API_BASE = "https://api.govinfo.gov"
COLLECTION = "CMR"
DEFAULT_SINCE = "2024-01-01T00:00:00Z"

OUT_DIR = Path("data/gpo")
PACKAGES_DIR = OUT_DIR / "packages"
SUBMISSIONS_PATH = OUT_DIR / "submissions.jsonl"
REQUIREMENTS_PATH = OUT_DIR / "requirements.jsonl"


def _http_get_json(url: str, params: dict, api_key: str, max_retries: int = 5) -> dict:
    """GET a JSON endpoint with rate-limit-aware backoff.

    The data.gov gateway returns X-RateLimit-Remaining; when it drops low we
    slow down. On 429 we sleep the Retry-After interval (or exponential
    backoff) and try again up to max_retries.
    """
    params = {**params, "api_key": api_key}
    qs = urllib.parse.urlencode(params)
    full = f"{url}?{qs}"

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "cmra-compare/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining is not None and int(remaining) < 20:
                    logger.warning("Rate limit low (%s remaining); slowing down", remaining)
                    time.sleep(2.0)
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "60"))
                logger.warning("429 Too Many Requests; sleeping %ds (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue
            if e.code in (500, 502, 503, 504):
                backoff = 2 ** attempt
                logger.warning("HTTP %d; backing off %ds", e.code, backoff)
                time.sleep(backoff)
                continue
            raise
        except urllib.error.URLError as e:
            backoff = 2 ** attempt
            logger.warning("URLError %s; backing off %ds", e, backoff)
            time.sleep(backoff)

    raise RuntimeError(f"Failed after {max_retries} retries: {full}")


def list_package_ids(api_key: str, since: str) -> list[dict]:
    """Page through the CMR collection, returning the summary stubs.

    Each stub has packageId, title, dateIssued, congress, docClass, lastModified.
    Use packageId to fetch full detail via fetch_package_summary().
    """
    out: list[dict] = []
    offset = 0
    page_size = 1000
    while True:
        data = _http_get_json(
            f"{API_BASE}/collections/{COLLECTION}/{since}",
            {"offset": offset, "pageSize": page_size},
            api_key,
        )
        pkgs = data.get("packages", [])
        out.extend(pkgs)
        total = data.get("count", 0)
        logger.info("Listed %d/%d", len(out), total)
        if not pkgs or len(out) >= total:
            break
        offset += len(pkgs)
        time.sleep(0.2)
    return out


def fetch_package_summary(api_key: str, package_id: str) -> dict:
    return _http_get_json(f"{API_BASE}/packages/{package_id}/summary", {}, api_key)


def cache_path_for(package_id: str) -> Path:
    return PACKAGES_DIR / f"{package_id}.json"


def fetch_all(since: str, force_refresh: list[str] | None = None) -> None:
    api_key = os.environ.get("GOVINFO_API_KEY")
    if not api_key:
        sys.exit("GOVINFO_API_KEY not set in environment (.env)")
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)

    stubs = list_package_ids(api_key, since)
    force_set = set(force_refresh or [])

    todo = [s for s in stubs if force_set and s["packageId"] in force_set or not cache_path_for(s["packageId"]).exists()]
    skipped = len(stubs) - len(todo)
    logger.info("To fetch: %d  (cached: %d)", len(todo), skipped)

    # Throttle: ~3 req/sec stays well under the 1000/hr data.gov key limit.
    sleep_between = 0.35
    for i, stub in enumerate(todo, 1):
        pid = stub["packageId"]
        try:
            summary = fetch_package_summary(api_key, pid)
        except Exception as e:
            logger.error("Failed to fetch %s: %s", pid, e)
            continue
        cache_path_for(pid).write_text(json.dumps(summary, indent=2))
        if i % 25 == 0 or i == len(todo):
            logger.info("Fetched %d/%d", i, len(todo))
        time.sleep(sleep_between)


def _agency_strings(summary: dict) -> dict:
    """Pull the three agency-name fields. Always returns the keys, even if blank."""
    orgs = summary.get("organization") or []
    return {
        "government_author": summary.get("governmentAuthor1", ""),
        "organization_display_name": summary.get("organizationDisplayName", ""),
        "organization_full": orgs[0] if orgs else "",
    }


def _parse_references(refs: list[dict]) -> dict[str, list[list]]:
    """Convert govinfo's `references` block into the same shape as
    normalize.parse_citations() output, but JSON-serializable (lists not sets).

    Examples of govinfo refs (per package):
      USCODE   {"title":"28","sections":["2412"]}    → [["28","2412"]]
      PLAW     {"number":"9","congress":"116"}       → [["116","9"]]
      STATUTE  {"title":"133","pages":["763"]}       → [["133","763"]]
    """
    out = {"usc": [], "plaw": [], "stat": []}
    for ref in refs or []:
        code = ref.get("collectionCode")
        for c in ref.get("contents") or []:
            if code == "USCODE":
                title = c.get("title", "")
                for sec in c.get("sections") or []:
                    out["usc"].append([title, sec])
            elif code == "PLAW":
                out["plaw"].append([c.get("congress", ""), c.get("number", "")])
            elif code == "STATUTE":
                title = c.get("title", "")
                for page in c.get("pages") or []:
                    out["stat"].append([title, page])
    return out


def derive_submissions(verbose: bool = False) -> int:
    """Flatten each cached package summary into a submission row."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(SUBMISSIONS_PATH, "w") as out:
        for path in sorted(PACKAGES_DIR.glob("*.json")):
            s = json.loads(path.read_text())
            # Dedupe: some packages list the same requirement.number multiple
            # times (e.g. CMR-FMC1-00195455 has req#319 three times), which
            # would otherwise inflate every Stage A attachment 3×.
            seen_req_nums: set[str] = set()
            req_numbers: list[str] = []
            for r in (s.get("requirement") or []):
                n = r.get("number")
                if n and n not in seen_req_nums:
                    seen_req_nums.add(n)
                    req_numbers.append(n)
            row = {
                "package_id": s.get("packageId"),
                "title": s.get("title", ""),
                "branch": s.get("branch", ""),
                "category": s.get("category", ""),
                "congress": s.get("congress", ""),
                "date_issued": s.get("dateIssued", ""),
                "submitted_to_congress_date": s.get("submittedToCongressDate", ""),
                "submitted_to_gpo_date": s.get("submittedToGpoDate", ""),
                "required_at_gpo_date": s.get("requiredAtGpoDate", ""),
                "is_on_time": s.get("isOnTime", ""),
                "is_replacement": s.get("isReplacement", ""),
                "doc_class": s.get("docClass", ""),
                "requirement_numbers": req_numbers,
                "references": _parse_references(s.get("references") or []),
                "details_link": s.get("detailsLink", ""),
                **_agency_strings(s),
            }
            out.write(json.dumps(row) + "\n")
            count += 1
    logger.info("Wrote %d submissions → %s", count, SUBMISSIONS_PATH)
    return count


def derive_requirements() -> int:
    """Collapse all requirement records across packages into unique mandate rows.

    A requirement.number is the stable GPO mandate ID. Across the corpus, the
    same number can appear in many packages (e.g. multi-agency mandates). Some
    packages carry only the `number` (the substantive fields are blank), so we
    merge with prefer-non-empty semantics: any package's populated value wins
    over a previously-seen empty.
    """
    by_number: dict[str, dict] = {}
    field_map = {
        "nature": "nature",
        "legalAuthority": "legal_authority",
        "frequency": "frequency",
        "submittingOfficial": "submitting_official",
        "submittingAgency": "submitting_agency_canonical",
        "parentAgency": "parent_agency",
        "updateDate": "update_date",
        "activeRecord": "active_record",
    }
    # Track per-requirement merged structured citations from package `references`
    # so requirements whose `legalAuthority` text is empty can still be matched
    # via their parsed USC/PLAW/Stat references.
    ref_acc: dict[str, dict[str, set[tuple]]] = {}

    for path in sorted(PACKAGES_DIR.glob("*.json")):
        s = json.loads(path.read_text())
        pid = s.get("packageId")
        pkg_refs = _parse_references(s.get("references") or [])
        for req in s.get("requirement") or []:
            num = req.get("number")
            if not num:
                continue
            existing = by_number.setdefault(num, {
                "requirement_number": num,
                **{v: "" for v in field_map.values()},
                "references": {"usc": [], "plaw": [], "stat": []},
                "submitting_agencies_seen": [],
                "package_ids": [],
            })
            for src, dst in field_map.items():
                v = (req.get(src) or "").strip()
                if v and not existing[dst]:
                    existing[dst] = v
            # Accumulate parsed references across all packages referencing this req
            acc = ref_acc.setdefault(num, {"usc": set(), "plaw": set(), "stat": set()})
            for k in ("usc", "plaw", "stat"):
                acc[k].update(tuple(x) for x in pkg_refs[k])
            agency = (s.get("governmentAuthor1") or "").strip()
            if agency and agency not in existing["submitting_agencies_seen"]:
                existing["submitting_agencies_seen"].append(agency)
            if pid and pid not in existing["package_ids"]:
                existing["package_ids"].append(pid)

    # Materialize accumulated references back onto the requirement records.
    for num, acc in ref_acc.items():
        rec = by_number[num]
        rec["references"] = {k: sorted(list(v)) for k, v in acc.items()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(REQUIREMENTS_PATH, "w") as out:
        for r in sorted(by_number.values(), key=lambda x: int(x["requirement_number"])):
            out.write(json.dumps(r) + "\n")
    logger.info("Wrote %d unique requirements → %s", len(by_number), REQUIREMENTS_PATH)
    return len(by_number)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default=DEFAULT_SINCE, help="Last-modified start (ISO 8601)")
    ap.add_argument("--derive-only", action="store_true", help="Skip fetch; rebuild submissions+requirements from cache")
    ap.add_argument("--refresh", action="append", default=[], help="Force re-fetch a packageId (repeatable)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.derive_only:
        fetch_all(args.since, force_refresh=args.refresh)

    derive_submissions(verbose=args.verbose)
    derive_requirements()


if __name__ == "__main__":
    main()
