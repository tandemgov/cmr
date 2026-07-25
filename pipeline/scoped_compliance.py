"""Compliance within CMRA's actual reach: the obligation-screened denominator.

CMRA's deposit obligation (per the Act + OMB M-23-17) has two prongs:
  - chamber-directed reports: covered regardless of statute age
  - committee-directed reports: covered ONLY if the mandating statute was
    enacted on or after Dec 23, 2022 (Pub. L. 117-263)

The House Doc table doesn't record destination (chamber vs committee), so
for legacy-statute mandates we cannot tell whether an obligation attaches.
But for mandates citing a POST-CMRA statute, the obligation attaches under
either prong. That slice is the fully-defensible compliance denominator:

  denominator = mandates where
    (a) newest cited statute is Pub. L. 117-263 or later   [obligation prong]
    (b) entity is CMRA-covered (not GAO/IC/President/House/Senate/AoC)
    (c) cadence puts a deposit due inside the window (2024 → today)

  numerator = those with >= 1 matched GPO filing (v1 + v2 pass1 + pass2)

Known un-screened carve-outs (all would REMOVE rows from the denominator,
so the computed rate is a LOWER BOUND on compliance within the slice):
  - reports to Senate/House Intelligence, Armed Services, Appropriations,
    Foreign Relations/Affairs are exempt (destination unknown from the Doc)
  - IG reports, 36 U.S.C. Part B, LE-sensitive, infosec, DC government

Outputs compare_output/scoped_compliance.json and a console summary.

Usage:
  uv run python scoped_compliance.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from cadence import cmra_exempt_reason, in_cmra_window
from match import ensure_extract, load_jsonl
from normalize import parse_citations

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

OUT_DIR = REPO_ROOT / "compare_output"
OUT_PATH = OUT_DIR / "scoped_compliance.json"

CMRA_CONGRESS, CMRA_NUMBER = 117, 263  # Pub. L. 117-263, Dec 23 2022


def newest_plaw(authority: str) -> tuple[int, int] | None:
    plaws = parse_citations(authority or "")["plaw"]
    if not plaws:
        return None
    return max((int(c), int(n)) for c, n in plaws)


def post_cmra(authority: str) -> bool | None:
    """True/False if determinable from PLAW cites; None when no PLAW cited."""
    newest = newest_plaw(authority)
    if newest is None:
        return None
    return newest >= (CMRA_CONGRESS, CMRA_NUMBER)


def covered_mandate_ids() -> set[str]:
    """Mandate IDs with >= 1 matched filing across v1 and both v2 passes."""
    ids: set[str] = set()
    for r in load_jsonl(OUT_DIR / "submission_coverage.jsonl"):
        if r["matched_mandate_id"]:
            ids.add(r["matched_mandate_id"])
    for name in ("v2_matches.jsonl", "v2_pass2.jsonl"):
        latest: dict[str, dict] = {}
        for line in (OUT_DIR / name).read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["package_id"]] = rec
        for rec in latest.values():
            if rec["verdict"] == "matched" and rec.get("mandate_id"):
                ids.add(rec["mandate_id"])
    return ids


def main() -> None:
    mandates = ensure_extract()
    covered = covered_mandate_ids()

    # v1-only numerator for the no-LLM floor
    v1_covered = {
        r["matched_mandate_id"]
        for r in load_jsonl(OUT_DIR / "submission_coverage.jsonl")
        if r["matched_mandate_id"]
    }

    tally = defaultdict(int)
    denom_rows, misses = [], []
    for m in mandates:
        is_new = post_cmra(m.get("authority", ""))
        if is_new is None:
            tally["excluded: no PLAW cite (age undeterminable)"] += 1
            continue
        if not is_new:
            tally["excluded: pre-CMRA statute (obligation depends on unknown destination)"] += 1
            continue
        if cmra_exempt_reason(m["reporting_entity"]):
            tally["excluded: exempt entity (GAO/IC/President/House-Senate-AoC)"] += 1
            continue
        in_scope, why = in_cmra_window(m.get("when_expected", ""))
        if not in_scope:
            tally[f"excluded: not due in window ({why})"] += 1
            continue
        tally["DENOMINATOR"] += 1
        row = {
            "mandate_id": m["mandate_id"],
            "reporting_entity": m["reporting_entity"],
            "nature_of_report": m["nature_of_report"],
            "authority": m["authority"],
            "when_expected": m["when_expected"],
            "cadence_reason": why,
            "covered": m["mandate_id"] in covered,
            "covered_v1_only": m["mandate_id"] in v1_covered,
        }
        denom_rows.append(row)
        if not row["covered"]:
            misses.append(row)

    n = len(denom_rows)
    n_cov = sum(1 for r in denom_rows if r["covered"])
    n_cov_v1 = sum(1 for r in denom_rows if r["covered_v1_only"])

    by_entity: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in denom_rows:
        by_entity[r["reporting_entity"]][0] += 1
        if r["covered"]:
            by_entity[r["reporting_entity"]][1] += 1

    # Upper bracket: entity + cadence screens only, all statute ages —
    # i.e. assume every mandate is chamber-directed and therefore obligated.
    # Empirical support: 1,056 of 1,057 actual deposits are chamber-received,
    # so the chamber prong is the pipe through which the repository fills.
    upper_denom = [
        m for m in mandates
        if not cmra_exempt_reason(m["reporting_entity"])
        and in_cmra_window(m.get("when_expected", ""))[0]
    ]
    upper_cov = sum(1 for m in upper_denom if m["mandate_id"] in covered)

    result = {
        "window": "2024-01-01 → today (deposits begin 2023-10-16)",
        "screens": dict(tally),
        "denominator": n,
        "covered_full_pipeline": n_cov,
        "covered_v1_only_floor": n_cov_v1,
        "compliance_full": round(100 * n_cov / n, 1) if n else None,
        "compliance_v1_floor": round(100 * n_cov_v1 / n, 1) if n else None,
        "upper_bracket": {
            "description": "all in-window covered-entity mandates, any statute age (assumes chamber-directed)",
            "denominator": len(upper_denom),
            "covered": upper_cov,
            "compliance": round(100 * upper_cov / len(upper_denom), 1) if upper_denom else None,
        },
        "caveats": [
            "lower bound: exempt-committee/IG/LE-sensitive rows not removable without destination data",
            "full-pipeline numerator includes v2 LLM matches pending adversarial verification",
            "post-CMRA screen uses newest cited Pub. L.; rows citing only USC/Stat excluded",
        ],
        "by_entity": {
            e: {"denominator": d, "covered": c}
            for e, (d, c) in sorted(by_entity.items(), key=lambda kv: -kv[1][0])
        },
        "misses": misses,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2))

    print(f"Obligation-screened denominator: {n}")
    for k, v in sorted(tally.items()):
        if k != "DENOMINATOR":
            print(f"  {v:5d}  {k}")
    print()
    print(f"Covered (v1 + v2):  {n_cov}/{n} = {result['compliance_full']}%  (lower bound on compliance)")
    print(f"Covered (v1 only):  {n_cov_v1}/{n} = {result['compliance_v1_floor']}%  (no-LLM floor)")
    print()
    print("Top entities in the obligated slice:")
    for e, (d, c) in sorted(by_entity.items(), key=lambda kv: -kv[1][0])[:12]:
        print(f"  {c:3d}/{d:<3d}  {e}")
    print(f"\nFull detail (incl. {len(misses)} misses) → {OUT_PATH}")


if __name__ == "__main__":
    main()
