"""LLM-as-judge verification for CMRA extraction.

For each sampled row, sends the extracted fields + the rendered PDF page image
to three vision judges (OpenAI GPT, Anthropic Claude, Google Gemini) and asks
each to verdict pass / fail / unclear with per-field issue notes.

Resumable: writes verify_output/llm_judgments.jsonl incrementally and skips
(row_id, judge) pairs already present in that file.

Env vars (only the ones for judges you enable need to be set):
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  GEMINI_API_KEY          (or GOOGLE_API_KEY)

Optional model overrides:
  OPENAI_MODEL   (default: gpt-4o)
  ANTHROPIC_MODEL (default: claude-sonnet-4-5)
  GEMINI_MODEL    (default: gemini-2.5-flash)

Usage:
  uv run python judge.py                              # full run, 76 samples, all 3 judges
  uv run python judge.py --samples 20                 # smaller run
  uv run python judge.py --judges openai,gemini       # subset of judges
  uv run python judge.py --dry-run                    # just print the plan
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

load_dotenv()  # picks up keys from .env in the current directory

from verify import extract_with_page_tracking
from verify_report import render_pdf_page_image

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("judge")

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

OUT_DIR = REPO_ROOT / "verify_output"
JUDGMENTS_PATH = OUT_DIR / "llm_judgments.jsonl"

SYSTEM_PROMPT = """You are evaluating whether an automated PDF extraction is correct.

The source is a US government document — a table titled "List of Reports Which It Is the Duty of Any Officer or Department to Make to Congress." You are judging three fields of a single row:
- nature_of_report: leftmost column — a short description of the report
- authority: middle column — US Code, Public Law, and Statutes-at-Large citations
- when_expected: right column — timing requirement

The row also has a `reporting_entity` field, but you are NOT judging it. The entity comes from a bold sub-header that often sits on a prior page, which makes it impossible to verify from a single page image. Ignore the reporting_entity completely.

You will see:
1. An EXTRACTED ROW (with nature_of_report, authority, when_expected)
2. A RENDERED IMAGE of the PDF page that should contain this row

Find the row on the page using nature_of_report as the search key, then compare each of the three judged fields against what is visually present.

Verdict rules:
- "pass": All three judged fields substantively match the source. Whitespace, hyphenation, and minor punctuation differences are OK.
- "fail": At least one of the three judged fields has a MATERIAL error — wrong/missing/spurious words, wrong citation number or section, missing dates, mid-citation truncation, content from a neighboring row leaking in, etc.
- "unclear": You cannot confidently locate the row on the page, or the image quality prevents a confident judgment.

Respond ONLY with a valid JSON object (no markdown fences, no commentary):
{
  "verdict": "pass" | "fail" | "unclear",
  "reasoning": "one short paragraph, max 300 chars",
  "field_issues": {
    "nature_of_report": null | "<problem description>",
    "authority": null | "<problem description>",
    "when_expected": null | "<problem description>"
  }
}
"""


def build_user_text(row: dict) -> str:
    pages = ", ".join(str(p) for p in row["pages"])
    return (
        f"EXTRACTED ROW (claimed page(s): {pages}):\n"
        f"  nature_of_report: {row['nature_of_report']!r}\n"
        f"  authority: {row['authority']!r}\n"
        f"  when_expected: {row['when_expected']!r}\n\n"
        f"Find this row on the attached page image and judge it per the system rules."
    )


def parse_json_loose(text: str) -> dict:
    """Parse JSON, stripping common markdown fences if a model added them."""
    t = text.strip()
    if t.startswith("```"):
        # remove first fence line and trailing fence
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
        if t.startswith("json"):
            t = t[4:].lstrip()
    return json.loads(t)


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------


def call_openai(row: dict, image_bytes: bytes) -> dict:
    from openai import OpenAI

    client = OpenAI()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=600,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_user_text(row)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ],
    )
    return parse_json_loose(resp.choices[0].message.content)


def call_anthropic(row: dict, image_bytes: bytes) -> dict:
    from anthropic import Anthropic

    client = Anthropic()
    b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": build_user_text(row)},
                ],
            },
        ],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return parse_json_loose(text)


def call_gemini(row: dict, image_bytes: bytes) -> dict:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            build_user_text(row),
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=1500,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return parse_json_loose(resp.text)


JUDGES = {
    "openai": (call_openai, lambda: OPENAI_MODEL, "OPENAI_API_KEY"),
    "claude": (call_anthropic, lambda: ANTHROPIC_MODEL, "ANTHROPIC_API_KEY"),
    "gemini": (call_gemini, lambda: GEMINI_MODEL, ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
}


def have_key(env_spec) -> bool:
    if isinstance(env_spec, tuple):
        return any(os.environ.get(k) for k in env_spec)
    return bool(os.environ.get(env_spec))


def judge_one(judge_name: str, row: dict, image_bytes: bytes) -> dict:
    fn, model_fn, _ = JUDGES[judge_name]
    t0 = time.time()
    try:
        result = fn(row, image_bytes)
        return {
            "judge": judge_name,
            "model": model_fn(),
            "verdict": result.get("verdict"),
            "reasoning": result.get("reasoning"),
            "field_issues": result.get("field_issues"),
            "error": None,
            "latency_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        logger.warning("Judge %s failed: %s", judge_name, e)
        return {
            "judge": judge_name,
            "model": model_fn(),
            "verdict": None,
            "reasoning": None,
            "field_issues": None,
            "error": f"{type(e).__name__}: {e}",
            "latency_ms": int((time.time() - t0) * 1000),
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def load_existing(path: Path) -> set[tuple[str, str]]:
    """Return the set of (row_id, judge) pairs already judged."""
    if not path.exists():
        return set()
    done: set[tuple[str, str]] = set()
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("error"):
                continue  # allow retry of errored rows
            done.add((rec["row_id"], rec["judge"]))
    return done


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pdf", default="data/CDOC-119hdoc4.pdf")
    parser.add_argument(
        "--samples",
        type=int,
        default=76,
        help="Sample count (default: 76 — matches verify_report.py)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--judges",
        default="gemini",
        help="Comma-separated subset of: openai,claude,gemini",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Concurrent judge calls (default 3 — one per judge)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan, don't call APIs"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    unknown = [j for j in judges if j not in JUDGES]
    if unknown:
        sys.exit(f"Unknown judges: {unknown}. Valid: {list(JUDGES)}")

    if not args.dry_run:
        missing = [j for j in judges if not have_key(JUDGES[j][2])]
        if missing:
            sys.exit(f"Missing API key env var for: {missing}")

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    OUT_DIR.mkdir(exist_ok=True)

    print(f"Extracting from {pdf_path}...", file=sys.stderr)
    rows = extract_with_page_tracking(pdf_path)
    print(f"Extracted {len(rows)} rows", file=sys.stderr)

    # Monotonic sampling: shuffle once with the seed, then take the first N.
    # This way --samples 150 is a strict superset of --samples 76 (same seed),
    # so bumping the sample size never invalidates prior verdicts.
    rng = random.Random(args.seed)
    shuffled = list(range(len(rows)))
    rng.shuffle(shuffled)
    sample_indices = sorted(shuffled[: min(args.samples, len(rows))])

    # Same content-hash IDs used in verify_report.py so verdicts can be cross-walked
    import hashlib

    def row_id(r):
        key = f"{r['nature_of_report'][:40]}|{r['authority'][:40]}"
        return "r-" + hashlib.md5(key.encode()).hexdigest()[:10]

    samples = []
    for idx in sample_indices:
        r = rows[idx]
        samples.append(
            {
                "row_id": row_id(r),
                "row_index": idx + 1,
                "page": r["pages"][0],
                "row": r,
            }
        )

    # Sort by page so consecutive Claude calls reuse the cached image
    samples.sort(key=lambda s: (s["page"], s["row_index"]))

    done = load_existing(JUDGMENTS_PATH)
    work: list[tuple[dict, str]] = []
    for s in samples:
        for j in judges:
            if (s["row_id"], j) not in done:
                work.append((s, j))

    print(
        f"Samples: {len(samples)}  Judges: {judges}  Existing verdicts: {len(done)}  To do: {len(work)}",
        file=sys.stderr,
    )

    if args.dry_run:
        for s in samples[:5]:
            print(
                f"  sample {s['row_index']:>4} (page {s['page']}): {s['row']['nature_of_report'][:60]!r}"
            )
        if len(samples) > 5:
            print(f"  ... +{len(samples) - 5} more")
        return

    if not work:
        print("All (row, judge) pairs already judged. Nothing to do.", file=sys.stderr)
        summarize(JUDGMENTS_PATH, samples, judges)
        return

    # Ensure all needed page images exist on disk
    pages_needed = {s["page"] for s in samples}
    print(f"Ensuring {len(pages_needed)} page images...", file=sys.stderr)
    for p in sorted(pages_needed):
        render_pdf_page_image(pdf_path, p, OUT_DIR)

    # Process samples sequentially, with each sample's judges in parallel
    image_cache: dict[int, bytes] = {}

    def get_image(page_num: int) -> bytes:
        if page_num not in image_cache:
            image_cache[page_num] = (OUT_DIR / f"page_{page_num}.png").read_bytes()
        return image_cache[page_num]

    # Group work by sample so we send one image to N judges concurrently
    by_sample: dict[str, list[str]] = defaultdict(list)
    for s, j in work:
        by_sample[s["row_id"]].append(j)
    sample_by_id = {s["row_id"]: s for s in samples}

    t_start = time.time()
    with JUDGMENTS_PATH.open("a") as out:
        for n, (rid, js) in enumerate(by_sample.items(), 1):
            s = sample_by_id[rid]
            img = get_image(s["page"])
            print(
                f"[{n}/{len(by_sample)}] row {s['row_index']:>4} (page {s['page']}) judges={js} ...",
                file=sys.stderr,
                end=" ",
                flush=True,
            )
            t0 = time.time()
            results = []
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(judge_one, j, s["row"], img): j for j in js}
                for fut in as_completed(futs):
                    results.append(fut.result())
            for res in results:
                rec = {
                    "row_id": s["row_id"],
                    "row_index": s["row_index"],
                    "page": s["page"],
                    "nature_of_report": s["row"]["nature_of_report"],
                    **res,
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
            verdicts = ",".join(
                f"{r['judge']}={r['verdict'] or 'ERR'}" for r in results
            )
            print(f"{verdicts} ({time.time() - t0:.1f}s)", file=sys.stderr)

    print(
        f"\nDone in {time.time() - t_start:.1f}s.  Output: {JUDGMENTS_PATH}",
        file=sys.stderr,
    )
    summarize(JUDGMENTS_PATH, samples, judges)


def summarize(path: Path, samples: list[dict], judges: list[str]):
    """Print per-judge and agreement summary."""
    if not path.exists():
        return

    sample_ids = {s["row_id"] for s in samples}

    # Dedupe by (row_id, judge), keeping the latest record so retried-after-error
    # rows aren't double-counted.
    latest: dict[tuple[str, str], dict] = {}
    with path.open() as f:
        for line in f:
            rec = json.loads(line)
            if rec["row_id"] not in sample_ids:
                continue
            latest[(rec["row_id"], rec["judge"])] = rec

    by_row: dict[str, dict[str, str]] = defaultdict(dict)
    judge_counts: dict[str, Counter] = defaultdict(Counter)
    errors: dict[str, int] = Counter()
    for rec in latest.values():
        if rec.get("error"):
            errors[rec["judge"]] += 1
            continue
        v = rec["verdict"] or "null"
        by_row[rec["row_id"]][rec["judge"]] = v
        judge_counts[rec["judge"]][v] += 1

    print("\n=== Per-judge verdict counts ===", file=sys.stderr)
    for j in judges:
        c = judge_counts.get(j, Counter())
        total = sum(c.values())
        if total == 0:
            print(f"  {j:<8}  (no verdicts)", file=sys.stderr)
            continue
        parts = []
        for v in ("pass", "fail", "unclear", "null"):
            if c[v]:
                parts.append(f"{v}={c[v]} ({100 * c[v] / total:.0f}%)")
        err = errors.get(j, 0)
        if err:
            parts.append(f"errors={err}")
        print(f"  {j:<8}  {'  '.join(parts)}", file=sys.stderr)

    # Cross-judge agreement
    full_rows = [r for r in by_row.values() if all(j in r for j in judges)]
    print(
        f"\n=== Cross-judge agreement (over {len(full_rows)} rows judged by all {len(judges)} judges) ===",
        file=sys.stderr,
    )
    if not full_rows:
        return
    unanimous_pass = sum(1 for r in full_rows if all(r[j] == "pass" for j in judges))
    unanimous_fail = sum(1 for r in full_rows if all(r[j] == "fail" for j in judges))
    split = len(full_rows) - unanimous_pass - unanimous_fail
    print(
        f"  unanimous pass:  {unanimous_pass} ({100 * unanimous_pass / len(full_rows):.0f}%)",
        file=sys.stderr,
    )
    print(
        f"  unanimous fail:  {unanimous_fail} ({100 * unanimous_fail / len(full_rows):.0f}%)",
        file=sys.stderr,
    )
    print(
        f"  split / unclear: {split} ({100 * split / len(full_rows):.0f}%)",
        file=sys.stderr,
    )

    # Show split/fail rows for quick review
    interesting = [
        (rid, verdicts)
        for rid, verdicts in by_row.items()
        if all(j in verdicts for j in judges)
        and (set(verdicts[j] for j in judges) != {"pass"})
    ]
    if interesting:
        print(f"\n=== {len(interesting)} rows worth a human look ===", file=sys.stderr)
        sample_lookup = {s["row_id"]: s for s in samples}
        for rid, verdicts in interesting[:30]:
            s = sample_lookup.get(rid, {})
            tag = " ".join(f"{j}={verdicts[j]}" for j in judges)
            nature = (s.get("row", {}).get("nature_of_report", "") or "")[:60]
            print(
                f"  row {s.get('row_index', '?'):>4} page {s.get('page', '?'):>3}  {tag:<40}  {nature!r}",
                file=sys.stderr,
            )
        if len(interesting) > 30:
            print(f"  ... +{len(interesting) - 30} more (see {path})", file=sys.stderr)


if __name__ == "__main__":
    main()
