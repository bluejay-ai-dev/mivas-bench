# MIVAS image for twilio/conversationrelay-gpt4.1.
# Build from repo root:
#   docker build -f voice-agent-harnesses/twilio/conversationrelay-gpt4.1/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:twilio-conversationrelay-gpt4.1-control-industry .
#
# Copies the whole twilio/ tree (shared harness.py) and runs this runtime.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=twilio/conversationrelay-gpt4.1 \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=twilio \
    HARNESS_RUNTIME=conversationrelay-gpt4.1 \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/conversationrelay-gpt4.1 \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-twilio \
    OPENAI_API_KEY="" \
    PUBLIC_URL="" \
    TWILIO_LLM_MODEL=gpt-4.1 \
    TWILIO_TTS_PROVIDER=google \
    TWILIO_TTS_VOICE=en-US-Journey-O \
    TWILIO_TRANSCRIPTION_PROVIDER=deepgram \
    TWILIO_LANGUAGE=en-US \
    TWILIO_WELCOME_GREETING="" \
    CHIRP_PORT=8773 \
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
COPY voice-agent-harnesses/twilio/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
