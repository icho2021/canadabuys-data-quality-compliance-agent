# Stage 0 Source and Data Contract

## Decision

This first-stage project uses the CanadaBuys Contract History dataset only. It
does not use the CanadaBuys Award Notices CSV currently present in the project
directory. The two inputs below are official files from the same business
domain across two system periods, not two independent datasets used to
cross-check one another's business facts.

The batch contains two official consolidated source files:

| Source file | Initial-award coverage | Source rows | Role |
| --- | --- | ---: | --- |
| Legacy contract history | 2009-01 through 2023-05 | 485,573 | Pre-CanadaBuys system-of-record history |
| All CanadaBuys contract history | 2023-06-01 onward | 21,274 | CanadaBuys system-of-record history |

The two files have the same 91 physical CSV columns. Thirty-three English and
French field pairs represent the same business concept, leaving 58 logical
field groups to understand and model.

## Data grain

The raw grain is a published Contract History record. A record can describe an
original contract, a later amendment, or a termination. It must not be treated
as one independent contract or as one additive spend event.

The observed candidate version key is:

`referenceNumber-numeroReference + contractNumber-numeroContrat + amendmentNumber-numeroModification`

This candidate was unique within each source file and across the two files in
the current batch. It remains a testable candidate, not a permanent global
primary-key claim.

## Dictionary map

| Field family | Contract History fields | What the data represents | MVP handling |
| --- | --- | --- | --- |
| Identity and version | title, referenceNumber, amendmentNumber, procurementNumber, solicitationNumber, contractNumber, numberOfRecords | Published identifiers and the contract-history version sequence. `numberOfRecords` counts records for the contract history rather than a monetary quantity. | Preserve as text. Test the candidate version key before any model uses it. |
| Dates | publicationDate, contractAwardDate, amendmentDate, contractStartDate, contractEndDate | Publication, initial award, amendment, effective-start, and effective-end dates. | Parse to dates while retaining the raw strings. Test missingness, parseability, and documented sequencing. |
| Amounts and state | contractAmount, totalContractValue, contractCurrency, contractStatus, instrumentType, amendmentType | Published contract amounts, cumulative value, currency, status, contract instrument, and whether the record is an original, amendment, or termination. | Preserve source amounts. Do not create spend totals until a version-selection rule is approved. |
| Procurement classification | gsin, unspsc, procurementCategory, noticeType, procurementMethod, selectionCriteria, limitedTenderingReason, tradeAgreements, regionsOfDelivery | What was procured, procurement category, sourcing route, bid-selection basis, limited-tender rationale, applicable trade agreements, and delivery region. | Use for later dimensions and quality checks. Multi-valued text fields require a documented bridge-table design. |
| Supplier | supplierLegalName, supplierStandardizedName, supplierOperatingName, supplierEmployeeCount, supplierAddress fields | The legal, standardized, and operating supplier names plus organization and address attributes. | Keep raw and standardized values separately. Measure normalization gaps instead of overwriting names. |
| Government roles | contractingEntity fields, endUserEntities fields | The government buyer and the government organization expected to use the goods or services. | Model as distinct roles. Do not assume the buyer and end user are the same. |
| Contact and narrative | contactInfo fields, percentageOfGoodsByCountry, tenderDescription | Contact details, country-of-origin allocation text, and tender narrative. | Retain for lineage and future investigations. Do not use contact details in public outputs. |

Many text fields have an English and French counterpart. They are not two
separate business facts. Analytical models should choose a display language or
retain both with an explicit language policy.

## Baseline discovery results

| Check | Legacy | Current | Interpretation |
| --- | ---: | ---: | --- |
| Physical columns | 91 | 91 | Header order is identical. |
| Initial-award date range | 2009-01-01 to 2023-05-31 | 2023-06-01 to 2026-07-27 | The observed split matches the official cutover. |
| Candidate version-key duplicates | 0 | 0 | Promising, but must be rerun for each new batch. |
| `amendmentType` blank | 469,520 | 1,808 | Legacy semantics cannot rely on this field alone. |
| `contractAmount` blank | 1,486 | 3,267 | Missingness must be a finding, not converted to zero. |
| `contractAmount` negative | 19,332 | 618 | Negative amounts need investigation before financial aggregation. |
| `contractAmount` zero | 172,968 | 5,430 | Zero is a real reported value and must not be treated as missing. |

The official supporting documentation records a 2026 CSV restructuring that
corrected amendment-date, original-amount, and administrative-duplicate issues.
The source schema and interpretation must therefore be versioned by batch.

## First MVP rules

1. Schema: both sources must have the exact expected 91-column header.
2. Cutover: legacy initial award dates cannot be after 2023-05-31; current
   initial award dates cannot be before 2023-06-01 when populated.
3. Uniqueness: the candidate version key must not be duplicated within the
   batch.
4. Completeness: missing reference number, contract number, amendment number,
   or initial-award date becomes a review finding rather than a rejected record.
5. Validity: unparseable amounts or dates are findings. Negative and zero
   amounts are separately counted for review; they are not silently changed.
   Negative amendments may reflect decreases, so non-negativity is not a
   universal Contract History rule.
6. Consistency: date ordering and amendment semantics are evaluated only where
   the relevant fields are populated.

## Layer boundary

Stage 0 is read-only source discovery and field profiling. Stage 1 creates a
raw-faithful Bronze representation with immutable source lineage; all 91
source columns remain strings, including blanks and negative signs. Typed
analytical columns belong in Silver. This stage does not decide a contract
snapshot rule, calculate total spend, modify source records, or use an LLM.
