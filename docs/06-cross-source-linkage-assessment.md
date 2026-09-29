# Cross-Source Linkage Assessment: Contract History vs Proactive Publication

Assessment date: 2026-09-21
Question: can CanadaBuys Contract History be reconciled per contract against the
Treasury Board Proactive Publication of Contracts over $10,000?

**Answer: no. The two streams share a population but have no reliable join key.**
Best achieved match rate on the correctly scoped population is 8.7%. This
document records the evidence, because a documented negative result is the
deliverable here.

## 1. What was acquired

See [`data/manifest/batch_002_manifest.yaml`](../data/manifest/batch_002_manifest.yaml).

| Resource | Size | Rows | SHA-256 prefix |
| --- | --- | --- | --- |
| `contracts.csv` | 641,803,860 B | 1,313,634 | `e1cb4843` |
| `contracts.json` (schema) | 188,118 B | | `2027589a` |
| dictionary `.xlsx` | 69,247 B | | `effe9573` |

The portal also lists a "Legacy Data" CSV (`load-contracts-2020-10-01.csv`). Its
blob returns **404 The specified blob does not exist**. The portal advertises a
resource that cannot be retrieved.

## 2. The two streams do cover a shared population

The official dictionary defines `contracting_entity` as identifying who awarded
the procurement. The relevant codes:

| Code | Official definition | Rows |
| --- | --- | ---: |
| `PWC` | Contract or Call-up awarded by PSPC on behalf of the client department | 40,719 |
| `PWSOSA` | Call-up or Contract awarded by client department against a Standing Offer or Supply Arrangement established by PSPC | 235,589 |
| `DC` | (departmental contract) | 214,074 |
| `DSOSA` | Call-up or Contract against an agreement established by the department | 123,200 |
| `SSCC` | Contract awarded or Call-up by SSC on behalf of the client department | 11,775 |
| `SSCSOSA` | Against an agreement established by Shared Services Canada | 18,807 |
| `OGDC` | Contract awarded by another department on behalf of the client department | 8,609 |
| *(null)* | | 660,861 |

`PWC` is definitionally the same population as Contract History, whose scope is
contracts awarded by PWGSC/PSPC on behalf of federal departments. So the
question is well-posed: these rows *should* describe the same contracts.

This field was not visible in the published JSON schema summary. The schema
lists 24 fields; the actual CSV has **43 columns**, including
`contracting_entity`, `solicitation_procedure`, `instrument_type`,
`standing_offer_number`, `reporting_period`, `owner_org`, `number_of_bids`,
`award_criteria`, `indigenous_business` and `former_public_servant`. Field
inventories must be taken from the delivered file, not from the schema endpoint.

## 3. `reference_number` is a false friend

Both datasets have a field named `reference_number`, with unrelated meanings.

| Dataset | Field | Official definition |
| --- | --- | --- |
| Contract History | `referenceNumber-numeroReference` | Internal identifier that may specify a sourcing project, contract workspace and/or contractor |
| Proactive Publication | `reference_number` | "Unique identifier for each line item in the spreadsheet" |

Joining on the matching name produces **0 matches out of 92,128 attempted**
Contract History rows awarded 2019 or later. The join silently returns nothing
rather than erroring. Had the field been populated with overlapping values, it
would have silently returned garbage.

## 4. The real candidate key does not work either

`procurement_id` is defined as "Contract/commitment/requisition number" and is
the only plausible counterpart to Contract History's `contractNumber`. It is
fully populated from 2019 onward (0 blanks; 43 blanks across 2017-2018).

The formats are structurally different:

| Source | Example values |
| --- | --- |
| Contract History `contractNumber` | `EP922-191669/001/PWU`, `EQ734-131066/001/PWL`, `EZ897-161236/001/PWY` |
| Proactive `procurement_id` where `contracting_entity = PWC` | `4700014003`, `47419B1699`, `2023030975`, `4750436409` |

Contract History carries PWGSC's own contract file number. Proactive Publication
carries the **client department's** commitment or requisition number from its own
financial system. The dictionary wording "commitment/requisition" is the tell.

Match rates on the correctly scoped population (`PWC` rows with
`contract_date >= 2019-01-01`, n = 37,235):

| Method | Matched | Rate |
| --- | ---: | ---: |
| Exact string equality | 3,083 | 8.3% |
| Case-folded, non-alphanumerics stripped | 3,235 | 8.7% |

Normalization adds 152 matches. The residual 91% is not a formatting problem, it
is a different identifier. 8.7% cannot support any statement about agreement or
disagreement between the streams: the matched subset is not representative and
the unmatched majority cannot be attributed.

This is consistent with official supporting documentation, which already warns
that reference numbers are not always consistent between historical and new
files and recommends `solicitationNumber` for tender-to-award linkage.
`solicitation_procedure` exists in Proactive Publication but is a *method*
(competitive, sole-source), not a solicitation identifier, so it is not a key.

## 5. What this does establish

The inability to trace one contract across two official federal reporting
streams is itself the finding, and it is measured rather than asserted. It is
the concrete form of the concern raised in the Auditor General's 2025 Report 5
on Professional Services Contracts and in the Procurement Ombudsman's study on
the usefulness of available procurement information.

For this project it means:

- **No reconciliation layer will be built.** Building one would produce a match
  rate low enough that any conclusion drawn from it would be misleading.
- **Aggregate comparison remains available and needs no key.** Counting
  PWGSC-awarded contracts per year in Contract History against `PWC` rows per
  year in Proactive Publication compares two independent counts of one
  population. Divergence would be a governance finding. This is cheap and honest.
- **Proactive Publication is not promoted into the pipeline.** Manifest gate
  `linkage_to_contract_history: failed`, `promoted_to_pipeline: false`.

## 6. Quality defects observed in Proactive Publication

Not acted on, since the dataset is not promoted, but recorded:

- `contract_date` minimum is **1899-12-30**, which is day zero of the Excel 1900
  date system. These are empty cells that passed through a spreadsheet.
- `contract_date` maximum is **2026-12-01**, later than the assessment date.
- `contracting_entity` is null in **660,861 of 1,313,634 rows** (50.3%) despite
  the dictionary's own validation rule requiring it in stated cases.

## 7. Correction to an earlier conclusion in this project

An earlier note in this project stated that the proactive publication schema
contained no field identifying whether PSPC acted as contracting authority, and
that non-matches therefore could not be attributed. The first half was wrong:
`contracting_entity` provides exactly that scoping, and it was used to define
the population in section 4. The conclusion that non-matches cannot be
attributed still holds, but for a different reason: the identifier differs, not
the scope.
