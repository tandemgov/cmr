"""Normalizers used by the matcher.

Three jobs:

1. ``parse_citations(text)`` — structured citation extraction (USC, PLAW, Stat).
   Same regex run on both House Doc ``authority`` and GPO ``legalAuthority``
   gives a set of triples we can intersect to score authority overlap.

2. ``canonicalize_agency(name)`` — collapse the many variant names for the
   same agency (e.g. "Health and Human Services Department" ↔
   "Department of Health and Human Services") to a comparable key.

3. ``normalize_text(s)`` — lowercase, collapse whitespace, strip trailing
   punctuation. Used for nature/title comparisons.
"""

from __future__ import annotations

import re
import unicodedata

# ─────────────────────────────────────────────────────────────────────────────
# Citation parsing
# ─────────────────────────────────────────────────────────────────────────────

# USC: e.g. "28 U.S.C. 2412(d)(5)(A)", "5 U.S.C. 2301 note", "36 U.S.C. § 152412",
# "7 U.S.C. 950cc(d)" (multi-letter suffix), "12 U.S.C. 2279aa-10(b)(4)" (dash-suffix).
# We capture (title, section). Section may have parens like (d)(5)(A); we keep
# only the leading base — the part before the first paren — because the House
# Doc and GPO sometimes cite the same authority with vs. without subparts.
# Trailing letters can be multiple (e.g. `950cc`, `2349aa`, `286yy`); the
# original `[A-Za-z]?` truncated those to just one letter (`950c`) and risked
# false-positive matches against unrelated sections with that abbreviated key.
_USC_RE = re.compile(
    r"\b(\d+)\s*U\.?\s*S\.?\s*C\.?\s*(?:§\s*)?(\d+[A-Za-z]*(?:-\d+)?)",
    re.IGNORECASE,
)

# PLAW: "Pub. L. 116-9", "Public Law 116-9", "P.L. 116-9", "P L 116-9"
_PLAW_RE = re.compile(
    r"\b(?:Pub(?:lic)?(?:\.|\s)*\s*L(?:aw)?(?:\.|\s)*)\s*(\d+)\s*[-–]\s*(\d+)",
    re.IGNORECASE,
)

# Statutes at Large: "(133 Stat. 763)", "133 Stat 763"
_STAT_RE = re.compile(
    r"\b(\d+)\s*Stat\.?\s*(\d+)",
    re.IGNORECASE,
)


def parse_citations(text: str) -> dict[str, set[tuple]]:
    """Return {"usc": {(title, section)}, "plaw": {(congress, number)}, "stat": {(volume, page)}}.

    All keys present (possibly with empty sets) so callers can intersect freely.
    """
    if not text:
        return {"usc": set(), "plaw": set(), "stat": set()}
    return {
        "usc": {(t, s) for t, s in _USC_RE.findall(text)},
        "plaw": {(c, n) for c, n in _PLAW_RE.findall(text)},
        "stat": {(v, p) for v, p in _STAT_RE.findall(text)},
    }


def citation_overlap(a: dict[str, set[tuple]], b: dict[str, set[tuple]]) -> dict[str, int]:
    """Per-kind intersection size."""
    return {k: len(a[k] & b[k]) for k in ("usc", "plaw", "stat")}


# ─────────────────────────────────────────────────────────────────────────────
# Agency canonicalization
# ─────────────────────────────────────────────────────────────────────────────

# Hand-curated alias map. Keys are *canonical* names (whatever we pick as the
# preferred form); values are sets of known variants. Variants are compared
# after _norm_agency_basic (lowercased, punctuation stripped).
#
# This is intentionally small and seeded from observation; expand as the
# matcher surfaces unmatched variants. ``alias_to_canonical`` builds the
# reverse lookup at import time.
_ALIASES: dict[str, list[str]] = {
    "Department of Health and Human Services": [
        "Department of Health and Human Services",
        "Health and Human Services Department",
        "HHS",
        "Department of Health & Human Services",
    ],
    "Department of Defense": [
        "Department of Defense",
        "Defense Department",
        "DoD",
        "DOD",
    ],
    "Department of Homeland Security": [
        "Department of Homeland Security",
        "Homeland Security Department",
        "Homeland Security",
        "DHS",
    ],
    "Department of the Treasury": [
        "Department of the Treasury",
        "Treasury Department",
        "Department of Treasury",
    ],
    "Department of Justice": [
        "Department of Justice",
        "Justice Department",
        "DOJ",
    ],
    "Department of State": [
        "Department of State",
        "State Department",
    ],
    "Department of Commerce": [
        "Department of Commerce",
        "Commerce Department",
    ],
    "Department of Labor": [
        "Department of Labor",
        "Labor Department",
        "DOL",
    ],
    "Department of Agriculture": [
        "Department of Agriculture",
        "Agriculture Department",
        "USDA",
    ],
    "Department of Education": [
        "Department of Education",
        "Education Department",
    ],
    "Department of Energy": [
        "Department of Energy",
        "Energy Department",
        "DOE",
    ],
    "Department of the Interior": [
        "Department of the Interior",
        "Interior Department",
        "Department of Interior",
    ],
    "Department of Transportation": [
        "Department of Transportation",
        "Transportation Department",
        "DOT",
    ],
    "Department of Veterans Affairs": [
        "Department of Veterans Affairs",
        "Veterans Affairs Department",
        "VA",
    ],
    "Department of Housing and Urban Development": [
        "Department of Housing and Urban Development",
        "Housing and Urban Development Department",
        "HUD",
    ],
    "Government Accountability Office": [
        "Government Accountability Office",
        "GAO",
    ],
    "Office of the U.S. Trade Representative": [
        "Office of the U.S. Trade Representative",
        "Office of the United States Trade Representative",
        "Office of the US Trade Representative",
        "U.S. Trade Representative",
        "United States Trade Representative",
        "USTR",
    ],
    "Office of Management and Budget": [
        "Office of Management and Budget",
        "OMB",
    ],
    "Office of Personnel Management": [
        "Office of Personnel Management",
        "OPM",
        "Personnel Management Office",
    ],
    "Environmental Protection Agency": [
        "Environmental Protection Agency",
        "EPA",
    ],
    "Small Business Administration": [
        "Small Business Administration",
        "SBA",
    ],
    "National Aeronautics and Space Administration": [
        "National Aeronautics and Space Administration",
        "NASA",
    ],
    "General Services Administration": [
        "General Services Administration",
        "GSA",
    ],
    "Nuclear Regulatory Commission": [
        "Nuclear Regulatory Commission",
        "NRC",
    ],
    "Securities and Exchange Commission": [
        "Securities and Exchange Commission",
        "SEC",
    ],
    "Federal Trade Commission": [
        "Federal Trade Commission",
        "FTC",
    ],
    "Federal Communications Commission": [
        "Federal Communications Commission",
        "FCC",
    ],
    "Multiple Executive Agencies and Departments": [
        "Multiple Executive Agencies and Departments",
        "Multiple Agencies",
    ],
    "Administrative Office of the United States Courts": [
        "Administrative Office of the United States Courts",
        "Administrative Office of the U.S. Courts",
        "AOUSC",
    ],
    "Federal Emergency Management Agency": [
        "Federal Emergency Management Agency",
        "FEMA",
    ],
    "Department of the Army": [
        "Department of the Army",
        "Army Department",
        "Department of Army",
    ],
    "Department of the Navy": [
        "Department of the Navy",
        "Navy Department",
        "Department of Navy",
    ],
    "Department of the Air Force": [
        "Department of the Air Force",
        "Air Force Department",
        "Department of Air Force",
    ],
    "Centers for Medicare & Medicaid Services": [
        "Centers for Medicare & Medicaid Services",
        "Centers for Medicare and Medicaid Services",
        "CMS",
    ],
    # House Doc lists this entity surname-last ("…Foundation, Barry").
    "Barry Goldwater Scholarship & Excellence in Education Foundation": [
        "Goldwater Scholarship & Excellence in Education Foundation, Barry",
        "Barry Goldwater Scholarship and Excellence in Education Foundation",
    ],
}


_WORD_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")
# Strip any subagency suffix that GPO inserts after a dash on `organization[0]`,
# e.g. "Department of Health and Human Services - National Institutes of Health".
# This keeps the *parent* agency for matching; subagency is held separately.
_SUBAGENCY_SEP = re.compile(r"\s+-\s+")


def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_basic(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for *comparison* only."""
    s = _strip_diacritics(s or "")
    # "&" must normalize to "and", not vanish: the House Doc writes
    # "Centers for Medicare & Medicaid Services" while GPO writes
    # "Centers for Medicare and Medicaid Services" — stripping the "&" as
    # punctuation made those keys permanently unequal.
    s = s.replace("&", " and ")
    s = _WORD_RE.sub(" ", s.lower())
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


# Build reverse lookup at import time.
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for canonical, variants in _ALIASES.items():
    _ALIAS_TO_CANONICAL[_norm_basic(canonical)] = canonical
    for v in variants:
        _ALIAS_TO_CANONICAL[_norm_basic(v)] = canonical


_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def split_parent_subagency(name: str) -> tuple[str, str]:
    """For names like 'Department of HHS - National Institutes of Health', return
    ('Department of HHS', 'National Institutes of Health'). For plain names,
    return (name, ''). The GPO `organization[0]` field uses this dash convention."""
    if not name:
        return ("", "")
    # Strip trailing parenthetical acronyms like "(MACPAC)" or "(NIH)" — these
    # are the same agency under a different presentation, not a subagency.
    name = _PAREN_SUFFIX.sub("", name).strip()
    parts = _SUBAGENCY_SEP.split(name, maxsplit=1)
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    return (name.strip(), "")


def canonicalize_agency(name: str) -> str:
    """Return a stable canonical form for an agency name.

    Tries the alias map first (after basic normalization). If no alias matches,
    returns a lightly normalized form: original case preserved but extra
    whitespace collapsed. Caller should use ``agency_key`` for *comparison*
    (which lower-cases) and ``canonicalize_agency`` only for display.
    """
    if not name:
        return ""
    parent, _ = split_parent_subagency(name)
    key = _norm_basic(parent)
    if key in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[key]
    # Heuristic: try swapping "X Department" ↔ "Department of X"
    m = re.match(r"^(.+?)\s+department$", key)
    if m:
        flipped = f"department of {m.group(1)}"
        if flipped in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[flipped]
    return _WHITESPACE_RE.sub(" ", parent).strip()


def agency_key(name: str) -> str:
    """Comparison key — lowercased canonical name. Use for ==/dict-keying."""
    return _norm_basic(canonicalize_agency(name))


# ─────────────────────────────────────────────────────────────────────────────
# Text normalization (for nature / title comparison)
# ─────────────────────────────────────────────────────────────────────────────


def normalize_text(s: str) -> str:
    """Lowercase, strip diacritics, drop trailing punctuation, collapse whitespace."""
    s = _strip_diacritics(s or "")
    s = s.lower()
    s = re.sub(r"[\.,;:!?]+\s*$", "", s)  # trailing terminal punct
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


_STOPWORDS = frozenset(
    # Function words
    "a an and as at be by for from in is it of on or that the to with regarding".split()
    # Federal-report boilerplate that drives spurious token-jaccard hits between
    # otherwise-unrelated reports (e.g. every USDA paper has "fiscal year" and
    # "report to congress"). Don't strip topical words like "broadband" /
    # "assistance" even if they're common in a single agency's reports.
    + "report reports submitted submitting "
      "congress congressional house senate "
      "fiscal year years annual annually biennial biennially "
      "department departments office offices agency agencies "
      "program programs "
      "section sections title titles part parts chapter chapters "
      "act acts public law laws pub stat "
      "pursuant concerning".split()
)


def token_set(s: str) -> set[str]:
    """Tokenize a normalized string into content tokens (no stopwords, no punct)."""
    s = normalize_text(s)
    return {w for w in re.findall(r"[a-z0-9]+", s) if w not in _STOPWORDS and len(w) > 1}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
