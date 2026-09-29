#!/usr/bin/env python3
"""Independently compare every parsed CSV cell with its Bronze Parquet cell."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconciliation", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.reconciliation.read_text(encoding="utf-8"))
    if report["gate"] != "passed":
        raise ValueError("Bronze reconciliation gate has not passed")

    connection = duckdb.connect()
    for source, info in report["sources"].items():
        with Path(info["source_file"]).open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            names = ", ".join(
                '"' + field.replace('"', '""') + '"' for field in header
            )
            cursor = connection.execute(
                f"SELECT {names} FROM read_parquet(?) ORDER BY source_row_number",
                [info["bronze_file"]],
            )
            compared = 0
            while batch := cursor.fetchmany(1000):
                for values in batch:
                    original = next(reader, None)
                    if original is None:
                        raise AssertionError(f"Extra Bronze row: {source} #{compared + 1}")
                    if values != tuple(original[field] for field in header):
                        raise AssertionError(
                            f"Raw-field mismatch: {source} #{compared + 1}"
                        )
                    compared += 1
            if next(reader, None) is not None or compared != info["rows"]:
                raise AssertionError(f"Row-count mismatch for {source}")
            print(
                f"{source}: PASS {compared:,} rows × {len(header)} "
                "original fields exactly matched"
            )
    connection.close()


if __name__ == "__main__":
    main()
