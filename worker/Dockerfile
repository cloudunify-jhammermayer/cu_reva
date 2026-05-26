FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    nodejs \
    npm \
    gosu \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

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

COPY worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Container starts as root so entrypoint can chown the /repos volume,
# then drops to worker via su-exec.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "worker.main"]
