#!/usr/bin/env python3
"""Create the AWS Batch resources for the pipeline, idempotently.

Run inside the built image, because the AWS CLI on the development machine is
2.0.30 and predates Batch Fargate support: it rejects a valid Fargate compute
environment during client-side validation. The image already carries a current
boto3, so it is the shortest path that does not require installing anything.

Fargate rather than EC2: the job runs for minutes once a month. Paying for a
managed instance pool that sits idle, or waiting for one to scale from zero,
buys nothing here. Fargate also removes the instance role and AMI patching that
EC2 compute environments need, which matters in a lab account with restricted
IAM.
"""

from __future__ import annotations

import os
import sys
import time

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
ACCOUNT = os.environ["ACCOUNT_ID"]
SUBNETS = os.environ["SUBNETS"].split(",")
SECURITY_GROUP = os.environ["SECURITY_GROUP"]
BUCKET = os.environ["S3_BUCKET"]
IMAGE_TAG = os.environ.get("IMAGE_TAG", "v1")

COMPUTE_ENV = "canadabuys-dq-fargate"
QUEUE = "canadabuys-dq-queue"
JOB_DEF = "canadabuys-dq-job"
LOG_GROUP = "/aws/batch/canadabuys-dq"
# Learner Lab supplies one pre-made role. It covers both the execution role
# (pull from ECR, write logs) and the job role (read and write S3).
LAB_ROLE = f"arn:aws:iam::{ACCOUNT}:role/LabRole"
IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/canadabuys-dq:{IMAGE_TAG}"

batch = boto3.client("batch", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


def wait_valid(describe, name: str, key: str) -> None:
    for _ in range(60):
        items = describe()[key]
        if not items:
            time.sleep(5)
            continue
        status = items[0]["status"]
        print(f"  {name}: {status}", flush=True)
        if status == "VALID":
            return
        if status == "INVALID":
            raise SystemExit(f"{name} is INVALID: {items[0].get('statusReason')}")
        time.sleep(5)
    raise SystemExit(f"{name} did not reach VALID")


def main() -> None:
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        print(f"log group {LOG_GROUP} created")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"log group {LOG_GROUP} exists")

    existing = batch.describe_compute_environments(computeEnvironments=[COMPUTE_ENV])
    if not existing["computeEnvironments"]:
        batch.create_compute_environment(
            computeEnvironmentName=COMPUTE_ENV,
            type="MANAGED",
            state="ENABLED",
            computeResources={
                "type": "FARGATE",
                "maxvCpus": 4,
                "subnets": SUBNETS,
                "securityGroupIds": [SECURITY_GROUP],
            },
            serviceRole=f"arn:aws:iam::{ACCOUNT}:role/LabRole",
        )
        print(f"compute environment {COMPUTE_ENV} creating")
    else:
        print(f"compute environment {COMPUTE_ENV} exists")
    wait_valid(
        lambda: batch.describe_compute_environments(computeEnvironments=[COMPUTE_ENV]),
        COMPUTE_ENV, "computeEnvironments",
    )

    if not batch.describe_job_queues(jobQueues=[QUEUE])["jobQueues"]:
        batch.create_job_queue(
            jobQueueName=QUEUE, state="ENABLED", priority=1,
            computeEnvironmentOrder=[{"order": 1, "computeEnvironment": COMPUTE_ENV}],
        )
        print(f"job queue {QUEUE} creating")
    else:
        print(f"job queue {QUEUE} exists")
    wait_valid(lambda: batch.describe_job_queues(jobQueues=[QUEUE]), QUEUE, "jobQueues")

    response = batch.register_job_definition(
        jobDefinitionName=JOB_DEF,
        type="container",
        platformCapabilities=["FARGATE"],
        # Two attempts: a transient ECR pull or S3 blip should not need a human.
        # A gate failure is deterministic and will simply fail twice, which is
        # the correct outcome; retries must never turn a real failure into a pass.
        retryStrategy={"attempts": 2},
        timeout={"attemptDurationSeconds": 3600},
        containerProperties={
            "image": IMAGE,
            "executionRoleArn": LAB_ROLE,
            "jobRoleArn": LAB_ROLE,
            "resourceRequirements": [
                {"type": "VCPU", "value": "2"},
                {"type": "MEMORY", "value": "8192"},
            ],
            # The legacy CSV alone is 832 MB and Bronze writes Parquet beside it.
            "ephemeralStorage": {"sizeInGiB": 40},
            "networkConfiguration": {"assignPublicIp": "ENABLED"},
            "environment": [
                {"name": "S3_BUCKET", "value": BUCKET},
                {"name": "AWS_DEFAULT_REGION", "value": REGION},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": LOG_GROUP,
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": "job",
                },
            },
        },
    )
    print(f"job definition {JOB_DEF} revision {response['revision']}")
    print("\nREADY")


if __name__ == "__main__":
    sys.exit(main())
