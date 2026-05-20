FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash scheduler
WORKDIR /app

# Build context: project root (so reva/ and scheduler/ are both reachable).

# 1. Install the reva library and its deps first.
COPY pyproject.toml .
COPY reva/ ./reva/
RUN pip install --no-cache-dir .

# 2. Install scheduler-only deps (rq, redis).
COPY scheduler/requirements.txt ./scheduler-requirements.txt
RUN pip install --no-cache-dir -r scheduler-requirements.txt

# 3. Copy scheduler source and runtime data.
COPY scheduler/scheduler/ ./scheduler/
COPY db/migrations/ ./db/migrations/

USER scheduler

CMD ["python", "-m", "scheduler.main"]
