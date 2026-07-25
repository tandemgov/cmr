"""Deterministic structured data extraction from rotated government PDF tables.

This module extracts tabular data from PDFs where text is rendered via a 90°
rotated text matrix. In this coordinate system:
  - PDF x0 → visual row position (top to bottom on screen)
  - PDF top → visual column position (high top = left on screen)

Bold text is simulated by stamping each character twice at a ~0.4pt x-offset.

A handful of pages (6 in CDOC-119hdoc4: 20, 183, 184, 186, 420, 421) were
typeset UPRIGHT instead of rotated — same table, ordinary coordinates
(top → visual row, x0 → visual column). Those get a parallel extraction
path (`_extract_page_upright`) that feeds the same logical-row merge.
Body text on upright pages is double-stamped per *line* (~1pt top offset),
which the same spatial dedup collapses.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

import pdfplumber

from schema import Report

logger = logging.getLogger(__name__)

# Citation pattern that signals the START of a new authority field
_CITATION_START = re.compile(
    r"^\d+\s*U\.?S\.?C\.?"  # "2 U.S.C." or "12 U.S.C."
    r"|^Pub\.?\s*L\."  # "Pub. L."
    r"|^Added\s+by\s+Pub"  # "Added by Pub. L."
    r"|^Aug\.\s+\d"  # "Aug. 2, 1954" (historical statutes)
)


def extract(pdf_path: str | Path) -> list[Report]:
    """Extract structured report data from a government PDF table.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        List of Report objects, one per logical row in the table.
    """
    pdf_path = Path(pdf_path)
    with pdfplumber.open(pdf_path) as pdf:
        # Find data pages (rotated text) vs. front matter (upright text)
        data_pages = _find_data_pages(pdf.pages)
        if not data_pages:
            logger.warning("No rotated data pages found in %s", pdf_path)
            return []

        logger.info(
            "Found %d data pages (pages %d–%d)",
            len(data_pages),
            data_pages[0][0],
            data_pages[-1][0],
        )

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
                    logger.warning("Page %d: upright layout undetected, skipping", page_num)
                    continue
                last_upright_layout = layout
                page_rows = _extract_page_upright(page, page_num, *layout)
            all_page_rows.extend(page_rows)

        merged = _merge_logical_rows(all_page_rows)

        return [
            Report(
                reporting_entity=row["reporting_entity"],
                nature_of_report=row["nature_of_report"],
                authority=row["authority"],
                when_expected=row["when_expected"],
            )
            for row in merged
        ]


_EXPECTED_TITLE = "LIST OF REPORTS WHICH IT IS THE DUTY"


def _find_data_pages(
    pages: list[pdfplumber.page.Page],
) -> list[tuple[int, pdfplumber.page.Page, bool]]:
    """Identify pages with the expected table data, rotated or upright.

    Filters to pages that have the expected title text, excluding
    index/appendix sections with different table structures.

    Returns list of (1-based page number, page, is_rotated) tuples.
    """
    data_pages = []
    for i, page in enumerate(pages):
        chars = page.chars
        if not chars:
            continue
        rotated = sum(1 for c in chars if not c.get("upright", True))
        is_rotated = rotated > len(chars) * 0.5

        if is_rotated:
            if not _has_expected_title(chars):
                logger.debug("Skipping page %d: unexpected title", i + 1)
                continue
        else:
            if not _has_expected_title_upright(chars):
                logger.debug("Skipping upright page %d: unexpected title", i + 1)
                continue
            logger.info("Page %d: upright data page", i + 1)

        data_pages.append((i + 1, page, is_rotated))
    return data_pages


def _has_expected_title(chars: list[dict]) -> bool:
    """Check if a page's title matches the expected report table title."""
    rot_chars = [c for c in chars if not c.get("upright", True)]
    if not rot_chars:
        return False

    # Title is at the lowest x0 values (top of visual page)
    by_x0: dict[int, list[dict]] = defaultdict(list)
    for c in rot_chars:
        by_x0[round(c["x0"])].append(c)

    for x0 in sorted(by_x0.keys())[:2]:
        group = sorted(by_x0[x0], key=lambda c: -c["top"])
        text = "".join(c["text"] for c in group).strip()
        if _EXPECTED_TITLE in text:
            return True

    return False


def _has_expected_title_upright(chars: list[dict]) -> bool:
    """Check an upright page's title (topmost text lines) for the table title."""
    by_top: dict[int, list[dict]] = defaultdict(list)
    for c in chars:
        by_top[round(c["top"])].append(c)

    for top in sorted(by_top.keys())[:4]:
        group = sorted(by_top[top], key=lambda c: c["x0"])
        text = "".join(c["text"] for c in group).strip()
        if _EXPECTED_TITLE in text:
            return True
    return False


# ---------------------------------------------------------------------------
# Upright-page extraction (same table, ordinary coordinates)
# ---------------------------------------------------------------------------


def _detect_layout_upright(
    page: pdfplumber.page.Page,
    fallback: tuple[tuple[float, float], float] | None = None,
) -> tuple[tuple[float, float], float] | None:
    """Detect ((nature_authority_x, authority_when_x), data_top_cutoff).

    Column separators are vertical lines repeated down the page (many
    segments at the same x). The data area starts below the header box,
    whose bottom edge is the lowest long horizontal line in the top half.
    """
    v_lines = [l for l in page.lines if abs(l["x0"] - l["x1"]) < 1]
    x_counts: dict[float, int] = defaultdict(int)
    for l in v_lines:
        x_counts[round(l["x0"], 1)] += 1
    multi = {x: c for x, c in x_counts.items() if c >= 3}

    h_lines = [
        l for l in page.lines
        if abs(l["top"] - l["bottom"]) < 1
        and (l["x1"] - l["x0"]) > 100
        and l["top"] < page.height / 2
    ]

    if len(multi) >= 2 and h_lines:
        best = sorted(multi, key=multi.get, reverse=True)[:2]  # type: ignore[arg-type]
        boundaries = tuple(sorted(best))
        cutoff = max(l["top"] for l in h_lines)
        logger.debug("Upright layout: cols=%s cutoff=%.1f", boundaries, cutoff)
        return ((boundaries[0], boundaries[1]), cutoff)

    if fallback:
        logger.debug("Using fallback upright layout: %s", fallback)
        return fallback
    return None


def _filter_dot_leaders_upright(chars: list[dict], min_run: int = 5) -> list[dict]:
    """Remove horizontal dot-leader runs (same top, consecutive x0)."""
    by_top: dict[int, list[dict]] = defaultdict(list)
    for c in chars:
        by_top[round(c["top"])].append(c)

    dot_leader_ids: set[int] = set()
    for group in by_top.values():
        dots = sorted([c for c in group if c["text"] == "."], key=lambda c: c["x0"])
        if len(dots) < min_run:
            continue
        run: list[dict] = [dots[0]]
        for k in range(1, len(dots)):
            if abs(dots[k]["x0"] - dots[k - 1]["x0"]) < 5:
                run.append(dots[k])
            else:
                if len(run) >= min_run:
                    dot_leader_ids.update(id(c) for c in run)
                run = [dots[k]]
        if len(run) >= min_run:
            dot_leader_ids.update(id(c) for c in run)

    return [c for c in chars if id(c) not in dot_leader_ids]


def _filter_page_number_chars_upright(chars: list[dict]) -> list[dict]:
    """Remove a bare page number sitting at the bottom of an upright page."""
    if not chars:
        return chars
    max_top = max(c["top"] for c in chars)
    candidates = [c for c in chars if c["top"] > max_top - 5]
    text = "".join(c["text"] for c in sorted(candidates, key=lambda c: c["x0"]))
    if text.strip().isdigit() and len(text.strip()) <= 3:
        drop = {id(c) for c in candidates}
        logger.debug("Filtered upright page number: %r", text.strip())
        return [c for c in chars if id(c) not in drop]
    return chars


def _group_into_visual_rows_upright(
    chars: list[dict], top_tolerance: float = 2.0
) -> list[list[dict]]:
    """Group chars into visual rows by top proximity."""
    if not chars:
        return []
    sorted_chars = sorted(chars, key=lambda c: c["top"])
    rows: list[list[dict]] = []
    current = [sorted_chars[0]]
    for c in sorted_chars[1:]:
        if c["top"] - current[-1]["top"] > top_tolerance:
            rows.append(current)
            current = [c]
        else:
            current.append(c)
    rows.append(current)
    return rows


def _chars_to_text_upright(chars: list[dict]) -> str:
    if not chars:
        return ""
    return "".join(c["text"] for c in sorted(chars, key=lambda c: c["x0"]))


def _assign_column_upright(x0: float, col_boundaries: tuple[float, float]) -> int:
    if x0 < col_boundaries[0]:
        return 0  # Nature of report
    elif x0 < col_boundaries[1]:
        return 1  # Authority
    else:
        return 2  # When expected


def _extract_page_upright(
    page: pdfplumber.page.Page,
    page_num: int,
    col_boundaries: tuple[float, float],
    data_top_cutoff: float,
) -> list[dict]:
    """Extract classified rows from one upright page (mirror of _extract_page)."""
    data_chars = [c for c in page.chars if c["top"] > data_top_cutoff + 1]

    data_chars = _filter_dot_leaders_upright(data_chars)
    data_chars = _filter_page_number_chars_upright(data_chars)
    # Upright body text is double-stamped per line (~1pt top offset);
    # the spatial dedup collapses it with a slightly wider tolerance.
    deduped = _deduplicate_chars(data_chars, tolerance=1.5)
    logger.debug(
        "Page %d (upright): %d data chars → %d after dedup",
        page_num, len(data_chars), len(deduped),
    )

    visual_rows = _group_into_visual_rows_upright(deduped)

    results: list[dict] = []
    for row_chars in visual_rows:
        has_bold = any("Bold" in c.get("fontname", "") for c in row_chars)
        cols_present = {
            _assign_column_upright(c["x0"], col_boundaries) for c in row_chars
        }
        if has_bold and cols_present == {0}:
            results.append({
                "type": "entity_header",
                "x0": round(sum(c["top"] for c in row_chars) / len(row_chars), 1),
                "text": _clean_text(_chars_to_text_upright(row_chars)),
            })
            continue

        col_chars: dict[int, list[dict]] = defaultdict(list)
        for c in row_chars:
            col_chars[_assign_column_upright(c["x0"], col_boundaries)].append(c)

        row_data: dict = {
            "type": "data_line",
            "x0": round(sum(c["top"] for c in row_chars) / len(row_chars), 1),
        }
        for col_num, col_name in [(0, "nature"), (1, "authority"), (2, "when_expected")]:
            if col_num in col_chars:
                row_data[col_name] = _clean_text(_chars_to_text_upright(col_chars[col_num]))
            else:
                row_data[col_name] = ""
        results.append(row_data)

    return results


# ---------------------------------------------------------------------------
# Column boundary detection
# ---------------------------------------------------------------------------


def _detect_column_boundaries(
    page: pdfplumber.page.Page,
    fallback: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Auto-detect the two column-separator top values from PDF line objects.

    Finds horizontal lines in the data area that appear at exactly two
    distinct top values with multiple segments each (the column grid).

    Returns (nature_authority_boundary, authority_when_boundary) as top values.
    """
    lines = page.lines
    h_lines = [l for l in lines if abs(l["top"] - l["bottom"]) < 1]

    # Column separator lines span the data x-range. To find that range,
    # use the header box vertical lines (longest-span verticals).
    header_x0s = _find_header_box_x_range(page)

    # Column separator h_lines are in the data x-range
    # Use header box x0 if available, otherwise accept any line in a
    # reasonable range (x0 > 80 filters out page-edge decorations)
    min_x0 = header_x0s[0] if header_x0s else 80.0
    top_counts: dict[float, int] = defaultdict(int)
    for l in h_lines:
        if l["x0"] >= min_x0:
            key = round(l["top"], 1)
            top_counts[key] += 1

    # Filter to top values with multiple segments (column grid lines)
    multi_segment = {t: c for t, c in top_counts.items() if c >= 3}

    if len(multi_segment) >= 2:
        sorted_tops = sorted(multi_segment, key=multi_segment.get, reverse=True)  # type: ignore[arg-type]
        boundary_tops = sorted(sorted_tops[:2], reverse=True)
        logger.debug("Detected column boundaries: %s", boundary_tops)
        return (boundary_tops[0], boundary_tops[1])

    if fallback:
        logger.debug("Using fallback column boundaries: %s", fallback)
        return fallback

    logger.warning("Could not detect column boundaries from lines, using header fallback")
    return _detect_column_boundaries_from_headers(page)


def _detect_column_boundaries_from_headers(
    page: pdfplumber.page.Page,
) -> tuple[float, float]:
    """Fallback: detect column boundaries from header word positions."""
    chars = page.chars
    # Find "Authority" and "When" header text by looking for those chars
    # at header x0 positions (just above data area)
    # The header row has "Nature of report", "Authority", "When expected"
    # Their top values indicate column start positions

    # Group chars into visual rows
    rows = _group_into_visual_rows(
        [c for c in chars if not c.get("upright", True)], x_tolerance=2.0
    )
    for row in rows:
        text = "".join(c["text"] for c in sorted(row, key=lambda c: -c["top"]))
        if "Authority" in text and "Nature" in text:
            # Find the top position of 'A' in 'Authority'
            auth_chars = [c for c in row if c["text"] == "A"]
            if auth_chars:
                # Authority starts at a certain top value
                # Use the gap between Nature and Authority as boundary
                nature_tops = [c["top"] for c in row if c["top"] > 500]
                auth_tops = [c["top"] for c in row if 300 < c["top"] < 500]
                if nature_tops and auth_tops:
                    boundary1 = (min(nature_tops) + max(auth_tops)) / 2
                    # Look for "When" top values
                    when_tops = [c["top"] for c in row if c["top"] < 300]
                    if when_tops:
                        boundary2 = (min(auth_tops) + max(when_tops)) / 2
                        return (boundary1, boundary2)

    logger.warning("Could not detect boundaries from headers, using test-doc defaults")
    return (433.8, 270.9)


def _find_header_box_x_range(page: pdfplumber.page.Page) -> tuple[float, float] | None:
    """Find the x0 range of the header box from vertical lines.

    The header box is defined by two vertical lines with long spans in the
    top dimension. Returns (lower_x0, higher_x0) or None.
    """
    lines = page.lines
    v_lines = [l for l in lines if abs(l["x0"] - l["x1"]) < 1]

    # Find vertical lines with significant top span (> 100pt)
    long_v = [
        l for l in v_lines if abs(l["top"] - l["bottom"]) > 100
    ]
    if len(long_v) < 2:
        return None

    # Group by x0 (within 1pt)
    x0_groups: dict[int, list] = defaultdict(list)
    for l in long_v:
        x0_groups[round(l["x0"])].append(l)

    # The header box has exactly two x0 values
    x0_vals = sorted(x0_groups.keys())
    if len(x0_vals) < 2:
        return None

    # Return the two lowest x0 values (header box is at the top of visual page)
    return (float(x0_vals[0]), float(x0_vals[1]))


def _detect_title_cutoff(page: pdfplumber.page.Page) -> float:
    """Auto-detect the x0 value below which text is data (not title/header).

    Uses the header box vertical lines — data starts after the higher x0
    of the two header box lines.
    """
    header_box = _find_header_box_x_range(page)
    if header_box:
        cutoff = header_box[1]
        logger.info("Detected title cutoff: x0 > %.1f", cutoff)
        return cutoff

    logger.warning("Could not detect title cutoff from lines, using default")
    return 200.0


# ---------------------------------------------------------------------------
# Char-level processing
# ---------------------------------------------------------------------------


def _filter_dot_leaders(chars: list[dict], min_run: int = 5) -> list[dict]:
    """Remove dot-leader chars that would interfere with spatial dedup."""
    by_x0: dict[int, list[dict]] = defaultdict(list)
    for c in chars:
        by_x0[round(c["x0"])].append(c)

    dot_leader_ids: set[int] = set()

    for x0_key, group in by_x0.items():
        dots = sorted(
            [c for c in group if c["text"] == "."],
            key=lambda c: c["top"],
        )
        if len(dots) < min_run:
            continue

        run: list[dict] = [dots[0]]
        for k in range(1, len(dots)):
            gap = abs(dots[k]["top"] - dots[k - 1]["top"])
            if gap < 5:
                run.append(dots[k])
            else:
                if len(run) >= min_run:
                    for c in run:
                        dot_leader_ids.add(id(c))
                run = [dots[k]]
        if len(run) >= min_run:
            for c in run:
                dot_leader_ids.add(id(c))

    result = [c for c in chars if id(c) not in dot_leader_ids]
    removed = len(chars) - len(result)
    if removed:
        logger.debug("Filtered %d dot-leader chars", removed)
    return result


def _filter_page_number_chars(chars: list[dict]) -> list[dict]:
    """Remove page number chars that sit at the visual page bottom.

    Page numbers are at the highest x0 values on each page, typically
    at x0 ≈ max_x0 and low top values. They're short digit sequences
    that would otherwise leak into the last data row's fields.
    """
    if not chars:
        return chars

    max_x0 = max(c["x0"] for c in chars)
    # Page number chars are within ~5pt of the max x0 and consist of digits/spaces
    threshold = max_x0 - 5
    candidate_ids: set[int] = set()
    candidates = [c for c in chars if c["x0"] > threshold]

    # Only filter if the candidates look like a page number (all digits/spaces)
    text = "".join(c["text"] for c in sorted(candidates, key=lambda c: -c["top"]))
    if text.strip().isdigit() and len(text.strip()) <= 3:
        candidate_ids = {id(c) for c in candidates}
        logger.debug("Filtered %d page number chars: %r", len(candidate_ids), text.strip())

    return [c for c in chars if id(c) not in candidate_ids]


def _deduplicate_chars(chars: list[dict], tolerance: float = 1.0) -> list[dict]:
    """Remove spatially-overlapping duplicate chars (bold-simulation artifacts)."""
    kept = []
    used: set[int] = set()

    sorted_chars = sorted(chars, key=lambda c: (c["x0"], c["top"], c["text"]))

    for i, c in enumerate(sorted_chars):
        if i in used:
            continue

        for j in range(i + 1, len(sorted_chars)):
            if j in used:
                continue
            other = sorted_chars[j]

            if other["x0"] - c["x0"] > tolerance:
                break

            if (
                c["text"] == other["text"]
                and abs(c["x0"] - other["x0"]) <= tolerance
                and abs(c["top"] - other["top"]) <= tolerance
            ):
                used.add(j)

        kept.append(c)

    return kept


def _group_into_visual_rows(
    chars: list[dict], x_tolerance: float = 2.0
) -> list[list[dict]]:
    """Group chars into visual rows by x0 proximity."""
    if not chars:
        return []

    sorted_chars = sorted(chars, key=lambda c: c["x0"])
    rows: list[list[dict]] = []
    current_row = [sorted_chars[0]]

    for c in sorted_chars[1:]:
        if c["x0"] - current_row[-1]["x0"] > x_tolerance:
            rows.append(current_row)
            current_row = [c]
        else:
            current_row.append(c)
    rows.append(current_row)

    return rows


def _chars_to_text(chars: list[dict]) -> str:
    """Concatenate chars sorted by decreasing top (visual left to right)."""
    if not chars:
        return ""
    return "".join(c["text"] for c in sorted(chars, key=lambda c: -c["top"]))


def _clean_text(text: str) -> str:
    """Strip dot-leader remnants and normalize whitespace."""
    text = re.sub(r"\.{2,}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove trailing lone dot (dot-leader remnant) that isn't part of an abbreviation
    text = re.sub(r"(?<![A-Za-z])\s*\.\s*$", "", text)
    return text


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------


def _is_entity_header(
    row_chars: list[dict], col_boundaries: tuple[float, float]
) -> bool:
    """Check if a visual row is a bold entity header spanning only column 0."""
    has_bold = any("Bold" in c.get("fontname", "") for c in row_chars)
    if not has_bold:
        return False

    cols_present = set(_assign_column(c["top"], col_boundaries) for c in row_chars)
    return cols_present == {0}


def _is_page_number(row_chars: list[dict]) -> bool:
    """Check if a row is just a page number at the page edge."""
    text = _chars_to_text(row_chars).strip()
    if len(text) <= 3 and text.isdigit():
        x0_avg = sum(c["x0"] for c in row_chars) / len(row_chars)
        if x0_avg > 300:
            return True
    return False


def _assign_column(top_val: float, col_boundaries: tuple[float, float]) -> int:
    """Assign a char to a column based on its top coordinate."""
    if top_val > col_boundaries[0]:
        return 0  # Nature of report
    elif top_val > col_boundaries[1]:
        return 1  # Authority
    else:
        return 2  # When expected


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------


def _extract_page(
    page: pdfplumber.page.Page,
    page_num: int,
    col_boundaries: tuple[float, float],
    title_cutoff: float,
) -> list[dict]:
    """Extract classified rows from one page."""
    chars = page.chars
    data_chars = [c for c in chars if c["x0"] > title_cutoff]

    data_chars = _filter_dot_leaders(data_chars)
    data_chars = _filter_page_number_chars(data_chars)
    deduped = _deduplicate_chars(data_chars)
    logger.debug(
        "Page %d: %d data chars → %d after dedup",
        page_num,
        len(data_chars),
        len(deduped),
    )

    visual_rows = _group_into_visual_rows(deduped)
    visual_rows = [r for r in visual_rows if not _is_page_number(r)]

    results: list[dict] = []
    for row_chars in visual_rows:
        x0_avg = sum(c["x0"] for c in row_chars) / len(row_chars)

        if _is_entity_header(row_chars, col_boundaries):
            results.append(
                {
                    "type": "entity_header",
                    "x0": round(x0_avg, 1),
                    "text": _clean_text(_chars_to_text(row_chars)),
                }
            )
        else:
            col_chars: dict[int, list[dict]] = defaultdict(list)
            for c in row_chars:
                col = _assign_column(c["top"], col_boundaries)
                col_chars[col].append(c)

            row_data: dict = {"type": "data_line", "x0": round(x0_avg, 1)}
            for col_num, col_name in [
                (0, "nature"),
                (1, "authority"),
                (2, "when_expected"),
            ]:
                if col_num in col_chars:
                    row_data[col_name] = _clean_text(_chars_to_text(col_chars[col_num]))
                else:
                    row_data[col_name] = ""

            results.append(row_data)

    return results


# ---------------------------------------------------------------------------
# Logical row merging
# ---------------------------------------------------------------------------


def _is_new_logical_row(row: dict, current_row: dict | None) -> bool:
    """Determine if a data_line starts a new logical row."""
    nature = row.get("nature", "").strip()
    authority = row.get("authority", "").strip()

    if current_row is None:
        return True

    if not nature:
        return False

    # If the in-progress logical row's authority is mid-citation (doesn't
    # end with a closing paren), this row MUST be a continuation —
    # regardless of how its own nature/authority appear to start. Real
    # complete authorities always close with ")" after the Stat. citation.
    prev_auth = current_row.get("authority", "").rstrip()
    if prev_auth and not prev_auth.endswith(")"):
        return False

    first_alpha = next((ch for ch in nature if ch.isalpha()), "")
    nature_starts_lowercase = first_alpha and first_alpha.islower()

    if authority and _CITATION_START.match(authority):
        # A citation is present, but if nature starts with a lowercase letter
        # it's a continuation (e.g. "property or abrogated...") — the authority
        # just happens to wrap from the previous row starting with "Pub. L.".
        # Exception: nature starting with a digit (e.g. "5-year STEM...") where
        # the first alpha is lowercase but it's genuinely a new row.
        first_char = next((ch for ch in nature if ch.isalnum()), "")
        if first_char and first_char.isdigit():
            return True  # e.g. "5-year STEM education strategic plan"
        if nature_starts_lowercase:
            return False  # continuation with wrapped citation
        return True

    if nature_starts_lowercase:
        return False

    dots = sum(1 for ch in nature if ch == ".")
    if len(nature) > 0 and dots / len(nature) > 0.5:
        return False

    if authority and not _CITATION_START.match(authority):
        return False

    # No authority at all — almost certainly a continuation, not a new row.
    # New rows always have an authority citation on their first line.
    return False


def _merge_logical_rows(rows: list[dict]) -> list[dict]:
    """Merge consecutive data_line rows into logical rows."""
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
                # Continuation of multi-line entity header
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
            }
        elif current_row is not None:
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
            }

    if current_row:
        merged.append(current_row)

    return merged
