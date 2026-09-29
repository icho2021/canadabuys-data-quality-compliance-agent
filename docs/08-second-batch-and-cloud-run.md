# Second Batch and Cloud Execution

Date: 2026-09-22. Two gaps closed: the pipeline had only ever seen one snapshot,
and it had only ever run on a laptop.

---

## Part 1: AWS Batch execution

### 1.1 What was built, and what was deliberately left out

| Resource | Identifier |
| --- | --- |
| S3 bucket | `canadabuys-dq-<ACCOUNT_ID>`, public access blocked |
| ECR image | `canadabuys-dq:v1`, 125 MB, linux/amd64 |
| Batch compute environment | `canadabuys-dq-fargate`, maxvCpus 4, 3 AZs |
| Job queue | `canadabuys-dq-queue` |
| Job definition | `canadabuys-dq-job:1`, 2 vCPU / 8 GB / 40 GiB ephemeral, 2 attempts |
| Log group | `/aws/batch/canadabuys-dq` |

Code in [`deploy/`](../deploy): `Dockerfile`, `entrypoint.py` (S3 in and out),
`setup_batch.py` (create resources), `run_job.py` (submit and follow).

**Fargate, not EC2.** The job runs for two minutes once a month. A managed
instance pool would either sit idle or cold-start, and it needs an instance role
and AMI patching that a restricted lab account makes awkward.

**Batch, not Kubernetes.** This is a scheduled job that exits, not a service
that stays up. Kubernetes would mean paying for a control plane and nodes
around the clock to run something for two minutes a month.

**The agent layer is not in the image.** It is 21 model calls a reviewer
triggers, not a 506,847-row batch workload. Shipping 5 GB of model weights to
Fargate to run it would be the wrong trade. See [docs/07](07-agent-and-approval-layer.md) section 1.

**The pipeline scripts were not modified to know about S3.** `entrypoint.py`
materialises the directory layout they already expect under `/work`, runs them
unchanged, and uploads what they produce. Teaching them to read `s3://` URLs
would have put network IO inside the code path that
[`verify_bronze_values.py`](../src/verify_bronze_values.py) is supposed to be
proving.

### 1.2 The result that matters: identical output, two environments

```
verify_bronze_cells: legacy PASS 485,573 × 91 / current PASS 21,274 × 91
Silver 506,847 rows; 21 rules evaluated
  integrity 0 / review 135,380 / normalization 282,779 / observe 650,571
Rows needing human review: 126,708
trade thresholds: 32,141 -> 354
RESULT: all gates passed
```

Every number matches the local run exactly. Not "it runs in the cloud" but
"it produces the same answer in the cloud", which is what `--as-of-date` and
`ruleset=v2` were for. 124 seconds, 2 vCPU / 8 GB, one attempt, exit 0,
about USD 0.004 per run.

Evidence retained at [`artifacts/aws/batch_run_evidence.json`](../artifacts/aws/batch_run_evidence.json)
(both jobs, full CloudWatch logs) and [`artifacts/aws/s3_output/`](../artifacts/aws/s3_output).

### 1.3 Three failures worth keeping

**Version drift between laptop and container.** The image pinned `duckdb==1.1.3`
and the Bronze stage died: `read_csv(strict_mode=...)` does not exist in that
release. The laptop had 1.5.5. Caught by the smoke slice, which is what the
smoke slice is for. Pinning a version is not the same as pinning the *right*
version.

**The AWS CLI on the machine is 2.0.30, from 2020.** It predates Batch Fargate
support and rejected a valid compute environment during client-side validation,
demanding `instanceTypes` and `instanceRole`, which are EC2-mode parameters.
Worked around by running `setup_batch.py` inside the image, which already
carries a current boto3. No local installation was needed.

**The watcher crashed twice; the job succeeded both times.** First on
`ServiceUnavailableException` from CloudWatch Logs, then on
`ConnectionClosedError`, which is not a `ClientError` subclass and so was not
caught by the first fix. Both times the traceback looked like a failed job.

That third one is the one to remember: **monitoring failure misread as job
failure.** Acting on the traceback would have meant resubmitting a job that had
already succeeded. `run_job.py` now catches both exception families and accepts
a `JOB_ID` to attach to an existing job rather than submitting a duplicate.

### 1.4 Teardown

All created resources deleted and independently verified empty: compute
environment, job queue, job definition, ECR repository, S3 bucket, log group.
Also checked for the costs that hide: no running EC2, no EBS volumes, no
Elastic IPs, no NAT gateways. Outputs and logs were pulled down before deletion.

---

## Part 2: The second batch

A month had passed since batch 001 was captured, so a genuinely new publisher
snapshot existed. This is a real second batch, not a re-run of the same file.

| | batch 001 (2026-07-28) | batch 002 (2026-08-28) |
| --- | --- | --- |
| legacy | 831,970,335 B, `8220082…` | 832,626,178 B, `55b8383…` |
| current | 55,853,902 B, `14792cd…` | 57,669,819 B, `1d6c172…` |
| rows | 506,847 | 507,703 |

The `legacy` file covers 2009 to 2023-05, which is closed history. It grew.

Acquiring it also surfaced a small operational fact: `canadabuys.canada.ca`
returns **403** to Python `requests` but 200 to `curl`. The download uses curl;
no user agent was spoofed and TLS verification stayed on.

### 2.1 Every gate passed, with no schema drift

```
profile : gate passed, quarantine None, 91 columns both sides,
          cutover boundaries respected, 0 cross-source key duplicates
bronze  : gate passed, 507,703 rows
verify  : legacy PASS 485,813 × 91 / current PASS 21,890 × 91
quality : integrity 0 / review 136,145 / normalization 284,137 / observe 650,898
          Rows needing human review: 127,438
```

The design was built for change and change arrived; nothing had to be edited to
absorb it.

### 2.2 What the publisher actually changed

[`src/compare_batches.py`](../src/compare_batches.py) diffs Bronze against
Bronze on the candidate version key.

```
506,847 -> 507,703 rows
  added     857      (615 current, 242 legacy)
  removed   1
  restated  13,218   (2.608% of common rows)
```

**13,218 already-published records came back with different content.**

| Field | Rows changed |
| --- | ---: |
| `tradeAgreements` (eng) | 10,832 |
| `tradeAgreements` (fra) | 10,832 |
| `numberOfRecords` | 2,053 |
| **`totalContractValue`** | **1,093** |
| `endUserEntities` name / address | ~235 |
| `publicationDate` | 112 |

### 2.3 The headline: CAD 6.09 billion in restated contract values

```
totalContractValue restated on 1,093 rows
  967 increased, 126 decreased
  net change            CAD 6,085,874,045.83
  largest single change CAD   623,529,056.19
```

Examples:

| referenceNumber | amendment | before | after |
| --- | --- | ---: | ---: |
| `CW2351815` | 001 | 145,078,030.67 | 768,607,086.86 |
| `24062-190560/002/XE` | 000-003 | 350,626,753.13 | 501,521,328.58 |

Two monthly snapshots of the same official dataset disagree by six billion
dollars on contracts that were already published.

This is reported, not judged. A restatement may be a correction. The point is
that a consumer who computed a number from batch 001 has no way to learn the
input moved, unless someone kept batch 001 and compared.

**This is the payoff of the immutable-batch rule.** Had the pipeline overwritten
`data/bronze` on each run, all 13,218 restatements and the entire six billion
would be invisible, and the dataset would appear to have always said what it
says today. That rule stopped being theoretical here.

### 2.4 One record was removed from the public record

| field | value |
| --- | --- |
| referenceNumber | `EP745-230795/002/ZF` |
| amendment | 004 |
| award date | 2022-09-12 |
| contract amount | 0.00 |
| supplier | Acolad Canada Inc. |

Present in batch 001, absent in batch 002. A published contract-history record
that is no longer published. Routed to `review`: this is a question for the
publisher, not a conclusion.

### 2.5 Rule deltas, and what they confirm

| Rule | class | 001 | 002 | delta |
| --- | --- | ---: | ---: | ---: |
| `non_canonical_supplier_country` | normalization | 37,340 | 38,208 | +868 |
| `missing_standardized_supplier_name` | review | 31,402 | 32,112 | +710 |
| `non_canonical_trade_agreements` | normalization | 244,613 | 245,103 | +490 |
| `zero_contract_amount` | observe | 178,398 | 178,696 | +298 |
| `contract_amount_exceeds_total_value` | review | 14,686 | 14,737 | +51 |
| `negative_amount_on_known_amendment` | observe | 845 | 874 | +29 |
| `future_amendment_date` | review | 7 | 11 | +4 |
| all 5 `integrity` rules | integrity | 0 | 0 | **0** |
| `missing_award_date` | review | 69,345 | 69,345 | 0 |
| `unknown_amendment_type` | observe | 471,328 | 471,328 | 0 |
| `amendment_date_before_award_date` | review | 368 | 368 | 0 |
| `negative_total_contract_value` | review | 252 | 252 | 0 |

Two things worth noting. The integrity rules stayed at zero across a genuinely
different snapshot, which is a stronger statement than zero on one batch. And
the unchanged counts are informative: the 857 new rows introduced no new missing
award dates, no new date-ordering contradictions, and no new negative cumulative
values. The churn is concentrated in normalization and supplier identity, which
is where it already was.

---

## What this adds to the known-gaps list

Closed:

- ~~only ever run on one batch~~ two batches, with a drift report
- ~~no cloud deployment~~ ran on AWS Batch with identical results

Still open:

- No orchestration. Nothing schedules the monthly run; `run_job.py` is invoked
  by hand. EventBridge on a cron plus a failure alarm is the obvious next step.
- No CI. The fault-injection suite is not wired to anything.
- Findings still have no real consumer beyond the reviewer running the project.
- `verify_bronze_values.py` remains outside the Bronze gate.
- `restated_since_previous_batch` is measured by `compare_batches.py` but is not
  yet a rule inside the quality layer, so it does not route to a class.
