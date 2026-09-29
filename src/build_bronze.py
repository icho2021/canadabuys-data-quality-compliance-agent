#!/usr/bin/env python3
"""Build an immutable, raw-faithful Bronze layer from Contract History CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


METADATA_COLUMNS = [
    "batch_id",
    "source_dataset",
    "source_file",
    "source_row_number",
    "ingested_at_utc",
    "source_sha256",
]
REQUIRED_SOURCE_IDS = {
    "legacy_contract_history": "legacy",
    "current_contract_history": "current",
}


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_manifest(path: Path) -> tuple[str, dict[str, dict[str, str]]]:
    """Parse only the stable scalar fields needed from this project's YAML."""
    batch_id = ""
    resources: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if line.startswith("batch_id:"):
            batch_id = unquote(line.split(":", 1)[1])
        elif stripped.startswith("- source_id:"):
            source_id = unquote(stripped.split(":", 1)[1])
            current = {"source_id": source_id}
            resources[source_id] = current
        elif current is not None and line.startswith("    ") and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key] = unquote(value)
    if not batch_id:
        raise ValueError(f"Missing batch_id in {path}")
    return batch_id, resources


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_header_and_first_row(path: Path) -> tuple[list[str], dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        header = reader.fieldnames or []
        first = next(reader)
    return header, {field: first.get(field) or "" for field in header}


def sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_source_parquet(
    connection: duckdb.DuckDBPyConnection,
    *,
    batch_id: str,
    source_dataset: str,
    source_file: str,
    source_path: Path,
    source_sha256: str,
    ingested_at: str,
    output_path: Path,
) -> None:
    query = f"""
        COPY (
            SELECT
                {sql_literal(batch_id)}::VARCHAR AS batch_id,
                {sql_literal(source_dataset)}::VARCHAR AS source_dataset,
                {sql_literal(source_file)}::VARCHAR AS source_file,
                row_number() OVER ()::BIGINT AS source_row_number,
                {sql_literal(ingested_at)}::TIMESTAMPTZ AS ingested_at_utc,
                {sql_literal(source_sha256)}::VARCHAR AS source_sha256,
                *
            FROM read_csv(
                {sql_literal(source_path)},
                header = true,
                all_varchar = true,
                nullstr = '__CANADABUYS_NO_NULL_SENTINEL__',
                strict_mode = true
            )
        )
        TO {sql_literal(output_path)}
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
    """
    connection.execute(query)


def validate_parquet(
    connection: duckdb.DuckDBPyConnection,
    *,
    parquet_path: Path,
    expected_rows: int,
    expected_header: list[str],
    expected_first_row: dict[str, str],
) -> dict[str, Any]:
    parquet = sql_literal(parquet_path)
    stats = connection.execute(
        f"""
        SELECT
            count(*) AS row_count,
            count(DISTINCT source_row_number) AS distinct_row_numbers,
            min(source_row_number) AS min_row_number,
            max(source_row_number) AS max_row_number,
            count(*) FILTER (
                WHERE batch_id IS NULL
                   OR source_dataset IS NULL
                   OR source_file IS NULL
                   OR source_row_number IS NULL
                   OR ingested_at_utc IS NULL
                   OR source_sha256 IS NULL
            ) AS missing_lineage_rows
        FROM read_parquet({parquet})
        """
    ).fetchone()
    described = connection.execute(
        f"DESCRIBE SELECT * FROM read_parquet({parquet})"
    ).fetchall()
    output_columns = [row[0] for row in described]
    source_columns = output_columns[len(METADATA_COLUMNS) :]
    quoted_fields = ", ".join(
        '"' + field.replace('"', '""') + '"' for field in expected_header
    )
    first_output = connection.execute(
        f"""
        SELECT {quoted_fields}
        FROM read_parquet({parquet})
        WHERE source_row_number = 1
        """
    ).fetchone()
    first_output_dict = dict(zip(expected_header, first_output, strict=True))
    checks = {
        "row_count_matches_manifest": stats[0] == expected_rows,
        "source_row_number_unique": stats[1] == expected_rows,
        "source_row_number_contiguous": stats[2] == 1 and stats[3] == expected_rows,
        "lineage_complete": stats[4] == 0,
        "source_header_preserved": source_columns == expected_header,
        "first_source_row_preserved": first_output_dict == expected_first_row,
    }
    return {
        "rows": stats[0],
        "columns": len(output_columns),
        "metadata_columns": METADATA_COLUMNS,
        "source_columns": len(source_columns),
        "checks": checks,
        "gate": "passed" if all(checks.values()) else "failed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--reconciliation-output", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    project_root = manifest_path.parents[2]
    batch_id, manifest_resources = parse_manifest(manifest_path)
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    if profile.get("profiling_gate") != "passed":
        raise ValueError("Profiling gate must pass before Bronze ingestion")
    final_dir = args.output_root / f"batch_id={batch_id}"
    if final_dir.exists():
        raise FileExistsError(
            f"Bronze batch already exists: {final_dir}. "
            "Use a new batch id; immutable batches are not overwritten."
        )

    specs: list[dict[str, Any]] = []
    common_header: list[str] | None = None
    for source_id, source_dataset in REQUIRED_SOURCE_IDS.items():
        resource = manifest_resources.get(source_id)
        if resource is None:
            raise ValueError(f"Manifest is missing {source_id}")
        source_path = project_root / resource["local_path"]
        actual_sha256 = sha256_file(source_path)
        if actual_sha256 != resource["sha256"]:
            raise ValueError(f"Checksum mismatch for {source_path}")
        header, first_row = read_header_and_first_row(source_path)
        if common_header is None:
            common_header = header
        elif header != common_header:
            raise ValueError("The two Contract History headers do not match")
        if header != profile[source_dataset]["header"]:
            raise ValueError(f"Header differs from approved profile: {source_dataset}")
        if int(resource["parser_records"]) != profile[source_dataset]["rows"]:
            raise ValueError(f"Row count differs from approved profile: {source_dataset}")
        specs.append(
            {
                "source_id": source_id,
                "source_dataset": source_dataset,
                "source_file": resource["local_path"],
                "source_path": source_path,
                "source_sha256": actual_sha256,
                "expected_rows": int(resource["parser_records"]),
                "header": header,
                "first_row": first_row,
            }
        )

    args.output_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{batch_id}-", dir=args.output_root)
    )
    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    reconciliation: dict[str, Any] = {
        "batch_id": batch_id,
        "source_scope": "CanadaBuys Contract History only",
        "ingested_at_utc": ingested_at,
        "bronze_contract": {
            "raw_value_policy": "all 91 source fields preserved as strings",
            "empty_value_policy": "source empty strings preserved as empty strings",
            "lineage_columns": METADATA_COLUMNS,
            "mutation_policy": "immutable batch; existing batch directories are not overwritten",
        },
        "sources": {},
    }

    connection = duckdb.connect()
    connection.execute("SET threads = 1")
    connection.execute("SET preserve_insertion_order = true")
    try:
        for spec in specs:
            parquet_name = f"{spec['source_dataset']}.parquet"
            temp_parquet = temp_dir / parquet_name
            build_source_parquet(
                connection,
                batch_id=batch_id,
                source_dataset=spec["source_dataset"],
                source_file=spec["source_file"],
                source_path=spec["source_path"],
                source_sha256=spec["source_sha256"],
                ingested_at=ingested_at,
                output_path=temp_parquet,
            )
            validation = validate_parquet(
                connection,
                parquet_path=temp_parquet,
                expected_rows=spec["expected_rows"],
                expected_header=spec["header"],
                expected_first_row=spec["first_row"],
            )
            validation.update(
                {
                    "source_id": spec["source_id"],
                    "source_file": spec["source_file"],
                    "source_sha256": spec["source_sha256"],
                    "bronze_file": str(final_dir / parquet_name),
                    "bronze_bytes": temp_parquet.stat().st_size,
                    "bronze_sha256": sha256_file(temp_parquet),
                }
            )
            reconciliation["sources"][spec["source_dataset"]] = validation
        reconciliation["total_rows"] = sum(
            source["rows"] for source in reconciliation["sources"].values()
        )
        reconciliation["gate"] = (
            "passed"
            if all(
                source["gate"] == "passed"
                for source in reconciliation["sources"].values()
            )
            else "failed"
        )
        if reconciliation["gate"] != "passed":
            raise ValueError("Bronze reconciliation gate failed")
        os.replace(temp_dir, final_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        connection.close()

    args.reconciliation_output.parent.mkdir(parents=True, exist_ok=True)
    temp_reconciliation = args.reconciliation_output.with_suffix(".json.tmp")
    temp_reconciliation.write_text(
        json.dumps(reconciliation, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_reconciliation, args.reconciliation_output)
    print(f"Bronze gate: {reconciliation['gate']}")
    print(f"Rows written: {reconciliation['total_rows']:,}")
    print(f"Output: {final_dir}")


if __name__ == "__main__":
    main()
