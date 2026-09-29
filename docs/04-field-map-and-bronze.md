# First stage: profiling and Bronze ingestion

## Fixed boundary

The only batch inputs are Legacy Contract History (485,573 rows) and Current
Contract History (21,274 rows). They cover two system periods of the same
Contract History domain. Award Notices, Tender Notices, and yearly CSVs are
not inputs. The existing SQLite database is a separate browsing aid, not the
Bronze source of truth.

## Profiling Part 2: Field Map

Run from the project root:

```bash
python3 src/profile_contract_history.py \
  --legacy data/raw/legacy/contract_history_2009_2023_05.csv \
  --current data/raw/current/contract_history_2023_06_present.csv \
  --output artifacts/profiles/batch_001_profile.json \
  --field-map-output artifacts/profiles/batch_001_field_map.csv
```

The JSON contains the batch gate, row counts, exact headers, candidate-version
key checks, date/amount diagnostics, and per-field blank counts. The CSV Field
Map contains one row for each of the 91 physical fields: source order and
header; logical name/language; family; Bronze and candidate Silver type;
public-output policy; semantic status; blank counts and rates in each source;
and a note for fields with important unresolved behavior. Bilingual versions
remain separate physical fields but represent one logical concept (58 logical
concepts overall). The seven families are identity/version (8), dates (5),
amounts/state (9), procurement classification (19), supplier (17), government
roles (17), and contact/narrative (16).

Current batch: gate **passed**. Both headers contain the same 91 ordered
fields, cutover extrema match the initial award-date split, and the candidate
version key has no duplicate in this batch. Passing this structural gate does
not certify a legal compliance outcome or justify financial aggregation.

Open findings that pass through unchanged:

- `amendmentType` blank: 469,520 Legacy and 1,808 Current rows; no imputation.
- `contractAwardDate` blank: 69,040 Legacy and 305 Current rows; cutover is
  checked where populated, and missing dates remain findings.
- `contractAmount` negative: 19,332 Legacy and 618 Current rows; zero:
  172,968 Legacy and 5,430 Current rows. These are retained.
- 11 physical contact fields are completely blank in this batch. Contact and
  address details plus free-text descriptions are excluded from public
  analytical outputs, though still retained for lineage in Bronze.
- Sentinel-like and future dates have no universal official interpretation.
  Their raw strings are retained pending an explicit review rule.

The Field Map is a proposed handling classification, not a claim that every
field is business-approved. In particular, Silver type candidates and
public-output decisions need review before they become downstream contracts.

## Bronze: raw-faithful immutable batch

Requires Python and `duckdb`. Run **only after** inspecting the profiling
gate:

```bash
python3 src/build_bronze.py \
  --manifest data/manifest/batch_001_manifest.yaml \
  --profile artifacts/profiles/batch_001_profile.json \
  --output-root data/bronze \
  --reconciliation-output artifacts/bronze/batch_001_reconciliation.json
```

Before writing, the builder requires a passed profiling gate, checks profiled
headers and row counts, verifies SHA-256 of each exact source file against
the acquisition manifest, and requires identical headers. It creates
two compressed Parquet files under
`data/bronze/batch_id=canadabuys_contract_history_20260822_001/`:
`legacy.parquet` and `current.parquet`. Each row has the 91 original source
fields as strings plus `batch_id`, `source_dataset`, `source_file`,
`source_row_number`, `ingested_at_utc`, and `source_sha256`. Empty source
strings remain empty strings; no type conversion or value correction occurs.
The output directory is assembled in a temporary location and moved into
place only after all checks pass; an existing batch is never overwritten.

Current batch: Bronze gate **passed**. Reconciled output is 485,573 Legacy +
21,274 Current = 506,847 rows, with 97 columns per file. Checks cover counts,
continuous unique row numbers, lineage presence, exact source headers, and
first-row preservation. No original field became null.

For a complete parsed-value audit using a separate Python CSV parser:

```bash
python3 src/verify_bronze_values.py \
  --reconciliation artifacts/bronze/batch_001_reconciliation.json
```

This audit passed for all 506,847 rows and all 91 original fields, comparing
the values and row order exactly to Bronze. It tests parsed field equality,
not byte-for-byte equivalence between two different file formats.

## Silver and quality-reporting Gold

Run after both upstream gates have passed:

```bash
python3 src/build_quality_layers.py \
  --profile artifacts/profiles/batch_001_profile.json \
  --bronze-reconciliation artifacts/bronze/batch_001_reconciliation.json \
  --bronze-dir data/bronze/batch_id=canadabuys_contract_history_20260822_001 \
  --output-root data/derived
```

The batch output under `data/derived/batch_id=.../` has a typed
`silver_contract_activity.parquet`, row-level `quality_findings.parquet`,
`gold_quality_summary.parquet`, and `reconciliation.json`. Silver selects
non-contact analytical columns while retaining their raw values and lineage;
the full 91 fields remain available in Bronze. Typed date and decimal values
are additional columns, not destructive replacements.

The Gold file reports counts by source, rule, and severity; it does not sum
money. Context findings (empty amendment type, zero contract amount) are not
errors. Review findings (missing award date, negative amount, distant end
date, future amendment date) call for investigation, not automatic correction
or legal judgment. The distant-date cutoff (`>= 3000-01-01`) is a project
diagnostic threshold, not an officially defined sentinel. Future is relative
to the date of execution.

Current derived gate **passed**: 506,847 Silver rows, 739,080 finding records
(one source row may have multiple findings), and 12 Gold rule/source groups.
Four independently profiled metrics for each source (missing award date, blank
amendment type, negative amount, zero amount) match the Gold counts. No
nonblank selected award date or amount failed the Silver type conversion.

## Remaining interview boundary

Contract-value or spend Gold metrics remain blocked until version-selection
and amount semantics are agreed. AWS deployment is a later execution target
for the tested pipeline; no AWS deployment, approval-gated remediation, or
legal compliance determination has been implemented by this step.

For the résumé, say that this batch validates and reconciles **two official
source files across two system periods**. Do not describe it as comparing
Contract History with Award Notices or as proving the same business fact from
two independent systems. Existing résumé claims about AWS processing and
human approval must be verified against future actual implementation.
