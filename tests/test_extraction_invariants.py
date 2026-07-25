"""Whole-document invariants for the House Doc extractor.

These are deliberately high level: they assert properties of the full
extraction rather than the behavior of individual helpers. Each one exists
because the corresponding failure actually happened and went unnoticed for a
month — see the notes on each test.

They run against the real 476-page source PDF and take a little over two
minutes in total — the parity test needs both extraction paths, so the
document is walked more than once. Skip them with `-m "not slow"`.
"""

import sys
from pathlib import Path

import pdfplumber
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PDF = REPO_ROOT / "data" / "CDOC-119hdoc4.pdf"

sys.path.insert(0, str(REPO_ROOT / "extraction"))

from extract import _find_data_pages, extract  # noqa: E402
from verify import extract_with_page_tracking  # noqa: E402

pytestmark = pytest.mark.slow

# The extractor's current output. This is a golden number: it should change
# only when a change to the extractor is understood and intended. It moved
# 3250 -> 3297 in 2026-06 when six upright (non-rotated) pages that had been
# silently dropped were recovered, which restored the entire NRC.
EXPECTED_ROW_COUNT = 3297

# Page classification, the step where the NRC rows were actually lost. The
# original classifier required a page to be majority-rotated, so these six
# pages — real table data, merely typeset upright — were never considered data
# pages at all and no later stage had a chance to extract them.
EXPECTED_DATA_PAGE_COUNT = 436
EXPECTED_ROTATED_PAGE_COUNT = 430
UPRIGHT_DATA_PAGES = [20, 183, 184, 186, 420, 421]


@pytest.fixture(scope="session")
def rows():
    """Extract the full source document once for the whole session."""
    return extract(SOURCE_PDF)


@pytest.fixture(scope="session")
def tracked_rows():
    """Same document via the page-tracking path used by the verify harness."""
    return extract_with_page_tracking(SOURCE_PDF)


@pytest.fixture(scope="session")
def data_pages():
    """1-based page numbers the extractor considers table data."""
    with pdfplumber.open(SOURCE_PDF) as pdf:
        return [(num, rotated) for num, _page, rotated in _find_data_pages(pdf.pages)]


def test_row_count_matches_baseline(rows):
    """A silent change in row count means mandates appeared or vanished.

    Update EXPECTED_ROW_COUNT deliberately, in the same commit as the
    extractor change that moves it, and say why in the message.
    """
    assert len(rows) == EXPECTED_ROW_COUNT


def test_page_tracking_agrees_with_extract(rows, tracked_rows):
    """verify.py must stay in step with extract.py.

    extract_with_page_tracking() duplicates extract()'s per-page branching.
    When extract() gained the upright-page path, this copy did not, so it
    raised on every document and took verify_report.py and judge.py down with
    it. Parity here is what catches that drift.
    """
    assert len(tracked_rows) == len(rows)


def test_upright_data_pages_are_classified_as_data(data_pages):
    """The six upright pages whose omission hid the NRC for a month.

    This is the root-cause guard. The bug was in classification, not
    extraction: `_find_data_pages` required a page to be majority-rotated, so
    these six never entered the pipeline. No later stage could have recovered
    them, and no invariant about extracted pages would have noticed — the
    pages simply were not in the set being reasoned about.
    """
    upright = sorted(num for num, rotated in data_pages if not rotated)
    assert upright == UPRIGHT_DATA_PAGES, (
        "upright page classification changed; this is how the NRC rows were lost"
    )


def test_data_page_classification_baseline(data_pages):
    """Guards the classifier's total reach, in both directions.

    A drop means pages stopped being recognized (the NRC failure). A jump
    means front matter or index pages started being treated as table data,
    which produces junk rows rather than missing ones.
    """
    rotated = sum(1 for _num, r in data_pages if r)
    assert (len(data_pages), rotated) == (
        EXPECTED_DATA_PAGE_COUNT,
        EXPECTED_ROTATED_PAGE_COUNT,
    )


def test_no_data_page_is_silently_dropped(data_pages, tracked_rows):
    """Every page classified as table data must contribute at least one row.

    This guards the extraction step rather than classification: a page that is
    recognized as data but yields nothing. Both `extract()` and
    `extract_with_page_tracking()` have a `continue` on undetected layout that
    would do exactly that without raising.
    """
    produced = {p for row in tracked_rows for p in row["pages"]}
    classified = {num for num, _rotated in data_pages}
    missing = sorted(classified - produced)
    assert not missing, (
        f"{len(missing)} page(s) classified as data produced no rows: {missing[:20]}"
    )


def test_sentinel_entities_are_present(rows):
    """Entities that vanished wholesale when upright pages were dropped.

    An entity missing from the extract is indistinguishable, downstream, from
    an entity with no reporting mandates — its GPO filings simply become
    unmatchable orphans. The NRC spent a month in that state.
    """
    entities = [r.reporting_entity for r in rows]
    for name, minimum in (("Nuclear Regulatory Commission", 14),
                          ("Department of Energy", 1),
                          ("Government Accountability Office", 1)):
        found = sum(1 for e in entities if name.lower() in e.lower())
        assert found >= minimum, f"expected >= {minimum} rows for {name}, found {found}"


def test_required_fields_are_populated(rows):
    """Entity and nature are what every downstream join keys on."""
    for field in ("reporting_entity", "nature_of_report"):
        blank = [i for i, r in enumerate(rows) if not getattr(r, field).strip()]
        assert not blank, f"{len(blank)} row(s) have an empty {field}: {blank[:10]}"
