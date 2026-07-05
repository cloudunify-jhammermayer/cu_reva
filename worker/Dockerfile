FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    nodejs \
    npm \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# The review engine. PINNED: its flag/config-loading semantics ARE the SECU-1
# trust boundary (--setting-sources/--strict-mcp-config + cwd config scrubbing),
# so an unpinned `latest` could silently reopen the RCE on the next rebuild.
# Bump deliberately and re-run the hostile-clone regression test after each bump.
ARG REVA_CLAUDE_CODE_VERSION=2.1.160
RUN npm install -g @anthropic-ai/claude-code@${REVA_CLAUDE_CODE_VERSION}

# CodeGraph engine layer (repo-aware reviews/audits). Pinned (pre-1.0); keep this
# in sync with Settings.codegraph_version. The feature stays off unless
# REVA_CODEGRAPH_ENABLED is set, so installing it here is inert until enabled.
ARG REVA_CODEGRAPH_VERSION=0.9.8
RUN npm install -g @colbymchenry/codegraph@${REVA_CODEGRAPH_VERSION}

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
COPY scripts/worker_healthcheck.py /app/scripts/worker_healthcheck.py

COPY worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Container starts as root so entrypoint can chown the /repos volume,
# then drops to worker via gosu.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "worker.main"]
