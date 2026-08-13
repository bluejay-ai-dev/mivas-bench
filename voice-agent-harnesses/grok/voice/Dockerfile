# MIVAS image for grok/voice.
# Build from repo root:
#   docker build -f voice-agent-harnesses/grok/voice/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:grok-voice-control-industry .
#
# Copies the whole grok/ tree (shared harness.py) and runs this runtime.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=grok/voice \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=grok \
    HARNESS_RUNTIME=voice \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/voice \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-grok \
    GROK_API_KEY="" \
    XAI_API_KEY="" \
    GROK_VOICE_MODEL=grok-voice-latest \
    GROK_VOICE=eve \
    GROK_WS_URL=wss://api.x.ai/v1/realtime \
    CHIRP_PORT=8768 \
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
COPY voice-agent-harnesses/grok/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
