# CanadaBuys official field semantics research

Research date: 2026-09-20  
Scope: primary official sources only (CanadaBuys / Open Government Canada / Government of Canada / DBeaver / DuckDB). This note separates confirmed facts from inference and unresolved questions. It does not redefine the source data.

## Decision summary

1. Keep the existing SQLite file for **read-only exploration**. DBeaver is a database client, not a replacement storage format; it can open the SQLite file directly. Creating a second DuckDB copy is not needed for the current profiling step.
2. If DuckDB is selected later for Bronze/Silver analytics, create the `.duckdb` file through a reproducible pipeline with explicit schema and lineage. DuckDB can also attach and query the existing SQLite file directly, so DBeaver export/import is not a prerequisite.
3. Finish the unresolved-business-semantics review before Data Profiling Part 2. Do not infer blank `amendmentType`, sentinel-like dates, or amount rules from field names alone.
4. Do not add Award Notices to the current Contract History data-quality MVP merely because Contract History contains `contractAwardDate`. “Award date” is a field on a contract-history record; Award Notices are a separate publication dataset.

## 1. `amendmentType`

### Confirmed

- The CanadaBuys data dictionary defines `amendmentType-typeModification-eng/fra` as whether an entry corresponds to the **original contract**, a subsequent **amendment**, or a **termination**.
- The field was added to the restructured Contract History and Award Notices CSVs during the March 17-April 10, 2026 update. The same update also addressed inconsistent use of `contractAwardDate` and `amendmentDate`, incorrect original amounts on amendment `000`, and duplicated amounts on purely administrative changes.
- The Contract History dataset is compiled from multiple procurement systems. Open Government explicitly warns that source systems do not capture all information in the same format, so fields can remain blank when a source system does not provide them.

### Evidence-backed inference

- The high blank rate in Legacy is consistent with the fact that `amendmentType` was newly added in the 2026 restructuring and historical source systems may not have supplied a reliable activity type. This is plausible, but the official documentation does **not** state that this is the specific reason for every Legacy blank.

### Unresolved

- No official source found gives a per-system or per-period explanation for the Legacy blank pattern.
- The dictionary does not publish a complete controlled-value list or a safe inference rule from `amendmentNumber`. Therefore blank values must remain `unknown/not supplied`; they must not be backfilled as Original, Amendment, or Termination without a separately validated rule.

## 2. Amount fields, zeroes, and negatives

### Confirmed

| Field | Official CanadaBuys meaning |
| --- | --- |
| `contractAmount-montantContrat` | Monetary value of the awarded contract, including taxes, in the currency specified by `contractCurrency`. |
| `totalContractValue-valeurTotaleContrat` | Cumulative total value from the contract award date to the present: original plus all amendments, including values predating January 2009. |
| `contractCurrency-contratMonnaie` | Currency applicable to `contractAmount` or `totalContractValue`. |

- The CanadaBuys portal describes total contract value as the initial contract value plus the sum of all amendments. Official contract-history pages display negative “Amendment value” examples alongside a non-negative cumulative total.
- Government of Canada proactive-publication guidance, for a related but different contracts dataset, explicitly permits negative amendment values (decreases) and requires a minus sign. It also documents legitimate zero values for standing-offer/supply-arrangement agreements and zero amendment values where no amendment exists.
- The 2026 Contract History support note says repeated prior `contractAmount` values on purely administrative changes could falsely imply a value change; this issue was corrected. Therefore the amount on each raw history row is not safely additive without first identifying the row/activity semantics.

### Evidence-backed inference

- Negative `contractAmount` values on Contract History amendment rows can represent contract-value decreases. Zeroes can represent no value change or a zero-valued instrument. These interpretations are supported by official procurement reporting conventions and CanadaBuys examples, but CanadaBuys does not publish a row-by-row rule that explains every observed zero or negative value.
- `totalContractValue` is the better candidate for a current cumulative value, while `contractAmount` may carry an original or amendment activity amount depending on the entry. This is consistent with the portal display and 2026 correction notice, but the dictionary's `contractAmount` wording alone is not sufficiently precise to lock a Gold aggregation rule.

### Unresolved / required handling

- No official CanadaBuys source found defines every legitimate zero/negative scenario or says that all such values are valid.
- Do not delete, take absolute values, replace zeroes with nulls, or sum all raw `contractAmount` rows.
- For profiling, report blank/zero/negative separately by source and by populated `amendmentType`; retain raw amount and currency. Gold metrics remain blocked until version-selection and amount-grain rules are approved.

## 3. Date fields

### Confirmed official definitions

| Field | Meaning |
| --- | --- |
| `contractAwardDate-dateAttributionContrat` | Date when a contract is awarded. In the Contract History dataset this is the **initial award date** used to place records into fiscal-year / Legacy-vs-CanadaBuys files. |
| `contractStartDate-contratDateDebut` | Date on which an awarded contract legally comes into effect. |
| `amendmentDate-dateModification` | Date on which a tender notice, or subsequent contract, is amended. |
| `contractEndDate-dateFinContrat` | Last date on which an awarded contract is considered valid; it expires after this date. |
| `publicationDate-datePublication` | Date on which the tender notice, award notice, or contract is publicly published. |

- “Current” Contract History means contracts **initially awarded on or after 2023-06-01**, not contracts whose legal start date is on or after that date. Therefore an earlier `contractStartDate` in the Current file does not by itself violate the published file definition.
- The official definitions do not state a required ordering among award, start, amendment, publication, and end dates. A future start/end/amendment date can be planned or erroneous; the documentation does not supply a universal decision rule.

### Unresolved

- No official documentation found defines `9999-09-09` or `3000-12-31` as sentinel values. CanadaBuys publicly displays `9999/09/09` as an expiry date on multiple contract pages, but does not explain its business meaning there.
- No official source found states whether future amendment/start dates are planned dates, data-entry errors, migration artefacts, or valid exceptions.
- Handling rule: retain the original value, parse it technically if possible, and flag these values as `needs_review`; do not convert them to null, “open-ended”, or invalid without source confirmation.

## 4. Contract History versus Award Notices

### Confirmed distinction

| Dataset | Official scope | System-of-record cutover / refresh |
| --- | --- | --- |
| Contract History | History of contracts awarded by PWGSC on behalf of federal departments/agencies; a file contains contracts by **initial contract award date** and their later amendments. | CanadaBuys authoritative from 2023-06-01; monthly. |
| Award Notices | Government of Canada award notices published under the Financial Administration Act; includes notice lifecycle amendments and status Active/Cancelled/Expired. | CanadaBuys system of record for tender/award notices from 2022-08-08; current files refreshed daily. |

- They are related publications, not interchangeable tables. An Award Notice communicates an award publication and its lifecycle; Contract History represents contract-history entries over time.
- The shared dictionary identifies potential linking fields:
  - `contractNumber`: unique identifier assigned to an awarded contract; used in Award Notices and Contract History.
  - `solicitationNumber`: department-issued identifier for tender and award notices; used in Tender Notices, Award Notices, and Contract History.
  - `referenceNumber`: internal identifier that may specify a sourcing project, contract workspace, and/or contractor; used across the three datasets.
  - `amendmentNumber`: amendment sequence value; used across the three datasets.
- Official supporting documentation warns that reference numbers are not always consistent between new and historical files and recommends `solicitationNumber` when linking tender notices to award notices.

### Evidence-backed inference

- For Award Notice-to-Contract History exploration, `contractNumber` is a strong candidate, possibly combined with `solicitationNumber` and/or `amendmentNumber`; however, no official source found guarantees a stable one-to-one foreign key across all legacy/current records. Candidate joins must be profiled for nulls, duplicates, and one-to-many relationships before use.
- Award Notices are **not required** for the present Contract History-only quality MVP. They become useful only if the scope explicitly adds cross-publication reconciliation (for example, notice status/description comparison or linkage coverage). Adding them now would introduce a second grain, a different cutover date, and a different refresh cadence before Contract History semantics are settled.

## 5. SQLite, DBeaver, and DuckDB

### Confirmed product facts

- DBeaver can connect to embedded databases such as SQLite by providing the database file path. Its Data Transfer feature can move tables between databases and can import/export common formats.
- DuckDB has an official DBeaver/JDBC workflow: connect to a `.duckdb` file and inspect/query its tables.
- DuckDB can operate with a persistent local database file. `.duckdb` is a common extension, not a special export format that DBeaver must create.
- DuckDB's official SQLite extension can attach an existing SQLite file and query its tables directly; it can also copy data between SQLite and DuckDB. DuckDB is strongly typed while SQLite uses type affinity, so type mapping must be reviewed.

### Project recommendation

- Keep `canadabuys_contract_history.sqlite` as an **exploration convenience**, not as approved Bronze. Open the same file in DBeaver if its table browser is easier than the VS Code extension. This changes the viewing tool, not the data.
- Do not import the raw CSVs into DBeaver again and do not make a second ad hoc DuckDB copy now. That would add duplicated storage without resolving the current semantic questions.
- If DuckDB is chosen for the implemented layers, build it from the official raw CSVs (or deliberately copy from an audited staging representation), with source checksum, source file, source row number, ingestion timestamp, raw strings, typed columns, and rejected/flagged parse outcomes. Treat it as a new pipeline artifact, not an export for viewing.
- If analytical performance is needed before that design is approved, use DuckDB to attach/query the SQLite file read-only. Be aware that the existing SQLite builder has already normalized names/types; use the official CSVs for raw-field verification.

## What should be confirmed before Data Profiling Part 2

Proceed with field completeness/category profiling, but carry these as explicit open questions rather than resolved business rules:

1. Blank `amendmentType`: preserve as unknown; do not infer from amendment number yet.
2. `contractAmount`: profile by activity type and amendment number; do not define spend or additive value yet.
3. `totalContractValue`: treat as cumulative published value, but do not select a “latest” row until version ordering is validated.
4. Date anomalies: flag sentinel-like and future dates without rewriting them.
5. Award Notices: leave out of MVP inputs; record cross-dataset reconciliation as an optional later scope change.

## Official sources used

### CanadaBuys / Government of Canada

- [CanadaBuys Contract History dataset metadata (Open Government Portal)](https://open.canada.ca/data/en/dataset/4fe645a1-ffcd-40c1-9385-2c771be956a4)
- [CanadaBuys data dictionary](https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/ac-cb/achatscanada-canadabuys-dd.xml) (also checked against the project's downloaded official PDF snapshot)
- [Supporting Documentation: CanadaBuys contract history - 2026 CSV restructuring](https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/ac-cb/COsoutien-CHsupport-eng.html)
- [CanadaBuys Award Notices dataset metadata (Open Government Portal)](https://open.canada.ca/data/en/dataset/a1acb126-9ce8-40a9-b889-5da2b1dd20cb)
- [Supporting Documentation: tender and award notice datasets](https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/ac-cb/soutien-support-eng.html)
- [Example CanadaBuys Contract History page: negative amendment and `9999/09/09` expiry](https://canadabuys.canada.ca/en/tender-opportunities/contract-history/t8009-200049/001/xlv-001)
- [Government of Canada proactive-publication contracts data dictionary](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/resource/fac950c0-00d5-4ec1-a4d3-9cbebf98a305)

### DBeaver / DuckDB

- [DBeaver: Create connection](https://dbeaver.com/docs/dbeaver/Create-Connection/)
- [DBeaver: Data transfer](https://dbeaver.com/docs/dbeaver/Data-transfer/)
- [DuckDB: DBeaver SQL IDE](https://duckdb.org/docs/current/guides/sql_editors/dbeaver)
- [DuckDB: SQLite extension](https://duckdb.org/docs/current/core_extensions/sqlite)
- [DuckDB: persistent database connections](https://duckdb.org/docs/current/connect/overview)
