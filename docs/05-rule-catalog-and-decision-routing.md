# Rule Catalog and Decision Routing

Audit date: 2026-09-21
Batch audited: `canadabuys_contract_history_20260822_001` (506,847 rows: legacy 485,573, current 21,274)

> Sections 2-6 describe ruleset **v1** as audited. Section 8 records what
> changed in **v2**, which is implemented and has been run against this batch.
> Section 10 answers the question that prompted the audit: is the source data
> actually clean?

This document answers one question for every check in the pipeline: **who changes
their behaviour because of it?** A check that cannot change anyone's behaviour is
not a control. It is decoration, and it is listed here as such.

## 1. The four possible outcomes

Every check must route to exactly one of these. "Filtering" is only one of them,
and it is the rarest.

| Action | Meaning | Who acts | Effect on rows |
| --- | --- | --- | --- |
| `abort` | The batch cannot be trusted at all. Stop before writing any output. | Pipeline owner | Nothing is published |
| `quarantine` | The batch is written, but is not promoted for use, pending a decision. | Pipeline owner | All rows held |
| `review` | Specific rows carry a condition a human must judge. Rows stay in the layer. | Business reviewer | Rows labelled, not removed |
| `observe` | A measured characteristic of the data with a documented explanation. No action. | Nobody | Rows unaffected, counted only |

Two things follow from this table that are currently blurred in the codebase:

- **`abort` is for technical untruth, not business wrongness.** A checksum
  mismatch means the file on disk is not the file we documented. That is a lie
  about provenance and nothing downstream can be trusted. A negative contract
  amount is not a lie; it is a number we do not yet know how to interpret.
- **`observe` is not a finding.** 649,726 of the current 739,080 finding rows
  are `observe`-class facts with published explanations. Emitting them as
  findings buries the 89,354 rows that actually need a person.

## 2. Layer 0: manifest and ingest preconditions

Source: [`src/build_bronze.py`](../src/build_bronze.py) lines 206-217. These raise
before any output is written.

| ID | Check | Authority | This batch | Action | Verdict |
| --- | --- | --- | --- | --- | --- |
| MF-01 | Recomputed SHA-256 of each raw CSV equals the manifest value | Project control | pass | `abort` | **Keep.** The only thing standing between you and silently ingesting an edited file. |
| MF-02 | The two source files have byte-identical headers | Official: both files share one published schema | pass | `abort` | **Keep.** |
| MF-03 | Header matches the header recorded in the approved profile | Project control | pass | `abort` | **Keep.** Detects a schema change published between profiling and ingest. |
| MF-04 | Manifest `parser_records` equals profiled row count | Project control | pass | `abort` | **Keep.** Detects a truncated or partially re-downloaded file. |

These four never fired on real data. That is the point of them. MF-01 and MF-04
are now proven to fire by the fault-injection suite in section 7. MF-02 and
MF-03 are covered indirectly by the renamed-column scenario, which stops at the
profiling stage before reaching them.

## 3. Layer 1: profiling gate

Source: [`src/profile_contract_history.py`](../src/profile_contract_history.py) lines 310-335.
All eight conditions must hold or `profiling_gate` is `failed`, which blocks Bronze.

| ID | Check | Authority | This batch | Action | Verdict |
| --- | --- | --- | --- | --- | --- |
| PF-01 | `headers_identical` | Official schema | pass | `abort` | Keep. Duplicates MF-02 at a different stage; acceptable. |
| PF-02 | Legacy header has exactly 91 columns | Official schema | pass | `abort` | Keep. |
| PF-03 | Current header has exactly 91 columns | Official schema | pass | `abort` | Keep. |
| PF-04 | Legacy max `contractAwardDate` **equals** `2023-05-31` | Official cutover definition | pass | `abort` | **Fix.** See 3.1. |
| PF-05 | Current min `contractAwardDate` **equals** `2023-06-01` | Official cutover definition | pass | `abort` | **Fix.** See 3.1. |
| PF-06 | Cross-source candidate version key duplicates == 0 | Project hypothesis | pass (0 dupes) | `abort` | **Keep, but reclassify.** See 3.2. |
| PF-07 | `field_map_rows == 91` | none | pass | `abort` | **Drop.** Tautology. See 3.3. |
| PF-08 | `all_fields_classified` | none | pass | `abort` | **Drop.** Tautology. See 3.3. |

### 3.1 PF-04 and PF-05 will produce a false failure

The official definition is that the legacy file contains contracts *initially
awarded on or before* 2023-05-31 and the current file contains those awarded
*on or after* 2023-06-01. The code asserts equality against the observed min/max:

```python
"legacy_cutover_observed": legacy["dates"][...]["max"] == "2023-05-31",
"current_cutover_observed": current["dates"][...]["min"] == "2023-06-01",
```

This additionally requires that at least one contract was awarded on exactly
2023-05-31 and at least one on exactly 2023-06-01. That happens to be true in
this batch. It is not guaranteed by any official rule, and it is not what we
mean. Correct form:

```python
"legacy_cutover_respected":  legacy_max  <= "2023-05-31",
"current_cutover_respected": current_min >= "2023-06-01",
```

### 3.2 PF-06 is a hypothesis being enforced as a law

`referenceNumber + contractNumber + amendmentNumber` was unique in this batch.
[CONTEXT.md](../CONTEXT.md) and [docs/01](01-source-and-data-contract.md) both
correctly call it a *candidate*. But the gate treats a duplicate as `abort`,
which means the first future batch containing a legitimate duplicate will stop
the pipeline rather than teach us something.

Recommended: route duplicates to `quarantine`, not `abort`, and emit the
duplicate key groups as a finding so the next batch produces evidence about
whether the key hypothesis survives. Official supporting documentation already
warns that reference numbers are not always consistent between new and
historical files, so this is a predictable event, not an emergency.

### 3.3 PF-07 and PF-08 cannot fail

`field_map` is built by iterating the header that PF-02 already asserted is 91
entries long, so `len(field_map) == 91` is implied by PF-02. And
`field_family()` ends with an unconditional `return "procurement_classification"`
([line 106](../src/profile_contract_history.py)), so it can never return an
empty string, so `all(row["family"] for row in field_map)` is always `True`.

Two of the eight gate conditions are therefore unfalsifiable. They make the gate
look more thorough than it is. Delete them, or replace PF-08 with a check that
can actually fail, for example: no field falls through to the default family
without being listed in an explicit allow-list of expected fall-through fields.

## 4. Layer 2: Bronze gate

Source: [`src/build_bronze.py`](../src/build_bronze.py) `validate_parquet`, run per source file.

| ID | Check | This batch | Action | Verdict |
| --- | --- | --- | --- | --- |
| BR-01 | Parquet row count equals manifest count | pass ×2 | `abort` | Keep. |
| BR-02 | `source_row_number` unique | pass ×2 | `abort` | Keep. |
| BR-03 | `source_row_number` contiguous 1..N | pass ×2 | `abort` | Keep. Together with BR-02 this proves no row was dropped, duplicated, or reordered. |
| BR-04 | All six lineage columns non-null | pass ×2 | `abort` | Keep. |
| BR-05 | The 91 source columns survive in order after the metadata columns | pass ×2 | `abort` | Keep. |
| BR-06 | Row 1's 91 values match the CSV's first row exactly | pass ×2 | `abort` | Weak on its own (one row), but see BR-07. |
| BR-07 | **Every cell of every row** compared against a fresh `csv.DictReader` pass | pass, 506,847 rows × 91 fields | `abort` | **Keep and promote.** This is the strongest control in the pipeline. |

BR-07 lives in a separate script,
[`src/verify_bronze_values.py`](../src/verify_bronze_values.py), and is not part
of the gate. It should be, or it should at least be a required step recorded in
the reconciliation JSON. Right now the Bronze gate can pass while BR-07 has never
been run for that batch, and nothing in the artifacts says which.

This layer is where the project's claim "you can always get back to the official
original value" is actually earned. MF-01 proves the input file is the one we
downloaded; BR-07 proves the Parquet is that file. Those two together are the
whole provenance argument.

## 5. Layer 3: derived gate

Source: [`src/build_quality_layers.py`](../src/build_quality_layers.py) lines 176-184.

| ID | Check | This batch | Action | Verdict |
| --- | --- | --- | --- | --- |
| SV-01 | Silver row count equals Bronze total | 506,847 | `abort` | Keep. |
| SV-02 | Per-source Silver counts equal Bronze per-source counts | pass | `abort` | Keep. |
| SV-03 | `findings_rows > 0` | 739,080 | `abort` | **Remove. The logic is backwards.** |
| SV-04 | `gold_summary_rows > 0` | 12 | `abort` | Remove, for the same reason. |

SV-03 fails the batch if the data is clean. It was presumably written to catch
"the findings query silently produced nothing", but it encodes that intent as a
business assertion about the data instead of a test about the code. A batch with
zero quality problems is the best possible outcome and this gate would reject it.

The real intent belongs in the fault-injection suite (section 7): assert that a
*known-dirty* fixture produces a known non-zero count. Against real data, assert
only that the query ran and the file was written.

Also in this layer: the script raises `FileExistsError` if the output directory
exists, so a re-run requires manually deleting the previous output. For an
immutable-batch design that is defensible, but it means there is no safe way to
re-run after fixing a rule. Once rule versioning exists (your item 1 from the
earlier list), the output path should include the rule-set version so a re-run
writes alongside rather than colliding.

## 6. Layer 4: the ten row-level rules

Source: [`src/build_quality_layers.py`](../src/build_quality_layers.py) lines 102-145.
Counts are legacy + current.

| ID | Rule | Authority | Findings | Current severity | Correct action | Verdict |
| --- | --- | --- | ---: | --- | --- | --- |
| QR-01 | `missing_award_date` | Project | 69,345 | review | `review` | **Keep.** Real gap: award date drives the legacy/current file assignment, so a blank one means that row's placement is unverifiable. |
| QR-02 | `unknown_amendment_type` | Official: field added in the 2026 restructuring; source systems do not all supply it | 471,328 | context | `observe` | **Demote to a profile metric.** The explanation is already published. This is a coverage statistic, not a finding. |
| QR-03 | `negative_contract_amount` | Official: GC proactive-publication guidance permits negative amendment values | 19,950 | review | `review`, conditioned | **Keep, but sharpen.** A negative amount on an `amendmentType = Amendment` row is expected. A negative amount on an `Original` row is not. Splitting by amendment type turns 19,950 undifferentiated rows into a much smaller, much more defensible set. |
| QR-04 | `zero_contract_amount` | Official: zero values are legitimate for standing offers and supply arrangements | 178,398 | context | `observe` | **Demote,** same reasoning as QR-02. Cross-tabulate against `instrumentType` instead. |
| QR-05 | `unusually_distant_end_date` (>= 3000-01-01) | Project; CanadaBuys displays `9999/09/09` publicly without explaining it | 53 | review | `review` | **Keep.** Small, concrete, answerable by one question to a data steward. |
| QR-06 | `future_amendment_date` | Project | 6 | review | `review` | **Keep, but fix determinism.** Uses `CURRENT_DATE`; must take `as_of_date` from the batch. |
| QR-07 | `invalid_award_date` (non-empty, unparseable) | Project | 0 | `quarantine` | **Keep.** Technical invalidity. If this ever fires, a date column has changed format. |
| QR-08 | `invalid_contract_amount` | Project | 0 | `quarantine` | **Keep.** Same. |
| QR-09 | `legacy_cutover_mismatch` | Official cutover definition | 0 | `quarantine` | **Keep.** A row in the wrong file means the publisher's own partitioning broke. |
| QR-10 | `current_cutover_mismatch` | Official cutover definition | 0 | `quarantine` | **Keep.** Same. |

### 6.1 What this table actually says

- **Four rules (QR-07..10) returned zero and are the only rules with `abort`/`quarantine`
  authority.** They are the rules that can legitimately stop a batch without any
  business judgement, because they detect the data not matching its own published
  definition. They are also the four that have never been proven to work.
- **Two rules (QR-02, QR-04) generate 88% of the volume and warrant no action.**
  Moving them out of `quality_findings` and into the profile drops the findings
  table from 739,080 rows to 89,354 and makes the review queue legible.
- **Only 12 of 91 columns are covered.** `tradeAgreements`, `gsin`, `unspsc`,
  `procurementMethod`, `contractStatus`, `instrumentType`, `noticeType`,
  `selectionCriteria`, `limitedTenderingReason`, and both entity families have
  no rules at all.

### 6.2 Rules that should exist and do not

These are all derivable from sources already identified, and none of them needs
business judgement to define.

| Proposed ID | Rule | Authority | Expected to fire? |
| --- | --- | --- | --- |
| QR-11 | `value_outside_published_domain` for the eight coded fields with published enumerations | Official data dictionary XML | Yes. `noticeType` contains `Request for Standing Offer` (7,330), `Request for Supply Arrangement` (5,095) and `RFP against Supply Arrangement` (2,302), none of which appear in the dictionary's published list. Needs verification against the parsed XML, not a summary. |
| QR-12 | `non_canonical_multivalue_encoding` for `tradeAgreements` | Structural | Yes. 12,706 distinct raw values, largely permutations of the same agreement sets. |
| QR-13 | `unexpected_currency` (anything other than CAD) | Observed: `contractCurrency` distinct == 1 across all 506,847 rows | No, today. Cheap insurance for future batches. |
| QR-14 | `end_date_before_start_date` where both populated | Structural | Unknown, not yet measured. |
| QR-15 | `amendment_date_before_award_date` where both populated | Structural; note the official docs define no required ordering, so this is `review`, not `quarantine` | Unknown, not yet measured. |
| QR-16 | `unknown_contracting_entity` against the GC organization reference table | Official: Data Reference Standard on GC Organization Names and Codes | Unknown; likely yes given legacy free-text entry. |

## 7. Proving the rules work: fault injection

Implemented in [`src/verify_rules.py`](../src/verify_rules.py). Result:
**15/15 scenarios pass**, recorded at
[`artifacts/rule_verification/batch_001_v2.json`](../artifacts/rule_verification/batch_001_v2.json).

The fixture is 4,000 real rows per source, reconstructed from Bronze. There is
no synthetic data and `data/raw/` is never written to. A pristine fixture was
not possible: all 506,847 rows in the batch trigger at least one of the 21
rules, so the suite asserts **deltas against a measured baseline** rather than
against zero.

### 7.1 Tier one: pipeline gates

Mutate the CSV or manifest, run the real stage scripts, assert the named stage
stops with the expected diagnosis.

| Scenario | Targets | Result |
| --- | --- | --- |
| One cell of the legacy CSV changes after the manifest is written | MF-01 | `ValueError: Checksum mismatch` |
| A row is deleted from the CSV after the manifest is written | MF-01 | `ValueError: Checksum mismatch` |
| CSV and checksum agree but the manifest declares the wrong record count | MF-04 | `ValueError: Row count differs from approved profile: legacy` |
| `contractAmount-montantContrat` renamed in the current file only | PF-01 | stage exits non-zero |
| A legacy row awarded 2024-01-15, after the cutover | PF-04 | `profiling_gate: failed`, condition `legacy_cutover_respected` |
| A version key duplicated | PF-06 | `profiling_gate: passed`, `quarantine: candidate_version_key_duplicates` |

Two of these needed the test design to be corrected, which is itself the point
of writing them:

- The dropped-row case was **not** testing MF-04. Deleting a row also changes the
  checksum, so MF-01 fires first and MF-04 is never reached. A separate scenario
  now isolates MF-04 by declaring a wrong record count against an intact file.
- The renamed-column case stops the pipeline, but via an uncaught
  `KeyError: 'contractAmount-montantContrat'` rather than a stated gate failure.
  It is safe (nothing is written) but the diagnosis is poor. Worth converting to
  an explicit schema check with a readable message.

### 7.2 Tier two: row-level rules

Mutate one cell in Bronze, run only the quality layer, assert the target rule
moves by exactly the expected delta.

All five `integrity` rules are now proven to fire, which is what the "zero
integrity findings" result in section 8.1 depends on:

| Mutation | Rule proven |
| --- | --- |
| `contractAwardDate` = `15/03/2024` | `invalid_award_date` |
| `contractAmount` = `1,234.00` | `invalid_contract_amount` |
| Current-file row awarded 2022-01-15 | `current_cutover_mismatch` |
| Legacy-file row awarded 2024-01-15 | `legacy_cutover_mismatch` |
| `contractCurrency` = `USD` | `unexpected_currency` |
| `totalContractValue` = `-500.00` | `negative_total_contract_value` |
| End 2024-01-01 against start 2024-06-01 | `end_date_before_start_date` |
| `contractEndDate` = `9999-09-09` | `unusually_distant_end_date` |
| `supplierAddressCountry` = `CA` | `non_canonical_supplier_country` |

### 7.3 Rule interactions the suite exposed

Seven of nine mutations moved exactly one rule. Two moved two, and both are
correct rather than defects:

- Setting a legacy row's award date to 2024-01-15 also tripped
  `end_date_before_award_date`, because that row's real end date precedes the
  injected award date. One edit genuinely created two conditions.
- Setting `totalContractValue` to -500.00 also tripped
  `contract_amount_exceeds_total_value`, because the row's real amount exceeds
  500 in magnitude.

These are worth keeping visible. A row carrying several findings is normal, and
the suite reports which rules moved rather than asserting that only one did.

## 8. What shipped in ruleset v2

Implemented in [`src/build_quality_layers.py`](../src/build_quality_layers.py)
and [`src/profile_contract_history.py`](../src/profile_contract_history.py).

| Change | Affected | Status |
| --- | --- | --- |
| Deleted unfalsifiable gate conditions; replaced PF-08 with a check that names fields falling through to the default family against an explicit 11-field allow-list | PF-07, PF-08 | done |
| Equality changed to boundary comparison (`<=` / `>=`) | PF-04, PF-05 | done |
| Version-key duplicates now set a `quarantine` reason instead of failing the gate | PF-06 | done |
| Removed the "must find something" gate conditions | SV-03, SV-04 | done |
| Every rule declares a `rule_class` that determines routing | all | done |
| `observe`-class rules write to `quality_observations.parquet`, not `quality_findings.parquet` | QR-02, QR-04 | done |
| Negative amounts split by whether the activity type is known | QR-03 | done |
| `--as-of-date` is a required argument; `CURRENT_DATE` removed | QR-06 | done |
| Output path carries `ruleset=` so a re-run writes alongside rather than colliding | all | done |
| Gold summary is a cross join of the full rule catalog against sources, so zero-hit rules are reported as zero rather than being absent | all | done |
| 11 new rules added | QR-11..21 | done |
| Fault-injection verification, 15 scenarios | all | done, see section 7 |
| BR-07 folded into the Bronze gate | BR-07 | **not done** |
| Reference-data joins (GC org codes, GSIN/UNSPSC, dictionary domains) | QR-11, QR-16 | **not done** |

### 8.1 v2 results on this batch

```
Gate: passed (technical integrity)  Decision: promote_with_review_queue
Silver 506,847 rows; 21 rules evaluated
  integrity              0
  review           135,380
  normalization    282,779
  observe          650,571
Rows needing human review: 126,708
```

The findings table now holds 418,159 rows across `integrity`, `review` and
`normalization`, down from an undifferentiated 739,080. The 650,571 documented
characteristics moved to a separate observations file. 126,708 distinct rows
carry at least one condition a person must judge.

`future_amendment_date` moved from 6 hits to 7. That is the determinism fix
working: the count is now anchored to the batch's `as_of_date` (2026-08-22)
instead of to whatever day the pipeline happened to run.

## 9. Full v2 rule list with counts

| Class | Rule | Authority | Count |
| --- | --- | --- | ---: |
| integrity | `invalid_award_date` | project technical | 0 |
| integrity | `invalid_contract_amount` | project technical | 0 |
| integrity | `legacy_cutover_mismatch` | official cutover definition | 0 |
| integrity | `current_cutover_mismatch` | official cutover definition | 0 |
| integrity | `unexpected_currency` | observed single value in batch 001 | 0 |
| review | `missing_award_date` | official cutover definition | 69,345 |
| review | `missing_standardized_supplier_name` | project normalization gap | 31,402 |
| review | `negative_amount_unknown_activity` | official guidance covers amendments only | 19,105 |
| review | `contract_amount_exceeds_total_value` | official cumulative value definition | 14,686 |
| review | `amendment_date_before_award_date` | logical contradiction | 368 |
| review | `negative_total_contract_value` | official cumulative value definition | 252 |
| review | `end_date_before_award_date` | logical contradiction | 148 |
| review | `unusually_distant_end_date` | project unexplained sentinel | 53 |
| review | `end_date_before_start_date` | official definition (end expires after start) | 14 |
| review | `future_amendment_date` | project technical | 7 |
| normalization | `non_canonical_trade_agreements` | structural | 244,613 |
| normalization | `non_canonical_supplier_country` | structural | 37,340 |
| normalization | `postal_code_format_mismatch_canada` | structural | 826 |
| observe | `unknown_amendment_type` | official: field added in 2026 restructuring | 471,328 |
| observe | `zero_contract_amount` | official: zero legitimate for standing offers | 178,398 |
| observe | `negative_amount_on_known_amendment` | official: guidance permits decreases | 845 |

## 10. Is the source data clean?

No. The audit was prompted by the hypothesis that an official government
dataset is already validated upstream and therefore nothing could legitimately
be filtered. The evidence contradicts that.

**Logical contradictions.** 368 rows have an amendment dated before the contract
was awarded. Example: award 2009-01-20, amendment 002 dated 2009-01-16. 14 rows
end before they start. 148 end before they are awarded.

**Contradictions of the dataset's own published definitions.** `totalContractValue`
is officially the cumulative value from award date to present, and CanadaBuys
displays it as non-negative even where individual amendment values are negative.
252 rows carry a negative cumulative value. Reference `EE520-182837/001/MTC`,
amendment `000` (an original contract), reports `contractAmount` 8,791,885.00
against `totalContractValue` of -1,912,829.00. This is the same class of defect
the official 2026 restructuring note says it corrected for amendment `000` rows.
A further 14,686 rows report a single amount larger in magnitude than the
cumulative total that is supposed to contain it.

**Encoding that was never normalized.** `tradeAgreements` has 12,705 distinct raw
values that collapse to 351 once the newline-delimited set is deduplicated and
sorted; 244,613 rows carry a non-canonical ordering. Supplier country mixes
ISO-2 codes with full names (Canada 423,432 / CA 34,135; United States 34,976 /
US 2,415). `contractingEntityName` holds one organization under two spellings:
"Public Works and Government Services Canada" (469,499 rows) and "Department of
Public Works and Government Services (PSPC)" (37,348 rows), and the split does
not follow the legacy/current boundary.

**Coverage that forces an external join.** `unspsc` is blank in 469,502 rows and
`gsin` in 35,808, with only 13 rows blank in both. GSIN covers the legacy period,
UNSPSC the current one, reflecting the 2021 transition. A single product-category
dimension spanning 2009-2026 is therefore impossible without the official
[GSIN to UNSPSC mapping](https://open.canada.ca/data/en/dataset/588eab5b-7b16-4a26-b996-23b955965ffa).

**What upstream publication does and does not guarantee.** It guarantees that
what must be disclosed was disclosed. It does not guarantee internal consistency,
and the Auditor General's 2025 Report 5 found gaps in the completeness and
accuracy of proactively disclosed contract information, after which CBSA built
its own validation process. The reason nothing was filtered in v1 is not that
the data is clean. It is that v1 only looked at 12 of 91 columns.

## 11. Sources

- [CanadaBuys data dictionary (XML)](https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/ac-cb/achatscanada-canadabuys-dd.xml)
- [Supporting Documentation: CanadaBuys contract history, 2026 CSV restructuring](https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/ac-cb/COsoutien-CHsupport-eng.html)
- [GC proactive-publication contracts data dictionary](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/resource/fac950c0-00d5-4ec1-a4d3-9cbebf98a305)
- [Data Reference Standard on GC Organization Names and Codes](https://open.canada.ca/data/en/dataset/57180b36-3428-4a7f-afe3-2161a6b44ec5)
- [OAG Report 5, Professional Services Contracts (PACP material, 2025)](https://www.canada.ca/en/department-national-defence/corporate/reports-publications/proactive-disclosure/2026/professional-services-contracts.html)
