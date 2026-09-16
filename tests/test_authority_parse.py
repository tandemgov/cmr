"""Exact-value tests for citation parsing and USLM addressing.

Fast, no network. These pin the behaviours that statutory-text resolution
depends on — above all dash folding, which is the difference between 99.3%
and 91.1% citation resolution and silently disguises live law as repealed.
"""

import pytest

from authority_parse import (
    Authority,
    PlawRef,
    StatRef,
    UscRef,
    fold_dashes,
    parse_authority,
    summarize,
)


class TestDashFolding:
    """USLM uses EN DASH in suffixed section numbers; the House Doc uses hyphen."""

    @pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "−"])
    def test_every_dash_variant_folds_to_ascii_hyphen(self, dash):
        assert fold_dashes(f"12 U.S.C. 635a{dash}5") == "12 U.S.C. 635a-5"

    def test_en_dashed_citation_yields_the_hyphenated_uslm_id(self):
        ref = parse_authority("12 U.S.C. 635a–5(c)").usc[0]
        assert ref.section == "635a-5"
        assert ref.uslm_id == "/us/usc/t12/s635a-5/c"

    def test_folding_leaves_plain_text_untouched(self):
        assert fold_dashes("2 U.S.C. 807(b)") == "2 U.S.C. 807(b)"


class TestUscParsing:
    def test_simple_cite_with_subsection(self):
        auth = parse_authority("2 U.S.C. 807(b); Pub. L. 96-114, title I, Sec. 107; (104 Stat. 2308)")
        assert auth.usc == [UscRef("2", "807", ("b",), False, False)]
        assert auth.usc[0].uslm_id == "/us/usc/t2/s807/b"
        assert auth.usc[0].section_id == "/us/usc/t2/s807"

    def test_deep_subsection_path(self):
        ref = parse_authority("42 U.S.C. 300gg-111(a)(2)(A)(iii)").usc[0]
        assert ref.subsections == ("a", "2", "A", "iii")
        assert ref.uslm_id == "/us/usc/t42/s300gg-111/a/2/A/iii"

    @pytest.mark.parametrize(
        "text,section",
        [
            ("7 U.S.C. 950cc(d)", "950cc"),          # multi-letter suffix
            ("22 U.S.C. 2349aa-9(c)", "2349aa-9"),   # letters then dash-number
            ("22 U.S.C. 2394-1a", "2394-1a"),        # dash-number then letter
            ("42 U.S.C. 4370m-10", "4370m-10"),
            ("12 U.S.C. 2279aa-10(b)(4)", "2279aa-10"),
        ],
    )
    def test_section_number_shapes(self, text, section):
        assert parse_authority(text).usc[0].section == section

    def test_section_symbol_is_optional(self):
        assert parse_authority("36 U.S.C. § 152412").usc[0].section == "152412"

    def test_note_reference_is_flagged_and_addresses_the_section(self):
        ref = parse_authority("42 U.S.C. 300gg-118 note; Pub. L. 116-260").usc[0]
        assert ref.is_note is True
        # Notes are not addressable nodes, so the id must not carry a subpath.
        assert ref.uslm_id == "/us/usc/t42/s300gg-118"

    def test_appendix_is_flagged(self):
        assert parse_authority("50 U.S.C. App. 2170").usc[0].is_appendix is True

    def test_duplicate_cites_collapse_but_order_is_kept(self):
        auth = parse_authority("50 U.S.C. 1641(c); see also 22 U.S.C. 2151; 50 U.S.C. 1641(c)")
        assert [u.section for u in auth.usc] == ["1641", "2151"]


class TestPlawAndStat:
    def test_public_law_forms(self):
        for text in ("Pub. L. 117-263", "Public Law 117-263", "P.L. 117-263"):
            assert parse_authority(text).plaw[0] == PlawRef(117, 263)

    def test_public_law_with_en_dash(self):
        assert parse_authority("Pub. L. 116–260").plaw[0] == PlawRef(116, 260)

    def test_plaw_package_id(self):
        assert PlawRef(117, 263).package_id == "PLAW-117publ263"

    def test_statutes_at_large(self):
        assert parse_authority("(134 Stat. 2761)").stat[0] == StatRef(134, 2761)


class TestAuthorityShape:
    def test_uncodified_authority_has_no_usc(self):
        auth = parse_authority("Pub. L. 117-328, div. H, title II; (136 Stat. 4900)")
        assert auth.is_codified is False
        assert auth.plaw and auth.stat

    def test_empty_authority_is_safe(self):
        auth = parse_authority("")
        assert auth == Authority(raw="", usc=[], plaw=[], stat=[])
        assert auth.is_codified is False

    def test_to_dict_is_json_serializable(self):
        import json

        d = parse_authority("2 U.S.C. 807(b); Pub. L. 96-114; (104 Stat. 2308)").to_dict()
        assert json.loads(json.dumps(d))["usc"][0]["uslm_id"] == "/us/usc/t2/s807/b"


class TestSummarize:
    def test_counts_split_codified_from_uncodified(self):
        parsed = [
            {"authority": parse_authority("2 U.S.C. 807(b); Pub. L. 96-114").to_dict()},
            {"authority": parse_authority("Pub. L. 117-328; (136 Stat. 4900)").to_dict()},
            {"authority": parse_authority("Pub. L. 90-100; (81 Stat. 1)").to_dict()},
        ]
        s = summarize(parsed)
        assert s["mandates"] == 3
        assert s["with_usc"] == 1
        assert s["uncodified"] == 2
        # 117th is reachable via govinfo PLAW; the 90th predates that collection.
        assert s["uncodified_reachable_via_plaw"] == 1
        assert s["uncodified_needs_statutes_at_large"] == 1
