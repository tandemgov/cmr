"""Tests for deciding whether a discovered mandate is still live.

Fast, no network. The distinction that matters here is between *expired* — a
stated end bound has passed, which the text settles — and *review*, where the
text raises doubt it cannot resolve. Collapsing the second into the first would
silently discard live mandates; collapsing it into `current` would keep dead
ones. Several tests exist purely to keep those apart.
"""

import json

import pytest

import currency as cu

YEAR = 2026


class TestTerminalYear:
    @pytest.mark.parametrize("text,expected", [
        ("annually thereafter through 2010", 2010),
        ("and annually thereafter through fiscal year 2023", 2023),
        ("for each of fiscal years 2019 through 2023", 2023),
        ("until December 31, 2015", 2015),
        ("reports ending in 2008", 2008),
        ("through FY 2021", 2021),
    ])
    def test_reads_the_stated_end_bound(self, text, expected):
        assert cu.terminal_year(text) == expected

    def test_takes_the_latest_when_a_sunset_was_extended(self):
        """An original bound plus an extension: the extension governs."""
        assert cu.terminal_year("through 2016, and thereafter through 2024") == 2024

    @pytest.mark.parametrize("text", [
        "the Secretary shall submit an annual report to Congress",
        "not later than 180 days after the date of enactment",
        "enacted in 1996 to address the matter",          # a start date, not a bound
    ])
    def test_returns_none_when_no_end_bound_is_stated(self, text):
        assert cu.terminal_year(text) is None

    def test_empty_is_safe(self):
        assert cu.terminal_year("") is None
        assert cu.terminal_year(None) is None


class TestClassify:
    def test_passed_sunset_is_expired(self):
        status, reason = cu.classify("annually thereafter through 2010", YEAR)
        assert status == "expired"
        assert "2010" in reason

    def test_future_sunset_is_current(self):
        """A duty running to 2030 is live now and must not be discarded."""
        assert cu.classify("annually thereafter through 2030", YEAR)[0] == "current"

    def test_sunset_in_the_current_year_is_still_current(self):
        assert cu.classify("through fiscal year 2026", YEAR)[0] == "current"

    @pytest.mark.parametrize("text", [
        "There is hereby established the Interim Compliance Panel",
        "the requirement shall cease to apply",
        "this section shall terminate on the date specified",
        "the report shall no longer be required",
        "sunset provisions apply to this subchapter",
    ])
    def test_temporary_language_goes_to_review_not_expired(self, text):
        """Undecidable from the text — a queue for a human, not a verdict."""
        status, reason = cu.classify(text, YEAR)
        assert status == "review"
        assert reason

    def test_interim_report_type_is_not_a_temporary_body(self):
        """'interim and final reports' is a report kind, not a defunct body."""
        assert cu.classify(
            "the Secretary shall submit interim and final reports to Congress", YEAR
        )[0] == "current"

    def test_a_passed_sunset_outranks_review_language(self):
        status, _ = cu.classify(
            "the Interim Panel shall report annually through 2011", YEAR)
        assert status == "expired"

    def test_plain_recurring_duty_is_current(self):
        assert cu.classify(
            "The Special Counsel shall submit to Congress, on an annual basis, a report.", YEAR
        ) == ("current", "")

    def test_year_is_a_parameter_not_a_wall_clock(self):
        """Re-running next year must reclassify, and old runs stay reproducible."""
        text = "annually thereafter through 2026"
        assert cu.classify(text, 2026)[0] == "current"
        assert cu.classify(text, 2027)[0] == "expired"


class TestAnnotate:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        cands = tmp_path / "cand.jsonl"
        cands.write_text("".join(json.dumps(r) + "\n" for r in [
            {"uslm_id": "/live", "text": "The Secretary shall submit an annual report to Congress."},
            {"uslm_id": "/dead", "text": "shall report annually thereafter through 2010"},
            {"uslm_id": "/maybe", "text": "the Interim Compliance Panel shall report"},
            {"uslm_id": "/dropped", "text": "not a mandate"},
        ]))
        conf = tmp_path / "conf.jsonl"
        conf.write_text("".join(json.dumps(r) + "\n" for r in [
            {"uslm_id": "/live", "is_mandate": True, "verdict": {"frequency": "annual"}},
            {"uslm_id": "/dead", "is_mandate": True, "verdict": {"frequency": "annual"}},
            {"uslm_id": "/maybe", "is_mandate": True, "verdict": {"frequency": "annual"}},
            {"uslm_id": "/dropped", "is_mandate": False, "verdict": {}},
        ]))
        nov = tmp_path / "nov.jsonl"
        nov.write_text(json.dumps({"uslm_id": "/live", "frequency": "annual"}) + "\n")
        return cands, conf, nov, tmp_path / "out.jsonl"

    def test_splits_the_confirmed_set_three_ways(self, wired):
        cands, conf, nov, out = wired
        stats = cu.annotate(YEAR, confirmed=conf, candidates=cands, novelty=nov, out=out)
        assert stats["confirmed"] == 3, "rows the confirm pass rejected are excluded"
        assert stats["current"] == 1
        assert stats["expired"] == 1
        assert stats["review"] == 1

    def test_every_confirmed_row_gets_exactly_one_status(self, wired):
        cands, conf, nov, out = wired
        stats = cu.annotate(YEAR, confirmed=conf, candidates=cands, novelty=nov, out=out)
        buckets = sum(v for k, v in stats.items() if k != "confirmed")
        assert buckets == stats["confirmed"]

    def test_output_carries_the_reason(self, wired):
        cands, conf, nov, out = wired
        cu.annotate(YEAR, confirmed=conf, candidates=cands, novelty=nov, out=out)
        rows = {json.loads(l)["uslm_id"]: json.loads(l) for l in out.read_text().splitlines()}
        assert "2010" in rows["/dead"]["reason"]
        assert rows["/live"]["reason"] == ""

    def test_missing_inputs_fail_loudly(self, tmp_path):
        with pytest.raises(SystemExit, match="sweep"):
            cu.annotate(YEAR, confirmed=tmp_path / "nope.jsonl",
                        candidates=tmp_path / "nope2.jsonl", out=tmp_path / "o.jsonl")
