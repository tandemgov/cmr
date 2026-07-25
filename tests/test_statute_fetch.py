"""Tests for USLM parsing and citation resolution.

Fast, no network. Runs against a small inline USLM fixture rather than the
814 MB release-point corpus, so it stays runnable on a clean checkout.
"""

import xml.etree.ElementTree as ET

import pytest

import statute_fetch as sf

USLM = """<?xml version="1.0" encoding="UTF-8"?>
<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0">
 <main>
  <section identifier="/us/usc/t2/s807">
   <num>§807.</num><heading>Audits</heading>
   <subsection identifier="/us/usc/t2/s807/a">
    <num>(a)</num><heading>Contracts with independent public accountant</heading>
    <content>The Board shall enter into a contract with an independent public accountant.</content>
   </subsection>
   <subsection identifier="/us/usc/t2/s807/b">
    <num>(b)</num><heading>Annual report to Congress on audit results</heading>
    <content>Not later than May 15 of each calendar year, the Board shall submit a report.</content>
   </subsection>
   <subsection identifier="/us/usc/t2/s807/c">
    <num>(c)</num><heading>Review by the Comptroller General</heading>
    <paragraph identifier="/us/usc/t2/s807/c/1">
     <num>(1)</num><content>The Comptroller General shall review each annual audit.</content>
    </paragraph>
    <paragraph identifier="/us/usc/t2/s807/c/3">
     <num>(3)</num>
     <content>Not later than 180 days after the date on which the Comptroller General
      receives a report under <ref href="/us/usc/t2/s807/b">subsection (b)</ref>, the
      Comptroller General shall submit to Congress a report.</content>
    </paragraph>
   </subsection>
   <notes>
    <note topic="amendments">Amendments 1996—Subsec. (b) amended by Pub. L. 104-316.</note>
    <note topic="editorialNotes" role="crossHeading">Editorial Notes</note>
    <note topic="referencesInText">References in Text The Act referred to in subsec. (f) is Pub. L. 91-190.</note>
    <note topic="statutoryNotes" role="crossHeading">Statutory Notes and Related Subsidiaries</note>
    <note topic="miscellaneous">Congressional Award Program The Board shall transmit to Congress a plan describing the program.</note>
   </notes>
  </section>
  <section identifier="/us/usc/t2/s808">
   <num>§808.</num><heading>Definitions</heading>
   <content>As used in this chapter, the term "Board" means the Congressional Award Board.</content>
  </section>
 </main>
</uscDoc>
"""


@pytest.fixture
def title(tmp_path):
    p = tmp_path / "usc02.xml"
    p.write_text(USLM)
    return p


@pytest.fixture
def root():
    return ET.fromstring(USLM)


def _section(root, ident):
    for s in root.iter(f"{sf._NS}section"):
        if s.get("identifier") == ident:
            return s
    raise AssertionError(f"no section {ident}")


class TestTextFlattening:
    def test_block_boundaries_get_a_separator(self, root):
        """Heading and content must not weld into 'audit resultsNot later'."""
        sub = _section(root, "/us/usc/t2/s807").find(f"{sf._NS}subsection[@identifier='/us/usc/t2/s807/b']")
        text = sf._text_of(sub)
        assert "audit results Not later than May 15" in text
        assert "resultsNot" not in text

    def test_inline_refs_do_not_gain_spurious_spaces(self, root):
        para = _section(root, "/us/usc/t2/s807").find(f".//{sf._NS}paragraph[@identifier='/us/usc/t2/s807/c/3']")
        text = sf._text_of(para)
        assert "receives a report under subsection (b), the" in text

    def test_notes_are_excluded_from_operative_text(self, root):
        text = sf._text_of(_section(root, "/us/usc/t2/s807"))
        assert "Board shall enter into a contract" in text
        assert "Pub. L. 104-316" not in text
        assert "Amendments" not in text

    def test_notes_can_be_included_explicitly(self, root):
        text = sf._text_of(_section(root, "/us/usc/t2/s807"), include_notes=True)
        assert "Pub. L. 104-316" in text

    def test_whitespace_is_collapsed(self, root):
        text = sf._text_of(_section(root, "/us/usc/t2/s807"))
        assert "  " not in text
        assert "\n" not in text


class TestStatutoryNotes:
    def test_drafting_history_is_dropped_but_statutory_notes_kept(self, root):
        notes = sf._statutory_notes(_section(root, "/us/usc/t2/s807"))
        topics = {n["topic"] for n in notes}
        assert "miscellaneous" in topics
        assert "amendments" not in topics
        assert "editorialNotes" not in topics

    def test_apparatus_topics_are_excluded(self, root):
        """Without this, a `... note` cite returns 'References in Text ...'."""
        notes = sf._statutory_notes(_section(root, "/us/usc/t2/s807"))
        assert "referencesInText" not in {n["topic"] for n in notes}

    def test_crossheading_dividers_are_excluded(self, root):
        """'Statutory Notes and Related Subsidiaries' is a divider, not law."""
        notes = sf._statutory_notes(_section(root, "/us/usc/t2/s807"))
        assert all(n["role"] != "crossHeading" for n in notes)
        assert not any("Related Subsidiaries" in n["text"] for n in notes)

    def test_unrecognized_topics_are_kept(self):
        """Deny-list semantics: an unknown topic must not silently drop law."""
        xml = (
            '<section xmlns="http://xml.house.gov/schemas/uslm/1.0" identifier="/us/usc/t1/s1">'
            '<notes><note topic="somethingNew">The Secretary shall submit to Congress an annual report on the program.</note></notes>'
            "</section>"
        )
        notes = sf._statutory_notes(ET.fromstring(xml))
        assert len(notes) == 1 and notes[0]["topic"] == "somethingNew"

    def test_note_text_is_preserved(self, root):
        notes = sf._statutory_notes(_section(root, "/us/usc/t2/s807"))
        assert any("shall transmit to Congress a plan" in n["text"] for n in notes)

    def test_section_without_notes_returns_empty(self, root):
        assert sf._statutory_notes(_section(root, "/us/usc/t2/s808")) == []


class TestTitleIndex:
    def test_every_addressable_node_is_indexed(self, title):
        exact, _ = sf.build_title_index(title)
        for ident in (
            "/us/usc/t2/s807", "/us/usc/t2/s807/a", "/us/usc/t2/s807/b",
            "/us/usc/t2/s807/c", "/us/usc/t2/s807/c/1", "/us/usc/t2/s807/c/3",
            "/us/usc/t2/s808",
        ):
            assert ident in exact, ident

    def test_entries_carry_section_context(self, title):
        exact, _ = sf.build_title_index(title)
        e = exact["/us/usc/t2/s807/c/3"]
        assert e["section_id"] == "/us/usc/t2/s807"
        assert e["section_heading"] == "Audits"
        assert "180 days" in e["text"]

    def test_statutory_notes_attach_to_the_section_not_its_subsections(self, title):
        exact, _ = sf.build_title_index(title)
        assert exact["/us/usc/t2/s807"]["statutory_notes"]
        assert exact["/us/usc/t2/s807/b"]["statutory_notes"] == []

    def test_folded_index_is_case_insensitive(self, title):
        _, folded = sf.build_title_index(title)
        assert "/us/usc/t2/s807/c/3" in folded


class TestResolveMandates:
    @pytest.fixture(autouse=True)
    def _xml_dir(self, tmp_path, monkeypatch):
        (tmp_path / "usc02.xml").write_text(USLM)
        monkeypatch.setattr(sf, "XML_DIR", tmp_path)

    def _resolve(self, authority):
        rows = sf.resolve_mandates([{
            "mandate_id": "M00001", "reporting_entity": "GAO",
            "nature_of_report": "n", "when_expected": "w", "authority": authority,
        }])
        return rows[0]["provisions"][0]

    def test_exact_subsection_resolves_to_its_own_text(self):
        p = self._resolve("2 U.S.C. 807(b); Pub. L. 96-114")
        assert p["resolved_at"] == "exact"
        assert "Not later than May 15" in p["text"]

    def test_deep_path_resolves(self):
        p = self._resolve("2 U.S.C. 807(c)(3)")
        assert p["resolved_at"] == "exact"
        assert "180 days" in p["text"]

    def test_note_citation_returns_the_note_not_the_section_body(self):
        """The bug this guards: `807 note` returning the text of § 807 itself."""
        p = self._resolve("2 U.S.C. 807 note; Pub. L. 116-260")
        assert p["resolved_at"] == "note"
        assert "shall transmit to Congress a plan" in p["text"]
        assert "shall enter into a contract" not in p["text"]

    def test_missing_subsection_falls_back_to_the_section(self):
        p = self._resolve("2 U.S.C. 807(z)")
        assert p["resolved_at"] == "section"
        assert p["resolved"] is True

    def test_missing_section_is_reported_unresolved(self):
        p = self._resolve("2 U.S.C. 9999(a)")
        assert p["resolved_at"] == "none"
        assert p["resolved"] is False

    def test_note_on_a_section_without_notes_is_flagged(self):
        p = self._resolve("2 U.S.C. 808 note")
        assert p["resolved_at"] == "note_absent"
        assert p["resolved"] is False

    def test_uncodified_authority_yields_no_provisions(self):
        rows = sf.resolve_mandates([{
            "mandate_id": "M00002", "reporting_entity": "X", "nature_of_report": "",
            "when_expected": "", "authority": "Pub. L. 117-328; (136 Stat. 4900)",
        }])
        assert rows[0]["provisions"] == []
        assert rows[0]["is_codified"] is False
        assert rows[0]["plaw"] == ["Pub. L. 117-328"]


class TestTitleFileMapping:
    @pytest.fixture(autouse=True)
    def _xml_dir(self, tmp_path, monkeypatch):
        for name in ("usc02.xml", "usc05A.xml", "usc11a.xml", "usc50.xml"):
            (tmp_path / name).write_text("<x/>")
        monkeypatch.setattr(sf, "XML_DIR", tmp_path)

    def test_zero_pads_the_title_number(self):
        assert sf._title_file("2").name == "usc02.xml"

    def test_appendix_suffix_case_varies_upstream(self):
        assert sf._title_file("5", appendix=True).name == "usc05A.xml"
        assert sf._title_file("11", appendix=True).name == "usc11a.xml"

    def test_appendix_and_plain_title_are_different_files(self):
        assert sf._title_file("50").name == "usc50.xml"
        assert sf._title_file("50", appendix=True) is None

    def test_missing_title_returns_none(self):
        assert sf._title_file("99") is None
