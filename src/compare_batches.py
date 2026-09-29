#!/usr/bin/env python3
"""Diff two immutable batches and report what the publisher changed.

Reports added, removed and restated records on the candidate version key. This
only works because batches are never overwritten. Restatement is reported, not
judged: it may well be a correction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

LINEAGE = ("batch_id", "source_dataset", "source_file", "source_row_number",
           "ingested_at_utc", "source_sha256", "vk")
VERSION_KEY_FIELDS = ("referenceNumber-numeroReference",
                      "contractNumber-numeroContrat",
                      "amendmentNumber-numeroModification")
# Unit separator: cannot occur in the source text, so the composite key is
# unambiguous where a plain concatenation would not be.
SEPARATOR = "chr(31)"
MONEY_FIELD = "totalContractValue-valeurTotaleContrat"


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-bronze", type=Path, required=True)
    parser.add_argument("--new-bronze", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    key = f" || {SEPARATOR} || ".join(f'"{f}"' for f in VERSION_KEY_FIELDS)
    connection = duckdb.connect()
    connection.execute("SET threads = 4")
    for alias, path in (("old", args.old_bronze), ("new", args.new_bronze)):
        connection.execute(
            f"CREATE VIEW {alias} AS SELECT *, {key} AS vk "
            f"FROM read_parquet({quoted(path / '*.parquet')})"
        )
    columns = [
        row[0] for row in connection.execute("DESCRIBE SELECT * FROM old").fetchall()
        if row[0] not in LINEAGE
    ]
    differs = " OR ".join(f'a."{c}" IS DISTINCT FROM b."{c}"' for c in columns)

    counts = connection.execute(f"""
        SELECT (SELECT count(*) FROM old) AS old_rows,
               (SELECT count(*) FROM new) AS new_rows,
               (SELECT count(*) FROM (SELECT vk FROM new EXCEPT SELECT vk FROM old)) AS added,
               (SELECT count(*) FROM (SELECT vk FROM old EXCEPT SELECT vk FROM new)) AS removed,
               (SELECT count(*) FROM old a JOIN new b USING (vk) WHERE {differs}) AS restated
    """).df().to_dict("records")[0]

    per_field = " UNION ALL ".join(
        f"""SELECT {quoted(c)} AS field, count(*) AS rows_changed
            FROM old a JOIN new b USING (vk) WHERE a."{c}" IS DISTINCT FROM b."{c}" """
        for c in columns
    )
    field_churn = connection.execute(
        f"SELECT * FROM ({per_field}) WHERE rows_changed > 0 ORDER BY rows_changed DESC"
    ).df()

    money = connection.execute(f"""
        WITH d AS (
            SELECT a.vk,
                try_cast(nullif(a."{MONEY_FIELD}", '') AS DECIMAL(24,2)) AS before_value,
                try_cast(nullif(b."{MONEY_FIELD}", '') AS DECIMAL(24,2)) AS after_value
            FROM old a JOIN new b USING (vk)
            WHERE a."{MONEY_FIELD}" IS DISTINCT FROM b."{MONEY_FIELD}"
        )
        SELECT count(*) AS rows_changed,
            count(*) FILTER (WHERE after_value > before_value) AS increased,
            count(*) FILTER (WHERE after_value < before_value) AS decreased,
            round(sum(after_value - before_value), 2) AS net_change_cad,
            round(max(abs(after_value - before_value)), 2) AS largest_single_change_cad
        FROM d
    """).df().to_dict("records")[0]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, sql in (
        ("added_records", "SELECT * EXCLUDE (vk) FROM new WHERE vk IN (SELECT vk FROM new EXCEPT SELECT vk FROM old)"),
        ("removed_records", "SELECT * EXCLUDE (vk) FROM old WHERE vk IN (SELECT vk FROM old EXCEPT SELECT vk FROM new)"),
    ):
        connection.execute(
            f"COPY ({sql}) TO {quoted(args.output_dir / (name + '.parquet'))} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    connection.execute(
        f"""COPY (
            SELECT a.vk AS version_key, a.source_dataset,
                a."{MONEY_FIELD}" AS before_value, b."{MONEY_FIELD}" AS after_value
            FROM old a JOIN new b USING (vk)
            WHERE a."{MONEY_FIELD}" IS DISTINCT FROM b."{MONEY_FIELD}"
        ) TO {quoted(args.output_dir / 'restated_total_contract_value.parquet')}
        (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )

    report = {
        "old_batch": args.old_bronze.name,
        "new_batch": args.new_bronze.name,
        "row_counts": {k: int(v) for k, v in counts.items()},
        "restated_pct_of_common": round(
            100 * counts["restated"] / max(counts["old_rows"] - counts["removed"], 1), 3
        ),
        "field_churn": field_churn.to_dict("records"),
        "total_contract_value_restatement": {
            k: (float(v) if v == v else None) for k, v in money.items()
        },
        "interpretation": [
            "A restatement is reported, not judged: it may be a correction.",
            "Detection depends on batches being immutable. Overwriting Bronze "
            "would make every restatement here invisible.",
            "A removed record means a row that was published is no longer "
            "published. That warrants a question to the publisher.",
        ],
    }
    (args.output_dir / "batch_comparison.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    print(f"{counts['old_rows']:,} -> {counts['new_rows']:,} rows")
    print(f"  added     {counts['added']:,}")
    print(f"  removed   {counts['removed']:,}")
    print(f"  restated  {counts['restated']:,}  ({report['restated_pct_of_common']}% of common rows)")
    print(f"\ntotalContractValue restated on {money['rows_changed']:,} rows")
    print(f"  net change CAD {money['net_change_cad']:,.2f}")
    print(f"  largest single CAD {money['largest_single_change_cad']:,.2f}")
    print(f"\nTop changed fields:")
    for row in field_churn.head(6).itertuples():
        print(f"  {row.field:<55} {row.rows_changed:>7,}")
    print(f"\n-> {args.output_dir}")
    connection.close()


if __name__ == "__main__":
    main()
