# CanadaBuys Data Quality & Compliance Agent

A data quality and governance layer over Canada's published federal contract history.

## What this is, in one paragraph

Canada publishes the history of every contract awarded by PSPC on behalf of
federal departments as two open CSV files, 506,847 rows spanning 2009 to 2026.
This project takes those files and produces a defensible answer to one question:
**which of these rows can be used, and which need a person to look at them first?**
It proves the files were ingested without alteration, evaluates them against 21
rules that each cite an authority, classifies every finding by what action it
requires, and routes the batch to one of three outcomes. It does not modify,
delete, or reinterpret a single source value.

## Where to verify each claim

| Claim | Where it is backed |
| --- | --- |
| Validates 506,847 records against quality and compliance rules | 21 rules in [`src/build_quality_layers.py`](src/build_quality_layers.py), catalogued with their authority in [docs/05](docs/05-rule-catalog-and-decision-routing.md); one policy rule in [`src/check_trade_thresholds.py`](src/check_trade_thresholds.py) |
| Flags records for review with a full audit trail | 126,708 rows routed to review; every applied change recorded per cell with its old value and approver by [`src/apply_remediation.py`](src/apply_remediation.py) |
| Cross-checks records across two official sources | Four checks *between* the two files, listed below |
| Checksums and atomic batch writes | SHA-256 recomputed per run in [`src/build_bronze.py`](src/build_bronze.py); output written to a temp directory and moved with `os.replace`, so a failed run leaves nothing behind |
| Reproducible and auditable runs | `--as-of-date` is required rather than read from the clock, output paths carry a ruleset version, and batches are never overwritten. The AWS run reproduced the laptop's numbers exactly ([docs/08](docs/08-second-batch-and-cloud-run.md)) |
| Approval-gated remediation before automated correction | 7 proposals, four refusals enforced in code ([docs/07](docs/07-agent-and-approval-layer.md) section 7) |
| AWS batch processing | Container on AWS Batch with Fargate, 124 seconds ([docs/08](docs/08-second-batch-and-cloud-run.md)) |

### The two official sources, and the four checks between them

The dataset ships as two separately published files covering two systems of
record: **legacy** (contracts awarded up to 2023-05-31) and **current**
(2023-06-01 onward). Each has its own URL, checksum and row count in
[`data/manifest/`](data/manifest).

1. **Identical schema** — all 91 column names compared between the two files, character for character
2. **No shared version key** — one counter spans both files, checking that no `referenceNumber + contractNumber + amendmentNumber` appears in both
3. **Mutually defined boundary** — the legacy file's latest award date and the current file's earliest are each checked against the other file's range. This is what makes it a cross-check rather than two independent validations
4. **Row-level cutover** — `legacy_cutover_mismatch` and `current_cutover_mismatch` flag individual rows on the wrong side

A genuinely different dataset was also tested: the Treasury Board proactive
publication of contracts over $10,000. **It could not be linked.** The
identically-named `reference_number` field matched 0 of 92,128 attempts, and the
real candidate key reached 8.7%, because one stream records the PSPC contract
number and the other records the client department's own commitment number.
No reconciliation layer was built. See [docs/06](docs/06-cross-source-linkage-assessment.md).

## The problem it solves

Without this layer, every analyst who touches this dataset independently
rediscovers the same 21 problems, makes a different silent decision about each
one, and produces numbers that do not reconcile with the next analyst's. The
negative amounts, the blank activity types, the 12,705 spellings of 351 trade
agreement combinations: each person either misses them or invents a private
workaround. This layer makes those decisions once, in the open, with the
reasoning attached.

## What it is not

| Not this | Why |
| --- | --- |
| An analytics project | It computes no spend totals. Summing raw rows is invalid until a version-selection rule is approved, and that rule is not yet approved. |
| A data cleaning project | Nothing is modified or deleted at any layer. Findings are labels, not edits. |
| A master data project | It detects that a supplier has two spellings. It does not decide they are the same company. See "Scope boundaries" below. |
| A distributed computing project | 506,847 rows is a single-node workload. Profiling runs in 13 seconds. |
| A compliance audit | Contract History publishes contract fields, not the rationale, evaluation, and approval records a procurement audit examines. Findings are data-quality observations, never legal conclusions. |

## The five layers and what each one guarantees

Each layer answers a **different** question. The first four prove the pipeline
is not lying. Only the fifth is about the data itself.

| Layer | Question it answers | Failure means |
| --- | --- | --- |
| Manifest | Is the file on disk the one we documented? | The source changed behind our back |
| Profiling | Does the structure match the published definition? | The publisher changed something |
| Bronze | Is our copy byte-faithful to the source? | Our ingestion code is wrong |
| Derived | Did we lose rows in transformation? | Our transformation is wrong |
| Rules | Which rows need a person? | The data has problems |

This separation is the point. When something breaks, you immediately know
whether the pipeline broke or the data did. The gate is named
`technical integrity gate` for exactly this reason: **passing it is not a
quality certificate.**

## The four outcomes every rule routes to

"Filtering" is one of four options, and it is the rarest.

| Class | Meaning | Effect | Count in batch 001 |
| --- | --- | --- | ---: |
| `integrity` | Data contradicts its own published definition | Quarantine the batch | 0 |
| `review` | A human must judge this row | Label the row, keep it | 135,380 |
| `normalization` | Structurally non-canonical, deterministically fixable | Label until a canonical form is approved | 282,779 |
| `observe` | Documented characteristic with a published explanation | Count only, no action | 650,571 |

Batch decision: `promote_with_review_queue`. **126,708 rows** carry at least one
condition needing human judgement, out of 506,847.

The `observe` class matters more than it looks. 650,571 hits are things the
official documentation already explains (blank `amendmentType` was added in the
2026 restructuring; zero amounts are legitimate for standing offers). Keeping
them out of the findings table is what makes the 126,708 legible.

## Is the source data clean? No.

This was the project's central uncertainty. It is settled, with evidence in
[docs/05 section 10](docs/05-rule-catalog-and-decision-routing.md):

- **368 rows** have an amendment dated before the contract was awarded
- **252 rows** report a negative cumulative contract value, contradicting the
  official definition and CanadaBuys' own display convention
- **516 contracts** report conflicting initial award dates across their own rows
- **244,613 rows** carry a non-canonical `tradeAgreements` encoding: 12,705
  distinct raw values collapse to 351 once sorted
- One organization appears under two names in **all 506,847 rows**

Official publication guarantees that what must be disclosed was disclosed. It
does not guarantee internal consistency. The Auditor General's 2025 Report 5
found gaps in exactly this.

## Why zero integrity findings is a result, not an accident

A rule reporting zero hits and a rule that is silently broken produce identical
artifacts. [`src/verify_rules.py`](src/verify_rules.py) removes that ambiguity:
**15 fault-injection scenarios, all passing**, each deliberately breaking one
control on a real 4,000-row slice and asserting that exactly that control fires.

All five `integrity` rules are proven to fire. Writing the suite also caught a
flaw in the suite itself: the "delete a row" case was never reaching the
row-count check, because the checksum fires first.

## Scope boundaries, and why they are where they are

**We detect that a supplier has two spellings. We do not decide they are the
same company.** Detection is measurement and needs no authority. Deciding legal
identity needs an authoritative registry and an accountable human. If we merge
and we are wrong, the information is destroyed and nobody downstream can tell;
if we flag, downstream still has the choice.

The dataset proves why: `Oxford Economics USA, Inc.` and `Oxford Economics` are
one company with a high string similarity. `Techstreet Solutions Ltd` and
`Allium US Holding LLC` are one contract whose supplier changed by corporate
succession, with almost no string similarity. Fuzzy matching gets the second
case backwards. Only corporate records settle it.

The same principle produces every other boundary: no row deletion, no spend
aggregation, no backfilling blank `amendmentType`, no automated remediation.

## Cross-source work: one assessment done, one declined

| Source | Result |
| --- | --- |
| Proactive Publication of Contracts over $10,000 | **Not usable.** Shares the population but not the identifier. Best match rate 8.7%. `reference_number` exists in both datasets with unrelated meanings and joins to 0 rows. See [docs/06](docs/06-cross-source-linkage-assessment.md). |
| CanadaBuys Award Notices | **Viable but not pursued.** Shares the CanadaBuys identifier space; 56.3% linkage on a single fiscal year sample. Out of scope per [docs/03](docs/03-official-field-semantics-research.md): different grain, different cutover date, different refresh cadence. |

A documented negative result is a deliverable. The proactive disclosure
assessment cost one afternoon and prevented building a reconciliation whose
conclusions would have been misleading.

## Layout

```
src/
  profile_contract_history.py   Layer 1: structure profiling and gate
  build_bronze.py               Layer 2: raw-faithful ingestion with lineage
  verify_bronze_values.py       Layer 2: cell-by-cell verification, 506,847 x 91
  build_quality_layers.py       Layers 3-4: typed Silver, 21 rules, routing
  verify_rules.py               Fault injection, 15 scenarios
  build_contract_history_db.py  Exploration convenience only, not a pipeline stage

docs/01  Source scope and data contract
docs/02  Querying the exploration database
docs/03  Official field semantics: confirmed vs inferred vs unresolved
docs/04  Field map and Bronze design
docs/05  Rule catalog, decision routing, fault injection, cleanliness evidence
docs/06  Cross-source linkage assessment (negative result)

data/raw/        Official CSVs, read-only, never written to
data/bronze/     batch_id=<id>/            immutable, all 91 fields as strings
data/derived/    batch_id=<id>/ruleset=<v>/ re-runnable per rule version
data/reference/  Official dictionaries and schemas
data/manifest/   Provenance: URL, checksum, HTTP metadata, row counts
artifacts/       Profiles, reconciliation reports, rule verification results
```

## Two batches, and what the second one revealed

Batch 002 is a genuinely new publisher snapshot taken a month after batch 001,
not a re-run. Every gate passed with no schema drift, and the five `integrity`
rules stayed at zero across different data. Then
[`src/compare_batches.py`](src/compare_batches.py) diffed Bronze against Bronze:

```
506,847 -> 507,703 rows
  added     857
  removed   1
  restated  13,218   (2.608% of common rows)

totalContractValue restated on 1,093 rows
  net change CAD 6,085,874,045.83   largest single CAD 623,529,056.19
```

Two monthly snapshots of the same official dataset disagree by six billion
dollars on contracts that were already published, and one published record
disappeared entirely. Restatement is reported, not judged: it may be a
correction. The point is that a consumer who computed a number from batch 001
cannot learn the input moved unless someone kept batch 001.

**This is what the immutable-batch rule was for.** Overwriting `data/bronze` on
each run would have made all 13,218 restatements invisible. Details in
[docs/08](docs/08-second-batch-and-cloud-run.md).

## It runs on AWS, with identical results

Packaged as a container and run on AWS Batch with Fargate: S3 in and out, ECR
image, CloudWatch logs, 2 attempts on failure. 124 seconds, about USD 0.004 per
run. Every number matched the local run exactly, which is the claim worth
making: not "it runs in the cloud" but "it produces the same answer there".

Batch rather than Kubernetes because this is a job that exits, not a service
that stays up. The agent layer is deliberately not in the image. All resources
were torn down after verification; evidence kept in
[`artifacts/aws/`](artifacts/aws). See [docs/08](docs/08-second-batch-and-cloud-run.md).

## Known gaps

- No orchestration. Nothing schedules the monthly run; the job is submitted by
  hand. EventBridge on a cron plus a failure alarm is the obvious next step.
- No CI. The fault-injection suite is not wired to anything that runs it.
- Findings still have no consumer beyond the person running the project.
- `verify_bronze_values.py` is a separate script, not part of the Bronze gate. A
  batch can pass while it has never been run, and nothing records which.
- A schema change surfaces as an uncaught `KeyError`, not a stated gate failure.
- `conflicting_award_date_within_contract` (516 contracts, 2,486 rows) and
  `restated_since_previous_batch` are both measured but neither is a rule inside
  the quality layer, so neither routes to a class.
- Reference-data joins are not built: GC organization codes would resolve the
  two-spellings-of-one-department problem, and the GSIN to UNSPSC mapping is
  required for any product category spanning 2009 to 2026, since `unspsc` is
  blank in 469,502 rows and `gsin` in 35,808.
