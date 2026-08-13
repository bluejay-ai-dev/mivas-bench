# MIVAS image for vapi/flux-gpt4.1-flash2.5.
# Build from repo root:
#   docker build -f voice-agent-harnesses/vapi/flux-gpt4.1-flash2.5/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:vapi-flux-gpt4.1-flash2.5-control-industry .
#
# Copies the whole vapi/ tree (shared harness.py) and runs this runtime.
# Bluejay CHIRP + Vapi cloud; POST /tool/* webhooks need PUBLIC_URL.
# Required at runtime: VAPI_API_KEY, PUBLIC_URL.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=vapi/flux-gpt4.1-flash2.5 \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=vapi \
    HARNESS_RUNTIME=flux-gpt4.1-flash2.5 \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/flux-gpt4.1-flash2.5 \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-vapi \
    VAPI_API_KEY="" \
    PUBLIC_URL="" \
    VAPI_LLM_MODEL=gpt-4.1 \
    VAPI_STT_MODEL=flux-general-en \
    VAPI_TTS_MODEL=eleven_flash_v2_5 \
    VAPI_VOICE_ID=21m00Tcm4TlvDq8ikWAM \
    VAPI_GREETING="" \
    CHIRP_PORT=8770 \
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
COPY voice-agent-harnesses/vapi/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
