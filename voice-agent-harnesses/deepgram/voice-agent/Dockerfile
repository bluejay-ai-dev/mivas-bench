# MIVAS image for deepgram/voice-agent.
# Build from repo root:
#   docker build -f voice-agent-harnesses/deepgram/voice-agent/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:deepgram-voice-agent-control-industry .
#
# Copies the whole deepgram/ tree (shared harness.py) and runs this runtime.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=deepgram/voice-agent \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=deepgram \
    HARNESS_RUNTIME=voice-agent \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/voice-agent \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-deepgram \
    DEEPGRAM_API_KEY="" \
    DEEPGRAM_LISTEN_MODEL=flux-general-en \
    DEEPGRAM_THINK_MODEL=gpt-4.1 \
    DEEPGRAM_SPEAK_MODEL=flux-hannah-en \
    DEEPGRAM_GREETING="" \
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

COPY industries/${INDUSTRY}/ /app/industry/
COPY voice-agent-harnesses/deepgram/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
