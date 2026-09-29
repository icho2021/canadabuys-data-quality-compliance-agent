#!/usr/bin/env python3
"""Submit a Batch job, wait for it, and stream its CloudWatch log.

Exits with the job's own outcome so a caller (a scheduler, a CI step, a human)
sees a real pass or fail rather than "submitted successfully".
"""

from __future__ import annotations

import os
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
QUEUE = os.environ.get("JOB_QUEUE", "canadabuys-dq-queue")
JOB_DEF = os.environ.get("JOB_DEFINITION", "canadabuys-dq-job")
JOB_NAME = os.environ.get("JOB_NAME", "canadabuys-dq-run")
BUCKET = os.environ["S3_BUCKET"]
BATCH_ID = os.environ["BATCH_ID"]
AS_OF_DATE = os.environ["AS_OF_DATE"]
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "input")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "output")
VERIFY_CELLS = os.environ.get("VERIFY_CELLS", "1")

batch = boto3.client("batch", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


def main() -> int:
    # Watching an already-submitted job matters more than it looks: a crashed
    # watcher must never be mistaken for a crashed job, and must never cause a
    # duplicate submission.
    job_id = os.environ.get("JOB_ID")
    if job_id:
        print(f"attaching to existing job {job_id}", flush=True)
    else:
        job = batch.submit_job(
            jobName=JOB_NAME,
            jobQueue=QUEUE,
            jobDefinition=JOB_DEF,
            containerOverrides={
                "environment": [
                    {"name": "S3_BUCKET", "value": BUCKET},
                    {"name": "BATCH_ID", "value": BATCH_ID},
                    {"name": "AS_OF_DATE", "value": AS_OF_DATE},
                    {"name": "INPUT_PREFIX", "value": INPUT_PREFIX},
                    {"name": "OUTPUT_PREFIX", "value": OUTPUT_PREFIX},
                    {"name": "VERIFY_CELLS", "value": VERIFY_CELLS},
                ]
            },
        )
        job_id = job["jobId"]
        print(f"submitted {job_id}  queue={QUEUE}  batch={BATCH_ID}", flush=True)

    seen, stream, last_status = set(), None, None
    while True:
        detail = batch.describe_jobs(jobs=[job_id])["jobs"][0]
        status = detail["status"]
        if status != last_status:
            reason = detail.get("statusReason", "")
            print(f"  [{status}] {reason}".rstrip(), flush=True)
            last_status = status
        stream = stream or detail.get("container", {}).get("logStreamName")
        if stream:
            # CloudWatch Logs returns transient ServiceUnavailable and throttling
            # errors. The watcher must survive them: losing the log tail is a
            # cosmetic problem, but crashing here would report a healthy job as
            # a failure.
            try:
                for event in logs.get_log_events(
                    logGroupName="/aws/batch/canadabuys-dq",
                    logStreamName=stream, startFromHead=True,
                )["events"]:
                    key = (event["timestamp"], event["message"])
                    if key not in seen:
                        seen.add(key)
                        print(f"    {event['message']}", flush=True)
            except ClientError as error:
                code = error.response["Error"]["Code"]
                if code not in ("ResourceNotFoundException", "ServiceUnavailableException",
                                "ThrottlingException"):
                    raise
                print(f"    (log fetch {code}, retrying)", flush=True)
            except BotoCoreError as error:
                # ConnectionClosedError and friends are not ClientError. Both
                # of this watcher's crashes were here, and both times the job
                # itself had already succeeded.
                print(f"    (log fetch {type(error).__name__}, retrying)", flush=True)
        if status in ("SUCCEEDED", "FAILED"):
            exit_code = detail.get("container", {}).get("exitCode")
            print(f"\n{status}  exitCode={exit_code}", flush=True)
            return 0 if status == "SUCCEEDED" else 1
        time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
