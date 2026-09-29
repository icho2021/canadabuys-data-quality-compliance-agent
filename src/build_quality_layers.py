#!/usr/bin/env python3
"""Build typed Silver rows, classified quality findings, and a batch decision.

Each rule declares a class that decides what happens to the batch and the rows:
integrity quarantines the batch, review labels rows for a person, normalization
is deterministically fixable after approval, observe is counted only. No row is
deleted and no source value is rewritten.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

import duckdb

RULESET_VERSION = "v2"
# Substituted into time-relative predicates. A literal token is used rather than
# str.format so that regex quantifiers such as {2} survive untouched.
AS_OF_TOKEN = "@AS_OF_DATE@"
LEGACY_CUTOVER_LAST_DAY = "2023-05-31"
CURRENT_CUTOVER_FIRST_DAY = "2023-06-01"
EXPECTED_CURRENCY = "CAD"
DISTANT_DATE_THRESHOLD = "3000-01-01"
CA_POSTAL_PATTERN = r"^[A-Z][0-9][A-Z][0-9][A-Z][0-9]$"

# rule_id, rule_class, authority, predicate, observed column
RULES: tuple[tuple[str, str, str, str, str], ...] = (
    # --- integrity: contradicts the dataset's own published definition -------
    (
        "invalid_award_date",
        "integrity",
        "project_technical",
        "award_date_raw <> '' AND award_date IS NULL",
        "award_date_raw",
    ),
    (
        "invalid_contract_amount",
        "integrity",
        "project_technical",
        "contract_amount_raw <> '' AND contract_amount IS NULL",
        "contract_amount_raw",
    ),
    (
        "legacy_cutover_mismatch",
        "integrity",
        "official_cutover_definition",
        f"source_dataset = 'legacy' AND award_date > DATE '{LEGACY_CUTOVER_LAST_DAY}'",
        "award_date_raw",
    ),
    (
        "current_cutover_mismatch",
        "integrity",
        "official_cutover_definition",
        f"source_dataset = 'current' AND award_date < DATE '{CURRENT_CUTOVER_FIRST_DAY}'",
        "award_date_raw",
    ),
    (
        "unexpected_currency",
        "integrity",
        "observed_single_value_in_batch_001",
        f"currency_raw <> '' AND currency_raw <> '{EXPECTED_CURRENCY}'",
        "currency_raw",
    ),
    # --- review: needs human judgement --------------------------------------
    (
        "missing_award_date",
        "review",
        "official_cutover_definition",
        "award_date_raw = ''",
        "award_date_raw",
    ),
    (
        "amendment_date_before_award_date",
        "review",
        "logical_contradiction",
        "amendment_date IS NOT NULL AND award_date IS NOT NULL AND amendment_date < award_date",
        "amendment_date_raw",
    ),
    (
        "end_date_before_start_date",
        "review",
        "official_definition_end_expires_after_start",
        "end_date IS NOT NULL AND start_date IS NOT NULL AND end_date < start_date",
        "end_date_raw",
    ),
    (
        "end_date_before_award_date",
        "review",
        "logical_contradiction",
        "end_date IS NOT NULL AND award_date IS NOT NULL AND end_date < award_date",
        "end_date_raw",
    ),
    (
        "negative_total_contract_value",
        "review",
        "official_cumulative_value_definition",
        "total_contract_value < 0",
        "total_contract_value_raw",
    ),
    (
        "contract_amount_exceeds_total_value",
        "review",
        "official_cumulative_value_definition",
        "contract_amount IS NOT NULL AND total_contract_value IS NOT NULL "
        "AND abs(contract_amount) > abs(total_contract_value)",
        "contract_amount_raw",
    ),
    (
        # Official guidance permits negative amendment values as decreases. That
        # explanation only applies where the activity type is known, so the
        # reviewable population is negatives on rows of unknown type.
        "negative_amount_unknown_activity",
        "review",
        "official_guidance_covers_amendments_only",
        "contract_amount < 0 AND amendment_type_raw = ''",
        "contract_amount_raw",
    ),
    (
        "unusually_distant_end_date",
        "review",
        "project_unexplained_sentinel",
        f"end_date >= DATE '{DISTANT_DATE_THRESHOLD}'",
        "end_date_raw",
    ),
    (
        "future_amendment_date",
        "review",
        "project_technical",
        "amendment_date > DATE '@AS_OF_DATE@'",
        "amendment_date_raw",
    ),
    (
        "missing_standardized_supplier_name",
        "review",
        "project_normalization_gap",
        "supplier_name_raw = '' AND supplier_legal_name_raw <> ''",
        "supplier_legal_name_raw",
    ),
    # --- normalization: structurally non-canonical, deterministically fixable
    (
        "non_canonical_trade_agreements",
        "normalization",
        "structural",
        "trade_agreements_raw <> '' AND trade_agreements_raw <> trade_agreements_canonical",
        "trade_agreements_raw",
    ),
    (
        "non_canonical_supplier_country",
        "normalization",
        "structural",
        r"regexp_matches(supplier_country_raw, '^[A-Z]{2}$')",
        "supplier_country_raw",
    ),
    (
        "postal_code_format_mismatch_canada",
        "normalization",
        "structural",
        "supplier_country_raw IN ('Canada', 'CA') AND supplier_postal_code_raw <> '' "
        f"AND NOT regexp_matches(upper(replace(supplier_postal_code_raw, ' ', '')), '{CA_POSTAL_PATTERN}')",
        "supplier_postal_code_raw",
    ),
    # --- observe: documented characteristics, no action ----------------------
    (
        "unknown_amendment_type",
        "observe",
        "official_field_added_in_2026_restructuring",
        "amendment_type_raw = ''",
        "amendment_type_raw",
    ),
    (
        "zero_contract_amount",
        "observe",
        "official_zero_legitimate_for_standing_offers",
        "contract_amount = 0",
        "contract_amount_raw",
    ),
    (
        "negative_amount_on_known_amendment",
        "observe",
        "official_guidance_permits_decreases",
        "contract_amount < 0 AND amendment_type_raw <> ''",
        "contract_amount_raw",
    ),
)

FINDING_CLASSES = ("integrity", "review", "normalization")


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def load_passed(path: Path, gate_key: str) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get(gate_key) != "passed":
        raise ValueError(f"Required upstream gate is not passed: {path}")
    return report


def rule_select(
    rule_id: str, rule_class: str, authority: str, predicate: str, observed: str,
    *, silver: str, as_of_date: str,
) -> str:
    return f"""
        SELECT batch_id, source_dataset, source_row_number,
            {quoted(RULESET_VERSION)} AS ruleset_version,
            {quoted(rule_id)} AS rule_id,
            {quoted(rule_class)} AS rule_class,
            {quoted(authority)} AS authority,
            {quoted(as_of_date)}::DATE AS as_of_date,
            {observed} AS observed_value
        FROM {silver} WHERE {predicate.replace(AS_OF_TOKEN, as_of_date)}
    """


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--bronze-reconciliation", type=Path, required=True)
    parser.add_argument("--bronze-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        required=True,
        help="Evaluation date for time-relative rules. Must be supplied per "
        "batch so a re-run produces identical findings.",
    )
    args = parser.parse_args()
    as_of_date = args.as_of_date.isoformat()

    profile = load_passed(args.profile, "profiling_gate")
    bronze = load_passed(args.bronze_reconciliation, "gate")
    batch_id = bronze["batch_id"]
    batch_dir = args.output_root / f"batch_id={batch_id}"
    output_dir = batch_dir / f"ruleset={RULESET_VERSION}"
    if output_dir.exists():
        raise FileExistsError(f"Derived output already exists: {output_dir}")
    if sum(profile[source]["rows"] for source in ("legacy", "current")) != bronze["total_rows"]:
        raise ValueError("Profile/Bronze row counts do not match")
    source_paths = [
        Path(bronze["sources"][source]["bronze_file"]).resolve()
        for source in ("legacy", "current")
    ]
    expected_dir = args.bronze_dir.resolve()
    if any(path.parent != expected_dir or not path.is_file() for path in source_paths):
        raise ValueError("Bronze sources do not match the requested batch directory")

    batch_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{RULESET_VERSION}-", dir=batch_dir))
    source_list = ", ".join(quoted(path) for path in source_paths)
    connection = duckdb.connect()
    connection.execute("SET threads = 1")
    try:
        silver_path = temp_dir / "silver_contract_activity.parquet"
        findings_path = temp_dir / "quality_findings.parquet"
        observations_path = temp_dir / "quality_observations.parquet"
        gold_path = temp_dir / "gold_quality_summary.parquet"
        connection.execute(
            f"""
            COPY (
                SELECT
                    batch_id, source_dataset, source_file, source_row_number,
                    ingested_at_utc, source_sha256,
                    "referenceNumber-numeroReference" AS reference_number_raw,
                    "contractNumber-numeroContrat" AS contract_number_raw,
                    "amendmentNumber-numeroModification" AS amendment_number_raw,
                    "amendmentType-typeModification-eng" AS amendment_type_raw,
                    "instrumentType-typeInstrument-eng" AS instrument_type_raw,
                    "contractCurrency-contratMonnaie" AS currency_raw,
                    "supplierStandardizedName-nomNormaliseFournisseur-eng" AS supplier_name_raw,
                    "supplierLegalName-nomLegalFournisseur-eng" AS supplier_legal_name_raw,
                    "supplierAddressCountry-fournisseurAdressePays-eng" AS supplier_country_raw,
                    "supplierAddressPostalCode-fournisseurAdresseCodePostal" AS supplier_postal_code_raw,
                    "contractingEntityName-nomEntitContractante-eng" AS contracting_entity_raw,
                    "endUserEntitiesName-nomEntitesUtilisateurFinal-eng" AS end_user_entity_raw,
                    "tradeAgreements-accordsCommerciaux-eng" AS trade_agreements_raw,
                    "contractAwardDate-dateAttributionContrat" AS award_date_raw,
                    "amendmentDate-dateModification" AS amendment_date_raw,
                    "contractStartDate-contratDateDebut" AS start_date_raw,
                    "contractEndDate-dateFinContrat" AS end_date_raw,
                    "contractAmount-montantContrat" AS contract_amount_raw,
                    "totalContractValue-valeurTotaleContrat" AS total_contract_value_raw,
                    try_cast(nullif("contractAwardDate-dateAttributionContrat", '') AS DATE)
                        AS award_date,
                    try_cast(nullif("amendmentDate-dateModification", '') AS DATE)
                        AS amendment_date,
                    try_cast(nullif("contractStartDate-contratDateDebut", '') AS DATE)
                        AS start_date,
                    try_cast(nullif("contractEndDate-dateFinContrat", '') AS DATE)
                        AS end_date,
                    try_cast(nullif("contractAmount-montantContrat", '') AS DECIMAL(24,2))
                        AS contract_amount,
                    try_cast(nullif("totalContractValue-valeurTotaleContrat", '') AS DECIMAL(24,2))
                        AS total_contract_value,
                    -- Deterministic canonical form for the newline-delimited
                    -- multi-value field: deduplicated and sorted. The raw value
                    -- is retained; this column exists so the ordering-only
                    -- variance is measurable rather than inflating cardinality.
                    array_to_string(
                        list_sort(list_distinct(
                            str_split("tradeAgreements-accordsCommerciaux-eng", chr(10))
                        )), chr(10)
                    ) AS trade_agreements_canonical
                FROM read_parquet([{source_list}])
            ) TO {quoted(silver_path)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        silver = f"read_parquet({quoted(silver_path)})"

        all_rules = " UNION ALL ".join(
            rule_select(*rule, silver=silver, as_of_date=as_of_date) for rule in RULES
        )
        connection.execute(f"CREATE TEMP VIEW rule_hits AS {all_rules}")
        finding_filter = ", ".join(quoted(name) for name in FINDING_CLASSES)
        connection.execute(
            f"COPY (SELECT * FROM rule_hits WHERE rule_class IN ({finding_filter})) "
            f"TO {quoted(findings_path)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            f"COPY (SELECT * FROM rule_hits WHERE rule_class = 'observe') "
            f"TO {quoted(observations_path)} (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        # Every declared rule appears for every source, including rules that
        # matched nothing. A rule with zero hits is a result; a rule missing from
        # the report is indistinguishable from a rule that never ran.
        rule_catalog = " UNION ALL ".join(
            f"SELECT {quoted(rule_id)} AS rule_id, {quoted(rule_class)} AS rule_class, "
            f"{quoted(authority)} AS authority"
            for rule_id, rule_class, authority, _, _ in RULES
        )
        connection.execute(
            f"""
            COPY (
                SELECT
                    {quoted(RULESET_VERSION)} AS ruleset_version,
                    d.source_dataset, c.rule_class, c.rule_id, c.authority,
                    coalesce(h.finding_count, 0)::BIGINT AS finding_count,
                    coalesce(h.affected_rows, 0)::BIGINT AS affected_rows
                FROM ({rule_catalog}) c
                CROSS JOIN (SELECT DISTINCT source_dataset FROM {silver}) d
                LEFT JOIN (
                    SELECT rule_id, source_dataset,
                        count(*) AS finding_count,
                        count(DISTINCT source_row_number) AS affected_rows
                    FROM rule_hits GROUP BY 1, 2
                ) h ON h.rule_id = c.rule_id AND h.source_dataset = d.source_dataset
                ORDER BY c.rule_class, c.rule_id, d.source_dataset
            ) TO {quoted(gold_path)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )

        silver_rows = connection.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        per_source = dict(
            connection.execute(
                f"SELECT source_dataset, count(*) FROM {silver} GROUP BY 1"
            ).fetchall()
        )
        by_class = dict(
            connection.execute(
                "SELECT rule_class, count(*) FROM rule_hits GROUP BY 1"
            ).fetchall()
        )
        rows_needing_review = connection.execute(
            "SELECT count(DISTINCT source_dataset || '#' || source_row_number) "
            "FROM rule_hits WHERE rule_class IN ('integrity', 'review')"
        ).fetchone()[0]
        integrity_hits = by_class.get("integrity", 0)

        # Structural checks only. Whether the data is clean is the finding table's
        # job to report, never a precondition for the pipeline succeeding.
        checks = {
            "silver_rows_equal_bronze": silver_rows == bronze["total_rows"],
            "per_source_rows_equal_bronze": all(
                per_source.get(source) == bronze["sources"][source]["rows"]
                for source in ("legacy", "current")
            ),
            "every_rule_reported_per_source": connection.execute(
                f"SELECT count(*) FROM read_parquet({quoted(gold_path)})"
            ).fetchone()[0]
            == len(RULES) * len(per_source),
        }
        if integrity_hits:
            decision = "quarantine"
        elif rows_needing_review:
            decision = "promote_with_review_queue"
        else:
            decision = "promote"
        report = {
            "batch_id": batch_id,
            "ruleset_version": RULESET_VERSION,
            "as_of_date": as_of_date,
            "scope": "typed activity rows and classified quality observations only",
            "silver_rows": silver_rows,
            "silver_rows_by_source": per_source,
            "rule_count": len(RULES),
            "hits_by_class": by_class,
            "rows_needing_human_review": rows_needing_review,
            "checks": checks,
            "gate": "passed" if all(checks.values()) else "failed",
            "gate_meaning": "technical integrity only; not a data quality certificate",
            "batch_decision": decision,
            "excluded_decisions": [
                "no latest-version selection",
                "no financial aggregation",
                "no business-invalidity conclusion from negative or zero amounts",
                "no automated remediation",
                "no row deletion at any layer",
            ],
        }
        if report["gate"] != "passed":
            raise ValueError(f"Quality layer gate failed: {checks}")
        (temp_dir / "reconciliation.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temp_dir, output_dir)
        print(f"Gate: passed (technical integrity)  Decision: {decision}")
        print(f"Silver {silver_rows:,} rows; {len(RULES)} rules evaluated")
        for rule_class in ("integrity", "review", "normalization", "observe"):
            print(f"  {rule_class:<14} {by_class.get(rule_class, 0):>9,}")
        print(f"Rows needing human review: {rows_needing_review:,}")
        print(f"Output: {output_dir}")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
