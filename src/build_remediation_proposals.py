#!/usr/bin/env python3
"""Turn quality findings into a small set of human-approvable proposals.

126,708 rows collapse into 7 proposals, because a reviewer approves a
standardisation rule rather than individual edits. A proposal is either
normalize (a deterministic, reversible transform exists) or annotate (none does,
and approval cannot create one). Nothing here modifies data.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb

PROPOSAL_SET_VERSION = "p1"


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# proposal_id, rule_id, type, target_column, transform_sql, authority, rationale
# transform_sql is evaluated against the Silver row; @MAP@ is substituted with
# the country reference table name where required.
PROPOSALS: tuple[dict, ...] = (
    {
        "proposal_id": "normalize_trade_agreements_ordering",
        "rule_id": "non_canonical_trade_agreements",
        "type": "normalize",
        "target_column": "trade_agreements_raw",
        "transform_sql": "trade_agreements_canonical",
        "authority": "structural: the field is a newline-delimited set, so element "
        "order carries no meaning. Deduplicating and sorting is information "
        "preserving and reversible in aggregate.",
        "rationale": "12,705 distinct raw values collapse to 351 canonical values. "
        "Until normalised the field cannot be grouped or counted.",
    },
    {
        "proposal_id": "normalize_supplier_country_encoding",
        "rule_id": "non_canonical_supplier_country",
        "type": "normalize",
        "target_column": "supplier_country_raw",
        "transform_sql": "coalesce(canonical_value, supplier_country_raw)",
        "authority": "ISO 3166-1 alpha-2, mapped in "
        "data/reference/country_canonical_map.csv. The full-name form is the "
        "dominant encoding in every observed case.",
        "rationale": "The same country appears as both an ISO-2 code and a full "
        "name (Canada 423,432 / CA 34,135). Any grouping by country is wrong "
        "until this is resolved.",
    },
    {
        "proposal_id": "annotate_postal_code_format",
        "rule_id": "postal_code_format_mismatch_canada",
        "type": "annotate",
        "target_column": "supplier_postal_code_raw",
        "transform_sql": None,
        "authority": "none. A malformed postal code cannot be deterministically "
        "corrected; the correct value is not recoverable from the wrong one.",
        "rationale": "826 rows give Canada as the country with a postal code that "
        "is not in A1A1A1 form. Correction requires looking the supplier up, "
        "which is a business task, not a transform.",
    },
    {
        "proposal_id": "annotate_supplier_identity_unresolved",
        "rule_id": "missing_standardized_supplier_name",
        "type": "annotate",
        "target_column": "supplier_name_raw",
        "transform_sql": None,
        "authority": "none. Deciding that two names denote one legal entity "
        "requires an authoritative corporate registry and an accountable owner.",
        "rationale": "31,402 rows have a legal name but no standardised name. "
        "Automated string matching gets this wrong in both directions: "
        "'Techstreet Solutions Ltd' and 'Allium US Holding LLC' are one contract "
        "by corporate succession despite near-zero string similarity.",
    },
    {
        "proposal_id": "annotate_negative_amount_unknown_activity",
        "rule_id": "negative_amount_unknown_activity",
        "type": "annotate",
        "target_column": "contract_amount_raw",
        "transform_sql": None,
        "authority": "official guidance permits negative amendment values as "
        "decreases, but only where the activity type is known. These rows have a "
        "blank amendmentType, so the explanation cannot be applied to them.",
        "rationale": "19,105 rows. Neither deleting nor absolute-valuing these is "
        "defensible; a data steward must confirm what the rows represent.",
    },
    {
        "proposal_id": "annotate_negative_total_contract_value",
        "rule_id": "negative_total_contract_value",
        "type": "annotate",
        "target_column": "total_contract_value_raw",
        "transform_sql": None,
        "authority": "official definition: totalContractValue is cumulative from "
        "the award date to the present, and CanadaBuys displays it as "
        "non-negative even where amendment values are negative.",
        "rationale": "252 rows contradict the published definition. Example "
        "EE520-182837/001/MTC, amendment 000, reports contractAmount 8,791,885.00 "
        "against totalContractValue -1,912,829.00. Small enough to escalate to "
        "the publisher.",
    },
    {
        "proposal_id": "annotate_date_ordering_contradictions",
        "rule_id": "amendment_date_before_award_date",
        "type": "annotate",
        "target_column": "amendment_date_raw",
        "transform_sql": None,
        "authority": "logical: a contract cannot be amended before it is awarded.",
        "rationale": "368 rows. The correct date is not recoverable from the "
        "incorrect one, so no transform exists.",
    },
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derived-dir", type=Path, required=True)
    parser.add_argument("--country-map", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    reconciliation = json.loads(
        (args.derived_dir / "reconciliation.json").read_text(encoding="utf-8")
    )
    silver = f"read_parquet({quoted(args.derived_dir / 'silver_contract_activity.parquet')})"
    findings = f"read_parquet({quoted(args.derived_dir / 'quality_findings.parquet')})"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    connection = duckdb.connect()
    connection.execute(
        f"CREATE TEMP VIEW country_map AS SELECT * FROM read_csv({quoted(args.country_map)}, header=true)"
    )
    connection.execute(f"CREATE TEMP VIEW s AS SELECT * FROM {silver}")

    rows = []
    for spec in PROPOSALS:
        affected = connection.execute(
            f"SELECT count(DISTINCT source_dataset || '#' || source_row_number) "
            f"FROM {findings} WHERE rule_id = ?",
            [spec["rule_id"]],
        ).fetchone()[0]
        sample_before = sample_after = None
        if spec["type"] == "normalize":
            joined = (
                "s LEFT JOIN country_map m ON m.observed_value = s.supplier_country_raw"
                if "canonical_value" in spec["transform_sql"]
                else "s"
            )
            sample = connection.execute(
                f"""
                SELECT s.{spec['target_column']} AS before_value,
                       {spec['transform_sql']} AS after_value
                FROM {joined}
                WHERE s.source_row_number IN (
                    SELECT source_row_number FROM {findings} WHERE rule_id = ?
                )
                AND s.{spec['target_column']} <> {spec['transform_sql']}
                LIMIT 1
                """,
                [spec["rule_id"]],
            ).fetchone()
            if sample:
                sample_before, sample_after = sample
        else:
            sample_before = connection.execute(
                f"SELECT observed_value FROM {findings} WHERE rule_id = ? LIMIT 1",
                [spec["rule_id"]],
            ).fetchone()[0]
        rows.append(
            {
                "proposal_set_version": PROPOSAL_SET_VERSION,
                "batch_id": reconciliation["batch_id"],
                "ruleset_version": reconciliation["ruleset_version"],
                "proposal_id": spec["proposal_id"],
                "rule_id": spec["rule_id"],
                "proposal_type": spec["type"],
                "target_column": spec["target_column"],
                "transform_sql": spec["transform_sql"] or "",
                "auto_appliable": spec["type"] == "normalize",
                "affected_rows": affected,
                "sample_before": (sample_before or "")[:300],
                "sample_after": (sample_after or "")[:300] if sample_after else "",
                "authority": spec["authority"],
                "rationale": spec["rationale"],
                "generated_at_utc": generated_at,
                "status": "pending",
                "decided_by": "",
                "decided_at_utc": "",
                "decision_note": "",
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    decisions_csv = args.output_dir / "remediation_decisions.csv"
    if decisions_csv.exists():
        raise FileExistsError(
            f"{decisions_csv} already exists. Approvals recorded there would be "
            "overwritten. Move or archive it before regenerating."
        )
    with decisions_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    connection.execute(
        f"CREATE TEMP VIEW proposals AS SELECT * FROM read_csv({quoted(decisions_csv)}, header=true, all_varchar=true)"
    )
    connection.execute(
        f"COPY (SELECT * FROM proposals) TO "
        f"{quoted(args.output_dir / 'remediation_proposals.parquet')} (FORMAT PARQUET)"
    )
    connection.close()

    auto = sum(1 for r in rows if r["auto_appliable"])
    covered = sum(r["affected_rows"] for r in rows)
    print(f"{len(rows)} proposals ({auto} auto-appliable, {len(rows) - auto} annotate-only)")
    print(f"Rows covered: {covered:,}")
    for r in rows:
        mark = "AUTO" if r["auto_appliable"] else "----"
        print(f"  [{mark}] {r['proposal_id']:<45} {r['affected_rows']:>8,}")
    print(f"\nApprove by editing: {decisions_csv}")
    print("Set status to 'approved' or 'rejected' and fill decided_by / decided_at_utc.")


if __name__ == "__main__":
    main()
