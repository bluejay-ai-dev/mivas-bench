# Pipecat Cloud agent image. The base image owns the HTTP entrypoint and calls
# `bot(args)` from /app/bot.py — do not add a CMD.
FROM dailyco/pipecat-base:latest

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY industries ./industries
