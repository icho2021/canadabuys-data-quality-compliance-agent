#!/usr/bin/env python3
"""Container entrypoint: fetch inputs from S3, run the pipeline, push results back.

The pipeline scripts are deliberately not taught about S3. This wrapper
materialises the directory layout they already expect under /work and runs them
unchanged, so the cell-by-cell verification still runs against a plain local
filesystem.

Exits non-zero on a gate failure, uploading partial output under FAILED/ so the
failure stays inspectable instead of being lost.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

WORK = Path("/work")
SRC = Path("/app/src")
BUCKET = os.environ["S3_BUCKET"]
BATCH_ID = os.environ.get("BATCH_ID", "canadabuys_contract_history_20260822_001")
AS_OF_DATE = os.environ.get("AS_OF_DATE", "2026-08-22")
INPUT_PREFIX = os.environ.get("INPUT_PREFIX", "input")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "output")
# Skip the full cell-by-cell verification only if explicitly asked. Default on:
# it is the control the provenance claim depends on.
VERIFY_CELLS = os.environ.get("VERIFY_CELLS", "1") == "1"

s3 = boto3.client("s3")


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def download_prefix(prefix: str, destination: Path) -> int:
    """Mirror s3://BUCKET/prefix/... into destination, preserving relative paths."""
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            target = destination / key[len(prefix):].lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, key, str(target))
            log(f"  in  {key}  ({obj['Size']:,} B)")
            count += 1
    return count


def upload_tree(source: Path, prefix: str) -> int:
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_file():
            key = f"{prefix}/{path.relative_to(source)}"
            s3.upload_file(str(path), BUCKET, key)
            log(f"  out {key}  ({path.stat().st_size:,} B)")
            count += 1
    return count


def stage(name: str, args: list[str]) -> None:
    log(f"STAGE {name}")
    started = time.time()
    result = subprocess.run(
        [sys.executable, str(SRC / args[0]), *args[1:]],
        capture_output=True, text=True, cwd=WORK,
    )
    for line in result.stdout.splitlines():
        print(f"    {line}", flush=True)
    if result.returncode != 0:
        log(f"STAGE {name} FAILED (exit {result.returncode})")
        for line in result.stderr.strip().splitlines()[-15:]:
            print(f"    ! {line}", flush=True)
        raise SystemExit(f"stage_failed:{name}")
    log(f"STAGE {name} ok ({time.time() - started:.1f}s)")


def main() -> None:
    log(f"bucket={BUCKET} batch={BATCH_ID} as_of={AS_OF_DATE} verify_cells={VERIFY_CELLS}")
    WORK.mkdir(parents=True, exist_ok=True)

    log("Fetching inputs")
    if download_prefix(INPUT_PREFIX, WORK) == 0:
        raise SystemExit(f"no objects under s3://{BUCKET}/{INPUT_PREFIX}/")

    artifacts = WORK / "artifacts"
    failed_stage = None
    try:
        stage("profile", [
            "profile_contract_history.py",
            "--legacy", "data/raw/legacy/contract_history_2009_2023_05.csv",
            "--current", "data/raw/current/contract_history_2023_06_present.csv",
            "--output", "artifacts/profiles/batch_profile.json",
            "--field-map-output", "artifacts/profiles/batch_field_map.csv",
        ])
        stage("bronze", [
            "build_bronze.py",
            "--manifest", f"data/manifest/{BATCH_ID}.yaml",
            "--profile", "artifacts/profiles/batch_profile.json",
            "--output-root", "data/bronze",
            "--reconciliation-output", "artifacts/bronze/reconciliation.json",
        ])
        if VERIFY_CELLS:
            stage("verify_bronze_cells", [
                "verify_bronze_values.py",
                "--reconciliation", "artifacts/bronze/reconciliation.json",
            ])
        stage("quality", [
            "build_quality_layers.py",
            "--profile", "artifacts/profiles/batch_profile.json",
            "--bronze-reconciliation", "artifacts/bronze/reconciliation.json",
            "--bronze-dir", f"data/bronze/batch_id={BATCH_ID}",
            "--output-root", "data/derived",
            "--as-of-date", AS_OF_DATE,
        ])
        stage("trade_thresholds", [
            "check_trade_thresholds.py",
            "--derived-dir", f"data/derived/batch_id={BATCH_ID}/ruleset=v2",
            "--bronze-dir", f"data/bronze/batch_id={BATCH_ID}",
            "--thresholds", "data/reference/trade_agreement_thresholds.csv",
            "--output-dir", f"data/governance/batch_id={BATCH_ID}",
        ])
    except SystemExit as exit_signal:
        failed_stage = str(exit_signal)

    suffix = "FAILED/" if failed_stage else ""
    log(f"Uploading outputs ({'partial' if failed_stage else 'complete'})")
    uploaded = 0
    for folder in ("artifacts", "data/derived", "data/governance"):
        source = WORK / folder
        if source.exists():
            uploaded += upload_tree(source, f"{OUTPUT_PREFIX}/{BATCH_ID}/{suffix}{folder}")
    log(f"{uploaded} objects uploaded")

    if failed_stage:
        log(f"RESULT: {failed_stage}")
        raise SystemExit(1)
    log("RESULT: all gates passed")


if __name__ == "__main__":
    main()
