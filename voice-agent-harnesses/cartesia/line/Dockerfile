# MIVAS image for cartesia/line.
# Build from repo root:
#   docker build -f voice-agent-harnesses/cartesia/line/Dockerfile \
#     --build-arg INDUSTRY=control-industry \
#     -t mivas-bench:cartesia-line-control-industry .
#
# Copies the whole cartesia/ tree (shared harness.py) and runs this runtime.

FROM python:3.12-slim

ARG INDUSTRY=control-industry

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv

ENV HARNESS=cartesia/line \
    INDUSTRY=${INDUSTRY} \
    APP_ROOT=/app \
    INDUSTRY_DIR=/app/industry \
    HARNESS_FAMILY=cartesia \
    HARNESS_RUNTIME=line \
    HARNESS_FAMILY_DIR=/app/harness \
    HARNESS_DIR=/app/harness/line \
    PYTHONPATH=/app/harness \
    MIVAS_DB_PATH=/data/industry.db \
    TOOL_SERVER_PORT=8000 \
    TOOL_SERVER_URL=http://127.0.0.1:8000 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    BLUEJAY_SERVICE_NAME=mivas-cartesia \
    CARTESIA_API_KEY="" \
    OPENAI_API_KEY="" \
    PUBLIC_URL="" \
    CHIRP_PORT=8773 \
    CHIRP_USER="" \
    CHIRP_PASS="" \
    BLUEJAY_API_KEY="" \
    BLUEJAY_API_URL="" \
    BLUEJAY_OTLP_ENDPOINT=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://cartesia.sh | sh

ENV PATH="/root/.cartesia/bin:${PATH}" \
    CARTESIA_CLI=/root/.cartesia/bin/cartesia

WORKDIR /app

COPY industries/${INDUSTRY}/ /app/industry/
COPY voice-agent-harnesses/cartesia/ /app/harness/
COPY runtime/ /app/runtime/

RUN uv pip install --system --no-cache \
      -r /app/industry/requirements.txt \
      -r /app/harness/requirements.txt \
    && chmod +x /app/runtime/entrypoint.sh \
    && mkdir -p /data \
    && test -f "${HARNESS_DIR}/agent.py"

EXPOSE 8000 8765

CMD ["/app/runtime/entrypoint.sh"]
