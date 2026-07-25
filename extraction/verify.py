"""Verification script: sample rows and compare against PDF ground truth."""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

from extract import (
    _clean_text,
    _chars_to_text,
    _deduplicate_chars,
    _detect_column_boundaries,
    _detect_layout_upright,
    _detect_title_cutoff,
    _extract_page,
    _extract_page_upright,
    _filter_dot_leaders,
    _filter_page_number_chars,
    _find_data_pages,
    _is_new_logical_row,
    _merge_logical_rows,
)
from schema import Report


def extract_with_page_tracking(pdf_path: str | Path) -> list[dict]:
    """Like extract() but includes page numbers in the output.

    Mirrors extract()'s per-page branching: rotated pages and upright pages
    take different extraction paths. Keep the two in step — this function
    silently lost every upright page when that path was added to extract().
    """
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        data_pages = _find_data_pages(pdf.pages)
        if not data_pages:
            return []

        first_rotated = next((p for _, p, rot in data_pages if rot), None)
        first_data_page = first_rotated if first_rotated is not None else data_pages[0][1]
        title_cutoff = _detect_title_cutoff(first_data_page)
        last_good_boundaries = _detect_column_boundaries(first_data_page)

        all_page_rows: list[dict] = []
        last_upright_layout: tuple[tuple[float, float], float] | None = None
        for page_num, page, is_rotated in data_pages:
            if is_rotated:
                col_boundaries = _detect_column_boundaries(
                    page, fallback=last_good_boundaries
                )
                if col_boundaries != last_good_boundaries:
                    last_good_boundaries = col_boundaries
                page_rows = _extract_page(page, page_num, col_boundaries, title_cutoff)
            else:
                layout = _detect_layout_upright(page, fallback=last_upright_layout)
                if layout is None:
                    continue
                last_upright_layout = layout
                page_rows = _extract_page_upright(page, page_num, *layout)

            # Tag each row with its page number
            for row in page_rows:
                row["_page"] = page_num
            all_page_rows.extend(page_rows)

        # Merge logical rows, tracking page range
        merged = _merge_logical_rows_with_pages(all_page_rows)
        return merged


def _merge_logical_rows_with_pages(rows: list[dict]) -> list[dict]:
    """Merge rows and track which pages each logical row spans."""
    merged: list[dict] = []
    current_entity = ""
    current_row: dict | None = None

    prev_was_entity = False
    for row in rows:
        if row["type"] == "entity_header":
            if current_row:
                merged.append(current_row)
                current_row = None
            if prev_was_entity:
                current_entity = current_entity + " " + row["text"]
            else:
                current_entity = row["text"]
            prev_was_entity = True
            continue
        prev_was_entity = False

        if _is_new_logical_row(row, current_row):
            if current_row:
                merged.append(current_row)
            current_row = {
                "reporting_entity": current_entity,
                "nature_of_report": row.get("nature", ""),
                "authority": row.get("authority", ""),
                "when_expected": row.get("when_expected", ""),
                "pages": {row.get("_page", 0)},
            }
        elif current_row is not None:
            current_row["pages"].add(row.get("_page", 0))
            for field, key in [
                ("nature_of_report", "nature"),
                ("authority", "authority"),
                ("when_expected", "when_expected"),
            ]:
                addition = row.get(key, "").strip()
                if addition:
                    existing = current_row[field]
                    if existing:
                        current_row[field] = existing + " " + addition
                    else:
                        current_row[field] = addition
        else:
            current_row = {
                "reporting_entity": current_entity,
                "nature_of_report": row.get("nature", ""),
                "authority": row.get("authority", ""),
                "when_expected": row.get("when_expected", ""),
                "pages": {row.get("_page", 0)},
            }

    if current_row:
        merged.append(current_row)

    for row in merged:
        row["pages"] = sorted(row["pages"])

    return merged


def main():
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "data/CDOC-119hdoc4.pdf"
    n_samples = int(sys.argv[2]) if len(sys.argv) > 2 else 76
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42

    print(f"Extracting from {pdf_path}...", file=sys.stderr)
    rows = extract_with_page_tracking(pdf_path)
    print(f"Extracted {len(rows)} rows", file=sys.stderr)

    # Sample n rows
    random.seed(seed)
    sample_indices = sorted(random.sample(range(len(rows)), min(n_samples, len(rows))))

    # Group by page for efficient PDF reading
    pages_needed: dict[int, list[int]] = defaultdict(list)
    for idx in sample_indices:
        for page in rows[idx]["pages"]:
            pages_needed[page].append(idx)

    print(f"Sampled {len(sample_indices)} rows across {len(pages_needed)} pages", file=sys.stderr)
    print(f"Pages to verify: {sorted(pages_needed.keys())}", file=sys.stderr)

    # Output the sample for verification
    output = {
        "metadata": {
            "pdf": pdf_path,
            "total_rows": len(rows),
            "sample_size": len(sample_indices),
            "seed": seed,
            "pages_to_check": sorted(pages_needed.keys()),
        },
        "samples": [],
    }

    for idx in sample_indices:
        row = rows[idx]
        output["samples"].append({
            "row_index": idx + 1,
            "pages": row["pages"],
            "reporting_entity": row["reporting_entity"],
            "nature_of_report": row["nature_of_report"],
            "authority": row["authority"],
            "when_expected": row["when_expected"],
        })

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
