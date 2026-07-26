"""Tests for candidate extraction, the recipient gate, and endpoint config.

Fast, no network, no local-model host. Everything that talks to a model is
either pure request-construction (asserted on the dict, never sent) or excluded.
The USLM fixture reproduces the structural trap this module exists to fix: an
obligation split across levels, with the modal in the parent's chapeau.
"""

import json

import pytest

import mandate_classify as mc

# `MACPAC shall—` sits in the parent; `(D) ... submit a report to Congress` in
# the child. Extracting the child alone loses both the actor and the modal.
USLM = """<?xml version="1.0" encoding="UTF-8"?>
<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0">
 <main>
  <section identifier="/us/usc/t42/s1396">
   <num>§1396.</num><heading>Medicaid and CHIP Payment and Access Commission</heading>
   <subsection identifier="/us/usc/t42/s1396/b">
    <num>(b)</num><heading>Duties</heading>
    <paragraph identifier="/us/usc/t42/s1396/b/1">
     <num>(1)</num><heading>Review of access policies</heading>
     <chapeau>MACPAC shall—</chapeau>
     <subparagraph identifier="/us/usc/t42/s1396/b/1/A">
      <num>(A)</num><content>review policies of the Medicaid program established under this subchapter and report on the operation of those policies as they affect access.</content>
     </subparagraph>
     <subparagraph identifier="/us/usc/t42/s1396/b/1/D">
      <num>(D)</num><content>by not later than June 15 of each year, submit a report to Congress containing an examination of issues affecting Medicaid and CHIP.</content>
     </subparagraph>
    </paragraph>
   </subsection>
  </section>
  <section identifier="/us/usc/t42/s1397">
   <num>§1397.</num><heading>Definitions</heading>
   <content>As used in this subchapter, the term "State" means each of the fifty States, and the term "Secretary" means the Secretary of Health and Human Services acting through the Administrator.</content>
  </section>
 </main>
</uscDoc>
"""


@pytest.fixture
def root():
    import xml.etree.ElementTree as ET
    return ET.fromstring(USLM)


def provisions(root, **kw):
    return dict(mc.iter_provisions(root, min_len=20, **kw))


class TestChapeauContext:
    """The largest single accuracy lever measured on this task."""

    def test_never_leaves_the_split_child_without_its_modal(self, root):
        text = provisions(root, context="never")["/us/usc/t42/s1396/b/1/D"]
        assert "submit a report to Congress" in text
        assert "MACPAC shall" not in text
        assert not mc._MODAL_RE.search(text)

    def test_always_restores_actor_and_modal(self, root):
        text = provisions(root, context="always")["/us/usc/t42/s1396/b/1/D"]
        assert "MACPAC shall" in text
        assert "submit a report to Congress" in text
        assert mc._MODAL_RE.search(text)

    def test_always_also_prefixes_the_section_heading(self, root):
        text = provisions(root, context="always")["/us/usc/t42/s1396/b/1/D"]
        assert "Medicaid and CHIP Payment and Access Commission" in text

    def test_conditional_repairs_only_nodes_missing_a_modal(self, root):
        cond = provisions(root, context="conditional")
        never = provisions(root, context="never")
        # The child lacks a modal, so it is repaired...
        assert "MACPAC shall" in cond["/us/usc/t42/s1396/b/1/D"]
        # ...while the paragraph already carries one and is left untouched.
        assert cond["/us/usc/t42/s1396/b/1"] == never["/us/usc/t42/s1396/b/1"]

    def test_conditional_and_always_differ_only_on_complete_nodes(self, root):
        cond = provisions(root, context="conditional")
        always = provisions(root, context="always")
        assert cond["/us/usc/t42/s1396/b/1/D"] == always["/us/usc/t42/s1396/b/1/D"]
        assert cond["/us/usc/t42/s1396/b/1"] != always["/us/usc/t42/s1396/b/1"]

    def test_rejects_an_unknown_context_mode(self, root):
        with pytest.raises(ValueError, match="conditional|always|never"):
            list(mc.iter_provisions(root, context="sometimes"))

    def test_length_bounds_apply_to_the_node_not_the_context(self, root):
        """Context must not push a provision over max_len and drop it."""
        ids = provisions(root, context="always", max_len=200)
        assert "/us/usc/t42/s1396/b/1/D" in ids
        assert len(ids["/us/usc/t42/s1396/b/1/D"]) > 200

    def test_every_addressable_node_with_an_identifier_is_emitted(self, root):
        ids = set(provisions(root))
        assert {"/us/usc/t42/s1396", "/us/usc/t42/s1396/b", "/us/usc/t42/s1396/b/1",
                "/us/usc/t42/s1396/b/1/A", "/us/usc/t42/s1396/b/1/D",
                "/us/usc/t42/s1397"} <= ids


class TestRecipientGate:
    """Recall-first by design: ~72% recall (frontier-audited), keeping ~11% of the corpus.

    An earlier 99.1% figure was a raw count across two strata whose populations
    differ by 14x — see RUNBOOK §12. The misses concentrate in provisions that
    state a duty whose recipient the regex does not recognise.
    """

    @pytest.mark.parametrize("text", [
        "the Secretary shall submit to Congress a report",
        "transmit to the Committee on Armed Services of the Senate",
        "report to the House of Representatives",
        "notify the Speaker of the House and the President pro tempore",
        "the Comptroller General shall review",
        "submit to the appropriate congressional committees",
        # Plural is the dominant form in appropriations law and was silently
        # rejected by a singular-only `committee on`.
        "submit to the Committees on Appropriations of the House and Senate",
        "transmit to the Committees on Armed Services",
        "report to the Joint Committee on Taxation",
        "furnish to the Congressional Budget Office",
        "file with the Clerk of the House and the Secretary of the Senate",
    ])
    def test_passes_when_a_congressional_recipient_is_named(self, text):
        assert mc.passes_gate(text)

    @pytest.mark.parametrize("text", [
        "the Secretary shall prescribe regulations to carry out this section",
        "there are authorized to be appropriated such sums as may be necessary",
        'As used in this chapter, the term "State" means each of the fifty States',
        "the Administrator shall notify the applicant in writing",
    ])
    def test_rejects_provisions_with_no_congressional_recipient(self, text):
        assert not mc.passes_gate(text)

    def test_gate_is_deliberately_recipient_only(self):
        """No duty verb required — drafting separates modal from verb."""
        assert mc.passes_gate(
            "shall, as soon as practicable but not later than one year after the "
            "date of enactment, prepare and transmit to the Congress a report"
        )
        # Recipient with no duty at all still passes; the judge filters it.
        assert mc.passes_gate("the Committee on Finance held hearings on the matter")

    def test_empty_and_none_are_safe(self):
        assert not mc.passes_gate("")
        assert not mc.passes_gate(None)


class TestEndpointConfig:
    """Request construction only — nothing is sent."""

    def test_all_endpoints_target_per_model_ports_not_the_proxy(self):
        for ep in mc.ENDPOINTS.values():
            assert ep.port != 4000, "the :4000 proxy silently drops chat_template_kwargs"
            assert ep.url.startswith(f"http://{mc.CHAT_HOST}:{ep.port}")

    def test_llama_cpp_endpoints_disable_thinking(self):
        body = mc.ENDPOINTS["nemotron"].body("SYS", "text")
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert "reasoning_effort" not in body

    def test_sglang_endpoint_uses_reasoning_effort_instead(self):
        """gpt-oss ignores chat_template_kwargs and needs reasoning ON."""
        body = mc.ENDPOINTS["gpt-oss"].body("SYS", "text")
        assert body["reasoning_effort"] == "low"
        assert "chat_template_kwargs" not in body

    def test_thinking_endpoints_get_a_larger_token_budget(self):
        """Too small and reasoning eats the allowance, returning empty content."""
        assert mc.ENDPOINTS["gpt-oss"].max_tokens >= 600
        assert mc.ENDPOINTS["nemotron"].max_tokens < 600

    def test_input_is_truncated_to_fit_the_context_window(self):
        """llama.cpp counts max_tokens against n_ctx; overflow truncates silently."""
        small = mc.ENDPOINTS["gemma-26b"]        # n_ctx 2048
        large = mc.ENDPOINTS["nemotron"]         # n_ctx 16384
        s = small.body("SYS", "x" * 100_000)["messages"][1]["content"]
        l = large.body("SYS", "x" * 100_000)["messages"][1]["content"]
        assert len(s) < len(l)
        assert len(s) < small.n_ctx * 4
        assert len(l) < large.n_ctx * 4

    def test_system_prompt_length_is_charged_against_the_budget(self):
        ep = mc.ENDPOINTS["gemma-26b"]
        short = ep.body("S", "x" * 100_000)["messages"][1]["content"]
        long = ep.body("S" * 3000, "x" * 100_000)["messages"][1]["content"]
        assert len(long) < len(short)

    def test_temperature_is_zero_for_reproducibility(self):
        for ep in mc.ENDPOINTS.values():
            assert ep.body("SYS", "t")["temperature"] == 0

    def test_sweep_and_confirm_models_are_registered(self):
        assert mc.SWEEP_MODEL in mc.ENDPOINTS
        assert mc.CONFIRM_MODEL in mc.ENDPOINTS
        assert mc.SWEEP_MODEL != mc.CONFIRM_MODEL


class TestVerdictParsing:
    def test_extracts_json_from_a_chatty_response(self):
        v = mc._extract_json('Sure! {"is_mandate": true, "recipient": "congress"} hope that helps')
        assert v == {"is_mandate": True, "recipient": "congress"}

    @pytest.mark.parametrize("s", ["", None, "no json here", "{not valid json}"])
    def test_unparseable_responses_return_none(self, s):
        assert mc._extract_json(s) is None

    def test_is_mandate_requires_congress_as_recipient(self):
        assert mc.is_mandate({"is_mandate": True, "recipient": "congress"})
        assert not mc.is_mandate({"is_mandate": True, "recipient": "agency"})
        assert not mc.is_mandate({"is_mandate": False, "recipient": "congress"})
        assert not mc.is_mandate(None)


class TestSimilarity:
    def test_max_cosine_against_known_vectors(self):
        known = [[1.0, 0.0], [0.0, 1.0]]
        cands = [[2.0, 0.0], [0.0, 5.0], [1.0, 1.0]]
        got = mc.max_similarity(cands, known)
        assert got[0] == pytest.approx(1.0, abs=1e-5)
        assert got[1] == pytest.approx(1.0, abs=1e-5)
        assert got[2] == pytest.approx(0.7071, abs=1e-3)

    def test_handles_more_candidates_than_the_chunk_size(self):
        known = [[1.0, 0.0]]
        cands = [[1.0, 0.0]] * 5000
        got = mc.max_similarity(cands, known)
        assert len(got) == 5000
        assert all(g == pytest.approx(1.0, abs=1e-5) for g in got)


class TestCandidateSampling:
    @pytest.fixture
    def xml_dir(self, tmp_path, monkeypatch):
        (tmp_path / "usc42.xml").write_text(USLM)
        monkeypatch.setattr(mc, "USC_XML_DIR", tmp_path)
        return tmp_path

    def test_sampled_text_carries_ancestor_context(self, xml_dir):
        """Sampling raw node text would reintroduce the split obligation."""
        rows = mc.sample_uncited_provisions(set(), 50, gated=False)
        d = {r["uslm_id"]: r["text"] for r in rows}
        assert "MACPAC shall" in d["/us/usc/t42/s1396/b/1/D"]

    def test_gated_sampling_drops_provisions_with_no_congressional_recipient(self, xml_dir):
        gated = {r["uslm_id"] for r in mc.sample_uncited_provisions(set(), 50, gated=True)}
        ungated = {r["uslm_id"] for r in mc.sample_uncited_provisions(set(), 50, gated=False)}
        # The definitions section names no congressional recipient.
        assert "/us/usc/t42/s1397" in ungated
        assert "/us/usc/t42/s1397" not in gated
        assert "/us/usc/t42/s1396/b/1/D" in gated

    def test_excluded_identifiers_are_never_sampled(self, xml_dir):
        rows = mc.sample_uncited_provisions({"/us/usc/t42/s1396/b/1/D"}, 50, gated=False)
        assert "/us/usc/t42/s1396/b/1/D" not in {r["uslm_id"] for r in rows}

    def test_sampling_is_seed_deterministic(self, xml_dir):
        a = mc.sample_uncited_provisions(set(), 3, seed=1, gated=False)
        b = mc.sample_uncited_provisions(set(), 3, seed=1, gated=False)
        assert [r["uslm_id"] for r in a] == [r["uslm_id"] for r in b]

    def test_missing_corpus_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mc, "USC_XML_DIR", tmp_path / "nope")
        with pytest.raises(SystemExit, match="statute_fetch"):
            mc.sample_uncited_provisions(set(), 5)


class TestSweepResume:
    """A ~10h run must survive a kill; these guard the resume path."""

    def test_done_ids_reads_prior_verdicts(self, tmp_path):
        p = tmp_path / "v.jsonl"
        p.write_text('{"uslm_id": "/a", "verdict": {}, "is_mandate": true}\n'
                     '{"uslm_id": "/b", "verdict": {}, "is_mandate": false}\n')
        assert mc._done_ids(p) == {"/a", "/b"}

    def test_done_ids_survives_a_torn_final_line(self, tmp_path):
        """A killed run can leave a half-written record."""
        p = tmp_path / "v.jsonl"
        p.write_text('{"uslm_id": "/a", "verdict": {}}\n'
                     '{"uslm_id": "/b", "verdict": {}}\n{"uslm_i')
        assert mc._done_ids(p) == {"/a", "/b"}

    def test_done_ids_on_missing_file_is_empty(self, tmp_path):
        assert mc._done_ids(tmp_path / "nope.jsonl") == set()

    def test_null_verdicts_are_not_done_and_get_retried(self, tmp_path):
        """A network drop writes nulls; treating them as done silently loses rows."""
        p = tmp_path / "v.jsonl"
        p.write_text('{"uslm_id": "/ok", "verdict": {"is_mandate": true}}\n'
                     '{"uslm_id": "/dropped", "verdict": null}\n')
        assert mc._done_ids(p) == {"/ok"}

    def test_retry_appends_and_last_record_wins(self, tmp_path, monkeypatch):
        cands = tmp_path / "c.jsonl"
        cands.write_text("".join(json.dumps({"uslm_id": u, "text": "t"}) + "\n"
                                 for u in ("/ok", "/dropped")))
        out = tmp_path / "v.jsonl"
        out.write_text('{"uslm_id": "/ok", "verdict": {"is_mandate": true}, "is_mandate": true}\n'
                       '{"uslm_id": "/dropped", "verdict": null, "is_mandate": false}\n')
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": True, "recipient": "congress"}] * len(t))
        assert mc.sweep(candidates=cands, out=out) == 1, "only the null row is retried"
        final = mc.load_verdicts(out)
        assert len(final) == 2, "deduped by identifier"
        assert final["/dropped"]["verdict"] is not None, "retry supersedes the null"
        assert final["/dropped"]["is_mandate"] is True

    def test_load_verdicts_keeps_the_last_record(self, tmp_path):
        p = tmp_path / "v.jsonl"
        p.write_text('{"uslm_id": "/a", "verdict": null}\n'
                     '{"uslm_id": "/a", "verdict": {"is_mandate": true}}\n')
        got = mc.load_verdicts(p)
        assert len(got) == 1 and got["/a"]["verdict"] == {"is_mandate": True}

    def test_sweep_skips_already_judged_and_appends(self, tmp_path, monkeypatch):
        cands = tmp_path / "c.jsonl"
        cands.write_text("".join(
            json.dumps({"uslm_id": f"/s{i}", "text": f"shall submit to Congress report {i}"}) + "\n"
            for i in range(5)))
        out = tmp_path / "v.jsonl"
        out.write_text('{"uslm_id": "/s0", "verdict": {}, "is_mandate": true}\n'
                       '{"uslm_id": "/s1", "verdict": {}, "is_mandate": true}\n')

        judged = []
        def fake(texts, workers=8, endpoint=None):
            judged.extend(texts)
            return [{"is_mandate": True, "recipient": "congress"}] * len(texts)
        monkeypatch.setattr(mc, "judge_many", fake)

        n = mc.sweep(candidates=cands, out=out, chunk=2)
        assert n == 3, "only the three unjudged candidates should be sent"
        assert len(judged) == 3
        ids = [json.loads(l)["uslm_id"] for l in out.read_text().splitlines()]
        assert ids == ["/s0", "/s1", "/s2", "/s3", "/s4"], "prior verdicts preserved"

    def test_sweep_records_negatives_too(self, tmp_path, monkeypatch):
        """Re-judging the corpus to change a threshold would be unaffordable."""
        cands = tmp_path / "c.jsonl"
        cands.write_text(json.dumps({"uslm_id": "/s0", "text": "t"}) + "\n")
        out = tmp_path / "v.jsonl"
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": False, "recipient": "agency"}])
        mc.sweep(candidates=cands, out=out)
        row = json.loads(out.read_text().splitlines()[0])
        assert row["is_mandate"] is False
        assert row["verdict"]["recipient"] == "agency"

    def test_sweep_shuffles_so_a_partial_run_is_unbiased(self, tmp_path, monkeypatch):
        """Title-ordered candidates would make a half-finished sweep titles 1-25."""
        cands = tmp_path / "c.jsonl"
        cands.write_text("".join(
            json.dumps({"uslm_id": f"/s{i:03d}", "text": "t"}) + "\n" for i in range(100)))
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": False}] * len(t))
        mc.sweep(candidates=cands, out=tmp_path / "v.jsonl", limit=20)
        got = [json.loads(l)["uslm_id"] for l in (tmp_path / "v.jsonl").read_text().splitlines()]
        assert got != [f"/s{i:03d}" for i in range(20)], "should not be corpus order"

    def test_sweep_shuffle_is_seed_stable_across_resumes(self, tmp_path, monkeypatch):
        cands = tmp_path / "c.jsonl"
        cands.write_text("".join(
            json.dumps({"uslm_id": f"/s{i:03d}", "text": "t"}) + "\n" for i in range(50)))
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": False}] * len(t))
        a = tmp_path / "a.jsonl"
        mc.sweep(candidates=cands, out=a, limit=10)
        first = [json.loads(l)["uslm_id"] for l in a.read_text().splitlines()]
        # Resuming must continue the same permutation, not restart it.
        mc.sweep(candidates=cands, out=a, limit=10)
        second = [json.loads(l)["uslm_id"] for l in a.read_text().splitlines()][10:]
        assert not set(first) & set(second), "resume must not re-judge or skip"

    def test_sweep_honours_limit(self, tmp_path, monkeypatch):
        cands = tmp_path / "c.jsonl"
        cands.write_text("".join(json.dumps({"uslm_id": f"/s{i}", "text": "t"}) + "\n" for i in range(10)))
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": True, "recipient": "congress"}] * len(t))
        assert mc.sweep(candidates=cands, out=tmp_path / "v.jsonl", limit=3) == 3

    def test_sweep_without_candidates_fails_loudly(self, tmp_path):
        with pytest.raises(SystemExit, match="build-candidates"):
            mc.sweep(candidates=tmp_path / "nope.jsonl", out=tmp_path / "v.jsonl")

    def test_build_candidates_applies_the_gate(self, tmp_path, monkeypatch):
        (tmp_path / "usc42.xml").write_text(USLM)
        monkeypatch.setattr(mc, "USC_XML_DIR", tmp_path)
        out = tmp_path / "c.jsonl"
        mc.build_candidates(out)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        ids = {r["uslm_id"] for r in rows}
        assert "/us/usc/t42/s1396/b/1/D" in ids       # names Congress
        assert "/us/usc/t42/s1397" not in ids         # definitions only
        assert all("MACPAC shall" in r["text"] or "Congress" in r["text"] for r in rows)


NOTED = """<?xml version="1.0" encoding="UTF-8"?>
<uscDoc xmlns="http://xml.house.gov/schemas/uslm/1.0">
 <main>
  <section identifier="/us/usc/t10/s2687">
   <num>§2687.</num><heading>Base closures and realignments</heading>
   <content>Notwithstanding any other provision of law, no action may be taken to close a military installation.</content>
   <notes>
    <note topic="amendments">Amendments 1996—Subsec. (b) amended by Pub. L. 104-106.</note>
    <note topic="miscellaneous">Base Realignment Reporting Pub. L. 115-232, provided that: the Secretary shall submit to the Committees on Armed Services an annual report on realignment actions.</note>
    <note topic="miscellaneous">A short note.</note>
   </notes>
  </section>
 </main>
</uscDoc>
"""


class TestStatutoryNotesAsCandidates:
    """Notes are not addressable USLM nodes — excluding them is a recall hole."""

    @pytest.fixture
    def root(self):
        import xml.etree.ElementTree as ET
        return ET.fromstring(NOTED)

    def test_iter_provisions_never_yields_notes(self, root):
        """The gap this closes: note text is invisible to the provision stream."""
        texts = " ".join(t for _, t in mc.iter_provisions(root, min_len=20))
        assert "shall submit to the Committees on Armed Services" not in texts

    def test_iter_notes_yields_operative_notes(self, root):
        got = dict(mc.iter_notes(root, min_len=20))
        assert any("shall submit to the Committees on Armed Services" in t for t in got.values())

    def test_iter_notes_excludes_drafting_apparatus(self, root):
        got = " ".join(mc.iter_notes(root, min_len=20) and
                       [t for _, t in mc.iter_notes(root, min_len=20)])
        assert "Pub. L. 104-106" not in got

    def test_note_identifiers_are_synthetic_and_scoped_to_the_section(self, root):
        ids = [i for i, _ in mc.iter_notes(root, min_len=20)]
        assert all(i.startswith("/us/usc/t10/s2687/note/") for i in ids)
        assert len(set(ids)) == len(ids), "identifiers must be unique"

    def test_notes_carry_the_section_heading_for_context(self, root):
        got = dict(mc.iter_notes(root, min_len=20))
        assert all("Base closures and realignments" in t for t in got.values())

    def test_length_bounds_drop_trivial_notes(self, root):
        got = dict(mc.iter_notes(root, min_len=120))
        assert not any(t.endswith("A short note.") for t in got.values())

    def test_build_candidates_includes_notes_and_marks_the_source(self, tmp_path, monkeypatch):
        (tmp_path / "usc10.xml").write_text(NOTED)
        monkeypatch.setattr(mc, "USC_XML_DIR", tmp_path)
        out = tmp_path / "c.jsonl"
        mc.build_candidates(out)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        sources = {r["source"] for r in rows}
        assert "note" in sources
        assert any("Committees on Armed Services" in r["text"] for r in rows)

    def test_notes_can_be_excluded_explicitly(self, tmp_path, monkeypatch):
        (tmp_path / "usc10.xml").write_text(NOTED)
        monkeypatch.setattr(mc, "USC_XML_DIR", tmp_path)
        out = tmp_path / "c.jsonl"
        mc.build_candidates(out, notes=False)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        assert all(r["source"] == "provision" for r in rows)


class TestConfirmScope:
    """Confirming all 29.5k flagged rows is ~6.6h; the defensible core is ~1.4h."""

    @pytest.fixture
    def novelty(self, tmp_path, monkeypatch):
        rows = [
            {"uslm_id": "/a", "novelty": "novel", "standing": True, "frequency": "annual"},
            {"uslm_id": "/b", "novelty": "novel", "standing": True, "frequency": "event-driven"},
            {"uslm_id": "/c", "novelty": "novel", "standing": False, "frequency": "one-time"},
            {"uslm_id": "/d", "novelty": "listed_usc", "standing": True, "frequency": "annual"},
            {"uslm_id": "/e", "novelty": "novel", "standing": True, "frequency": "biennial"},
        ]
        p = tmp_path / "n.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        monkeypatch.setattr(mc, "NOVELTY_PATH", p)
        return p

    def test_all_scope_does_not_restrict(self, novelty):
        assert mc.confirm_scope("all") is None

    def test_novel_scope_drops_listed_and_one_time(self, novelty):
        assert mc.confirm_scope("novel") == {"/a", "/b", "/e"}

    def test_novel_periodic_also_drops_event_driven(self, novelty):
        """Event-driven precision is 50% vs 82-88% for annual/biennial."""
        assert mc.confirm_scope("novel-periodic") == {"/a", "/e"}

    def test_missing_novelty_file_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mc, "NOVELTY_PATH", tmp_path / "nope.jsonl")
        with pytest.raises(SystemExit, match="novelty"):
            mc.confirm_scope("novel")

    def test_confirm_honours_the_restriction(self, tmp_path, monkeypatch):
        v = tmp_path / "v.jsonl"
        v.write_text("".join(json.dumps(
            {"uslm_id": f"/{c}", "is_mandate": True, "verdict": {}}) + "\n" for c in "abc"))
        monkeypatch.setattr(mc, "CANDIDATES_PATH", tmp_path / "c.jsonl")
        (tmp_path / "c.jsonl").write_text("".join(json.dumps(
            {"uslm_id": f"/{c}", "text": "t"}) + "\n" for c in "abc"))
        monkeypatch.setattr(mc, "judge_many",
                            lambda t, workers=8, endpoint=None: [{"is_mandate": True, "recipient": "congress"}] * len(t))
        out = tmp_path / "o.jsonl"
        n = mc.confirm(verdicts=v, out=out, restrict={"/a", "/c"})
        assert n == 2
        assert {json.loads(l)["uslm_id"] for l in out.read_text().splitlines()} == {"/a", "/c"}


class TestGoldSet:
    """The gold set is an expensive, tracked artifact; guard its shape."""

    def test_gold_set_is_present_and_well_formed(self):
        path = mc.REPO_ROOT / "data/gold/mandate_gold.json"
        if not path.exists():
            pytest.skip("gold set not present")
        rows = json.loads(path.read_text())
        assert len(rows) > 400
        for r in rows:
            assert {"text", "stratum", "gold"} <= set(r)
            assert isinstance(r["gold"]["is_mandate"], bool)
            assert r["gold"]["recipient"] in {"congress", "agency", "public", "other", "none"}

    def test_gold_set_has_both_classes_in_useful_proportion(self):
        path = mc.REPO_ROOT / "data/gold/mandate_gold.json"
        if not path.exists():
            pytest.skip("gold set not present")
        rows = json.loads(path.read_text())
        pos = sum(1 for r in rows if mc.is_mandate(r["gold"]))
        assert 0.3 < pos / len(rows) < 0.7, "gold set should not be wildly imbalanced"
