# MIVAS image for pipecat/cascaded.
# Build from repo root:
#   docker build -f voice-agent-harnesses/pipecat/cascaded/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:pipecat-cascaded-control-industry .
#
# Copies the whole pipecat/ tree (shared harness.py) and runs this runtime.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=pipecat/cascaded \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=pipecat \
    HARNESS_RUNTIME=cascaded \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/cascaded \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-pipecat \
    OPENAI_API_KEY="" \
    GOOGLE_API_KEY="" \
    DEEPGRAM_API_KEY="" \
    ELEVENLABS_API_KEY="" \
    PIPECAT_STT_MODEL="" \
    PIPECAT_TTS_MODEL="" \
    PIPECAT_TTS_VOICE_ID="" \
    LIVEKIT_URL="" \
    LIVEKIT_API_KEY="" \
    LIVEKIT_API_SECRET="" \
    BLUEJAY_API_KEY="" \
    BLUEJAY_API_URL="" \
    BLUEJAY_OTLP_ENDPOINT=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY industries/${INDUSTRY}/ /app/industry/
COPY voice-agent-harnesses/pipecat/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py" \
    && test -f /app/harness/adapters/livekit_worker.py

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
