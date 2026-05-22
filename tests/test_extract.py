"""Tests for PDF table extraction against known-good values from test_doc.pdf."""

import re
from pathlib import Path

import pytest

from extract import extract

TEST_PDF = Path(__file__).parent / "test_doc.pdf"


@pytest.fixture(scope="module")
def rows():
    """Extract rows once for all tests in this module."""
    return extract(TEST_PDF)


def test_row_count(rows):
    assert len(rows) == 46


def test_entity_count(rows):
    entities = set(r.reporting_entity for r in rows)
    assert len(entities) == 7


def test_entities_present(rows):
    entities = set(r.reporting_entity for r in rows)
    expected = {
        "Architect of the Capitol",
        "British-American Interparliamentary Group",
        "Canada-United States Interparliamentary Group",
        "Commission on the National Defense Strategy",
        "Commission on the State of U.S. Olympics and Paralympics",
        "Congressional Budget Office",
        "Government Accountability Office",
    }
    assert entities == expected


def test_row_1_vacancy(rows):
    r = rows[0]
    assert r.reporting_entity == "Architect of the Capitol"
    assert r.nature_of_report == "Vacancy of the Deputy Architect position"
    assert r.authority == (
        "2 U.S.C. 1805(e); Pub. L. 108-7, Sec. 1203 "
        "(as amended by Pub. L. 118-31, Sec. 5703); (137 Stat. 961)"
    )
    assert r.when_expected == "Immediately"


def test_row_2_expenditures(rows):
    r = rows[1]
    assert r.nature_of_report == "All expenditures made from monies appropriated"
    assert "128 Stat. 428" in r.authority
    assert r.when_expected == "Not later than 60 days after last day of each semiannual period"


def test_row_7_olympics(rows):
    r = rows[6]
    assert r.reporting_entity == "Commission on the State of U.S. Olympics and Paralympics"
    assert "Olympic and Paralympic Games" in r.nature_of_report
    assert r.when_expected == "Not later than July 27, 2021"


def test_row_12_audit_compliance(rows):
    r = rows[11]
    assert r.reporting_entity == "Government Accountability Office"
    assert r.nature_of_report == "Results of the audit of compliance"
    assert r.authority.startswith("1 U.S.C. 112b(h)(3)")
    assert "136 Stat. 3480" in r.authority


def test_row_17_congressional_award(rows):
    r = rows[16]
    assert r.nature_of_report == "Review of Congressional Award Foundation audit"
    assert "2 U.S.C. 807(b)" in r.authority
    assert r.when_expected == "Not later than 180 days after the date on which the audit is received"


def test_row_18_financial_records(rows):
    """Tests multi-line merge: 'Board and the Congressional Award Foundation'."""
    r = rows[17]
    assert "financial records of the Board and the Congressional Award Foundation" in r.nature_of_report
    assert "May 15 of each calendar year" in r.when_expected


def test_row_23_president_vice_president(rows):
    """Tests multi-line merge: 'President and Vice President'."""
    r = rows[22]
    assert "President and Vice President" in r.nature_of_report
    assert "3 U.S.C. 105(d)" in r.authority


def test_row_29_audits_money(rows):
    """The previously-garbled row — validates dedup fix."""
    r = rows[28]
    assert r.nature_of_report == "Audits of money and property"
    assert r.authority.startswith("10 U.S.C. 2350g(c)")
    assert "107 Stat. 1749" in r.authority


def test_row_37_consumer_cooperative_bank(rows):
    """Tests multi-line merge: 'National Consumer Cooperative Bank'."""
    r = rows[36]
    assert "National Consumer Cooperative Bank" in r.nature_of_report
    assert "12 U.S.C. 3025" in r.authority


def test_row_46_concrete_masonry(rows):
    r = rows[45]
    assert r.nature_of_report == "Concrete Masonry Products Board assessments"
    assert r.authority == "15 U.S.C. 8716; Pub. L. 115-254, Sec. 1317; (132 Stat. 3484)"
    assert r.when_expected == "Not later than October 5, 2026"


def test_citation_integrity(rows):
    """Every authority field must contain at least one citation marker."""
    citation_pattern = re.compile(r"U\.S\.C\.|Pub\.\s*L\.|Stat\.")
    for i, r in enumerate(rows):
        assert citation_pattern.search(r.authority), (
            f"Row {i+1} authority lacks citation: {r.authority!r}"
        )


def test_no_dot_artifacts(rows):
    """No field should contain residual dot-leader artifacts."""
    for i, r in enumerate(rows):
        for field in ["nature_of_report", "authority", "when_expected"]:
            value = getattr(r, field)
            assert ".." not in value, (
                f"Row {i+1} {field} has dot artifact: {value!r}"
            )


def test_no_empty_nature(rows):
    """Every row must have a non-empty nature_of_report."""
    for i, r in enumerate(rows):
        assert r.nature_of_report.strip(), f"Row {i+1} has empty nature_of_report"


def test_no_empty_authority(rows):
    """Every row must have a non-empty authority."""
    for i, r in enumerate(rows):
        assert r.authority.strip(), f"Row {i+1} has empty authority"


def test_authority_complete(rows):
    """Every authority must end with a closing paren — citations always close
    with ')' after the Stat. reference. A non-')' ending indicates a row was
    split mid-citation by faulty row-merging logic.
    """
    for i, r in enumerate(rows):
        assert r.authority.rstrip().endswith(")"), (
            f"Row {i+1} authority is mid-citation (missing closing paren): "
            f"...{r.authority[-60:]!r}"
        )


# ---------------------------------------------------------------------------
# Regression tests against the full source PDF
# ---------------------------------------------------------------------------

FULL_PDF = Path(__file__).parent.parent / "data" / "CDOC-119hdoc4.pdf"


@pytest.fixture(scope="module")
def full_rows():
    """Extract from the full source PDF (slow — module-scoped)."""
    if not FULL_PDF.exists():
        pytest.skip(f"Full PDF not present at {FULL_PDF}")
    return extract(FULL_PDF)


def test_full_no_mid_citation_authorities(full_rows):
    """REGRESSION: 7 rows in the full PDF used to be split mid-citation
    by _is_new_logical_row treating a single-word continuation (e.g.
    'Development') as a new row. After the fix, every authority must
    end with ')'.
    """
    offenders = [
        (i + 1, r.authority)
        for i, r in enumerate(full_rows)
        if r.authority.rstrip() and not r.authority.rstrip().endswith(")")
    ]
    assert not offenders, (
        f"{len(offenders)} rows have mid-citation authorities. Examples: "
        + "; ".join(f"row {i}: ...{a[-50:]!r}" for i, a in offenders[:3])
    )


def test_full_hud_three_line_wrap(full_rows):
    """REGRESSION: the HUD operations row spans 3 wrapped physical lines and
    used to be split — the final word 'Development' became its own row with
    a fragmented authority. Verify it's now a single complete row.
    """
    candidates = [
        r for r in full_rows
        if "Housing and Urban Development" in r.nature_of_report
        and r.nature_of_report.startswith("All operations and programs")
    ]
    assert len(candidates) == 1, (
        f"Expected exactly one HUD operations row, found {len(candidates)}"
    )
    r = candidates[0]
    assert r.nature_of_report == (
        "All operations and programs during the previous calendar year "
        "under the jurisdiction of the Department of Housing and Urban "
        "Development"
    )
    assert "12 U.S.C. 1701o" in r.authority
    assert "(101 Stat. 1950)" in r.authority
    assert r.when_expected == "As soon as practicable during each calendar year"


def test_full_no_single_word_orphan_rows(full_rows):
    """REGRESSION: no row should have a nature consisting of a single word
    AND an empty when_expected — that's the signature of a wrap-orphan
    that should have been merged into the previous row.
    """
    orphans = [
        (i + 1, r) for i, r in enumerate(full_rows)
        if len(r.nature_of_report.split()) == 1 and not r.when_expected.strip()
    ]
    assert not orphans, (
        f"{len(orphans)} suspected orphan rows. Examples: "
        + "; ".join(f"row {i}: {r.nature_of_report!r}" for i, r in orphans[:3])
    )
