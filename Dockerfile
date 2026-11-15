# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY camd.py index.html /app/

ENV PORT=8080 STREAM_PATH=/stream PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python3", "/app/camd.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python3", "/app/camd.py", "healthcheck"]
