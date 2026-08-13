# Pipecat Cloud agent image. The base image owns the HTTP entrypoint and calls
# `bot(args)` from /app/bot.py — do not add a CMD.
# 0.1.27 is the base image's own version, not the pipecat-ai library version —
# there is no dailyco/pipecat-base:1.7.0 and a build against it fails to resolve.
# Same digest as :latest (sha256:5d761b89...), so this pins what already shipped.
FROM dailyco/pipecat-base:0.1.27

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY industries ./industries
