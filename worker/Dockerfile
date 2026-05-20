FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash worker
WORKDIR /app

# Build context for this Dockerfile is the project root (see docker-compose),
# so `worker/`, `reva/`, and `prompts/` are all reachable here.

# 1. Install the reva library first so its deps + pinned versions land.
COPY pyproject.toml .
COPY reva/ ./reva/
RUN pip install --no-cache-dir .

# 2. Install worker-only deps (RQ, redis client).
COPY worker/requirements.txt ./worker-requirements.txt
RUN pip install --no-cache-dir -r worker-requirements.txt

# 3. Copy worker source and runtime data.
COPY worker/worker/ ./worker/
COPY prompts/ ./prompts/
COPY db/migrations/ ./db/migrations/

USER worker

CMD ["python", "-m", "worker.main"]
