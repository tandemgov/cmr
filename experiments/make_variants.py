"""Generate single-clause variants of the handwritten JUDGE_SYSTEM.

The DSPy comparison showed the optimizer's win was a *threshold shift*, not a
new capability: it flags more things that touch Congress, buying 2 real catches
and 9 spurious ones. A threshold shift is something the handwritten prompt can
make on its own, one clause at a time -- and unlike an optimizer artifact, an
edited clause is attributable in an audit.

Two clauses are implicated by the per-row disagreement analysis:

  xref    "it only cross-references a reporting duty created elsewhere"
          blocks 22 USC 8003(g)(4), a form-spec sub-element of a reporting
          subsection that gold counts as a mandate.

  approp  "the text merely defines terms, authorizes appropriations, ..."
          blocks 43 USC 1748, where a genuine biennial submission to the
          Speaker and the President of the Senate sits behind an opening
          appropriations-authorization clause. Both arms miss this one, so it
          is a shared hole rather than a difference between them.

Each variant changes exactly one clause; the script asserts that before
writing, so a reworded upstream prompt fails loudly instead of silently
producing a two-clause diff that nobody can attribute.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "pipeline"))

import mandate_classify as mc  # noqa: E402

OUT_DIR = REPO_ROOT / "experiments/variants"

XREF_OLD = "- it only cross-references a reporting duty created elsewhere"
XREF_NEW = (
    "- it only cross-references a reporting duty created elsewhere, with one\n"
    "  exception: a provision that sets the form, timing, classification, or\n"
    "  content of a report required in the SAME section or subsection is part\n"
    "  of that reporting duty — answer YES for it"
)

APPROP_OLD = ("- the text merely defines terms, authorizes appropriations, grants rulemaking "
              "authority, or establishes a body without requiring it to report to Congress")
APPROP_NEW = ("- the text merely defines terms, authorizes appropriations, grants rulemaking "
              "authority, or establishes a body without requiring it to report to Congress. "
              "Read the whole provision before applying this: an opening appropriations or "
              "definitions clause does NOT excuse a reporting duty stated later in the same "
              "text. If any part obligates delivery of information to Congress, answer YES")

VARIANTS = {
    "xref": [(XREF_OLD, XREF_NEW)],
    "approp": [(APPROP_OLD, APPROP_NEW)],
    "both": [(XREF_OLD, XREF_NEW), (APPROP_OLD, APPROP_NEW)],
}


def build(name: str, edits: list[tuple[str, str]]) -> str:
    text = mc.JUDGE_SYSTEM
    for old, new in edits:
        if text.count(old) != 1:
            raise SystemExit(
                f"variant {name!r}: expected exactly 1 occurrence of\n  {old!r}\n"
                f"found {text.count(old)}. JUDGE_SYSTEM was reworded; update this script."
            )
        text = text.replace(old, new)
    return text


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base_lines = mc.JUDGE_SYSTEM.splitlines()
    for name, edits in VARIANTS.items():
        text = build(name, edits)
        changed = sum(1 for a, b in zip(base_lines, text.splitlines()) if a != b)
        path = OUT_DIR / f"{name}.txt"
        path.write_text(text)
        print(f"{path.name:12s} {len(edits)} clause(s) edited, "
              f"{abs(len(text.splitlines()) - len(base_lines))} line(s) added, "
              f"first divergence at line {changed and next(i for i, (a, b) in enumerate(zip(base_lines, text.splitlines())) if a != b)}")


if __name__ == "__main__":
    main()
