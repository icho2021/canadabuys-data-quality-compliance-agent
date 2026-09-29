# CanadaBuys Contract History Context

## Source scope

The MVP uses CanadaBuys Contract History only. The Contract History batch is a
monthly public-data snapshot covering contracts awarded by federal government
organizations. Award Notices are a related but separate dataset and are out of
scope for this MVP unless selected through a documented scope change.

## Canonical terms

### Contract history record

One published record about a contract's original award, later amendment, or
termination. It is not automatically one independent contract and it is not
automatically one independent monetary event.

### Contract version key candidate

`referenceNumber + contractNumber + amendmentNumber` is the current candidate
for identifying one published contract-history version. It was unique in the
2026-08 batch, but is not yet declared a permanent primary key across future
monthly snapshots.

### Original, amendment, and termination

`amendmentType` distinguishes an original contract from a later amendment or
termination where that field is populated. Older legacy records frequently lack
this field, so the pipeline must retain the source value and cannot infer it.

### Contract amount and total contract value

`contractAmount` is the published amount for the awarded contract, including
taxes, in the published currency. `totalContractValue` is the cumulative value
at the record's reporting point. Neither can be summed across every raw row
without a documented version-selection rule.

### Contracting entity and end user entity

The contracting entity procures the goods or services. The end user entity is
the government organization expected to use the procured goods or services.
They are different business roles and must not be collapsed by default.

### Data-quality finding

A reproducible, rule-based observation about a batch or record. A finding is
not a legal or compliance conclusion. A human reviewer decides whether any
follow-up action is appropriate.
