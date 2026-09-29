#!/usr/bin/env python3
"""Flag non-competitive awards at or above a published trade-agreement threshold.

Authority is Treasury Board Contracting Policy Notices 2021-6, 2023-6 and
2025-8, transcribed into an effective-dated reference table and joined on each
contract award date. Thresholds are republished every two years, so a single
table of current values would be wrong for older contracts.

Findings are review-class: the published data omits national security
exceptions and agreement annex exclusions, so a hit means the combination needs
explaining, not that a contract was unlawful.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

RULE_ID = "non_competitive_above_trade_threshold"
COVERAGE_START = "2022-01-01"
SET_ASIDE_TOKENS = (
    "*Set-Aside Program for Aboriginal Business (SPAB)",
    "*Land Claim Set Asides (LCSA)",
)
CATEGORY_MAP = {"*GD": "goods", "*SRV": "services", "*CNST": "construction"}
UNASSESSABLE_CATEGORY = "*SRVTGD"
NON_COMPETITIVE = "Non-competitive"


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derived-dir", type=Path, required=True)
    parser.add_argument("--bronze-dir", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    connection = duckdb.connect()
    connection.execute("SET threads = 4")
    connection.execute(
        f"CREATE VIEW thresholds AS SELECT * FROM read_csv({quoted(args.thresholds)}, header=true)"
    )
    connection.execute(
        f"CREATE VIEW silver AS SELECT * FROM "
        f"read_parquet({quoted(args.derived_dir / 'silver_contract_activity.parquet')})"
    )
    connection.execute(
        f"CREATE VIEW bronze AS SELECT * FROM read_parquet({quoted(args.bronze_dir / '*.parquet')})"
    )
    category_cases = " ".join(
        f"WHEN {quoted(k)} THEN {quoted(v)}" for k, v in CATEGORY_MAP.items()
    )
    set_aside_predicate = " OR ".join(
        f"contains(s.trade_agreements_canonical, {quoted(t)})" for t in SET_ASIDE_TOKENS
    )
    connection.execute(
        f"""
        CREATE VIEW assessable AS
        SELECT s.batch_id, s.source_dataset, s.source_row_number, s.award_date,
            s.contract_amount, s.trade_agreements_canonical,
            b."procurementCategory-categorieApprovisionnement" AS category_raw,
            CASE b."procurementCategory-categorieApprovisionnement" {category_cases} END AS category,
            b."procurementMethod-methodeApprovisionnement-eng" AS procurement_method,
            b."limitedTenderingReason-raisonAppelOffresLimite-eng" AS limited_tendering_reason,
            ({set_aside_predicate}) AS is_set_aside
        FROM silver s
        JOIN bronze b ON b.source_dataset = s.source_dataset
                     AND b.source_row_number = s.source_row_number
        WHERE s.award_date >= DATE {quoted(COVERAGE_START)}
          AND s.contract_amount IS NOT NULL
        """
    )
    # One row per (contract row, listed agreement) with the threshold in force on
    # the award date. The lowest applicable threshold is the binding one.
    connection.execute(
        """
        CREATE VIEW applicable AS
        SELECT a.*, t.agreement_code, t.threshold_cad, t.source_notice
        FROM assessable a
        JOIN thresholds t
          ON trim(t.agreement_token) IN (
                SELECT trim(tok) FROM unnest(str_split(a.trade_agreements_canonical, chr(10))) AS u(tok)
             )
         AND t.category = a.category
         AND a.award_date BETWEEN t.effective_from AND t.effective_to
        WHERE a.category IS NOT NULL AND NOT a.is_set_aside
        """
    )

    coverage = connection.execute(
        f"""
        SELECT count(*) AS rows_since_coverage_start,
            count(*) FILTER (WHERE category IS NULL) AS unassessable_category,
            count(*) FILTER (WHERE is_set_aside) AS set_aside_excluded,
            count(*) FILTER (WHERE category_raw = {quoted(UNASSESSABLE_CATEGORY)}) AS mixed_category
        FROM assessable
        """
    ).df().to_dict("records")[0]

    findings = connection.execute(
        f"""
        SELECT batch_id, source_dataset, source_row_number, award_date,
            contract_amount, binding_threshold, binding_agreement, source_notice,
            procurement_method, category
        FROM (
            SELECT *, min(threshold_cad) OVER w AS binding_threshold,
                   first(agreement_code) OVER w AS binding_agreement
            FROM applicable
            WINDOW w AS (PARTITION BY source_dataset, source_row_number
                         ORDER BY threshold_cad
                         ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
        )
        WHERE contract_amount >= binding_threshold
          AND procurement_method = {quoted(NON_COMPETITIVE)}
          AND coalesce(trim(limited_tendering_reason), '') = ''
        QUALIFY row_number() OVER (PARTITION BY source_dataset, source_row_number) = 1
        """
    ).df()

    assessed = connection.execute(
        "SELECT count(DISTINCT source_dataset || '#' || source_row_number) FROM applicable"
    ).fetchone()[0]
    above = connection.execute(
        "SELECT count(DISTINCT source_dataset || '#' || source_row_number) FROM ("
        "  SELECT *, min(threshold_cad) OVER (PARTITION BY source_dataset, source_row_number) AS b"
        "  FROM applicable) WHERE contract_amount >= b"
    ).fetchone()[0]
    non_comp = connection.execute(
        f"SELECT count(DISTINCT source_dataset || '#' || source_row_number) FROM ("
        f"  SELECT *, min(threshold_cad) OVER (PARTITION BY source_dataset, source_row_number) AS b"
        f"  FROM applicable) WHERE contract_amount >= b "
        f"AND procurement_method = {quoted(NON_COMPETITIVE)}"
    ).fetchone()[0]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / "trade_threshold_findings.parquet"
    connection.register("f", findings)
    connection.execute(
        f"COPY (SELECT {quoted(RULE_ID)} AS rule_id, 'review' AS rule_class, "
        f"'TBS Contracting Policy Notices 2021-6 / 2023-6 / 2025-8' AS authority, * "
        f"FROM f) TO {quoted(out)} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    report = {
        "rule_id": RULE_ID,
        "rule_class": "review",
        "authority": "TBS Contracting Policy Notices 2021-6, 2023-6, 2025-8",
        "coverage_start": COVERAGE_START,
        "coverage_note": "Earliest transcribed notice. Contracts awarded before "
        "this date are not assessed because the threshold in force is not in the "
        "reference table.",
        "funnel": {
            "rows_awarded_since_coverage_start": int(coverage["rows_since_coverage_start"]),
            "excluded_set_aside": int(coverage["set_aside_excluded"]),
            "excluded_mixed_category_srvtgd": int(coverage["mixed_category"]),
            "assessable_with_listed_agreement": int(assessed),
            "at_or_above_binding_threshold": int(above),
            "and_non_competitive": int(non_comp),
            "and_no_limited_tendering_reason": int(len(findings)),
        },
        "interpretation": "A finding means the combination needs explaining. It is "
        "not a determination of non-compliance: the published data omits national "
        "security exceptions, agreement annex exclusions, and the approval record.",
    }
    (args.output_dir / "trade_threshold_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report["funnel"], indent=2))
    if len(findings):
        print("\nSample findings:")
        print(findings.head(8).to_string(index=False))
    print(f"\n-> {out}")
    connection.close()


if __name__ == "__main__":
    main()
