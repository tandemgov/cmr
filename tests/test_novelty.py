"""Tests for deciding whether a discovered provision is already on the Clerk's list.

Fast, no network. These matter because this module produces the headline number
— how many reporting mandates the House Document is missing — and the whole
point of it is that matching on USC citations alone overstates that figure.
"""

import json

import pytest

import novelty as nv


class TestCreditKeys:
    """Source credits are the join key for uncodified House Doc rows."""

    def test_extracts_public_law_and_section(self):
        plaw, stat = nv.credit_keys("(Pub. L. 89-670, § 9(c), Oct. 15, 1966, 80 Stat. 944.)")
        assert plaw == {(89, 670, "9")}
        assert stat == {(80, 944)}

    def test_folds_the_en_dash_uslm_prints(self):
        """USLM writes 'Pub. L. 89–670'; the House Doc writes a hyphen."""
        plaw, _ = nv.credit_keys("(Pub. L. 89–670, § 9(c), Oct. 15, 1966, 80 Stat. 944.)")
        assert plaw == {(89, 670, "9")}

    def test_handles_several_public_laws_in_one_credit(self):
        plaw, stat = nv.credit_keys(
            "(Pub. L. 91-375, § 6(o), Aug. 12, 1970, 84 Stat. 782; "
            "Pub. L. 104-208, § 101, Sept. 30, 1996, 110 Stat. 3009.)"
        )
        assert plaw == {(91, 375, "6"), (104, 208, "101")}
        assert stat == {(84, 782), (110, 3009)}

    def test_pre_1926_chapter_credits_yield_only_a_stat_key(self):
        """Older sections predate public-law numbering entirely."""
        plaw, stat = nv.credit_keys("(July 9, 1918, ch. 143, ch. XV, § 2, 40 Stat. 886.)")
        assert plaw == set()
        assert stat == {(40, 886)}

    def test_a_section_belongs_to_its_nearest_public_law(self):
        """R.S. sections precede the Pub. L.; its § must not bind to them."""
        plaw, _ = nv.credit_keys("(R.S. § 4793; Pub. L. 91-375, § 6(o), Aug. 12, 1970, 84 Stat. 782.)")
        assert plaw == {(91, 375, "6")}

    def test_empty_credit_is_safe(self):
        assert nv.credit_keys("") == (set(), set())
        assert nv.credit_keys(None) == (set(), set())


class TestSectionOf:
    @pytest.mark.parametrize("node,expected", [
        ("/us/usc/t42/s1396/b/1/D", "/us/usc/t42/s1396"),
        ("/us/usc/t42/s1396", "/us/usc/t42/s1396"),
        ("/us/usc/t12/s2279aa-10/b/4", "/us/usc/t12/s2279aa-10"),
        ("/us/usc/t5/s3341/a", "/us/usc/t5/s3341"),
        # Appendix titles put the enacting public law in the path.
        ("/us/usc/t18a/pl/96/456/s13/b", "/us/usc/t18a/pl/96/456/s13"),
    ])
    def test_reduces_any_node_to_its_section(self, node, expected):
        assert nv.section_of(node) == expected

    def test_unparseable_identifier_returns_none(self):
        assert nv.section_of("not-an-id") is None
        assert nv.section_of("") is None


class TestClerkKeys:
    @pytest.fixture
    def extract(self, tmp_path, monkeypatch):
        rows = [
            # codified: contributes a USC key
            {"authority": "2 U.S.C. 807(b); Pub. L. 96-114, Sec. 107; (104 Stat. 2308)"},
            # uncodified: no USC cite at all — the case USC-only matching misses
            {"authority": "Pub. L. 116-260, div. BB, title I, Sec. 102; (134 Stat. 2761)"},
        ]
        p = tmp_path / "extract.jsonl"
        p.write_text("".join(json.dumps({**r, "reporting_entity": "", "nature_of_report": "",
                                         "when_expected": ""}) + "\n" for r in rows))
        monkeypatch.setattr(nv, "EXTRACT_PATH", p)
        return p

    def test_collects_all_three_citation_forms(self, extract):
        k = nv.clerk_keys(extract)
        assert "/us/usc/t2/s807" in k["usc"]
        assert (96, 114, "107") in k["plaw"]
        assert (116, 260, "102") in k["plaw"]
        assert (104, 2308) in k["stat"]

    def test_uncodified_rows_contribute_a_plaw_key(self, extract):
        """The row with no USC cite must still be matchable."""
        k = nv.clerk_keys(extract)
        assert (116, 260, "102") in k["plaw"]


class TestClassify:
    KEYS = {
        "usc": {"/us/usc/t2/s807"},
        "plaw": {(116, 260, "102")},
        "stat": {(134, 2761)},
    }
    CREDITS = {
        "/us/usc/t42/s300gg-111": "(Pub. L. 116-260, div. BB, title I, § 102, Dec. 27, 2020, 134 Stat. 2761.)",
        "/us/usc/t42/s999": "(Pub. L. 99-999, § 1, Jan. 1, 1986, 134 Stat. 2761.)",
        "/us/usc/t42/s555": "(Pub. L. 90-1, § 3, Jan. 1, 1967, 81 Stat. 5.)",
    }

    def test_usc_match_wins(self):
        assert nv.classify("/us/usc/t2/s807/b", self.CREDITS, self.KEYS) == "listed_usc"

    def test_public_law_match_catches_an_uncodified_listing(self):
        """The correction this module exists for: listed by Pub. L., not USC."""
        got = nv.classify("/us/usc/t42/s300gg-111/a/2", self.CREDITS, self.KEYS)
        assert got == "listed_plaw"

    def test_statutes_at_large_is_the_weakest_fallback(self):
        """Same Stat. page, different public law — a possible match only."""
        assert nv.classify("/us/usc/t42/s999/a", self.CREDITS, self.KEYS) == "listed_stat"

    def test_no_key_matches_is_novel(self):
        assert nv.classify("/us/usc/t42/s555/a", self.CREDITS, self.KEYS) == "novel"

    def test_section_with_no_credit_is_novel(self):
        assert nv.classify("/us/usc/t42/s404/a", self.CREDITS, self.KEYS) == "novel"

    def test_unparseable_identifier_is_novel_not_a_crash(self):
        assert nv.classify("garbage", self.CREDITS, self.KEYS) == "novel"


class TestAnnotate:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        (tmp_path / "credits.json").write_text(json.dumps({
            "/us/usc/t42/s1396": "(Pub. L. 111-148, § 2801, Mar. 23, 2010, 124 Stat. 119.)",
        }))
        extract = tmp_path / "extract.jsonl"
        extract.write_text(json.dumps({
            "authority": "2 U.S.C. 807(b); Pub. L. 96-114, Sec. 107; (104 Stat. 2308)",
            "reporting_entity": "", "nature_of_report": "", "when_expected": "",
        }) + "\n")
        monkeypatch.setattr(nv, "CREDITS_PATH", tmp_path / "credits.json")
        monkeypatch.setattr(nv, "EXTRACT_PATH", extract)
        return tmp_path

    def _verdicts(self, tmp_path, rows):
        p = tmp_path / "v.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return p

    def test_only_flagged_rows_are_annotated(self, wired, tmp_path):
        v = self._verdicts(tmp_path, [
            {"uslm_id": "/us/usc/t42/s1396/b", "is_mandate": True,
             "verdict": {"frequency": "annual"}},
            {"uslm_id": "/us/usc/t42/s1397", "is_mandate": False,
             "verdict": {"frequency": "unknown"}},
        ])
        out = tmp_path / "out.jsonl"
        stats = nv.annotate(v, out)
        assert stats["judged"] == 2 and stats["flagged"] == 1
        assert len(out.read_text().strip().splitlines()) == 1

    @pytest.mark.parametrize("freq,standing", [
        ("annual", True), ("biennial", True), ("event-driven", True), ("quarterly", True),
        ("one-time", False), ("unknown", False), ("", False),
    ])
    def test_standing_excludes_one_time_and_unknown(self, wired, tmp_path, freq, standing):
        v = self._verdicts(tmp_path, [
            {"uslm_id": "/us/usc/t42/s1396/b", "is_mandate": True, "verdict": {"frequency": freq}},
        ])
        out = tmp_path / "out.jsonl"
        nv.annotate(v, out)
        assert json.loads(out.read_text().splitlines()[0])["standing"] is standing

    def test_missing_credits_index_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nv, "CREDITS_PATH", tmp_path / "nope.json")
        with pytest.raises(SystemExit, match="build-credits"):
            nv.annotate(self._verdicts(tmp_path, []), tmp_path / "o.jsonl")

    def test_tally_covers_every_flagged_row(self, wired, tmp_path):
        v = self._verdicts(tmp_path, [
            {"uslm_id": f"/us/usc/t42/s{i}/a", "is_mandate": True, "verdict": {"frequency": "annual"}}
            for i in (1396, 1397, 1398)
        ])
        stats = nv.annotate(v, tmp_path / "out.jsonl")
        buckets = {k: v for k, v in stats.items() if k.startswith("listed") or k == "novel"}
        assert sum(buckets.values()) == stats["flagged"] == 3
