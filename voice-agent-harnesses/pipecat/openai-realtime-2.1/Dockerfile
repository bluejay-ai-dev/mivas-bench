# syntax=docker/dockerfile:1
# MIVAS image for pipecat/openai-realtime-2.1.
# Build from repo root:
#   docker build -f voice-agent-harnesses/pipecat/openai-realtime-2.1/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:pipecat-openai-realtime-2.1-control-industry .
#
# Copies the whole pipecat/ tree (shared harness.py + bot.py). The pod
# runs a Daily dialin worker plus the industry tool server.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=pipecat/openai-realtime-2.1 \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=pipecat \
    HARNESS_RUNTIME=openai-realtime-2.1 \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/openai-realtime-2.1 \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-pipecat \
    DAILY_API_KEY="" \
    OPENAI_API_KEY="" \
    GOOGLE_API_KEY="" \
    DEEPGRAM_API_KEY="" \
    ELEVENLABS_API_KEY="" \
    PIPECAT_STT_MODEL="" \
    PIPECAT_TTS_MODEL="" \
    PIPECAT_TTS_VOICE_ID="" \
    BLUEJAY_API_KEY="" \
    BLUEJAY_API_URL="" \
    BLUEJAY_OTLP_ENDPOINT=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY industries/${INDUSTRY}/requirements.txt /tmp/industry-requirements.txt
COPY voice-agent-harnesses/pipecat/requirements.txt /tmp/harness-requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
      -r /tmp/industry-requirements.txt \
      -r /tmp/harness-requirements.txt

COPY industries/${INDUSTRY}/ /app/industry/
COPY voice-agent-harnesses/pipecat/ /app/harness/
COPY runtime/ /app/runtime/
RUN chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
