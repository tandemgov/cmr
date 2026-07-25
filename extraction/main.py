"""CLI entry point for cmra PDF table extraction."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from extract import extract


def main():
    parser = argparse.ArgumentParser(
        description="Extract structured data from government PDF report tables."
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the PDF file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None, help="Output file (default: stdout)"
    )
    parser.add_argument(
        "--format",
        choices=["jsonl", "json", "csv"],
        default="jsonl",
        help="Output format (default: jsonl)",
    )
    args = parser.parse_args()

    rows = extract(args.pdf_path)

    out = open(args.output, "w") if args.output else sys.stdout
    try:
        if args.format == "jsonl":
            for row in rows:
                print(json.dumps(row.model_dump(), ensure_ascii=False), file=out)
        elif args.format == "json":
            print(
                json.dumps(
                    [row.model_dump() for row in rows], ensure_ascii=False, indent=2
                ),
                file=out,
            )
        elif args.format == "csv":
            writer = csv.DictWriter(
                out,
                fieldnames=[
                    "reporting_entity",
                    "nature_of_report",
                    "authority",
                    "when_expected",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row.model_dump())
    finally:
        if args.output:
            out.close()

    if args.output:
        print(f"Wrote {len(rows)} rows to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
