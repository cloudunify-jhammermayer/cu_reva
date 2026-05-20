FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash api
WORKDIR /app

# Build context: project root (so reva/ and api/ are both reachable).

# 1. Install the reva library and its deps first.
COPY pyproject.toml .
COPY reva/ ./reva/
RUN pip install --no-cache-dir .

# 2. Install api-only deps (fastapi, uvicorn).
COPY api/requirements.txt ./api-requirements.txt
RUN pip install --no-cache-dir -r api-requirements.txt

# 3. Copy api source and runtime data.
COPY api/app/ ./app/
COPY db/migrations/ ./db/migrations/

USER api

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
