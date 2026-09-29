# Querying the Contract History SQLite database

The database is built from the two official Contract History CSVs. It keeps
the source dataset and source row number for lineage, preserves the original
headers in `source_columns`, and stores contract amounts as numeric SQLite
values.

## Build or rebuild

From the project directory:

```bash
python3 src/build_contract_history_db.py \
  --legacy data/raw/legacy/contract_history_2009_2023_05.csv \
  --current data/raw/current/contract_history_2023_06_present.csv \
  --output data/database/canadabuys_contract_history.sqlite
```

The output is a local SQLite file. Re-running the command rebuilds it from the
source CSVs and does not modify the CSVs.

## Open in VS Code

Install a SQLite extension such as `SQLite` by alexcvzz or `SQLite Viewer`.
Open `data/database/canadabuys_contract_history.sqlite`, then run SQL from the
extension's query editor.

## Example queries

```sql
-- Largest published contract amounts
SELECT
  supplier_standardized_name_eng,
  contracting_entity_name_eng,
  contract_amount,
  contract_currency
FROM contract_history
WHERE contract_amount IS NOT NULL
ORDER BY contract_amount DESC
LIMIT 20;
```

```sql
-- Contracts awarded in a date range
SELECT
  reference_number,
  contract_number,
  amendment_number,
  contract_award_date,
  supplier_standardized_name_eng,
  total_contract_value
FROM contract_history
WHERE contract_award_date >= '2025-01-01'
  AND contract_award_date < '2026-01-01'
ORDER BY total_contract_value DESC;
```

```sql
-- Find all source fields and their SQLite names
SELECT ordinal, source_header, sqlite_column
FROM source_columns
ORDER BY ordinal;
```

The raw rows are contract-history versions. Do not sum every row as if each row
were an independent spend event. Amendments and cumulative values require a
separate version-selection rule.
