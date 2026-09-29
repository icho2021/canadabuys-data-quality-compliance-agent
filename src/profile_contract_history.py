#!/usr/bin/env python3
"""Profile CanadaBuys Contract History without changing source values.

The profiler performs one streaming pass over each official CSV. It produces:

1. a JSON batch profile used by automated gates; and
2. a CSV field map with a classification and blank-rate baseline for all 91
   physical source fields.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any


EXPECTED_COLUMNS = 91
LEGACY_CUTOVER_LAST_DAY = "2023-05-31"
CURRENT_CUTOVER_FIRST_DAY = "2023-06-01"
DEFAULT_FIELD_FAMILY = "procurement_classification"
# field_family() ends in an unconditional default, so "every field has a family"
# can never fail. The gate instead names the fields that are allowed to land in
# the default family; anything new falling through is an unreviewed field.
EXPECTED_CLASSIFICATION_FALLTHROUGH = frozenset(
    {
        "gsin",
        "gsinDescription",
        "unspsc",
        "unspscDescription",
        "procurementCategory",
        "noticeType",
        "procurementMethod",
        "selectionCriteria",
        "limitedTenderingReason",
        "tradeAgreements",
        "regionsOfDelivery",
    }
)
VERSION_KEY = (
    "referenceNumber-numeroReference",
    "contractNumber-numeroContrat",
    "amendmentNumber-numeroModification",
)
DATE_FIELDS = (
    "publicationDate-datePublication",
    "contractAwardDate-dateAttributionContrat",
    "amendmentDate-dateModification",
    "contractStartDate-contratDateDebut",
    "contractEndDate-dateFinContrat",
)
AMOUNT_FIELDS = (
    "contractAmount-montantContrat",
    "totalContractValue-valeurTotaleContrat",
)
REQUIRED_REVIEW_FIELDS = VERSION_KEY + ("contractAwardDate-dateAttributionContrat",)
SELECTED_VALUE_FIELDS = (
    "amendmentType-typeModification-eng",
    "contractStatus-statutContrat-eng",
    "contractCurrency-contratMonnaie",
    "procurementCategory-categorieApprovisionnement",
    "instrumentType-typeInstrument-eng",
)
FIELD_MAP_COLUMNS = (
    "ordinal",
    "source_field",
    "logical_field",
    "language",
    "family",
    "bronze_type",
    "silver_type_candidate",
    "public_output_policy",
    "semantic_status",
    "legacy_blank_count",
    "legacy_blank_rate",
    "current_blank_count",
    "current_blank_rate",
    "combined_blank_count",
    "combined_blank_rate",
    "note",
)


def parse_iso_date(value: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def field_family(header: str) -> str:
    logical = header.split("-", 1)[0]
    if logical in {
        "title",
        "referenceNumber",
        "amendmentNumber",
        "procurementNumber",
        "solicitationNumber",
        "contractNumber",
        "numberOfRecords",
    }:
        return "identity_and_version"
    if header in DATE_FIELDS:
        return "dates"
    if header in AMOUNT_FIELDS or logical in {
        "contractCurrency",
        "contractStatus",
        "instrumentType",
        "amendmentType",
    }:
        return "amounts_and_state"
    if logical.startswith(("supplier",)):
        return "supplier"
    if logical.startswith(("contractingEntity", "endUserEntities")):
        return "government_roles"
    if logical.startswith("contactInfo") or logical in {
        "percentageOfGoodsByCountry",
        "tenderDescription",
    }:
        return "contact_and_narrative"
    return "procurement_classification"


def language_for(header: str) -> str:
    if header.endswith("-eng"):
        return "eng"
    if header.endswith("-fra"):
        return "fra"
    return "shared"


def silver_type_candidate(header: str) -> str:
    logical = header.split("-", 1)[0]
    if header in DATE_FIELDS:
        return "date"
    if header in AMOUNT_FIELDS:
        return "decimal"
    if logical == "numberOfRecords":
        return "integer"
    return "string"


def public_output_policy(header: str) -> str:
    logical = header.split("-", 1)[0]
    if (
        logical.startswith("contactInfo")
        or "Address" in logical
        or logical in {"tenderDescription", "percentageOfGoodsByCountry"}
    ):
        return "exclude_from_public_analytics"
    return "eligible_after_quality_checks"


def semantic_status(header: str) -> str:
    logical = header.split("-", 1)[0]
    if logical in {
        "amendmentType",
        "contractAmount",
        "totalContractValue",
        "amendmentDate",
        "contractStartDate",
        "contractEndDate",
    }:
        return "official_definition_with_unresolved_edge_cases"
    return "official_dictionary_definition"


def field_note(header: str) -> str:
    logical = header.split("-", 1)[0]
    notes = {
        "amendmentType": "Legacy blanks remain unknown; do not infer a value.",
        "contractAmount": "Preserve negatives and zeroes; raw rows are not safely additive.",
        "totalContractValue": "Published cumulative value; latest-version selection is not yet approved.",
        "contractEndDate": "Sentinel-like dates remain parseable source values and are flagged later.",
        "amendmentDate": "Future values are review findings, not automatic errors.",
        "contractStartDate": "The Legacy/Current cutover is based on initial award date, not start date.",
        "tenderDescription": "Free text is retained for lineage but excluded from public analytical output.",
        "percentageOfGoodsByCountry": "Semi-structured text; retain raw until a parser is designed.",
    }
    if logical.startswith("contactInfo"):
        return "Contact detail retained for lineage and excluded from public analytical output."
    if "Address" in logical:
        return "Address detail retained for lineage and excluded from public analytical output."
    return notes.get(logical, "")


def profile_source(
    label: str, path: Path, global_keys: Counter[tuple[str, ...]]
) -> dict[str, Any]:
    blank = Counter()
    values = {field: Counter() for field in SELECTED_VALUE_FIELDS}
    dates = {
        name: {"blank": 0, "invalid": 0, "min": None, "max": None}
        for name in DATE_FIELDS
    }
    amounts = {
        name: {"blank": 0, "invalid": 0, "negative": 0, "zero": 0}
        for name in AMOUNT_FIELDS
    }
    version_keys: Counter[tuple[str, ...]] = Counter()
    row_count = 0

    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        header = reader.fieldnames or []
        for row in reader:
            row_count += 1
            for field in header:
                if not (row.get(field) or "").strip():
                    blank[field] += 1
            for field, counter in values.items():
                counter[(row.get(field) or "").strip() or "<blank>"] += 1
            key = tuple((row.get(field) or "").strip() for field in VERSION_KEY)
            version_keys[key] += 1
            global_keys[key] += 1
            for field, stat in dates.items():
                raw = (row.get(field) or "").strip()
                if not raw:
                    stat["blank"] += 1
                    continue
                parsed = parse_iso_date(raw)
                if not parsed:
                    stat["invalid"] += 1
                    continue
                stat["min"] = parsed if stat["min"] is None or parsed < stat["min"] else stat["min"]
                stat["max"] = parsed if stat["max"] is None or parsed > stat["max"] else stat["max"]
            for field, stat in amounts.items():
                raw = (row.get(field) or "").strip()
                if not raw:
                    stat["blank"] += 1
                    continue
                try:
                    amount = float(raw.replace(",", ""))
                    if math.isnan(amount) or math.isinf(amount):
                        raise ValueError
                except ValueError:
                    stat["invalid"] += 1
                    continue
                if amount < 0:
                    stat["negative"] += 1
                elif amount == 0:
                    stat["zero"] += 1

    duplicate_count = sum(count - 1 for count in version_keys.values() if count > 1)
    return {
        "label": label,
        "path": str(path),
        "rows": row_count,
        "header": header,
        "header_columns": len(header),
        "blank_counts": {field: blank[field] for field in header},
        "required_field_blank_counts": {
            field: blank[field] for field in REQUIRED_REVIEW_FIELDS
        },
        "dates": dates,
        "amounts": amounts,
        "top_values": {
            field: counter.most_common(12) for field, counter in values.items()
        },
        "distinct_candidate_version_keys": len(version_keys),
        "candidate_version_key_duplicates": duplicate_count,
    }


def rate(count: int, rows: int) -> float:
    return round(count / rows, 6) if rows else 0.0


def build_field_map(
    header: list[str], legacy: dict[str, Any], current: dict[str, Any]
) -> list[dict[str, Any]]:
    combined_rows = legacy["rows"] + current["rows"]
    field_map = []
    for ordinal, field in enumerate(header, 1):
        legacy_blank = legacy["blank_counts"][field]
        current_blank = current["blank_counts"][field]
        combined_blank = legacy_blank + current_blank
        field_map.append(
            {
                "ordinal": ordinal,
                "source_field": field,
                "logical_field": field.split("-", 1)[0],
                "language": language_for(field),
                "family": field_family(field),
                "bronze_type": "string",
                "silver_type_candidate": silver_type_candidate(field),
                "public_output_policy": public_output_policy(field),
                "semantic_status": semantic_status(field),
                "legacy_blank_count": legacy_blank,
                "legacy_blank_rate": rate(legacy_blank, legacy["rows"]),
                "current_blank_count": current_blank,
                "current_blank_rate": rate(current_blank, current["rows"]),
                "combined_blank_count": combined_blank,
                "combined_blank_rate": rate(combined_blank, combined_rows),
                "note": field_note(field),
            }
        )
    return field_map


def write_field_map(path: Path, field_map: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=FIELD_MAP_COLUMNS)
        writer.writeheader()
        writer.writerows(field_map)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--field-map-output", type=Path, required=True)
    args = parser.parse_args()

    global_keys: Counter[tuple[str, ...]] = Counter()
    legacy = profile_source("legacy", args.legacy, global_keys)
    current = profile_source("current", args.current, global_keys)
    cross_source_duplicates = sum(
        count - 1 for count in global_keys.values() if count > 1
    )
    field_map = build_field_map(legacy["header"], legacy, current)
    legacy_award_max = legacy["dates"]["contractAwardDate-dateAttributionContrat"]["max"]
    current_award_min = current["dates"]["contractAwardDate-dateAttributionContrat"]["min"]
    unexpected_fallthrough = sorted(
        row["source_field"]
        for row in field_map
        if row["family"] == DEFAULT_FIELD_FAMILY
        and row["logical_field"] not in EXPECTED_CLASSIFICATION_FALLTHROUGH
    )
    checks = {
        "headers_identical": legacy["header"] == current["header"],
        "legacy_expected_column_count": legacy["header_columns"] == EXPECTED_COLUMNS,
        "current_expected_column_count": current["header_columns"] == EXPECTED_COLUMNS,
        # Official definition is a boundary, not an exact observation: the legacy
        # file holds contracts initially awarded on or before the cutover and the
        # current file holds those awarded on or after the following day. Asserting
        # equality additionally required a contract awarded on exactly each
        # boundary date, which no published rule guarantees.
        "legacy_cutover_respected": legacy_award_max is not None
        and legacy_award_max <= LEGACY_CUTOVER_LAST_DAY,
        "current_cutover_respected": current_award_min is not None
        and current_award_min >= CURRENT_CUTOVER_FIRST_DAY,
        "legacy_award_date_max": legacy_award_max,
        "current_award_date_min": current_award_min,
        "cross_source_candidate_version_key_duplicates": cross_source_duplicates,
        "field_map_rows": len(field_map),
        # The candidate version key is a hypothesis, and official supporting
        # documentation warns that reference numbers are not always consistent
        # between historical and new files. A duplicate is therefore evidence to
        # collect, not a reason to abort, so it quarantines instead of gating.
        "unclassified_fields": unexpected_fallthrough,
    }
    gate_conditions = (
        checks["headers_identical"],
        checks["legacy_expected_column_count"],
        checks["current_expected_column_count"],
        checks["legacy_cutover_respected"],
        checks["current_cutover_respected"],
        not unexpected_fallthrough,
    )
    result = {
        "source_scope": "CanadaBuys Contract History only",
        "expected_columns": EXPECTED_COLUMNS,
        "candidate_version_key": list(VERSION_KEY),
        "legacy": legacy,
        "current": current,
        "field_map": field_map,
        "checks": checks,
        "profiling_gate": "passed" if all(gate_conditions) else "failed",
        "quarantine": "candidate_version_key_duplicates"
        if cross_source_duplicates
        else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_field_map(args.field_map_output, field_map)


if __name__ == "__main__":
    main()
