#!/usr/bin/env python3
"""Fault injection: prove every gate and rule can actually fail.

A rule reporting zero hits and a rule that is silently broken produce identical
output. This breaks a real slice of the batch once per control and asserts that
exactly the intended control fires and no other one moves. data/raw is only
ever read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

SRC = Path(__file__).resolve().parent
PROJECT_ROOT = SRC.parent
SAMPLE_ROWS_PER_SOURCE = 4000
AS_OF_DATE = "2026-08-22"
FIXTURE_BATCH_ID = "faultinjection_fixture_001"
LINEAGE_COLUMNS = (
    "batch_id",
    "source_dataset",
    "source_file",
    "source_row_number",
    "ingested_at_utc",
    "source_sha256",
)


@dataclass
class Scenario:
    """One deliberate defect and what it must produce."""

    name: str
    tier: str
    targets: str
    description: str
    # pipeline tier
    expect_stage: str = ""
    expect_message: str = ""
    # rule tier
    expect_rule: str = ""
    expect_delta: int = 0
    mutations: list[dict] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------


def build_fixture(bronze_dir: Path, work: Path) -> dict[str, dict]:
    """Write a CSV slice per source, reconstructed from Bronze raw strings.

    Bronze preserves every source field as its original string, and
    verify_bronze_values.py proves that cell by cell, so a Bronze slice is a
    faithful stand-in for the raw CSV.
    """
    connection = duckdb.connect()
    sources = {}
    for source_dataset in ("legacy", "current"):
        parquet = bronze_dir / f"{source_dataset}.parquet"
        described = connection.execute(
            f"DESCRIBE SELECT * FROM read_parquet({quoted(parquet)})"
        ).fetchall()
        source_columns = [
            row[0] for row in described if row[0] not in LINEAGE_COLUMNS
        ]
        quoted_cols = ", ".join('"' + c.replace('"', '""') + '"' for c in source_columns)
        rows = connection.execute(
            f"SELECT {quoted_cols} FROM read_parquet({quoted(parquet)}) "
            f"ORDER BY source_row_number LIMIT {SAMPLE_ROWS_PER_SOURCE}"
        ).fetchall()
        target = work / "raw" / source_dataset / "contract_history.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(source_columns)
            writer.writerows(rows)
        sources[source_dataset] = {
            "path": target,
            "columns": source_columns,
            "rows": len(rows),
        }
    connection.close()
    return sources


def write_manifest(
    work: Path,
    sources: dict[str, dict],
    path: Path,
    record_count_override: dict[str, int] | None = None,
) -> None:
    source_ids = {
        "legacy": "legacy_contract_history",
        "current": "current_contract_history",
    }
    override = record_count_override or {}
    lines = [
        f"batch_id: {FIXTURE_BATCH_ID}",
        "status: fault_injection_fixture",
        "resources:",
    ]
    for source_dataset, info in sources.items():
        rel = info["path"].relative_to(work)
        declared = override.get(source_dataset, info["rows"])
        lines += [
            f"  - source_id: {source_ids[source_dataset]}",
            f"    local_path: {rel}",
            f"    parser_records: {declared}",
            "    columns: 91",
            f"    sha256: {sha256_file(info['path'])}",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


def mutate_csv(path: Path, mutations: list[dict]) -> None:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    header, body = rows[0], rows[1:]
    for mutation in mutations:
        kind = mutation["kind"]
        if kind == "rename_column":
            header[header.index(mutation["column"])] = mutation["to"]
        elif kind == "delete_row":
            body.pop(mutation["row"])
        elif kind == "duplicate_row":
            body.append(list(body[mutation["row"]]))
        elif kind == "set_cell":
            body[mutation["row"]][header.index(mutation["column"])] = mutation["value"]
        else:
            raise ValueError(f"Unknown CSV mutation: {kind}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        writer.writerows(body)


def mutate_bronze(source_parquet: Path, target: Path, mutations: list[dict]) -> None:
    """Rewrite a Bronze Parquet with the given cells overwritten on row 1."""
    connection = duckdb.connect()
    assignments = []
    for mutation in mutations:
        column = mutation["column"].replace('"', '""')
        assignments.append(
            f'CASE WHEN source_row_number = {int(mutation["row"])} '
            f"THEN {quoted(mutation['value'])} ELSE \"{column}\" END "
            f'AS "{column}"'
        )
    replace_clause = ", ".join(assignments)
    connection.execute(
        f"COPY (SELECT * REPLACE ({replace_clause}) "
        f"FROM read_parquet({quoted(source_parquet)})) "
        f"TO {quoted(target)} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    connection.close()


# --------------------------------------------------------------------------
# Stage runners
# --------------------------------------------------------------------------


def run_stage(script: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SRC / script), *args],
        capture_output=True,
        text=True,
    )


def rule_counts(derived_dir: Path) -> dict[str, int]:
    summary = derived_dir / "gold_quality_summary.parquet"
    connection = duckdb.connect()
    rows = connection.execute(
        f"SELECT rule_id, sum(finding_count)::BIGINT "
        f"FROM read_parquet({quoted(summary)}) GROUP BY 1"
    ).fetchall()
    connection.close()
    return {rule_id: int(count) for rule_id, count in rows}


def build_quality(bronze_dir: Path, work: Path, tag: str) -> dict[str, int]:
    """Run the quality layer against a Bronze directory using stub upstream reports."""
    profile = work / f"profile_{tag}.json"
    reconciliation = work / f"bronze_{tag}.json"
    connection = duckdb.connect()
    counts = {
        source: connection.execute(
            f"SELECT count(*) FROM read_parquet({quoted(bronze_dir / f'{source}.parquet')})"
        ).fetchone()[0]
        for source in ("legacy", "current")
    }
    connection.close()
    profile.write_text(
        json.dumps(
            {
                "profiling_gate": "passed",
                "legacy": {"rows": counts["legacy"]},
                "current": {"rows": counts["current"]},
            }
        ),
        encoding="utf-8",
    )
    reconciliation.write_text(
        json.dumps(
            {
                "gate": "passed",
                "batch_id": f"{FIXTURE_BATCH_ID}_{tag}",
                "total_rows": sum(counts.values()),
                "sources": {
                    source: {
                        "rows": counts[source],
                        "bronze_file": str(bronze_dir / f"{source}.parquet"),
                    }
                    for source in ("legacy", "current")
                },
            }
        ),
        encoding="utf-8",
    )
    output_root = work / "derived"
    result = run_stage(
        "build_quality_layers.py",
        [
            "--profile", str(profile),
            "--bronze-reconciliation", str(reconciliation),
            "--bronze-dir", str(bronze_dir),
            "--output-root", str(output_root),
            "--as-of-date", AS_OF_DATE,
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(f"Quality layer failed for {tag}:\n{result.stderr}")
    derived = next(
        (output_root / f"batch_id={FIXTURE_BATCH_ID}_{tag}").glob("ruleset=*")
    )
    return rule_counts(derived)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

PIPELINE_SCENARIOS = [
    Scenario(
        name="checksum_mismatch",
        tier="pipeline",
        targets="MF-01",
        description="One byte of the legacy CSV changes after the manifest is written",
        expect_stage="build_bronze.py",
        expect_message="Checksum mismatch",
        mutations=[{"source": "legacy", "kind": "set_cell", "row": 0,
                    "column": "title-titre-eng", "value": "FAULT INJECTION"}],
    ),
    Scenario(
        name="renamed_column",
        tier="pipeline",
        targets="PF-01 / MF-02",
        description="A published column is renamed in the current file only",
        expect_stage="profile_contract_history.py",
        expect_message="",
        mutations=[{"source": "current", "kind": "rename_column",
                    "column": "contractAmount-montantContrat", "to": "contractValue"}],
    ),
    Scenario(
        name="dropped_row",
        tier="pipeline",
        targets="MF-01",
        description="A row disappears from the legacy CSV after the manifest is written. "
        "Caught by the checksum before the row count is ever compared.",
        expect_stage="build_bronze.py",
        expect_message="Checksum mismatch",
        mutations=[{"source": "legacy", "kind": "delete_row", "row": 10}],
    ),
    Scenario(
        name="manifest_row_count_mismatch",
        tier="pipeline",
        targets="MF-04",
        description="The CSV and its checksum agree, but the manifest declares the "
        "wrong record count. Isolates MF-04, which the dropped_row case cannot "
        "reach because MF-01 fires first.",
        expect_stage="build_bronze.py",
        expect_message="Row count differs from approved profile",
        mutations=[{"kind": "declare_wrong_record_count", "source": "legacy",
                    "value": -1}],
    ),
    Scenario(
        name="legacy_cutover_violation",
        tier="pipeline",
        targets="PF-04",
        description="A legacy row is awarded after the 2023-05-31 cutover",
        expect_stage="profile_gate",
        expect_message="legacy_cutover_respected",
        mutations=[{"source": "legacy", "kind": "set_cell", "row": 0,
                    "column": "contractAwardDate-dateAttributionContrat",
                    "value": "2024-01-15"}],
    ),
    Scenario(
        name="duplicate_version_key",
        tier="pipeline",
        targets="PF-06",
        description="A contract version key appears twice; must quarantine, not abort",
        expect_stage="profile_quarantine",
        expect_message="candidate_version_key_duplicates",
        mutations=[{"source": "legacy", "kind": "duplicate_row", "row": 0}],
    ),
]

RULE_SCENARIOS = [
    Scenario(
        name="invalid_award_date", tier="rule", targets="invalid_award_date",
        description="A date the parser cannot read",
        expect_rule="invalid_award_date", expect_delta=1,
        mutations=[{"source": "current", "row": 1,
                    "column": "contractAwardDate-dateAttributionContrat",
                    "value": "15/03/2024"}],
    ),
    Scenario(
        name="invalid_contract_amount", tier="rule", targets="invalid_contract_amount",
        description="A thousands separator makes the amount unparseable",
        expect_rule="invalid_contract_amount", expect_delta=1,
        mutations=[{"source": "current", "row": 1,
                    "column": "contractAmount-montantContrat", "value": "1,234.00"}],
    ),
    Scenario(
        name="current_cutover_mismatch", tier="rule", targets="current_cutover_mismatch",
        description="A current-file row awarded before the cutover",
        expect_rule="current_cutover_mismatch", expect_delta=1,
        mutations=[{"source": "current", "row": 1,
                    "column": "contractAwardDate-dateAttributionContrat",
                    "value": "2022-01-15"}],
    ),
    Scenario(
        name="legacy_cutover_mismatch", tier="rule", targets="legacy_cutover_mismatch",
        description="A legacy-file row awarded after the cutover",
        expect_rule="legacy_cutover_mismatch", expect_delta=1,
        mutations=[{"source": "legacy", "row": 1,
                    "column": "contractAwardDate-dateAttributionContrat",
                    "value": "2024-01-15"}],
    ),
    Scenario(
        name="unexpected_currency", tier="rule", targets="unexpected_currency",
        description="A currency other than the only one observed in the batch",
        expect_rule="unexpected_currency", expect_delta=1,
        mutations=[{"source": "current", "row": 1,
                    "column": "contractCurrency-contratMonnaie", "value": "USD"}],
    ),
    Scenario(
        name="negative_total_contract_value", tier="rule",
        targets="negative_total_contract_value",
        description="A negative cumulative contract value",
        expect_rule="negative_total_contract_value", expect_delta=1,
        mutations=[{"source": "current", "row": 2,
                    "column": "totalContractValue-valeurTotaleContrat", "value": "-500.00"}],
    ),
    Scenario(
        name="end_date_before_start_date", tier="rule",
        targets="end_date_before_start_date",
        description="A contract that ends before it starts",
        expect_rule="end_date_before_start_date", expect_delta=1,
        mutations=[
            {"source": "current", "row": 3,
             "column": "contractStartDate-contratDateDebut", "value": "2024-06-01"},
            {"source": "current", "row": 3,
             "column": "contractEndDate-dateFinContrat", "value": "2024-01-01"},
        ],
    ),
    Scenario(
        name="unusually_distant_end_date", tier="rule",
        targets="unusually_distant_end_date",
        description="A sentinel-like far-future expiry date",
        expect_rule="unusually_distant_end_date", expect_delta=1,
        mutations=[{"source": "current", "row": 4,
                    "column": "contractEndDate-dateFinContrat", "value": "9999-09-09"}],
    ),
    Scenario(
        name="non_canonical_supplier_country", tier="rule",
        targets="non_canonical_supplier_country",
        description="An ISO-2 country code where the canonical form is the full name",
        expect_rule="non_canonical_supplier_country", expect_delta=1,
        mutations=[{"source": "legacy", "row": 2,
                    "column": "supplierAddressCountry-fournisseurAdressePays-eng",
                    "value": "CA"}],
    ),
]


def run_pipeline_scenario(
    scenario: Scenario, bronze_dir: Path, work: Path
) -> dict:
    case = work / "cases" / scenario.name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    sources = build_fixture(bronze_dir, case)
    manifest = case / "data" / "manifest" / "fixture.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)

    # Mutations applied before the manifest is written are "the source really
    # looked like this". Mutations applied after are "the source changed behind
    # our back", which is what the checksum exists to catch.
    mutates_behind_manifest = scenario.name in {"checksum_mismatch", "dropped_row"}
    overrides: dict[str, int] = {}
    pre_manifest, post_manifest = [], []
    for mutation in scenario.mutations:
        if mutation["kind"] == "declare_wrong_record_count":
            overrides[mutation["source"]] = (
                sources[mutation["source"]]["rows"] + mutation["value"]
            )
        elif mutates_behind_manifest:
            post_manifest.append(mutation)
        else:
            pre_manifest.append(mutation)
    for mutation in pre_manifest:
        mutate_csv(sources[mutation["source"]]["path"], [mutation])
        if mutation["kind"] == "delete_row":
            sources[mutation["source"]]["rows"] -= 1
        if mutation["kind"] == "duplicate_row":
            sources[mutation["source"]]["rows"] += 1
    write_manifest(case, sources, manifest, record_count_override=overrides)
    for mutation in post_manifest:
        mutate_csv(sources[mutation["source"]]["path"], [mutation])

    profile_out = case / "profile.json"
    result = run_stage(
        "profile_contract_history.py",
        [
            "--legacy", str(sources["legacy"]["path"]),
            "--current", str(sources["current"]["path"]),
            "--output", str(profile_out),
            "--field-map-output", str(case / "field_map.csv"),
        ],
    )
    observed = {"stage": "profile_contract_history.py", "returncode": result.returncode}
    if result.returncode != 0:
        observed["detail"] = result.stderr.strip().splitlines()[-1]
        caught = scenario.expect_stage == "profile_contract_history.py"
        return {"observed": observed, "passed": caught}

    report = json.loads(profile_out.read_text(encoding="utf-8"))
    if scenario.expect_stage == "profile_gate":
        failed_conditions = [
            key for key in ("legacy_cutover_respected", "current_cutover_respected")
            if report["checks"].get(key) is False
        ]
        observed.update(gate=report["profiling_gate"], failed=failed_conditions)
        return {
            "observed": observed,
            "passed": report["profiling_gate"] == "failed"
            and scenario.expect_message in failed_conditions,
        }
    if scenario.expect_stage == "profile_quarantine":
        observed.update(
            gate=report["profiling_gate"], quarantine=report.get("quarantine"),
            duplicates=report["checks"]["cross_source_candidate_version_key_duplicates"],
        )
        return {
            "observed": observed,
            "passed": report["profiling_gate"] == "passed"
            and report.get("quarantine") == scenario.expect_message,
        }

    bronze_result = run_stage(
        "build_bronze.py",
        [
            "--manifest", str(manifest),
            "--profile", str(profile_out),
            "--output-root", str(case / "data" / "bronze"),
            "--reconciliation-output", str(case / "bronze_reconciliation.json"),
        ],
    )
    observed = {"stage": "build_bronze.py", "returncode": bronze_result.returncode}
    detail = bronze_result.stderr.strip().splitlines()[-1] if bronze_result.stderr else ""
    observed["detail"] = detail
    return {
        "observed": observed,
        "passed": bronze_result.returncode != 0
        and scenario.expect_message in detail,
    }


def run_rule_scenario(
    scenario: Scenario, bronze_dir: Path, work: Path, baseline: dict[str, int]
) -> dict:
    case = work / "cases" / scenario.name / "bronze"
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    by_source: dict[str, list[dict]] = {}
    for mutation in scenario.mutations:
        by_source.setdefault(mutation["source"], []).append(mutation)
    for source in ("legacy", "current"):
        origin = bronze_dir / f"{source}.parquet"
        if source in by_source:
            mutate_bronze(origin, case / f"{source}.parquet", by_source[source])
        else:
            shutil.copy2(origin, case / f"{source}.parquet")
    counts = build_quality(case, work, scenario.name)
    deltas = {
        rule_id: counts.get(rule_id, 0) - baseline.get(rule_id, 0)
        for rule_id in set(baseline) | set(counts)
    }
    moved = {rule_id: delta for rule_id, delta in deltas.items() if delta}
    target_ok = deltas.get(scenario.expect_rule, 0) == scenario.expect_delta
    return {
        "observed": {"rules_that_moved": moved},
        "passed": target_ok,
        "only_target_moved": set(moved) == {scenario.expect_rule},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bronze-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    bronze_dir = args.bronze_dir.resolve()
    work = args.work_dir.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    print(f"Fixture: {SAMPLE_ROWS_PER_SOURCE:,} rows per source from {bronze_dir.name}")
    baseline_bronze = work / "baseline_bronze"
    baseline_bronze.mkdir()
    for source in ("legacy", "current"):
        shutil.copy2(bronze_dir / f"{source}.parquet", baseline_bronze / f"{source}.parquet")
    baseline = build_quality(baseline_bronze, work, "baseline")
    print(f"Baseline: {sum(baseline.values()):,} hits across {len(baseline)} rules\n")

    results = []
    for scenario in PIPELINE_SCENARIOS:
        outcome = run_pipeline_scenario(scenario, bronze_dir, work)
        results.append({"scenario": scenario.name, "tier": scenario.tier,
                        "targets": scenario.targets, **outcome})
        flag = "PASS" if outcome["passed"] else "FAIL"
        print(f"[{flag}] {scenario.name:<32} {scenario.targets:<14} {outcome['observed']}")

    print()
    for scenario in RULE_SCENARIOS:
        outcome = run_rule_scenario(scenario, bronze_dir, work, baseline)
        results.append({"scenario": scenario.name, "tier": scenario.tier,
                        "targets": scenario.targets, **outcome})
        flag = "PASS" if outcome["passed"] else "FAIL"
        isolated = "isolated" if outcome["only_target_moved"] else "SIDE EFFECTS"
        print(f"[{flag}] {scenario.name:<32} delta ok, {isolated}: "
              f"{outcome['observed']['rules_that_moved']}")

    passed = sum(1 for r in results if r["passed"])
    isolated = sum(1 for r in results if r.get("only_target_moved", True))
    report = {
        "fixture_rows_per_source": SAMPLE_ROWS_PER_SOURCE,
        "as_of_date": AS_OF_DATE,
        "baseline_rule_counts": baseline,
        "scenarios_total": len(results),
        "scenarios_passed": passed,
        "scenarios_isolated": isolated,
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(results)} scenarios passed; report: {args.report}")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
