"""Tests for the destination gate — the classifier that decides which pre-CMRA mandates carry an obligation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import destination as d  # noqa: E402


class TestRecipientAnchoring:
    def test_a_consulted_committee_is_not_the_recipient(self):
        """The error that made the first version unusable: 16% precision on the committee tier."""
        text = ("the Director, after consultation with the appropriate committees of the "
                "House of Representatives and Senate, shall submit to the Congress a report")
        assert d.classify_destination(text) == d.CONGRESS

    def test_a_direct_object_verb_still_finds_the_recipient(self):
        assert d.classify_destination("CBO shall notify the Congress if the deficit grows") == d.CONGRESS

    def test_no_delivery_language_is_unknown(self):
        assert d.classify_destination("The Secretary shall maintain a registry of such vessels.") == d.UNKNOWN

    def test_empty_text_is_unknown(self):
        assert d.classify_destination("") == d.UNKNOWN
        assert d.classify_destination(None) == d.UNKNOWN


class TestTiers:
    @pytest.mark.parametrize("text", [
        "shall submit to the Speaker of the House of Representatives a report",
        "shall transmit to both Houses of Congress a statement",
        "shall report to the President pro tempore of the Senate",
        "shall submit to the majority leader and the minority leader a summary",
    ])
    def test_chamber_officers(self, text):
        assert d.classify_destination(text) == d.CHAMBER

    def test_leadership_carries_the_chamber_prong(self):
        """'the appropriate congressional committees and leadership' was the single largest error source."""
        text = "shall submit to the appropriate congressional committees and leadership a report"
        assert d.classify_destination(text) == d.CHAMBER

    def test_committee_only_stays_committee(self):
        text = "shall submit to the Committee on Armed Services of the Senate a report"
        assert d.classify_destination(text) == d.COMMITTEE

    def test_congress_named_first_outranks_a_trailing_committee_list(self):
        """'to Congress, including the intelligence committees' is owed to Congress; the committees are elaboration."""
        text = ("shall submit to Congress, including the congressional intelligence committees "
                "and the Committee on Armed Services, a report")
        assert d.classify_destination(text) == d.CONGRESS

    def test_a_chamber_officer_outranks_a_committee(self):
        text = "shall submit to the Speaker of the House and the Committee on the Budget a report"
        assert d.classify_destination(text) == d.CHAMBER


class TestCorpus:
    @pytest.fixture(scope="class")
    def tiers(self):
        if not d.USC_PROVISIONS_PATH.exists():
            pytest.skip("resolved provisions not present")
        return d.final_destinations()

    def test_every_mandate_with_text_gets_exactly_one_tier(self, tiers):
        assert set(tiers.values()) <= {d.CHAMBER, d.COMMITTEE, d.CONGRESS, d.UNKNOWN}

    def test_congress_is_the_dominant_form(self, tiers):
        """If this inverts, the reading the compliance bracket turns on has changed and the bracket needs revisiting."""
        counts = {t: sum(1 for v in tiers.values() if v == t) for t in set(tiers.values())}
        assert counts[d.CONGRESS] > sum(v for k, v in counts.items() if k != d.CONGRESS)
