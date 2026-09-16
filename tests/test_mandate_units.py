"""Tests for collapsing swept provisions into mandates: each merge rule must fire, and must hold back."""

import pytest

import mandate_units as mu

CHAPEAU = "§ 9815. Reporting requirement The Administrator shall submit to the appropriate committees of Congress each year a report that provides the following:"


def row(uslm_id, entity="the Secretary", freq="annual", text=""):
    v = {"reporting_entity": entity, "frequency": freq, "deadline": ""}
    return mu.Row(uslm_id, mu.entity_key(entity), mu.cadence(freq), v, text)


class TestCadence:
    @pytest.mark.parametrize("label,expected", [
        ("annual", "annual"),
        ("semi-annual", "semiannual"),
        ("every 6 months thereafter", "semiannual"),
        ("biennially", "biennial"),
        ("every five years thereafter", "multi-year"),
        ("quadrennial", "multi-year"),
        ("event-driven", "event-driven"),
        ("one-time", "one-time"),
        ("", "unknown"),
        ("unknown", "unknown"),
    ])
    def test_folds_labels(self, label, expected):
        assert mu.cadence(label) == expected

    def test_a_first_deadline_does_not_make_a_recurring_duty_one_time(self):
        """"one-time and annual" is an annual duty with a first deadline."""
        assert mu.cadence("one-time and annual") == "annual"

    def test_a_cadence_beats_event_language(self):
        assert mu.cadence("event-driven, annual") == "annual"


class TestPaths:
    def test_section_and_parent(self):
        assert mu.section_of("/us/usc/t42/s300gg–111/a/2") == "/us/usc/t42/s300gg–111"
        assert mu.parent_of("/us/usc/t42/s300gg–111/a/2") == "/us/usc/t42/s300gg–111/a"
        assert mu.parent_of("/us/usc/t42/s300gg–111") is None

    def test_notes_do_not_nest(self):
        """A note's synthetic id is not a USLM path; its 'parent' is not an ancestor."""
        assert mu.parent_of("/us/usc/t10/s494/note/1") is None

    def test_appendix_titles_resolve_to_a_section(self):
        """Title 18 appendix ids carry a public-law path before the section."""
        assert mu.section_of("/us/usc/t18a/pl/96/456/s13/b") == "/us/usc/t18a/pl/96/456/s13"


class TestCitation:
    @pytest.mark.parametrize("uslm_id,expected", [
        ("/us/usc/t42/s300gg–111/a/2", "42 U.S.C. 300gg-111(a)(2)"),
        ("/us/usc/t2/s2a", "2 U.S.C. 2a"),
        ("/us/usc/t10/s494/note/1", "10 U.S.C. 494 note"),
        ("/us/usc/t18a/pl/96/456/s13/b", "Pub. L. 96-456, § 13(b) (18 U.S.C. App.)"),
    ])
    def test_formats(self, uslm_id, expected):
        assert mu.citation(uslm_id) == expected


class TestNesting:
    def test_a_subsection_echoing_its_section_folds_in(self):
        units = mu.build_units([row("/us/usc/t21/s360g–2"), row("/us/usc/t21/s360g–2/b")])
        assert len(units) == 1
        assert units[0].root.uslm_id == "/us/usc/t21/s360g–2"

    def test_a_nested_duty_by_a_different_entity_stands_alone(self):
        """6 U.S.C. 223(g): the Secretary's metrics report and GAO's review of it."""
        units = mu.build_units([
            row("/us/usc/t6/s223/g", entity="the Secretary"),
            row("/us/usc/t6/s223/g/2", entity="Comptroller General"),
        ])
        assert len(units) == 2

    def test_entity_matching_tolerates_a_short_form(self):
        units = mu.build_units([row("/us/usc/t1/s1", entity="the Secretary of State"),
                                row("/us/usc/t1/s1/a", entity="Secretary")])
        assert len(units) == 1

    def test_folding_follows_a_chain_of_echoes(self):
        units = mu.build_units([row("/us/usc/t1/s1"), row("/us/usc/t1/s1/a"), row("/us/usc/t1/s1/a/1")])
        assert len(units) == 1 and len(units[0].members) == 3


class TestSiblings:
    def test_a_contents_list_under_a_shared_chapeau_is_one_duty(self):
        rows = [row(f"/us/usc/t5/s9815/{i}", entity="the Administrator",
                    text=f"{CHAPEAU} ({i}) item {i}") for i in (1, 2, 3)]
        units = mu.build_units(rows)
        assert len(units) == 1 and len(units[0].members) == 3

    def test_sibling_reports_sharing_only_a_heading_stay_separate(self):
        """15 U.S.C. 3721(m) and (n) are two reports; the shared text is a heading."""
        head = "§ 3721. Federal loan guarantees for innovative technologies in manufacturing"
        units = mu.build_units([
            row("/us/usc/t15/s3721/m", text=f"{head} (m) Audit The Secretary shall arrange annual audits"),
            row("/us/usc/t15/s3721/n", text=f"{head} (n) Report to Congress The Secretary shall transmit"),
        ])
        assert len(units) == 2

    def test_top_level_sections_are_never_siblings(self):
        """Sections have no parent; unrelated sections must not merge on entity alone."""
        units = mu.build_units([row("/us/usc/t44/s2119", entity="Archivist", text=CHAPEAU),
                                row("/us/usc/t44/s3303a", entity="Archivist", text=CHAPEAU)])
        assert len(units) == 2

    def test_siblings_with_different_cadence_stay_separate(self):
        units = mu.build_units([
            row("/us/usc/t5/s9815/1", text=f"{CHAPEAU} (1) a", freq="annual"),
            row("/us/usc/t5/s9815/2", text=f"{CHAPEAU} (2) b", freq="quarterly"),
        ])
        assert len(units) == 2


class TestLoadRows:
    def _write(self, tmp_path, live, cands):
        import json
        lp, cp = tmp_path / "live.jsonl", tmp_path / "c.jsonl"
        lp.write_text("\n".join(json.dumps(r) for r in live))
        cp.write_text("\n".join(json.dumps(r) for r in cands))
        return lp, cp

    def test_drops_non_current_and_non_recurring(self, tmp_path):
        v = lambda f: {"reporting_entity": "x", "frequency": f, "deadline": ""}
        live = [
            {"uslm_id": "/a", "currency": "current", "verdict": v("annual")},
            {"uslm_id": "/b", "currency": "expired", "verdict": v("annual")},
            {"uslm_id": "/c", "currency": "current", "verdict": v("one-time")},
            {"uslm_id": "/d", "currency": "current", "verdict": v("event-driven")},
        ]
        lp, cp = self._write(tmp_path, live, [{"uslm_id": "/a", "text": "t"}])
        rows, dropped = mu.load_rows(lp, cp)
        assert [r.uslm_id for r in rows] == ["/a", "/d"]
        assert dropped == {"currency:expired": 1, "cadence:one-time": 1}
