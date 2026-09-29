# Deterministic batch pipeline only. The agent layer is deliberately NOT in this
# image: it is 21 model calls a reviewer triggers, not a 506,847-row batch
# workload, and shipping 5 GB of model weights to run it would be the wrong
# trade. See docs/07 section 1.
FROM python:3.12-slim

# Pinned so the image is reproducible. An unpinned build would undercut the
# whole point of a pipeline that version-stamps its own outputs.
#
# duckdb must match the development environment: the first build pinned 1.1.3
# and the bronze stage failed because read_csv(strict_mode=...) did not exist
# in that release. Silent version drift between laptop and container is exactly
# the class of bug the smoke test is for.
RUN pip install --no-cache-dir \
        duckdb==1.5.5 \
        pandas==2.2.3 \
        boto3==1.35.36

WORKDIR /app
COPY src/ /app/src/
COPY deploy/entrypoint.py /app/entrypoint.py

# Inputs are fetched to /work at runtime, not baked in. Data never lives in the
# image: it would bloat the layer cache and pin a snapshot to an image tag.
RUN mkdir -p /work
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "/app/entrypoint.py"]
