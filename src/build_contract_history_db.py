#!/usr/bin/env python3
"""Build a queryable SQLite database from the CanadaBuys Contract History CSVs."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path


AMOUNT_FIELDS = {
    "contractAmount-montantContrat",
    "totalContractValue-valeurTotaleContrat",
}
INDEXES = {
    "idx_contract_history_reference": ("reference_number",),
    "idx_contract_history_supplier": ("supplier_standardized_name_eng",),
    "idx_contract_history_award_date": ("contract_award_date",),
    "idx_contract_history_amount": ("contract_amount",),
}


def sql_name(header: str) -> str:
    """Convert the bilingual source header into a stable, readable SQL name."""
    language = ""
    base = header
    for suffix in ("-eng", "-fra"):
        if header.endswith(suffix):
            language = suffix[1:]
            base = header[: -len(suffix)]
            break
    base = base.split("-", 1)[0]
    base = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", base).lower()
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
    if language in {"eng", "fra"}:
        base = f"{base}_{language}"
    return base


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def read_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return next(csv.reader(source))


def create_schema(connection: sqlite3.Connection, headers: list[str]) -> dict[str, str]:
    names = [sql_name(header) for header in headers]
    if len(names) != len(set(names)):
        raise ValueError("Source headers produced duplicate SQLite column names")

    columns = [
        '"source_dataset" TEXT NOT NULL',
        '"source_row_number" INTEGER NOT NULL',
    ]
    for header, name in zip(headers, names):
        sqlite_type = "REAL" if header in AMOUNT_FIELDS else "TEXT"
        columns.append(f"{quote(name)} {sqlite_type}")

    connection.executescript(
        f"""
        CREATE TABLE contract_history (
            {", ".join(columns)},
            UNIQUE(source_dataset, source_row_number)
        );
        CREATE TABLE source_columns (
            ordinal INTEGER PRIMARY KEY,
            source_header TEXT NOT NULL UNIQUE,
            sqlite_column TEXT NOT NULL UNIQUE
        );
        CREATE VIEW contract_history_dates AS
        SELECT *
        FROM contract_history;
        """
    )
    connection.executemany(
        "INSERT INTO source_columns (ordinal, source_header, sqlite_column) VALUES (?, ?, ?)",
        ((ordinal, header, name) for ordinal, (header, name) in enumerate(zip(headers, names), 1)),
    )
    for index_name, columns_for_index in INDEXES.items():
        column_sql = ", ".join(quote(column) for column in columns_for_index)
        connection.execute(
            f"CREATE INDEX {quote(index_name)} ON contract_history ({column_sql})"
        )
    return dict(zip(headers, names))


def import_csv(
    connection: sqlite3.Connection,
    path: Path,
    dataset: str,
    headers: list[str],
    column_map: dict[str, str],
    batch_size: int,
) -> int:
    columns = ["source_dataset", "source_row_number", *[column_map[header] for header in headers]]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(quote(column) for column in columns)
    insert_sql = f"INSERT INTO contract_history ({column_sql}) VALUES ({placeholders})"
    amount_columns = {column_map[field] for field in AMOUNT_FIELDS}
    row_count = 0
    rows: list[tuple[object, ...]] = []

    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != headers:
            raise ValueError(f"Header mismatch in {path}")
        for row_count, row in enumerate(reader, 1):
            values: list[object] = [dataset, row_count]
            for header in headers:
                value = (row.get(header) or "").strip()
                column = column_map[header]
                if column in amount_columns:
                    try:
                        value = float(value.replace(",", "")) if value else None
                    except ValueError:
                        value = None
                values.append(value)
            rows.append(tuple(values))
            if len(rows) >= batch_size:
                connection.executemany(insert_sql, rows)
                rows.clear()
        if rows:
            connection.executemany(insert_sql, rows)
    return row_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=2_000)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.unlink()

    headers = read_header(args.legacy)
    if headers != read_header(args.current):
        raise ValueError("The two source CSVs do not have identical headers")

    with sqlite3.connect(args.output) as connection:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = NORMAL")
        column_map = create_schema(connection, headers)
        legacy_rows = import_csv(
            connection, args.legacy, "legacy", headers, column_map, args.batch_size
        )
        current_rows = import_csv(
            connection, args.current, "current", headers, column_map, args.batch_size
        )
        connection.execute("ANALYZE")
        connection.commit()

    print(f"Created {args.output}")
    print(f"Imported {legacy_rows:,} legacy rows and {current_rows:,} current rows")


if __name__ == "__main__":
    main()
