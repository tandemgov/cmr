"""Tests for public-law section extraction.

Fast, no network. Uses inline fixtures in both shapes govinfo serves: GPO's
USLM namespace (113th Congress onward) and the enrolled-bill HTML rendering
(everything back to the 104th).
"""

import pytest

import plaw_fetch as pf
from authority_parse import PlawSectionRef, parse_plaw_sections

# GPO's PLAW namespace differs from OLRC's US Code namespace — that mismatch
# silently returned zero sections until extraction was made namespace-agnostic.
USLM = """<?xml version="1.0" encoding="UTF-8"?>
<pLaw xmlns="http://schemas.gpo.gov/xml/uslm">
 <main>
  <section><num value="1">SECTION 1.</num><heading>SHORT TITLE.</heading>
   <content>This Act may be cited as the "Example Act".</content></section>
  <section><num value="1095">SEC. 1095.</num><heading>COMMISSION ON THE NATIONAL DEFENSE STRATEGY.</heading>
   <subsection><num>(g)</num><content>The Commission shall submit to Congress a report.</content></subsection>
  </section>
 </main>
</pLaw>
"""

HTM = """<html><body><pre>
TABLE OF CONTENTS
Sec. 301. Off-base transition training for veterans.
Sec. 302. Something else entirely.

TITLE III--MATTERS

SEC. 301. &lt;&lt;NOTE: 10 USC 1144 note.&gt;&gt; OFF-BASE TRANSITION TRAINING.
    (a) &lt;&lt;NOTE: Time period.&gt;&gt; Provision.--During the two-year period,
the Secretary of Labor shall provide training and shall submit to Congress
a report on the results.

SEC. 302. SOMETHING ELSE ENTIRELY.
    Nothing to see here.
</pre></body></html>
"""


@pytest.fixture
def uslm(tmp_path):
    p = tmp_path / "PLAW-117publ81.xml"
    p.write_text(USLM)
    return p


@pytest.fixture
def htm(tmp_path):
    p = tmp_path / "PLAW-112publ260.htm"
    p.write_text(HTM)
    return p


class TestPlawSectionParsing:
    def test_section_binds_to_the_nearest_preceding_public_law(self):
        """`Sec. 301` belongs to the amending law, not the base law."""
        refs = parse_plaw_sections(
            "Pub. L. 101-510, Sec. 502(a)(1) (as added by Pub. L. 112-260, Sec. 301(f)); (126 Stat. 2424)"
        )
        assert refs == [
            PlawSectionRef(101, 510, "502"),
            PlawSectionRef(112, 260, "301"),
        ]

    def test_single_law(self):
        assert parse_plaw_sections("Pub. L. 117-81, Sec. 1095(g)(1)") == [
            PlawSectionRef(117, 81, "1095")
        ]

    def test_law_without_a_section_yields_none_section(self):
        assert parse_plaw_sections("Pub. L. 94-59, title III; (89 Stat. 284)") == [
            PlawSectionRef(94, 59, None)
        ]

    def test_package_id_and_collection_boundary(self):
        assert PlawSectionRef(117, 81, "1").package_id == "PLAW-117publ81"
        # govinfo PLAW starts at the 104th; earlier laws 404.
        assert PlawSectionRef(104, 1, "1").in_plaw_collection is True
        assert PlawSectionRef(103, 1, "1").in_plaw_collection is False

    def test_empty_is_safe(self):
        assert parse_plaw_sections("") == []


class TestUslmExtraction:
    def test_finds_section_by_num_value(self, uslm):
        text = pf.extract_section_uslm(uslm, "1095")
        assert "COMMISSION ON THE NATIONAL DEFENSE STRATEGY" in text
        assert "shall submit to Congress a report" in text

    def test_does_not_bleed_into_other_sections(self, uslm):
        assert "Example Act" not in pf.extract_section_uslm(uslm, "1095")

    def test_section_one_printed_as_SECTION(self, uslm):
        assert "Example Act" in pf.extract_section_uslm(uslm, "1")

    def test_missing_section_returns_none(self, uslm):
        assert pf.extract_section_uslm(uslm, "9999") is None


class TestHtmExtraction:
    def test_prefers_the_body_over_the_table_of_contents(self, htm):
        """The TOC line matches the same header regex and comes first."""
        text = pf.extract_section_text(htm, "301")
        assert "Secretary of Labor shall provide training" in text
        assert len(text) > 100

    def test_stops_at_the_next_section(self, htm):
        assert "Nothing to see here" not in pf.extract_section_text(htm, "301")

    def test_strips_enrolled_bill_sidenotes(self, htm):
        text = pf.extract_section_text(htm, "301")
        assert "NOTE:" not in text
        assert "10 USC 1144 note" not in text

    def test_unescapes_html_entities(self, htm):
        assert "&lt;" not in pf.extract_section_text(htm, "301")

    def test_missing_section_returns_none(self, htm):
        assert pf.extract_section_text(htm, "999") is None


class TestSummarize:
    def test_counts_resolved_and_reasons(self):
        rows = [
            {"plaw_provisions": [{"resolved": True, "resolved_at": "section_uslm"}]},
            {"plaw_provisions": [{"resolved": False, "resolved_at": "out_of_collection"}]},
            {"plaw_provisions": [{"resolved": False, "resolved_at": "out_of_collection"}]},
            {"plaw_provisions": [{"resolved": False, "resolved_at": "section_unspecified"}]},
        ]
        s = pf.summarize(rows)
        assert s["gap_mandates"] == 4
        assert s["resolved_to_public_law_text"] == 1
        assert s["unresolved"] == 3
        assert s["  unresolved: out_of_collection"] == 2
