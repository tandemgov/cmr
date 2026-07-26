"""Find congressional reporting mandates in statutory text.

The House Doc lists 3,297 mandates and is known to be incomplete — that
incompleteness is what this repo measures. This module attacks the inverse
question: *given statutory text, is there a reporting mandate here?*

**Deliberately no fitted classifier.** Training one requires labelling every
uncited provision negative, which bakes the Clerk's blind spots into the model.
Instead this retrieves by similarity to known mandates and lets an LLM judge
each candidate, so nothing is assumed and every decision stays inspectable. A
fitted model becomes reasonable *after* there are judge-verified labels.

Pipeline:

1. **Retrieve.** Embed known mandates and candidate provisions; score each
   candidate by max cosine similarity to any known mandate.
2. **Judge.** A local open-weight model rules on the top candidates.
3. **Calibrate.** Frontier models judge a stratified sample, measuring the
   local judge rather than trusting it. This repo's extraction audit found
   gpt-4o-mini wrong 12% of the time with incoherent reasoning; an unmeasured
   judge is not evidence.

``--pilot`` runs the honest version of step 1+2 on a subset: it holds out a
slice of *known* mandates, buries them among presumed negatives, and measures
whether the pipeline rediscovers them. Ground truth is known, so recall and
precision are real numbers rather than impressions.

Env vars:
  LOCAL_LLM_HOST   OpenAI-compatible endpoint (embeddings + chat)

Usage:
  uv run python pipeline/mandate_classify.py --pilot
  uv run python pipeline/mandate_classify.py --pilot --negatives 20000 --judge-n 400
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import logging
import os
import random
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv()

logger = logging.getLogger("mandate_classify")

USC_XML_DIR = REPO_ROOT / "data/usc/xml"
PROVISIONS_PATH = REPO_ROOT / "data/usc/provisions.jsonl"
PLAW_PROVISIONS_PATH = REPO_ROOT / "data/usc/plaw_provisions.jsonl"
OUT_DIR = REPO_ROOT / "data/usc"

EMBED_MODEL = "granite-embedding"

# Chat goes to the per-model servers on this host (see Endpoint). Deliberately
# NOT the LOCAL_LLM_HOST env var, which points at the :4000 aggregating proxy —
# that is correct for embeddings and wrong for chat.
CHAT_HOST = "spark.tail0e837.ts.net"


@dataclasses.dataclass(frozen=True)
class Endpoint:
    """A model endpoint, with the knob needed to switch its reasoning off.

    Chat must go to the per-model servers, NOT the aggregating proxy on :4000.
    The proxy silently drops ``chat_template_kwargs``, so every request reasons:
    measured 146 completion tokens / 2636 ms via the proxy versus 2 tokens /
    19 ms direct, and reasoning also *cost* recall on this task.

    SGLang (gpt-oss) ignores ``chat_template_kwargs`` and takes
    ``reasoning_effort`` instead; it is the one model that needs reasoning ON,
    with a correspondingly larger token budget.
    """

    name: str
    port: int
    model: str = "x"
    thinking: bool = False
    max_tokens: int = 160
    n_ctx: int = 2048

    @property
    def url(self) -> str:
        return f"http://{CHAT_HOST}:{self.port}/v1/chat/completions"

    def body(self, system: str, user: str) -> dict:
        # Reserve output from the context window: llama.cpp counts max_tokens
        # against n_ctx, so an over-long prompt silently truncates. ~4 chars per
        # token, minus the system prompt, minus a safety margin.
        budget = max(600, (self.n_ctx - self.max_tokens) * 4 - len(system) - 200)
        b = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user[:budget]}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        if self.thinking:
            b["reasoning_effort"] = "low"
        else:
            b["chat_template_kwargs"] = {"enable_thinking": False}
        return b


# Benchmarked against data/gold/mandate_gold.json with context="always".
# nemotron leads on recall (95.6%), gemma-26B on precision (94.7%). Recall is
# what matters for the sweep — a false positive is rejected downstream, a false
# negative is invisible in a 546k-provision corpus — so nemotron sweeps and
# gemma-26B confirms.
ENDPOINTS = {
    "nemotron": Endpoint("nemotron", 8082, n_ctx=16384),
    "gemma-26b": Endpoint("gemma-26b", 8080, n_ctx=2048),
    "gemma-e2b": Endpoint("gemma-e2b", 8081, n_ctx=2048),
    "gpt-oss": Endpoint("gpt-oss", 30000, model="openai/gpt-oss-20b",
                        thinking=True, max_tokens=700, n_ctx=8192),
}

SWEEP_MODEL = "nemotron"    # highest recall
CONFIRM_MODEL = "gemma-26b"  # highest precision

# The embedding endpoint enforces a per-request *token* ceiling, not a row
# count: batches of 64 full-length provisions 500, while 128 truncated to 512
# characters succeed. Truncate first, then batch.
EMBED_CHARS = 512
EMBED_BATCH = 128
EMBED_WORKERS = 8

_NS = "{http://xml.house.gov/schemas/uslm/1.0}"
_ADDRESSABLE = {
    f"{_NS}section", f"{_NS}subsection", f"{_NS}paragraph",
    f"{_NS}subparagraph", f"{_NS}clause",
}
_WS_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# Local model client
# ─────────────────────────────────────────────────────────────────────────────


def _host() -> str:
    h = os.environ.get("LOCAL_LLM_HOST")
    if not h:
        sys.exit("LOCAL_LLM_HOST not set in environment (.env)")
    return h.rstrip("/")


def _post(path: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{_host()}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts, batched and parallelised against the local host."""
    batches = [
        [t[:EMBED_CHARS] for t in texts[i:i + EMBED_BATCH]]
        for i in range(0, len(texts), EMBED_BATCH)
    ]

    def one(batch: list[str]) -> list[list[float]]:
        for attempt in range(3):
            try:
                d = _post("/v1/embeddings", {"model": EMBED_MODEL, "input": batch})
                return [r["embedding"] for r in d["data"]]
            except urllib.error.HTTPError:
                if attempt == 2:
                    raise
                # Halve the batch and retry: a long row can push one request
                # over the token ceiling even at EMBED_CHARS truncation.
                mid = max(1, len(batch) // 2)
                return one(batch[:mid]) + one(batch[mid:])
        raise RuntimeError("unreachable")

    out: list[list[float]] = []
    with cf.ThreadPoolExecutor(EMBED_WORKERS) as ex:
        for i, vecs in enumerate(ex.map(one, batches), 1):
            out.extend(vecs)
            if i % 20 == 0 or i == len(batches):
                logger.info("embedded %d/%d batches", i, len(batches))
    return out


JUDGE_SYSTEM = """You decide whether a provision of federal statutory text creates a REPORTING REQUIREMENT DIRECTED TO CONGRESS.

Answer YES only if the text obligates some federal officer, agency, commission, or entity to deliver a report, study, notification, certification, plan, or other information TO Congress — that includes a chamber, a committee, a subcommittee, the Speaker, the President pro tempore, or the Comptroller General acting for Congress.

Answer NO if:
- the recipient is not Congress (an agency head, a court, a State, the public, a registry)
- the text merely defines terms, authorizes appropriations, grants rulemaking authority, or establishes a body without requiring it to report to Congress
- it only cross-references a reporting duty created elsewhere

Reply with STRICT JSON and nothing else:
{"is_mandate": true|false, "recipient": "congress"|"agency"|"public"|"other"|"none", "reporting_entity": "<who must report, or empty>", "deadline": "<deadline or trigger, or empty>", "frequency": "<one-time|annual|biennial|quarterly|event-driven|other|unknown>", "confidence": "high"|"medium"|"low"}"""


def _extract_json(s: str) -> dict | None:
    m = re.search(r"\{.*\}", s or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def judge_one(text: str, endpoint: str | Endpoint = SWEEP_MODEL) -> dict | None:
    """Ask one model to rule on one provision.

    The verdict lives in ``message.content``; any chain-of-thought goes to
    ``message.reasoning_content``. With thinking off the answer is ~60 tokens.
    """
    ep = ENDPOINTS[endpoint] if isinstance(endpoint, str) else endpoint
    try:
        req = urllib.request.Request(
            ep.url,
            data=json.dumps(ep.body(JUDGE_SYSTEM, text)).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            d = json.load(resp)
    except Exception as e:
        logger.debug("judge failed (%s): %s", ep.name, e)
        return None
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    return _extract_json(msg.get("content") or "")


def judge_many(texts: list[str], workers: int = 8,
               endpoint: str | Endpoint = SWEEP_MODEL) -> list[dict | None]:
    """Judge many provisions concurrently.

    Concurrency past ~8 buys nothing — the host is saturated, and 8/16/32
    workers all measured ~0.19 items/sec through the proxy. The win came from
    switching off reasoning, not from more parallelism.
    """
    out: list[dict | None] = [None] * len(texts)
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(judge_one, t, endpoint): i for i, t in enumerate(texts)}
        done = 0
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
            done += 1
            if done % 50 == 0 or done == len(texts):
                logger.info("judged %d/%d", done, len(texts))
    return out


def is_mandate(verdict: dict | None) -> bool:
    """A verdict counts only if it is a duty owed to Congress specifically."""
    return bool(verdict and verdict.get("is_mandate") and verdict.get("recipient") == "congress")


# ─────────────────────────────────────────────────────────────────────────────
# Corpora
# ─────────────────────────────────────────────────────────────────────────────


def _text_of(elem: ET.Element) -> str:
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        if e.tag == f"{_NS}notes":
            return
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
            if c.tag in _ADDRESSABLE or c.tag in (f"{_NS}num", f"{_NS}heading", f"{_NS}content"):
                parts.append(" ")
            if c.tail:
                parts.append(c.tail)

    walk(elem)
    return _WS_RE.sub(" ", "".join(parts)).strip()


# Elements that carry a parent's obligation down to its enumerated children.
_CONTEXT_TAGS = (f"{_NS}num", f"{_NS}heading", f"{_NS}chapeau")


def _own_context(elem: ET.Element) -> str:
    """The num / heading / chapeau this element contributes to its children."""
    parts = [_text_of(c) for c in elem if c.tag in _CONTEXT_TAGS]
    return " ".join(p for p in parts if p)


_MODAL_RE = re.compile(r"(?i)\b(shall|must|is directed to|are directed to|is required to)\b")

# Candidate gate. Deliberately recall-first and recipient-only: a provision that
# names no congressional recipient cannot be a mandate to Congress, but any
# attempt to also require a duty verb loses real mandates, because statutory
# drafting separates the modal from the verb ("shall, as soon as practicable,
# ... prepare and transmit to the Congress").
#
# Measured against data/gold/mandate_gold.json: 99.1% recall (113/114 positives
# in a random 22-title sample), keeping 37,141 of 546,625 provisions (6.8%).
# Adding "duty AND delivery" terms drops recall to ~82-85% for no useful gain.
# Embedding similarity was tried as an alternative and is far worse — AUC 0.71
# on hard negatives, discarding almost nothing at usable recall.
_RECIPIENT_RE = re.compile(
    r"(?i)\b("
    r"congress|congressional|senate|house of representatives|"
    r"committee on|committees? of|speaker of the house|"
    r"president pro tempore|comptroller general"
    r")\b"
)


def passes_gate(text: str) -> bool:
    """True if the text names a plausible congressional recipient."""
    return bool(_RECIPIENT_RE.search(text or ""))


def iter_provisions(root: ET.Element, min_len: int = 120, max_len: int = 4000,
                    context: str = "always"):
    """Yield ``(identifier, text)`` for every addressable node.

    USLM splits an obligation across levels: the modal sits in the parent's
    chapeau ("MACPAC shall—") while the duty verb sits in the child ("(D) ...
    submit a report to Congress"). Extracting the child alone yields text with
    the obligation removed, which defeats a modal-based filter and misleads an
    LLM judge — neither can see who is obligated.

    Repairing this is the largest single accuracy gain measured on this task.
    Against a 298-row adjudicated gold set (114 positives), recall rose from
    82.5% to 95.6% (nemotron) and from 66.7% to 93.9% (gemma-26B), with
    precision holding or improving in both cases.

    ``context`` controls the repair:

    - ``"always"`` (default) — prepend ancestor num/heading/chapeau to every
      node. The obvious worry, that an inherited heading like "Reports to
      Congress" would make unrelated children look like mandates, was measured
      and did not materialise: gemma-26B's precision went *up* (93.8% → 94.7%).
    - ``"conditional"`` — prepend only when the node's own text has no modal
      verb, so complete provisions are left untouched. Identical to ``always``
      for nemotron and slightly worse for gemma-26B, but it rewrites 44% of
      nodes rather than all of them; prefer it if heading contamination turns
      out to matter on a different corpus.
    - ``"never"`` — raw node text, i.e. the unrepaired split.

    ``min_len``/``max_len`` apply to the node's own text, so adding context
    cannot silently drop a long provision from the corpus.
    """
    if context not in {"conditional", "always", "never"}:
        raise ValueError(f"context must be conditional|always|never, got {context!r}")

    def walk(elem: ET.Element, prefix: str):
        ident = elem.get("identifier")
        if elem.tag in _ADDRESSABLE and ident:
            body = _text_of(elem)
            if min_len < len(body) < max_len:
                use = prefix and (
                    context == "always"
                    or (context == "conditional" and not _MODAL_RE.search(body))
                )
                yield ident, (f"{prefix} {body}".strip() if use else body)
        child_prefix = f"{prefix} {_own_context(elem)}".strip()
        for c in elem:
            if c.tag in _ADDRESSABLE:
                yield from walk(c, child_prefix)

    for sec in root.iter(f"{_NS}section"):
        yield from walk(sec, "")


def load_known_mandates() -> list[dict]:
    """Gold positives: mandates whose statutory text we resolved.

    Draws from both routes — codified US Code provisions and, for the
    uncodified ~28%, public-law text.
    """
    out = []
    if PROVISIONS_PATH.exists():
        for line in PROVISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            for p in r["provisions"]:
                if p.get("resolved") and p.get("text"):
                    out.append({
                        "mandate_id": r["mandate_id"], "source": "usc",
                        "uslm_id": p.get("uslm_id", ""), "text": p["text"],
                        "reporting_entity": r["reporting_entity"],
                        "when_expected": r["when_expected"],
                    })
                    break
    if PLAW_PROVISIONS_PATH.exists():
        for line in PLAW_PROVISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            for p in r["plaw_provisions"]:
                if p.get("resolved") and p.get("text"):
                    out.append({
                        "mandate_id": r["mandate_id"], "source": "plaw",
                        "uslm_id": p.get("package_id", ""), "text": p["text"],
                        "reporting_entity": r["reporting_entity"],
                        "when_expected": r["when_expected"],
                    })
                    break
    return out


def sample_uncited_provisions(exclude: set[str], n: int, seed: int = 7,
                              gated: bool = True) -> list[dict]:
    """Randomly sample US Code provisions that no House Doc mandate cites.

    These are *presumed* negatives, not verified ones — the whole premise of
    this repo is that the Clerk's list misses real mandates, so some fraction
    of this pool is positive. That is exactly what the judge is for.

    Text comes from ``iter_provisions``, so candidates carry their ancestor
    chapeau; sampling raw node text would reintroduce the split obligation the
    rest of the module exists to repair. With ``gated`` (the default) only
    provisions naming a congressional recipient are sampled, matching the
    population a real sweep would see.
    """
    rng = random.Random(seed)
    pool: list[dict] = []
    files = sorted(USC_XML_DIR.glob("usc*.xml"))
    if not files:
        raise SystemExit(f"{USC_XML_DIR} is empty — run pipeline/statute_fetch.py --fetch-only")
    for f in files:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for ident, text in iter_provisions(root):
            if ident in exclude:
                continue
            if gated and not passes_gate(text):
                continue
            row = {"uslm_id": ident, "text": text}
            # Reservoir-sample so memory stays flat over a 665 MB corpus.
            if len(pool) < n:
                pool.append(row)
            else:
                j = rng.randrange(len(pool) + 1)
                if j < n:
                    pool[j] = row
        logger.info("scanned %s (pool %d)", f.name, len(pool))
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────


def max_similarity(cands: list[list[float]], known: list[list[float]]) -> list[float]:
    """Max cosine similarity of each candidate to any known mandate.

    Vectors are unit-normalised, so cosine is a plain dot product and the whole
    comparison is one matrix multiply. Done row-by-row in Python this is ~17
    billion operations for a realistic pilot; numpy makes it seconds. Candidates
    are chunked so peak memory stays bounded by ``chunk × len(known)`` rather
    than the full pairwise matrix.
    """
    import numpy as np

    k = np.asarray(known, dtype=np.float32)
    k /= np.linalg.norm(k, axis=1, keepdims=True).clip(min=1e-12)
    c = np.asarray(cands, dtype=np.float32)
    c /= np.linalg.norm(c, axis=1, keepdims=True).clip(min=1e-12)

    out = np.empty(len(c), dtype=np.float32)
    chunk = 2048
    for i in range(0, len(c), chunk):
        out[i:i + chunk] = (c[i:i + chunk] @ k.T).max(axis=1)
    return out.tolist()


CANDIDATES_PATH = OUT_DIR / "sweep_candidates.jsonl"
VERDICTS_PATH = OUT_DIR / "sweep_verdicts.jsonl"


def build_candidates(out: Path = CANDIDATES_PATH) -> int:
    """Write every gate-passing provision in the corpus to a candidate file.

    Split from the judging pass so a resumed sweep does not re-parse 665 MB of
    XML (~5 minutes) just to work out what it already did.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    n = seen = 0
    with open(out, "w") as fh:
        for f in sorted(USC_XML_DIR.glob("usc*.xml")):
            try:
                root = ET.parse(f).getroot()
            except ET.ParseError:
                logger.warning("unparseable: %s", f.name)
                continue
            for ident, text in iter_provisions(root):
                seen += 1
                if not passes_gate(text):
                    continue
                fh.write(json.dumps({"uslm_id": ident, "text": text}) + "\n")
                n += 1
            logger.info("%-14s candidates %6d / %7d scanned", f.name, n, seen)
    logger.info("Wrote %d candidates (%.1f%% of %d provisions) → %s",
                n, n / seen * 100 if seen else 0, seen, out)
    return n


def _done_ids(path: Path) -> set[str]:
    """Identifiers already judged, so a resumed run skips them."""
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            done.add(json.loads(line)["uslm_id"])
        except (json.JSONDecodeError, KeyError):
            continue  # a torn final line from a killed run
    return done


def sweep(candidates: Path = CANDIDATES_PATH, out: Path = VERDICTS_PATH,
          endpoint: str = SWEEP_MODEL, limit: int | None = None,
          workers: int = 8, chunk: int = 200, seed: int = 17) -> int:
    """Judge every candidate, appending as it goes.

    Resumable by design: this is a ~13-hour run at the host's ~1.2 items/sec, so
    it appends each chunk and skips identifiers already present on restart.
    Verdicts are written for negatives too — a later confirm pass or a changed
    threshold should not require re-judging the corpus.

    Candidates are shuffled under a fixed seed, so any partial run is an
    unbiased sample of the corpus rather than the first N titles. That makes an
    interrupted sweep still statistically usable, and makes the running flag
    rate an estimate of the corpus rate instead of an artefact of title order.
    """
    if not candidates.exists():
        raise SystemExit(f"{candidates} not found — run with --build-candidates first")
    rows = [json.loads(l) for l in candidates.read_text().splitlines() if l.strip()]
    random.Random(seed).shuffle(rows)
    done = _done_ids(out)
    todo = [r for r in rows if r["uslm_id"] not in done]
    if limit:
        todo = todo[:limit]
    logger.info("candidates %d | already judged %d | to judge %d", len(rows), len(done), len(todo))

    written = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as fh:
        for i in range(0, len(todo), chunk):
            batch = todo[i:i + chunk]
            verdicts = judge_many([b["text"] for b in batch], workers=workers, endpoint=endpoint)
            for b, v in zip(batch, verdicts):
                fh.write(json.dumps({
                    "uslm_id": b["uslm_id"], "model": endpoint,
                    "verdict": v, "is_mandate": is_mandate(v),
                }) + "\n")
            fh.flush()
            written += len(batch)
            hits = sum(1 for v in verdicts if is_mandate(v))
            logger.info("%d/%d judged (+%d flagged this chunk)", written, len(todo), hits)
    return written


NOVELTY_PATH = OUT_DIR / "sweep_novelty.jsonl"

# Frequency label for conditional duties. Held out of the headline because its
# failures are a category error rather than noise: provisions where Congress is
# the *beneficiary* ("to assist Congress in evaluating") or a *requester*
# ("available upon written request of any committee") rather than the recipient
# of a required report. Explicit delivery language appears in 82-88% of the
# annual/biennial sets but only 50% of these.
EVENT_DRIVEN = "event-driven"


def confirm_scope(scope: str, novelty: Path | None = None) -> set[str] | None:
    """Identifiers to confirm, per scope.

    ``all`` — every flagged provision.
    ``novel`` — those not already on the Clerk's list, and recurring.
    ``novel-periodic`` — as ``novel``, minus event-driven; the defensible core.

    ``novelty`` resolves at call time, not import time: binding the module
    constant as a default would freeze it, so pointing the module at a
    different file would silently have no effect.
    """
    if scope == "all":
        return None
    novelty = novelty or NOVELTY_PATH
    if not novelty.exists():
        raise SystemExit(f"{novelty} missing — run pipeline/novelty.py first")
    rows = [json.loads(l) for l in novelty.read_text().splitlines() if l.strip()]
    keep = [r for r in rows if r["novelty"] == "novel" and r["standing"]]
    if scope == "novel-periodic":
        keep = [r for r in keep if r["frequency"] != EVENT_DRIVEN]
    return {r["uslm_id"] for r in keep}


def confirm(verdicts: Path = VERDICTS_PATH, out: Path | None = None,
            endpoint: str = CONFIRM_MODEL, workers: int = 8, chunk: int = 200,
            restrict: set[str] | None = None) -> int:
    """Re-judge the sweep's positives with the higher-precision model.

    The sweep model is chosen for recall and over-flags by design; this pass
    trades that back for precision. ``restrict`` narrows it to a subset — the
    full flagged set is ~29.5k rows (~6.6h), while the novel periodic core is
    ~6.1k (~1.4h) and is the part worth defending.
    """
    out = out or verdicts.with_name("sweep_confirmed.jsonl")
    rows = [json.loads(l) for l in verdicts.read_text().splitlines() if l.strip()]
    flagged = [r for r in rows if r.get("is_mandate")]
    if restrict is not None:
        flagged = [r for r in flagged if r["uslm_id"] in restrict]
    done = _done_ids(out)
    todo = [r for r in flagged if r["uslm_id"] not in done]
    logger.info("flagged %d | already confirmed %d | to confirm %d", len(flagged), len(done), len(todo))

    text_of = {}
    if CANDIDATES_PATH.exists():
        for line in CANDIDATES_PATH.read_text().splitlines():
            if line.strip():
                c = json.loads(line)
                text_of[c["uslm_id"]] = c["text"]

    written = 0
    with open(out, "a") as fh:
        for i in range(0, len(todo), chunk):
            batch = [r for r in todo[i:i + chunk] if r["uslm_id"] in text_of]
            vs = judge_many([text_of[r["uslm_id"]] for r in batch], workers=workers, endpoint=endpoint)
            for r, v in zip(batch, vs):
                fh.write(json.dumps({
                    "uslm_id": r["uslm_id"], "model": endpoint,
                    "verdict": v, "is_mandate": is_mandate(v),
                }) + "\n")
            fh.flush()
            written += len(batch)
            logger.info("%d/%d confirmed", written, len(todo))
    return written


def recall_at_k(ranked_labels: list[int], n_positives: int) -> dict[int, float]:
    """Share of held-out positives appearing in the top-k of a ranked list."""
    out = {}
    for k in (100, 250, 500, 1000, 2500, 5000):
        if k > len(ranked_labels):
            break
        out[k] = sum(ranked_labels[:k]) / n_positives if n_positives else 0.0
    return out


def run_pilot(args) -> dict:
    """Hold out known mandates, bury them among presumed negatives, measure recovery.

    This is the honest version of the question "does this work?": the held-out
    rows are mandates the Clerk *did* list and the retriever was never shown,
    so their rank among presumed negatives is a real recall measurement.
    """
    rng = random.Random(args.seed)

    known_all = load_known_mandates()
    if not known_all:
        raise SystemExit("No resolved provisions — run statute_fetch.py and plaw_fetch.py first")
    rng.shuffle(known_all)
    n_hold = int(len(known_all) * args.holdout)
    holdout, known = known_all[:n_hold], known_all[n_hold:]
    logger.info("known mandates: %d  (reference %d / held out %d)", len(known_all), len(known), len(holdout))

    exclude = {k["uslm_id"] for k in known_all if k["uslm_id"]}
    negatives = sample_uncited_provisions(exclude, args.negatives, seed=args.seed)
    logger.info("presumed-negative pool: %d", len(negatives))

    logger.info("embedding reference mandates…")
    known_vecs = embed([k["text"] for k in known])
    logger.info("embedding candidates…")
    cands = [{"label": 1, **h} for h in holdout] + [{"label": 0, **n} for n in negatives]
    rng.shuffle(cands)
    cand_vecs = embed([c["text"] for c in cands])

    logger.info("scoring similarity…")
    sims = max_similarity(cand_vecs, known_vecs)
    for c, s in zip(cands, sims):
        c["similarity"] = s
    cands.sort(key=lambda c: -c["similarity"])

    labels = [c["label"] for c in cands]
    rec = recall_at_k(labels, len(holdout))

    # Judge the top slice, plus a random tail slice as a control — if the judge
    # says yes just as often at the bottom of the ranking, retrieval adds nothing.
    top = cands[:args.judge_n]
    tail = rng.sample(cands[len(cands) // 2:], min(args.judge_n // 3, len(cands) // 2))
    logger.info("judging %d top + %d control candidates…", len(top), len(tail))
    top_v = judge_many([c["text"] for c in top])
    tail_v = judge_many([c["text"] for c in tail])

    def score(cand_slice, verdicts):
        ok = [(c, v) for c, v in zip(cand_slice, verdicts) if v]
        yes = [(c, v) for c, v in ok if v.get("is_mandate") and v.get("recipient") == "congress"]
        held = [c for c, _ in ok if c["label"] == 1]
        held_yes = [c for c, v in yes if c["label"] == 1]
        return {
            "n": len(cand_slice), "parsed": len(ok),
            "judged_mandate": len(yes),
            "held_out_positives_present": len(held),
            "held_out_positives_confirmed": len(held_yes),
            "recall_on_held_out": (len(held_yes) / len(held)) if held else None,
            "flag_rate": (len(yes) / len(ok)) if ok else None,
        }

    results = {
        "known_mandates": len(known_all),
        "reference_set": len(known),
        "held_out": len(holdout),
        "negative_pool": len(negatives),
        "retrieval_recall_at_k": rec,
        "top_slice": score(top, top_v),
        "control_slice": score(tail, tail_v),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        **results,
        "top_examples": [
            {k: c[k] for k in ("uslm_id", "similarity", "label")} | {"verdict": v, "text": c["text"][:300]}
            for c, v in list(zip(top, top_v))[:40]
        ],
    }, indent=1))

    print(f"\nKnown mandates {len(known_all)}  →  reference {len(known)} / held out {len(holdout)}")
    print(f"Presumed-negative pool: {len(negatives)}\n")
    print("RETRIEVAL — share of held-out mandates in the top k:")
    for k, v in rec.items():
        print(f"  recall@{k:<5} {v*100:5.1f}%")
    print("\nJUDGE:")
    for name, s in (("top slice", results["top_slice"]), ("control (random, lower half)", results["control_slice"])):
        fr = s["flag_rate"]
        rc = s["recall_on_held_out"]
        print(f"  {name}:")
        print(f"    parsed {s['parsed']}/{s['n']}   flagged as congressional mandate: {s['judged_mandate']}"
              f"  ({fr*100:.1f}%)" if fr is not None else "")
        print(f"    held-out positives present {s['held_out_positives_present']}, "
              f"confirmed {s['held_out_positives_confirmed']}"
              + (f"  (recall {rc*100:.1f}%)" if rc is not None else ""))
    print(f"\nWrote {args.out}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-candidates", action="store_true",
                    help="Scan the corpus and write every gate-passing provision")
    ap.add_argument("--sweep", action="store_true",
                    help="Judge all candidates (resumable; ~10h for the full corpus)")
    ap.add_argument("--confirm", action="store_true",
                    help="Re-judge the sweep's positives with the precision model")
    ap.add_argument("--confirm-scope", choices=("all", "novel", "novel-periodic"),
                    default="novel-periodic",
                    help="Which flagged provisions to confirm (default: the defensible core)")
    ap.add_argument("--endpoint", default=None, help="Override the judging model")
    ap.add_argument("--limit", type=int, help="Judge only the first N candidates")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--pilot", action="store_true", help="Held-out validation on a subset")
    ap.add_argument("--negatives", type=int, default=8000, help="Presumed-negative pool size")
    ap.add_argument("--holdout", type=float, default=0.2, help="Fraction of known mandates held out")
    ap.add_argument("--judge-n", type=int, default=300, help="Candidates to send to the local judge")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-o", "--out", type=Path, default=OUT_DIR / "pilot_results.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.build_candidates:
        build_candidates()
    if args.sweep:
        sweep(endpoint=args.endpoint or SWEEP_MODEL, limit=args.limit, workers=args.workers)
    if args.confirm:
        confirm(endpoint=args.endpoint or CONFIRM_MODEL, workers=args.workers,
                restrict=confirm_scope(args.confirm_scope))
    if args.pilot:
        run_pilot(args)
    if not any((args.build_candidates, args.sweep, args.confirm, args.pilot)):
        ap.error("pick one of --build-candidates / --sweep / --confirm / --pilot")


if __name__ == "__main__":
    main()
