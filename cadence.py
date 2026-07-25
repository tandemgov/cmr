"""Classify each mandate's `when_expected` text into a reporting cadence.

Categories (rough — the source text is human-written, not structured):
  annual         "annually" / "each year" / "every fiscal year" / "every year"
  semiannual     "semiannually" / "biannually" / "twice a year"
  quarterly      "quarterly" / "every quarter"
  monthly        "monthly"
  event_driven   conditional triggers ("if specified" / "upon" / "when" / "if any")
  one_time       a single dated deadline with no recurrence
  on_demand      "immediately" / "as soon as" / "promptly"
  other          everything else

Used by compare.py to surface mandates whose expected cadence is *clear* and
*recent* (annual / quarterly / monthly with at least one cycle inside the GPO
window) but for which we have no matched submission. Those are the rows where
"no submission" is most likely real non-compliance, not just a time-window
artifact.
"""

from __future__ import annotations

import re
from datetime import date

# Order matters: check more specific patterns first.
# NOTE: the "semiannual"/"biennial" pattern uses lookarounds because raw
# "biannual" is ambiguous (sometimes means biennial). We deliberately route
# "biennial(ly)" / "every 2 years" / "every other year" → biennial bucket below.
_CADENCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("biennial",    re.compile(r"\b(biennial(ly)?|every (other|2|two) years?|every 2nd year|once every (2|two) years|at least once every (2|two) years|not less than once every (2|two) years)\b", re.I)),
    ("triennial",   re.compile(r"\b(triennial(ly)?|every (3|three) years?|once every (3|three) years)\b", re.I)),
    ("semiannual",  re.compile(r"\b(semi[\s-]?annual(ly)?|twice (a|each) (year|fiscal year)|every six months|every 6 months|each semiannual period|each six[- ]month period|each 6[- ]month period|after the end of each (six[- ]?month|6[- ]?month) period)\b", re.I)),
    ("quarterly",   re.compile(r"\b(quarterly|every quarter|each quarter|every three months|every 3 months|after the end of each (fiscal )?quarter|each (fiscal )?quarter)\b", re.I)),
    ("monthly",     re.compile(r"\b(monthly|every month|each month|10th day of each month)\b", re.I)),
    ("annual",      re.compile(r"\b(annual(ly)?|each (year|fiscal year)|every (year|fiscal year)|yearly|once a year|after the end of (the |each )?(fiscal )?year|first monday in february|on or before january 15|days after the end of (the )?fiscal year|fiscal year thereafter)\b", re.I)),
    ("on_demand",   re.compile(r"\b(immediately|as soon as|without delay|promptly|as promptly as practicable)\b", re.I)),
    ("event_driven",re.compile(r"\b(if (any|specified|requested|necessary)|upon (a |the |any |request)|when (the |a |any |specified )|in the event|whenever|as needed|on demand|from time to time)\b", re.I)),
]

_HAS_DATE = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s*\d", re.I)
_HAS_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def classify(when_expected: str) -> str:
    """Return one of the cadence labels above."""
    if not when_expected:
        return "other"
    for label, pat in _CADENCE_PATTERNS:
        if pat.search(when_expected):
            return label
    # No keyword match: heuristic for one-time vs other.
    # A "Not later than <month> <day>, <year>" with no recurrence words → one_time.
    if _HAS_YEAR.search(when_expected) and not re.search(r"\b(each|every|annual|then|thereafter)\b", when_expected, re.I):
        return "one_time"
    return "other"


# How recently we expect to see a submission for each cadence to NOT consider
# the mandate overdue. Tuned to be conservative — we'd rather under-flag than
# falsely accuse an agency of non-compliance.
_FRESHNESS_DAYS = {
    "monthly":    60,
    "quarterly":  120,
    "semiannual": 240,
    "annual":     500,
    "biennial":   850,    # ~2.3 years
    "triennial":  1200,   # ~3.3 years
    "on_demand":  365,
}


def freshness_days(cadence: str) -> int | None:
    """How many days back we expect a submission, given the cadence.
    Returns None for cadences where 'overdue' isn't meaningful (event_driven,
    one_time, other)."""
    return _FRESHNESS_DAYS.get(cadence)


_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def deadline_year(when_expected: str) -> int | None:
    """For one-time deadlines like 'Not later than July 27, 2021', extract the
    year. If multiple years appear (rare), return the latest. Returns None if
    no year is present.

    Used together with `classify() == 'one_time'` to decide whether the
    deadline falls inside the CMRA reporting window.
    """
    if not when_expected:
        return None
    years = _YEAR_RE.findall(when_expected)
    if not years:
        return None
    return max(int(y) for y in years)


def in_cmra_window(when_expected: str, window_start_year: int = 2024,
                   window_end_year: int | None = None) -> tuple[bool, str]:
    """Decide whether a mandate's reporting cycle plausibly produced at least
    one submission inside the CMRA window (2024-01-01 through today).

    Returns (in_scope, reason_label). `reason_label` is one of:
      'recurring'        — annual/quarterly/monthly/semiannual mandate
      'one_time_in'      — single-deadline mandate with deadline in window
      'on_demand'        — triggered by external request; assume in scope
      'one_time_out'     — single-deadline mandate with deadline before window
      'one_time_future'  — single-deadline mandate not yet due
      'event_driven'     — only fires when conditions arise; can't judge
      'other'            — heterogeneous; can't judge

    Only `in_scope == True` cases (recurring, one_time_in, on_demand) should
    be counted in a CMRA-compliance denominator. Deadlines after the window
    end are excluded too — a mandate that is not yet due cannot be covered,
    and counting it as uncovered deflates the rate. (Year granularity only:
    a deadline later in the current year may not strictly be due yet.)
    """
    if window_end_year is None:
        window_end_year = date.today().year
    cad = classify(when_expected)
    if cad in ("annual", "semiannual", "quarterly", "monthly", "biennial", "triennial"):
        return True, "recurring"
    if cad == "on_demand":
        # "Immediately" / "as soon as" — we treat as in-scope on the
        # assumption these triggered at some point in the 2.5-year window.
        return True, "on_demand"
    if cad == "one_time":
        y = deadline_year(when_expected)
        if y is not None and window_start_year <= y <= window_end_year:
            return True, "one_time_in"
        if y is not None and y > window_end_year:
            return False, "one_time_future"
        return False, "one_time_out"
    if cad == "event_driven":
        return False, "event_driven"
    return False, "other"


# ─────────────────────────────────────────────────────────────────────────────
# CMRA entity coverage
# ─────────────────────────────────────────────────────────────────────────────
#
# CMRA (the Access to Congressionally Mandated Reports Act, Pub. L. 117-263,
# div. G, title LXXII, subtitle D, §§ 7241–7248, 136 Stat. 3677) defines
# "Federal agency" by reference to 40 U.S.C. 102 —
# which covers executive agencies AND establishments in the legislative and
# judicial branches (that is why AOUSC and CBO can file), EXCEPT the Senate,
# the House of Representatives, and the Architect of the Capitol — and then
# further excludes the Government Accountability Office and elements of the
# intelligence community (50 U.S.C. 3003). The President is not an "agency"
# at all. Mandates assigned to these entities can never legally appear in
# the CMR collection, so a CMR-channel compliance denominator must exclude
# them; counting them as "non-compliant" would penalize entities the statute
# does not reach.

_EXEMPT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("gao", re.compile(
        r"\b(government accountability office|comptroller general)\b", re.I)),
    ("president", re.compile(
        r"^(president|vice president) of the united states$", re.I)),
    ("intelligence_community", re.compile(
        r"\b(director of national intelligence"
        r"|central intelligence agency"
        r"|defense intelligence agency"
        r"|national security agency"
        r"|national reconnaissance office"
        r"|national geospatial[- ]intelligence agency"
        r"|bureau of intelligence and research"
        r"|national counterterrorism center"
        r"|intelligence community)\b", re.I)),
    ("house_senate_aoc", re.compile(
        r"\b(architect of the capitol"
        r"|house of representatives"
        r"|united states senate"
        r"|clerk of the house"
        r"|secretary of the senate)\b", re.I)),
]


def cmra_exempt_reason(reporting_entity: str) -> str | None:
    """Return why this entity is outside CMRA's filing obligation, or None
    if the entity is a CMRA-covered "Federal agency"."""
    if not reporting_entity:
        return None
    for reason, pat in _EXEMPT_PATTERNS:
        if pat.search(reporting_entity):
            return reason
    return None
