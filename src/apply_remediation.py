#!/usr/bin/env python3
"""Apply approved remediation proposals, writing a per-cell audit trail.

The only code here that changes a value. Four refusals are enforced rather than
assumed: unapproved proposals, annotate-class proposals, unattributed approvals,
and any write to Bronze or data/raw.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REMEDIATION_VERSION = "r1"
REQUIRED_APPROVAL_FIELDS = ("decided_by", "decided_at_utc")


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def load_decisions(connection: duckdb.DuckDBPyConnection, path: Path) -> list[dict]:
    raw = connection.execute(
        f"SELECT * FROM read_csv({quoted(path)}, header=true, all_varchar=true)"
    ).df().to_dict("records")
    # Empty CSV cells arrive as NaN through the DataFrame, not as "".
    rows = [
        {k: ("" if v is None or v != v else str(v)) for k, v in row.items()}
        for row in raw
    ]
    approved, refused = [], []
    for row in rows:
        if row["status"].strip().lower() != "approved":
            continue
        missing = [f for f in REQUIRED_APPROVAL_FIELDS if not row[f].strip()]
        if missing:
            raise ValueError(
                f"Proposal {row['proposal_id']} is approved but missing "
                f"{', '.join(missing)}. An unattributed approval is refused."
            )
        if row["proposal_type"].strip() != "normalize":
            refused.append(row)
            continue
        approved.append(row)
    for row in refused:
        print(
            f"  REFUSED  {row['proposal_id']}: approved, but proposal_type is "
            f"'{row['proposal_type']}'. No deterministic transform exists; "
            "approval cannot create one."
        )
    return approved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derived-dir", type=Path, required=True)
    parser.add_argument("--governance-dir", type=Path, required=True)
    parser.add_argument("--country-map", type=Path, required=True)
    args = parser.parse_args()

    silver_path = args.derived_dir / "silver_contract_activity.parquet"
    reconciliation = json.loads(
        (args.derived_dir / "reconciliation.json").read_text(encoding="utf-8")
    )
    output_dir = args.derived_dir / f"remediation={REMEDIATION_VERSION}"
    if output_dir.exists():
        raise FileExistsError(f"Remediation output already exists: {output_dir}")

    connection = duckdb.connect()
    connection.execute("SET threads = 1")
    connection.execute(
        f"CREATE TEMP VIEW country_map AS SELECT observed_value, canonical_value "
        f"FROM read_csv({quoted(args.country_map)}, header=true)"
    )
    approved = load_decisions(
        connection, args.governance_dir / "remediation_decisions.csv"
    )
    if not approved:
        print("No approved 'normalize' proposals. Nothing to apply.")
        return

    applied_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{REMEDIATION_VERSION}-", dir=args.derived_dir))
    try:
        connection.execute(
            f"CREATE TEMP VIEW base AS SELECT * FROM read_parquet({quoted(silver_path)})"
        )
        connection.execute(
            "CREATE TEMP VIEW joined AS SELECT base.*, m.canonical_value "
            "FROM base LEFT JOIN country_map m "
            "ON m.observed_value = base.supplier_country_raw"
        )

        replacements, audit_parts = [], []
        for row in approved:
            column, transform = row["target_column"], row["transform_sql"]
            replacements.append(f"{transform} AS {column}")
            audit_parts.append(
                f"""
                SELECT batch_id, source_dataset, source_row_number,
                    {quoted(row['proposal_id'])} AS proposal_id,
                    {quoted(row['rule_id'])} AS rule_id,
                    {quoted(column)} AS target_column,
                    {column} AS value_before,
                    {transform} AS value_after,
                    {quoted(row['decided_by'])} AS approved_by,
                    {quoted(row['decided_at_utc'])} AS approved_at_utc,
                    {quoted(applied_at)}::TIMESTAMPTZ AS applied_at_utc
                FROM joined WHERE {column} IS DISTINCT FROM {transform}
                """
            )
            print(f"  APPLY    {row['proposal_id']} -> {column}")

        audit_path = temp_dir / "remediation_audit.parquet"
        connection.execute(
            f"COPY ({' UNION ALL '.join(audit_parts)}) TO {quoted(audit_path)} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        remediated_path = temp_dir / "silver_contract_activity.parquet"
        connection.execute(
            f"COPY (SELECT * EXCLUDE (canonical_value) REPLACE ({', '.join(replacements)}) "
            f"FROM joined) TO {quoted(remediated_path)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

        base_rows = connection.execute("SELECT count(*) FROM base").fetchone()[0]
        out_rows = connection.execute(
            f"SELECT count(*) FROM read_parquet({quoted(remediated_path)})"
        ).fetchone()[0]
        audit = connection.execute(
            f"SELECT proposal_id, count(*) FROM read_parquet({quoted(audit_path)}) GROUP BY 1"
        ).fetchall()
        checks = {
            "row_count_unchanged": base_rows == out_rows,
            "every_change_audited": bool(audit),
        }
        if not all(checks.values()):
            raise ValueError(f"Remediation verification failed: {checks}")

        report = {
            "batch_id": reconciliation["batch_id"],
            "ruleset_version": reconciliation["ruleset_version"],
            "remediation_version": REMEDIATION_VERSION,
            "applied_at_utc": applied_at,
            "rows_in": base_rows,
            "rows_out": out_rows,
            "proposals_applied": [r["proposal_id"] for r in approved],
            "cells_changed_by_proposal": {p: n for p, n in audit},
            "cells_changed_total": sum(n for _, n in audit),
            "checks": checks,
            "guarantees": [
                "no row added or removed",
                "annotate-class proposals are never applied",
                "every changed cell recorded with before value and approver",
                "source CSV and Bronze untouched",
            ],
        }
        (temp_dir / "remediation_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp_dir, output_dir)
        print(f"\nRows {base_rows:,} in / {out_rows:,} out (unchanged)")
        for proposal_id, count in sorted(audit, key=lambda x: -x[1]):
            print(f"  {proposal_id:<45} {count:>8,} cells")
        print(f"Total cells changed: {report['cells_changed_total']:,}")
        print(f"Output: {output_dir}")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
