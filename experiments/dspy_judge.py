"""Does an optimizer beat the handwritten JUDGE_SYSTEM prompt?

`pipeline/mandate_classify.py` runs a local open-weight judge (nemotron) over
half a million statutory provisions, steered by a prompt that was written by
hand and tightened by audit. This experiment asks whether DSPy's MIPROv2 can
do better, holding everything else fixed: same model, same endpoint, same
thinking-off setting, same gold rows, same truncation.

The comparison is deliberately conservative. MIPROv2 is *seeded* with the
handwritten instruction rather than starting from a stub, so the question is
"can an optimizer improve a good prompt", not "can it rediscover the domain
knowledge from scratch". A frontier model proposes candidate instructions;
the local model only ever executes them.

Ground truth is `data/gold/mandate_gold.json` — 467 rows, 214 positive,
stratified over known Clerk mandates, gate-passing and gate-failing
provisions, and hard negatives that name Congress without creating a duty.
Rows are split once with a fixed seed; the test slice is scored exactly twice,
once per arm.

Usage:
  uv run python experiments/dspy_judge.py baseline   # handwritten prompt on test
  uv run python experiments/dspy_judge.py zeroshot   # DSPy, seeded, unoptimized
  uv run python experiments/dspy_judge.py optimize   # compile against val
  uv run python experiments/dspy_judge.py test       # compiled program on test
  uv run python experiments/dspy_judge.py report     # table up whatever exists
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "pipeline"))

import dspy  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import mandate_classify as mc  # noqa: E402

load_dotenv()

logger = logging.getLogger("dspy_judge")

GOLD_PATH = REPO_ROOT / "data/gold/mandate_gold.json"

# The fp=0.4 compiled instruction with its schema defects corrected by hand:
# MIPROv2 invented `recipient` values outside the enum, dropped "semiannual"
# and "other" from the cadence list, and replaced the empty-string convention
# for reporting_entity/deadline with a "not specified" sentinel. DSPy's adapter
# clamped all of that, which is why it survived scoring -- but `judge_one` in
# the production path parses raw JSON with no schema, so it would not.
FIXED_PROMPT_PATH = REPO_ROOT / "experiments/optimized_judge_prompt.txt"

OUT_DIR = REPO_ROOT / "experiments/dspy_output"
RESULTS_PATH = OUT_DIR / "results.json"


def _tag(fp_credit: float) -> str:
    """Artifacts are named for the objective that produced them.

    Two runs at different fp_credit settings are different experiments and must
    not overwrite each other's compiled prompt.
    """
    return f"fp{int(round(fp_credit * 100)):03d}"


def compiled_path(fp_credit: float) -> Path:
    return OUT_DIR / f"compiled_judge_{_tag(fp_credit)}.json"


def prompt_dump_path(fp_credit: float) -> Path:
    return OUT_DIR / f"compiled_prompt_{_tag(fp_credit)}.txt"

SPLIT_SEED = 0
SPLIT_FRACTIONS = (0.40, 0.25, 0.35)  # train / val / test

# Both arms see the same truncated text. 12 of 467 gold rows exceed this;
# production truncates far later (~62k chars), but a few-shot prompt carries
# several provisions at once and nemotron's n_ctx is 16384 tokens.
PROVISION_CHARS = 6000

# Partial credit for a false positive; a false negative always scores zero.
#
# This dial decides the experiment's answer, so it is a flag rather than a
# constant. A false positive costs one confirm-pass call and gets dropped at
# candidate selection; a false negative sits unevaluated forever in a
# 583k-provision corpus. That asymmetry is severe, so the default is 0.9.
#
# Measured on the held-out test slice, the arms cross over at ~0.83: below it
# the handwritten prompt wins, above it the DSPy arm does. A first run at 0.4
# therefore rejected the DSPy arm for making exactly the trade we want -- it
# buys the last 2 false negatives for 12 extra false positives, 6 FPs per
# recovered FN. Reported metrics below stay unweighted and separate.
FP_CREDIT = 0.9

# The whole point of the experiment is the cheap local model. A frontier model
# only proposes instruction candidates -- it never judges a provision.
TASK_MODEL = "nemotron"
PROPOSER_MODEL = "anthropic/claude-sonnet-4-5"

NUM_THREADS = 8


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────


def load_gold() -> list[dspy.Example]:
    rows = json.loads(GOLD_PATH.read_text())
    out = []
    for r in rows:
        g = r["gold"]
        out.append(
            dspy.Example(
                provision=r["text"][:PROVISION_CHARS],
                is_mandate=bool(g["is_mandate"]),
                recipient=g["recipient"],
                # Carried for analysis, not scored.
                uslm_id=r.get("id", ""),
                stratum=r["stratum"],
                gold_congressional=mc.is_mandate(g),
            ).with_inputs("provision")
        )
    return out


def split(examples: list[dspy.Example]) -> dict[str, list[dspy.Example]]:
    """Stratified on (stratum, label) so every slice keeps the hard negatives.

    A plain random split would let the 30-row `no_congress` stratum land
    unevenly and make the arms incomparable across slices.
    """
    buckets: dict[tuple[str, bool], list[dspy.Example]] = {}
    for ex in examples:
        buckets.setdefault((ex.stratum, ex.gold_congressional), []).append(ex)

    rng = random.Random(SPLIT_SEED)
    parts: dict[str, list[dspy.Example]] = {"train": [], "val": [], "test": []}
    f_train, f_val, _ = SPLIT_FRACTIONS
    for key in sorted(buckets, key=lambda k: (k[0], k[1])):
        rows = buckets[key][:]
        rng.shuffle(rows)
        a = round(len(rows) * f_train)
        b = a + round(len(rows) * f_val)
        parts["train"] += rows[:a]
        parts["val"] += rows[a:b]
        parts["test"] += rows[b:]
    for v in parts.values():
        rng.shuffle(v)
    return parts


# ─────────────────────────────────────────────────────────────────────────────
# Program
# ─────────────────────────────────────────────────────────────────────────────

Recipient = Literal["congress", "agency", "public", "other", "none"]
Frequency = Literal["one-time", "annual", "biennial", "quarterly", "semiannual",
                    "monthly", "event-driven", "other", "unknown"]
Confidence = Literal["high", "medium", "low"]

# The handwritten prompt, minus its JSON-format tail -- DSPy's adapter owns
# output formatting. Everything load-bearing (the recipient test, the
# frequency rules) is preserved verbatim so MIPROv2 starts from parity.
SEED_INSTRUCTION = mc.JUDGE_SYSTEM.split("Reply with STRICT JSON")[0].strip()


class JudgeProvision(dspy.Signature):
    provision: str = dspy.InputField(
        desc="Federal statutory text, with ancestor num/heading/chapeau prepended."
    )
    is_mandate: bool = dspy.OutputField(
        desc="True only if the text obligates someone to deliver information to Congress."
    )
    recipient: Recipient = dspy.OutputField(desc="Who receives the required information.")
    reporting_entity: str = dspy.OutputField(desc="Who must report; empty if none.")
    deadline: str = dspy.OutputField(desc="Stated deadline or trigger; empty if none.")
    frequency: Frequency = dspy.OutputField(desc="Cadence, decided from the text not the topic.")
    confidence: Confidence = dspy.OutputField()


JudgeProvision.__doc__ = SEED_INSTRUCTION


def build_program(instruction: str | None = None) -> dspy.Module:
    # Predict, not ChainOfThought: the RUNBOOK measured reasoning *costing*
    # recall on this task, and thinking is switched off at the endpoint.
    program = dspy.Predict(JudgeProvision)
    if instruction:
        program.signature = program.signature.with_instructions(instruction)
    return program


def task_lm() -> dspy.LM:
    ep = mc.ENDPOINTS[TASK_MODEL]
    return dspy.LM(
        f"openai/{ep.model}",
        api_base=f"http://{mc.CHAT_HOST}:{ep.port}/v1",
        api_key="none",
        temperature=0.0,
        max_tokens=400,
        num_retries=3,
        # On-disk cache: MIPROv2 re-evaluates the surviving candidates against
        # the same val rows repeatedly, and the host is the bottleneck.
        cache=True,
        # Verified: 3 completion tokens with this set, 144 without.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )


def proposer_lm() -> dspy.LM:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set; needed to propose instructions")
    return dspy.LM(PROPOSER_MODEL, temperature=1.0, max_tokens=4000)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────


def pred_positive(pred) -> bool:
    """Mirror `mc.is_mandate`: a duty owed to Congress specifically."""
    try:
        return bool(getattr(pred, "is_mandate", False)) and \
            str(getattr(pred, "recipient", "")) == "congress"
    except Exception:
        return False


def optimizer_metric(example, pred, trace=None) -> float:
    """Cost-sensitive: a miss scores zero, a false alarm keeps partial credit."""
    got, want = pred_positive(pred), bool(example.gold_congressional)
    if got == want:
        return 1.0
    return FP_CREDIT if got else 0.0


def score(examples: list[dspy.Example], predictions: list[bool]) -> dict:
    tp = sum(1 for e, p in zip(examples, predictions) if p and e.gold_congressional)
    fp = sum(1 for e, p in zip(examples, predictions) if p and not e.gold_congressional)
    fn = sum(1 for e, p in zip(examples, predictions) if not p and e.gold_congressional)
    tn = sum(1 for e, p in zip(examples, predictions) if not p and not e.gold_congressional)
    rec = tp / (tp + fn) if tp + fn else 0.0
    prec = tp / (tp + fp) if tp + fp else 0.0
    f2 = (5 * prec * rec / (4 * prec + rec)) if prec + rec else 0.0
    by_stratum = {}
    for e, p in zip(examples, predictions):
        s = by_stratum.setdefault(e.stratum, {"n": 0, "correct": 0})
        s["n"] += 1
        s["correct"] += int(p == e.gold_congressional)
    return {
        "n": len(examples), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "recall": rec, "precision": prec, "f2": f2,
        "accuracy": (tp + tn) / len(examples) if examples else 0.0,
        "by_stratum": by_stratum,
    }


def print_score(label: str, s: dict) -> None:
    print(f"\n{label}  (n={s['n']})")
    print(f"  recall     {s['recall'] * 100:5.1f}%   ({s['tp']}/{s['tp'] + s['fn']})")
    print(f"  precision  {s['precision'] * 100:5.1f}%   ({s['tp']}/{s['tp'] + s['fp'] or 1})")
    print(f"  F2         {s['f2'] * 100:5.1f}%")
    print(f"  accuracy   {s['accuracy'] * 100:5.1f}%   (tp {s['tp']} fp {s['fp']} fn {s['fn']} tn {s['tn']})")
    for k in sorted(s["by_stratum"]):
        v = s["by_stratum"][k]
        print(f"    {k:18s} {v['correct']:3d}/{v['n']:<3d} {v['correct'] / v['n'] * 100:5.1f}%")


def save_result(key: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_results = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else {}
    all_results[key] = payload
    RESULTS_PATH.write_text(json.dumps(all_results, allow_nan=False, indent=2))


def save_predictions(arm: str, examples: list[dspy.Example], preds: list[bool]) -> None:
    """Per-row verdicts, so two arms can be compared where they disagree.

    Aggregate confusion counts cannot distinguish a systematic recall hole from
    two coin-flips near the decision boundary; the disagreeing rows can.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"preds_{arm}.jsonl"
    with path.open("w") as fh:
        for e, p in zip(examples, preds):
            fh.write(json.dumps({
                "uslm_id": e.uslm_id, "stratum": e.stratum,
                "gold": bool(e.gold_congressional), "pred": bool(p),
                "provision": e.provision[:1200],
            }) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Arms
# ─────────────────────────────────────────────────────────────────────────────


def run_baseline(examples: list[dspy.Example], slice_name: str, arm: str = "") -> dict:
    """The production path, untouched: JUDGE_SYSTEM through judge_many."""
    verdicts = mc.judge_many([e.provision for e in examples],
                             workers=NUM_THREADS, endpoint=TASK_MODEL)
    preds = [mc.is_mandate(v) for v in verdicts]
    unparsed = sum(1 for v in verdicts if v is None)
    s = score(examples, preds)
    s["unparsed"] = unparsed
    print_score(f"baseline (handwritten JUDGE_SYSTEM) on {slice_name}", s)
    if unparsed:
        print(f"  {unparsed} responses failed to parse (counted as negative)")
    # An unparsed response counts as negative, so a degrading host renders as a plausible recall collapse rather than an error. RUNBOOK section 12.
    if unparsed > 0.02 * len(examples):
        raise SystemExit(
            f"{unparsed}/{len(examples)} responses unparsed -- refusing to save a "
            f"score built on them. Check the endpoint (a bare request reasons and "
            f"returns empty content) and re-run."
        )
    if arm:
        save_predictions(arm, examples, preds)
    return s


def run_dspy(program: dspy.Module, examples: list[dspy.Example], label: str,
             arm: str = "") -> dict:
    evaluator = dspy.Evaluate(
        devset=examples, metric=optimizer_metric, num_threads=NUM_THREADS,
        display_progress=True, provide_traceback=False,
        max_errors=len(examples),
    )
    result = evaluator(program)
    outputs = result.results
    preds, failed = [], 0
    for _ex, pred, _sc in outputs:
        if pred is None or isinstance(pred, Exception):
            preds.append(False)
            failed += 1
        else:
            preds.append(pred_positive(pred))
    s = score([o[0] for o in outputs], preds)
    s["unparsed"] = failed
    print_score(label, s)
    if failed:
        print(f"  {failed} predictions errored (counted as negative)")
    if arm:
        # dspy.Evaluate may reorder; take the examples back off its results.
        save_predictions(arm, [o[0] for o in outputs], preds)
    return s


def dump_prompt(program: dspy.Module, path: Path) -> None:
    """Write the compiled instruction and demos out in readable form.

    An optimizer-authored prompt that nobody can read is a liability in a
    deliverable whose methodology gets contested, so it gets checked in.
    """
    pred = program.predictors()[0]
    lines = ["=" * 78, "COMPILED INSTRUCTION", "=" * 78, "",
             pred.signature.instructions, "", "=" * 78,
             f"DEMOS ({len(pred.demos)})", "=" * 78]
    for i, d in enumerate(pred.demos, 1):
        d = d.toDict() if hasattr(d, "toDict") else dict(d)
        lines.append(f"\n--- demo {i} ---")
        for k, v in d.items():
            lines.append(f"{k}: {v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    print(f"\nwrote {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────


def cmd_optimize(parts: dict, args) -> None:
    from dspy.teleprompt import MIPROv2

    print(f"objective: correct=1.0, false positive={FP_CREDIT}, false negative=0.0")
    optimizer = MIPROv2(
        metric=optimizer_metric,
        prompt_model=proposer_lm(),
        task_model=task_lm(),
        auto=args.auto,
        num_threads=NUM_THREADS,
        max_bootstrapped_demos=args.demos,
        max_labeled_demos=args.demos,
        verbose=True,
    )
    compiled = optimizer.compile(
        build_program(),
        trainset=parts["train"],
        valset=parts["val"],
        requires_permission_to_run=False,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    compiled.save(str(compiled_path(FP_CREDIT)))
    dump_prompt(compiled, prompt_dump_path(FP_CREDIT))
    save_result(f"compiled_{_tag(FP_CREDIT)}_val",
                run_dspy(compiled, parts["val"], "compiled on val"))


def main() -> None:
    global FP_CREDIT

    ap = argparse.ArgumentParser()
    ap.add_argument("command",
                    choices=["baseline", "zeroshot", "optimize", "test", "fixed",
                             "variant", "report"])
    ap.add_argument("--variant", default="xref",
                    help="name under experiments/variants/ (xref, approp, both)")
    ap.add_argument("--slice", default="test", choices=["train", "val", "test"])
    ap.add_argument("--limit", type=int, default=0, help="cap rows, for smoke tests")
    ap.add_argument("--auto", default="light", choices=["light", "medium", "heavy"])
    ap.add_argument("--demos", type=int, default=3)
    ap.add_argument("--fp-credit", type=float, default=FP_CREDIT,
                    help="partial credit for a false positive (see FP_CREDIT); "
                         "names the compiled artifact, so runs do not collide")
    args = ap.parse_args()
    FP_CREDIT = args.fp_credit

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parts = split(load_gold())
    print("split sizes: " + ", ".join(f"{k}={len(v)} (+{sum(e.gold_congressional for e in v)})"
                                      for k, v in parts.items()))

    rows = parts[args.slice]
    if args.limit:
        rows = rows[:args.limit]

    dspy.configure(lm=task_lm())

    if args.command == "baseline":
        save_result(f"baseline_{args.slice}",
                    run_baseline(rows, args.slice, arm=f"baseline_{args.slice}"))
    elif args.command == "zeroshot":
        save_result(f"zeroshot_{args.slice}", run_dspy(build_program(), rows, f"DSPy zero-shot on {args.slice}"))
    elif args.command == "optimize":
        cmd_optimize(parts, args)
    elif args.command == "test":
        path = compiled_path(FP_CREDIT)
        if not path.exists():
            sys.exit(f"no compiled program at {path}; run `optimize --fp-credit {FP_CREDIT}` first")
        program = build_program()
        program.load(str(path))
        save_result(f"compiled_{_tag(FP_CREDIT)}_{args.slice}",
                    run_dspy(program, rows, f"compiled ({_tag(FP_CREDIT)}) on {args.slice}"))
    elif args.command == "variant":
        # Production path with one clause of JUDGE_SYSTEM swapped, so the only
        # difference from the `baseline` arm is that clause -- not the prompt
        # framework, the adapter, or the output contract.
        path = REPO_ROOT / "experiments/variants" / f"{args.variant}.txt"
        if not path.exists():
            sys.exit(f"no variant at {path}; run experiments/make_variants.py first")
        mc.JUDGE_SYSTEM = path.read_text()
        arm = f"variant_{args.variant}_{args.slice}"
        save_result(arm, run_baseline(rows, f"{args.slice} [variant:{args.variant}]", arm=arm))
    elif args.command == "fixed":
        program = build_program(FIXED_PROMPT_PATH.read_text().strip())
        save_result(f"fixed_{args.slice}",
                    run_dspy(program, rows, f"hand-corrected compiled prompt on {args.slice}",
                             arm=f"fixed_{args.slice}"))
    elif args.command == "report":
        results = json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else {}
        print(f"\n{'arm':32s} {'n':>4s} {'recall':>8s} {'prec':>8s} {'F2':>8s} {'acc':>8s}")
        for k in sorted(results):
            r = results[k]
            print(f"{k:32s} {r['n']:4d} {r['recall'] * 100:7.1f}% {r['precision'] * 100:7.1f}% "
                  f"{r['f2'] * 100:7.1f}% {r['accuracy'] * 100:7.1f}%")


if __name__ == "__main__":
    main()
