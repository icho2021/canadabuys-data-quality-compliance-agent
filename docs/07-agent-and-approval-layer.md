# Agent and Approval Layer

Built: 2026-09-21. Models: `nomic-embed-text` (768-dim) and `llama3`, both local
via Ollama. No cloud inference, no API keys, no data leaves the machine.

## 1. The architectural constraint that everything else follows from

**The language model is never in the decision path.**

Whether a row is a finding is decided by deterministic SQL in
[`build_quality_layers.py`](../src/build_quality_layers.py). That decision is
reproducible, version-pinned to a ruleset, anchored to an `as_of_date`, and
covered by 15 fault-injection scenarios. Putting a model there would destroy
every one of those properties.

The agent operates strictly downstream of a finding that already exists:

```
deterministic SQL  →  finding
                        ↓
                   retrieve   hybrid search over official documentation
                        ↓
                   assess     local model reads ONLY retrieved passages
                        ↓
                   propose    draft + cited chunk ids, status 'pending'
                        ↓
                   human      approves or rejects
                        ↓
                   apply      refuses unless approved AND a deterministic
                              transform exists
```

Section 4 shows why this is not theoretical caution.

## 2. Retrieval: measured, not assumed

Corpus: 123 chunks, 768-dim embeddings, built in 7.8 seconds from four official
documents already recorded in the manifests.

| Document | Chunks |
| --- | ---: |
| `achatscanada-canadabuys-dd.xml` | 67 |
| `proactive_contracts_dictionary.xlsx` | 43 |
| `canadabuys_data_dictionary.pdf` | 10 |
| `COsoutien-CHsupport-eng.html` | 3 |

**No vector database.** 123 chunks fit in a Parquet file and DuckDB's
`array_cosine_similarity` scores all of them in milliseconds. An external vector
service would add operational surface for no measurable gain at this scale.

`--evaluate` scores three retrieval modes against two query shapes:

| Query shape | exact | embedding | hybrid |
| --- | ---: | ---: | ---: |
| Identifier (`contractAwardDate-dateAttributionContrat`) | **21/21** | 0/21 | **21/21** |
| Natural language ("can a contract have a negative value?") | 0/8 | **7/8** | **7/8** |

Each mode fails completely on the other's query shape, so hybrid is justified by
measurement rather than by convention.

### 2.1 The first evaluation was wrong, and the correction is the point

The initial run reported embeddings at 0/21 and the conclusion drawn was
"embeddings do not work on this corpus". That was the evaluation being wrong,
not the embeddings. Every query in that suite contained the exact field
identifier, which is the case where lexical matching wins by construction.

Checked directly, embeddings behave correctly: the query *"What is the total
cumulative value of a contract including all amendments?"* returns
`totalContractValue-valeurTotaleContrat` at rank 1 with similarity 0.811. What
fails is querying with a camelCase bilingual identifier, because all such
identifiers embed into nearly the same region of the space.

The natural-language suite was added so the embedding half is measured on
queries it could plausibly win. Measuring only the flattering query shape would
have justified deleting a component that does real work.

## 3. What the official XML gave us that a summary could not

The dictionary publishes a `data_pattern` regex per field: a machine-readable
validation rule, not prose. 20 fields carry one; 5 encode a controlled-value
list.

```
amendmentType   ^$|^Original|Amendment|Administrative amendment|Termination|Modification|...
awardStatus     ^$|^Active|Cancelled|Expired|Actif|Annulé|Expiré$
contractAmount  ^$|^-?[0-9]*(\.[0-9]{2})?$
contractCurrency ^$|^[A-Z]{0,4}$
numberOfRecords ^[1-9]\d*$
```

Two consequences for existing rules:

- `contractAmount` **officially permits a leading minus sign**. This is the
  primary-source authority for negative amounts being schema-valid, replacing a
  paraphrase from a web summary.
- `contractCurrency` officially permits any 0-4 uppercase letters. Our
  `unexpected_currency` rule, which requires CAD, is therefore *stricter* than
  the published standard. It is correctly labelled
  `observed_single_value_in_batch_001`, a project rule, not an official one.
- `numberOfRecords` has **no empty alternative**, so blank is not permitted.
  Not yet checked against the data.

Acquiring the XML also closed the manifest's long-standing
`pending_tls_verified_download` status. The blocker was real: `curl` on this
machine rejects the host with "self signed certificate in certificate chain"
because its CA bundle lacks Sectigo Public Server Authentication Root R46.
`openssl s_client` verifies the same chain cleanly. Python `requests` with the
`certifi` bundle downloads it with verification fully enabled. The file was
never fetched with verification disabled.

## 4. An observed hallucination, and why it did no damage

Assessing `negative_total_contract_value`, `llama3` answered:

> CONTRADICTS: Yes. The data contradicts the published definition
> "https://schema.org/Number" **which allows only positive numbers or zero.**

This is fabricated. The chunk it was given, `c0063`, reads in full:

```
Field: totalContractValue-valeurTotaleContrat
Official definition: The cumulative total value of a contract, from contract
  award date to present (original plus all amendments), and includes values
  that are pre-January 2009 ...
Data type: https://schema.org/Number
Published validation pattern: ^$|^-?[0-9]*(\.[0-9]{2})?$
```

`schema.org/Number` places no constraint on sign, and the very next line of the
same passage contains `-?`, which explicitly permits a minus sign. The model
invented a constraint that the text it was handed directly refutes.

**Nothing downstream was affected.** The output was a draft with status
`pending`; `apply_remediation.py` will not act on it, and the cited chunk id
let the claim be checked against source text in seconds. Had the model been
permitted to decide findings, this would have entered the pipeline as a
confidently wrong authority citation.

This is the concrete justification for section 1.

## 5. Citation grounding verification

The hallucination in section 4 is now caught mechanically. The prompt requires a
**verbatim** quote of the governing text; `verify_grounding()` then checks that
the quote actually occurs in the retrieved passages, comparing on collapsed
whitespace and case. A quote that cannot be located is rejected rather than
shown to a reviewer as though it were sourced.

Result over 16 rules: **14 grounded, 2 rejected.**

The clearest catch is `future_amendment_date`. Instructed to copy the pattern
exactly, the model emitted:

```
^^[0-9]{4}-[0-9]{2}-[0-9]{2}$      model output, doubled caret
 ^$|^[0-9]{4}-[0-9]{2}-[0-9]{2}$   actual published pattern
```

It corrupted the string it was told to copy. The checker located nothing and
rejected the assessment. This is a deterministic guardrail on a
non-deterministic component, which is the same principle as keeping the model
out of the decision path, applied one level down.

## 6. Two prompt designs, two degenerate verdicts

The model's *judgement* fields could not be made useful, and this was measured
rather than assumed.

| Field | Prompt v1 | Prompt v2 (constrained enum) |
| --- | --- | --- |
| Verdict | "Partially" for **16 of 16** | "CONTRADICTED" for **16 of 16** |
| Question for steward | one genuinely good question | "none needed" for **16 of 16** |

Prompt v2's verdict is not merely uninformative, it is wrong.
`zero_contract_amount` is an `observe`-class rule because the official
documentation permits zero values for standing offers, and the retrieved pattern
`^$|^-?[0-9]*(\.[0-9]{2})?$` matches `0.00` exactly. The model still answered
CONTRADICTED.

Constraining the output to an enum did not improve the verdict and actively
degraded the questions. v1 produced, for negative totals: *"Can you confirm if
these negative values are errors in the data or intentional representations of
contract values that preceded January 2009?"*, which draws on the pre-2009
clause in the official definition and is worth sending to a steward. v2 produced
"none needed" everywhere.

### 6.1 The resulting design decision

**Acceptance depends on grounding alone. The verdict is recorded but is not a
gate.**

The deliverable of this layer is a *verified verbatim citation of the official
text governing each finding*, attached to the finding. That is genuinely useful
and it is now mechanically trustworthy. The model's opinion about that text is
advisory, and with `llama3` at this prompt it carries no information.

Calling this an agent remains fair: it is a multi-step retrieve, generate,
verify, reject loop, not a single completion. What it is not is a judge.

Open: re-measure against a stronger model to separate model capability from
prompt design. The architecture does not change either way, which is the point.

## 7. The approval layer

126,708 rows need attention. Nobody approves 126,708 edits. A person approves a
*standardisation rule*, so findings are collapsed into 7 proposals.

| Proposal | Type | Rows |
| --- | --- | ---: |
| `normalize_trade_agreements_ordering` | **normalize** | 244,613 |
| `normalize_supplier_country_encoding` | **normalize** | 37,340 |
| `annotate_supplier_identity_unresolved` | annotate | 31,402 |
| `annotate_negative_amount_unknown_activity` | annotate | 19,105 |
| `annotate_postal_code_format` | annotate | 826 |
| `annotate_date_ordering_contradictions` | annotate | 368 |
| `annotate_negative_total_contract_value` | annotate | 252 |

`normalize` means a deterministic, reversible transform exists. `annotate` means
one does not, and no approval can create one.

### 7.1 Four refusals, each tested

1. Status not `approved` → skipped.
2. `annotate` proposal, even when approved → refused.
   ```
   REFUSED  annotate_negative_total_contract_value: approved, but proposal_type
   is 'annotate'. No deterministic transform exists; approval cannot create one.
   ```
3. Approved without `decided_by` / `decided_at_utc` → aborts.
   ```
   ValueError: Proposal annotate_postal_code_format is approved but missing
   decided_by, decided_at_utc. An unattributed approval is refused.
   ```
4. Bronze and `data/raw/` are never opened for writing.

### 7.2 Result

```
Rows 506,847 in / 506,847 out (unchanged)
  normalize_trade_agreements_ordering    244,613 cells
  normalize_supplier_country_encoding     37,270 cells
Total cells changed: 281,883
```

| Field | Distinct before | After |
| --- | ---: | ---: |
| `tradeAgreements` | 12,706 | **352** |
| `supplier_country` | 151 | **133** |

37,340 rows were flagged but only 37,270 cells changed: 70 rows carry an ISO-2
code absent from the reference map. The gap is deliberately visible in the audit
rather than silently skipped.

Every changed cell is in `remediation_audit.parquet` with its before value,
after value, proposal id, and approver. Reversal is replaying the log backwards.
