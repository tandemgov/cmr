"""Generate a verification report: pipeline output + raw chars + PDF page refs.

Outputs an HTML file with side-by-side comparisons for sampled rows,
with embedded PDF page images for visual cross-checking.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from collections import defaultdict
from html import escape
from pathlib import Path

import pdfplumber

from verify import extract_with_page_tracking

# Repo-root anchored so these scripts run correctly from any working
# directory, not just the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent


def render_pdf_page_image(pdf_path: Path, page_num: int, out_dir: Path) -> Path:
    """Render a PDF page to PNG, rotated 90° clockwise for readability."""
    from PIL import Image

    img_path = out_dir / f"page_{page_num}.png"
    if img_path.exists():
        return img_path

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num - 1]
        img = page.to_image(resolution=150)
        img.save(str(img_path))

    # Rotate 90° clockwise so the table reads normally
    pil_img = Image.open(img_path)
    rotated = pil_img.rotate(-90, expand=True)
    rotated.save(str(img_path))

    return img_path


def get_raw_chars_for_row(pdf_path: Path, page_num: int, row: dict) -> str:
    """Get the raw char layers for a row's page, showing what pdfplumber sees."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num - 1]
        chars = [c for c in page.chars if not c.get("upright", True)]

        by_x0 = defaultdict(list)
        for c in chars:
            by_x0[round(c["x0"])].append(c)

        # Find the x0 group matching this row's nature text
        nature_fragment = row["nature_of_report"][:20].lower()
        matching = []
        for x0 in sorted(by_x0.keys()):
            group = sorted(by_x0[x0], key=lambda c: -c["top"])
            text = "".join(c["text"] for c in group).lower()
            if nature_fragment in text:
                matching.append(x0)

        if not matching:
            return "(could not locate row chars on page)"

        # Show chars in the matching x0 band ± 20
        x0_min = min(matching) - 2
        x0_max = max(matching) + 20
        band_chars = [c for c in chars if x0_min <= c["x0"] <= x0_max]

        precise = defaultdict(list)
        for c in band_chars:
            precise[round(c["x0"], 1)].append(c)

        lines = []
        for px0 in sorted(precise.keys()):
            layer = sorted(precise[px0], key=lambda c: -c["top"])
            text = "".join(c["text"] for c in layer)
            if len(text.strip()) < 3:
                continue
            lines.append(f"x0={px0:6.1f} ({len(layer):3d} chars): {text[:120]}")
        return "\n".join(lines)


def main():
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "data/CDOC-119hdoc4.pdf"
    n_samples = int(sys.argv[2]) if len(sys.argv) > 2 else 76
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
    out_dir = REPO_ROOT / "verify_output"
    out_dir.mkdir(exist_ok=True)

    print(f"Extracting from {pdf_path}...", file=sys.stderr)
    rows = extract_with_page_tracking(pdf_path)
    print(f"Extracted {len(rows)} rows", file=sys.stderr)

    random.seed(seed)
    sample_indices = sorted(random.sample(range(len(rows)), min(n_samples, len(rows))))

    # Collect unique pages
    pages_needed = set()
    for idx in sample_indices:
        for p in rows[idx]["pages"]:
            pages_needed.add(p)

    print(f"Rendering {len(pages_needed)} page images...", file=sys.stderr)
    for p in sorted(pages_needed):
        render_pdf_page_image(pdf_path, p, out_dir)
        print(f"  Page {p}", file=sys.stderr)

    # Build HTML report
    html_parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        "<title>CMRA Verification Report</title>",
        "<style>",
        "body { font-family: monospace; max-width: 1400px; margin: 0 auto; padding: 20px; }",
        "h1 { font-size: 1.4em; }",
        ".row-card { border: 2px solid #ccc; margin: 20px 0; padding: 15px; border-radius: 8px; }",
        ".row-card h2 { margin-top: 0; font-size: 1.1em; background: #f0f0f0; padding: 8px; margin: -15px -15px 15px; border-radius: 6px 6px 0 0; }",
        ".fields { display: grid; grid-template-columns: 120px 1fr; gap: 4px 10px; margin-bottom: 15px; }",
        ".fields dt { font-weight: bold; color: #555; }",
        ".fields dd { margin: 0; word-break: break-word; }",
        ".raw-chars { background: #f8f8f0; padding: 10px; font-size: 0.85em; white-space: pre-wrap; overflow-x: auto; max-height: 300px; border: 1px solid #ddd; }",
        ".page-img { max-width: 100%; border: 1px solid #ddd; margin-top: 10px; }",
        ".verdict-btns button { padding: 4px 12px; border: 2px solid #ccc; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 0.9em; margin-left: 6px; }",
        ".verdict-btns button.pass-btn { color: #155724; }",
        ".verdict-btns button.fail-btn { color: #721c24; }",
        ".verdict-btns button.active-pass { background: #d4edda; border-color: #155724; }",
        ".verdict-btns button.active-fail { background: #f8d7da; border-color: #721c24; }",
        ".status-bar { position: sticky; top: 0; background: #fff; border-bottom: 2px solid #ccc; padding: 10px 0; z-index: 10; margin-bottom: 10px; }",
        "details summary { cursor: pointer; color: #0066cc; }",
        ".fail-note { margin-top: 6px; }",
        ".fail-note input { width: 400px; padding: 4px; border: 1px solid #ccc; border-radius: 4px; font-family: monospace; }",
        "</style>",
        "<script>",
        "function markVerdict(rowId, verdict) {",
        "  const card = document.getElementById(rowId);",
        "  const btns = card.querySelectorAll('.verdict-btns button');",
        "  btns.forEach(b => b.classList.remove('active-pass', 'active-fail'));",
        "  const noteEl = card.querySelector('.fail-note');",
        "  if (verdict === 'pass') {",
        "    btns[0].classList.add('active-pass');",
        "    if (noteEl) noteEl.style.display = 'none';",
        "  } else {",
        "    btns[1].classList.add('active-fail');",
        "    if (noteEl) noteEl.style.display = 'block';",
        "  }",
        "  localStorage.setItem(rowId, verdict);",
        "  if (noteEl && verdict === 'pass') localStorage.removeItem(rowId + '-note');",
        "  updateSummary();",
        "}",
        "function saveNote(rowId) {",
        "  const card = document.getElementById(rowId);",
        "  const note = card.querySelector('.fail-note input').value;",
        "  localStorage.setItem(rowId + '-note', note);",
        "}",
        "function updateSummary() {",
        "  const total = document.querySelectorAll('.row-card').length;",
        "  let pass = 0, fail = 0;",
        "  document.querySelectorAll('.row-card').forEach(card => {",
        "    const v = localStorage.getItem(card.id);",
        "    if (v === 'pass') pass++;",
        "    if (v === 'fail') fail++;",
        "  });",
        "  const unchecked = total - pass - fail;",
        "  document.getElementById('summary').innerHTML = ",
        "    `<b>${pass}</b> pass &nbsp; <b>${fail}</b> fail &nbsp; <b>${unchecked}</b> remaining &nbsp; (${total} total)`;",
        "}",
        "function exportResults() {",
        "  const results = [];",
        "  document.querySelectorAll('.row-card').forEach(card => {",
        "    const v = localStorage.getItem(card.id);",
        "    if (!v) return;",
        "    const note = localStorage.getItem(card.id + '-note') || '';",
        "    const title = card.querySelector('h2').textContent.trim().split('PASS')[0].trim();",
        "    results.push({id: card.id, title, verdict: v, note});",
        "  });",
        "  const blob = new Blob([JSON.stringify(results, null, 2)], {type: 'application/json'});",
        "  const a = document.createElement('a');",
        "  a.href = URL.createObjectURL(blob);",
        "  a.download = 'verification_results.json';",
        "  a.click();",
        "}",
        "window.addEventListener('load', () => {",
        "  document.querySelectorAll('.row-card').forEach(card => {",
        "    const v = localStorage.getItem(card.id);",
        "    if (v) markVerdict(card.id, v);",
        "    const note = localStorage.getItem(card.id + '-note');",
        "    if (note) { const inp = card.querySelector('.fail-note input'); if(inp) inp.value = note; }",
        "  });",
        "  updateSummary();",
        "});",
        "</script>",
        "</head><body>",
        f"<h1>Verification Report: {escape(str(pdf_path))}</h1>",
        f"<p>Total rows: {len(rows)} | Sample size: {len(sample_indices)} | Seed: {seed}</p>",
        f"<p>Pages checked: {len(pages_needed)}</p>",
        '<div class="status-bar"><span id="summary">Loading...</span>'
        ' &nbsp; <button onclick="exportResults()" style="padding:6px 14px;cursor:pointer;background:#0066cc;color:white;border:none;border-radius:4px;font-weight:bold;font-size:1em">Export Results</button></div>',
        "<p>For each row: compare the <b>Pipeline Output</b> fields against the <b>PDF Page Image</b>. Mark as PASS/FAIL.</p>",
    ]

    for i, idx in enumerate(sample_indices):
        row = rows[idx]
        page_list = row["pages"]
        primary_page = page_list[0]

        raw_chars = get_raw_chars_for_row(pdf_path, primary_page, row)

        # Use content-based ID so verdicts survive regeneration
        content_key = f"{row['nature_of_report'][:40]}|{row['authority'][:40]}"
        row_id = "r-" + hashlib.md5(content_key.encode()).hexdigest()[:10]
        html_parts.append(f'<div class="row-card" id="{row_id}">')
        html_parts.append(
            f'<h2>Sample {i+1}/{len(sample_indices)} — Row {idx+1} (Page {", ".join(str(p) for p in page_list)})'
            f' <span class="verdict-btns">'
            f'<button class="pass-btn" onclick="markVerdict(\'{row_id}\', \'pass\')">PASS</button>'
            f'<button class="fail-btn" onclick="markVerdict(\'{row_id}\', \'fail\')">FAIL</button>'
            f'</span></h2>'
            f'<div class="fail-note" style="display:none">'
            f'<input type="text" placeholder="What\'s wrong?" oninput="saveNote(\'{row_id}\')">'
            f'</div>'
        )

        html_parts.append('<dl class="fields">')
        html_parts.append(f"<dt>Entity</dt><dd>{escape(row['reporting_entity'])}</dd>")
        html_parts.append(
            f"<dt>Nature</dt><dd>{escape(row['nature_of_report'])}</dd>"
        )
        html_parts.append(f"<dt>Authority</dt><dd>{escape(row['authority'])}</dd>")
        html_parts.append(
            f"<dt>When</dt><dd>{escape(row['when_expected'])}</dd>"
        )
        html_parts.append("</dl>")

        html_parts.append("<details><summary>Raw char layers</summary>")
        html_parts.append(f'<pre class="raw-chars">{escape(raw_chars)}</pre>')
        html_parts.append("</details>")

        html_parts.append(
            f'<details open><summary>PDF Page {primary_page}</summary>'
        )
        html_parts.append(
            f'<img class="page-img" src="page_{primary_page}.png" '
            f'alt="PDF page {primary_page}" loading="lazy">'
        )
        html_parts.append("</details>")

        html_parts.append("</div>")

    html_parts.append("</body></html>")

    report_path = out_dir / "verification_report.html"
    report_path.write_text("\n".join(html_parts))
    print(f"\nReport written to {report_path}", file=sys.stderr)
    print(str(report_path))


if __name__ == "__main__":
    main()
