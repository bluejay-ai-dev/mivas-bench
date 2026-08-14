# syntax=docker/dockerfile:1
# MIVAS image for nvidia/nemotron.
# Build from repo root:
#   docker build -f voice-agent-harnesses/nvidia/nemotron/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:nvidia-nemotron-control-industry .
#
# Copies the whole nvidia/ tree (shared harness.py) and runs this runtime.
# Cascaded Nemotron NIM path. Requires NVIDIA_API_KEY.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=nvidia/nemotron \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=nvidia \
    HARNESS_RUNTIME=nemotron \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/nemotron \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-nvidia \
    NVIDIA_API_KEY="" \
    NGC_API_KEY="" \
    CHIRP_PORT=8765 \
    CHIRP_USER="" \
    CHIRP_PASS="" \
    BLUEJAY_API_KEY="" \
    BLUEJAY_API_URL="" \
    BLUEJAY_OTLP_ENDPOINT=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY industries/${INDUSTRY}/requirements.txt /tmp/industry-requirements.txt
COPY voice-agent-harnesses/nvidia/requirements.txt /tmp/harness-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
      -r /tmp/industry-requirements.txt \
      -r /tmp/harness-requirements.txt

COPY industries/${INDUSTRY}/ /app/industry/
# CACHE_BUST: registry cache reused an older nvidia/ COPY (run 231249 image
# still had the pre-retry harness.py). Changing this line forces a miss.
ARG CACHE_BUST=gloria-727614-magpie-ascii
COPY voice-agent-harnesses/nvidia/ /app/harness/
COPY runtime/ /app/runtime/
RUN chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
