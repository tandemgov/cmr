"""Collapse swept provisions into mandates, and write the published list.

One duty surfaces as many swept nodes, so rows merge only on evidence of a shared duty — RUNBOOK section 12.

Usage:
  uv run python pipeline/mandate_units.py
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("mandate_units")

LIVE_PATH = REPO_ROOT / "data/usc/sweep_live.jsonl"
CANDIDATES_PATH = REPO_ROOT / "data/usc/sweep_candidates.jsonl"
OUT_PATH = REPO_ROOT / "data/discovered/mandates.jsonl"

# An excerpt, not the provision: records link to the full text, and whole provisions would put ~5 MB of the Code in git.
TEXT_CHARS = 800
CSV_FIELDS = ("citation", "reporting_entity", "cadence", "deadline", "source", "url", "uslm_id")

_SECTION_RE = re.compile(r"(/us/usc/t[0-9A-Za-z]+(?:/pl/\d+/\d+)?/s[^/]+)")
_NOTE_RE = re.compile(r"/note/\d+$")

# Applied only to text siblings share, so a heading like "Reports to Congress" (no modal) never licenses a merge.
_DUTY_RE = re.compile(
    r"(?is)\b(?:shall|must|is required to|are required to)\b[^.;]{0,200}?"
    r"\b(?:submit|transmit|report|provide|notify|furnish|deliver|prepare|make available)\w*"
)


# ─────────────────────────────────────────────────────────────────────────────
# Keys
# ─────────────────────────────────────────────────────────────────────────────


_PERIODIC = (
    ("weekly", r"weekly"),
    ("monthly", r"monthly|every month"),
    ("quarterly", r"quarter"),
    ("semiannual", r"semi-?annual|every 6 months|every six months|every 180 days|twice (?:a|each) year"),
    ("biennial", r"bienni|every 2 years|every two years"),
    ("annual", r"annual|each year|every year|yearly"),
    ("multi-year", r"trienni|quadrenni|every \w+ years|five-year|every \w+(?:th)? congress|\b\d+ years\b"),
    ("periodic", r"periodic|regular|recurring"),
)


def cadence(freq: str | None) -> str:
    """Fold a free-text frequency label into a cadence class.

    A named cadence wins over everything else: "one-time and annual" is an annual duty with a first deadline.
    """
    f = (freq or "").strip().lower()
    for label, pattern in _PERIODIC:
        if re.search(pattern, f):
            return label
    if "event" in f or "session" in f:
        return "event-driven"
    if "one-time" in f or "one time" in f:
        return "one-time"
    if f in ("", "unknown", "none"):
        return "unknown"
    return "other"


RECURRING = {"weekly", "monthly", "quarterly", "semiannual", "annual", "biennial",
             "multi-year", "periodic", "other"}


def entity_key(entity: str | None) -> str:
    e = re.sub(r"[^a-z ]", " ", (entity or "").lower())
    e = re.sub(r"\b(the|a|an|of the united states)\b", " ", e)
    return re.sub(r"\s+", " ", e).strip()


def same_entity(a: str, b: str) -> bool:
    """Prefix-tolerant: the judge writes "Secretary" and "Secretary of State" for one actor, and echo rows often drop the actor."""
    if not a or not b:
        return True
    return a == b or a.startswith(b) or b.startswith(a)


def section_of(uslm_id: str) -> str | None:
    m = _SECTION_RE.match(uslm_id or "")
    return m.group(1) if m else None


def parent_of(uslm_id: str) -> str | None:
    """The enclosing node, or None for a section or a note (neither nests)."""
    if _NOTE_RE.search(uslm_id) or uslm_id == section_of(uslm_id):
        return None
    return uslm_id.rsplit("/", 1)[0]


def ancestors(uslm_id: str):
    p = parent_of(uslm_id)
    while p:
        yield p
        p = parent_of(p)


# ─────────────────────────────────────────────────────────────────────────────
# Citations
# ─────────────────────────────────────────────────────────────────────────────


def citation(uslm_id: str) -> str:
    """`/us/usc/t42/s300gg–111/a/2` → `42 U.S.C. 300gg-111(a)(2)`."""
    u = uslm_id.replace("–", "-")
    m = re.match(r"/us/usc/t(\w+)/pl/(\d+)/(\d+)/s([^/]+)((?:/[^/]+)*)$", u)
    if m:
        title, cong, num, sec, rest = m.groups()
        path = "".join(f"({p})" for p in rest.split("/") if p)
        return f"Pub. L. {cong}-{num}, § {sec}{path} ({title.rstrip('a')} U.S.C. App.)"
    m = re.match(r"/us/usc/t(\w+)/s([^/]+)((?:/[^/]+)*)$", u)
    if not m:
        return uslm_id
    title, sec, rest = m.groups()
    parts = [p for p in rest.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "note":
        return f"{title} U.S.C. {sec} note"
    return f"{title} U.S.C. {sec}" + "".join(f"({p})" for p in parts)


def uscode_url(uslm_id: str) -> str | None:
    m = re.match(r"/us/usc/t(\d+)/s([^/]+)", uslm_id.replace("–", "-"))
    if not m:
        return None
    title, sec = m.groups()
    return (f"https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title{title}"
            f"-section{sec}&num=0&edition=prelim")


# ─────────────────────────────────────────────────────────────────────────────
# Units
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Row:
    uslm_id: str
    entity: str
    cadence: str
    verdict: dict
    text: str = ""


@dataclass
class Unit:
    root: Row
    members: list[Row] = field(default_factory=list)


def _common_prefix(texts: list[str]) -> str:
    if not texts:
        return ""
    lo, hi = min(texts), max(texts)
    i = 0
    while i < min(len(lo), len(hi)) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def build_units(rows: list[Row]) -> list[Unit]:
    """Group rows into duties. See the module docstring for the two merge rules."""
    by_id = {r.uslm_id: r for r in rows}

    def key_match(a: Row, b: Row) -> bool:
        return a.cadence == b.cadence and same_entity(a.entity, b.entity)

    # Rule 1: fold into the nearest flagged ancestor that is the same duty.
    owner: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r.uslm_id.count("/")):
        anc = next((a for a in ancestors(r.uslm_id) if a in by_id), None)
        if anc is not None and key_match(r, by_id[anc]):
            owner[r.uslm_id] = owner[anc]
        else:
            owner[r.uslm_id] = r.uslm_id

    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[owner[r.uslm_id]].append(r)

    # Rule 2: siblings whose shared text carries the obligation.
    roots = [by_id[k] for k in groups]
    families: dict[tuple, list[Row]] = defaultdict(list)
    for r in roots:
        p = parent_of(r.uslm_id)
        if p is not None:
            families[(p, r.cadence)].append(r)
    merged_into: dict[str, str] = {}
    for fam in families.values():
        if len(fam) < 2:
            continue
        # Split the family into entity-compatible clusters before testing text.
        clusters: list[list[Row]] = []
        for r in sorted(fam, key=lambda r: r.uslm_id):
            for c in clusters:
                if same_entity(c[0].entity, r.entity):
                    c.append(r)
                    break
            else:
                clusters.append([r])
        for c in clusters:
            if len(c) > 1 and _DUTY_RE.search(_common_prefix([r.text for r in c])):
                for r in c[1:]:
                    merged_into[r.uslm_id] = c[0].uslm_id

    units: dict[str, Unit] = {}
    for k, members in groups.items():
        target = merged_into.get(k, k)
        u = units.setdefault(target, Unit(root=by_id[target]))
        u.members.extend(members)
    return list(units.values())


# ─────────────────────────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────────────────────────


def load_rows(live: Path = LIVE_PATH, candidates: Path = CANDIDATES_PATH) -> tuple[list[Row], Counter]:
    """Live rows with a recurring or event-driven cadence, plus a tally of what was dropped."""
    text = {}
    for line in candidates.read_text().splitlines():
        if line.strip():
            c = json.loads(line)
            text[c["uslm_id"]] = c["text"]

    rows: list[Row] = []
    dropped: Counter[str] = Counter()
    for line in live.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["currency"] != "current":
            dropped[f"currency:{r['currency']}"] += 1
            continue
        v = r.get("verdict") or {}
        cad = cadence(v.get("frequency"))
        if cad not in RECURRING and cad != "event-driven":
            dropped[f"cadence:{cad}"] += 1
            continue
        rows.append(Row(r["uslm_id"], entity_key(v.get("reporting_entity")), cad, v,
                        text.get(r["uslm_id"], "")))
    return rows, dropped


def unit_record(u: Unit) -> dict:
    r = u.root
    v = r.verdict
    return {
        "uslm_id": r.uslm_id,
        "citation": citation(r.uslm_id),
        "url": uscode_url(r.uslm_id),
        "source": "note" if _NOTE_RE.search(r.uslm_id) else "provision",
        "reporting_entity": v.get("reporting_entity", ""),
        "cadence": r.cadence,
        "frequency_label": v.get("frequency", ""),
        "deadline": v.get("deadline", ""),
        "text": r.text[:TEXT_CHARS],
        "members": sorted(m.uslm_id for m in u.members),
    }


def write(units: list[Unit], out: Path = OUT_PATH) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    recs = sorted((unit_record(u) for u in units), key=lambda d: d["uslm_id"])
    with open(out, "w") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(out.with_suffix(".csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    logger.info("Wrote %d mandates → %s (+ .csv)", len(recs), out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    rows, dropped = load_rows()
    units = build_units(rows)
    write(units, args.out)

    tiers = Counter("event-driven" if u.root.cadence == "event-driven" else "recurring" for u in units)
    print(f"  live rows kept          {len(rows):>6,}")
    for k, n in sorted(dropped.items()):
        print(f"    dropped {k:<14}{n:>6,}")
    print(f"  mandates                {len(units):>6,}")
    print(f"    recurring             {tiers['recurring']:>6,}")
    print(f"    event-driven          {tiers['event-driven']:>6,}")
    print(f"  sections                {len({section_of(u.root.uslm_id) for u in units}):>6,}")


if __name__ == "__main__":
    main()
