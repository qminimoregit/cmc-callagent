FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y \
    libsndfile1 \
    libportaudio2 \
    ffmpeg \
    curl \
    awscli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY pyproject.toml .
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache . \
    && uv pip install --system --no-cache gunicorn

# Copy app source
COPY src/ ./src/
COPY dashboard/ ./dashboard/
COPY static/ ./static/
COPY main.py .
COPY trilingual_agent_prompt.md .

# Startup script (fetches Google creds from AWS Secrets Manager)
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Shared audio directory (mounted as a volume in docker-compose)
RUN mkdir -p /app/static

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]

# Production: Gunicorn manages N Uvicorn worker processes.
# WEB_CONCURRENCY controls the worker count (set in docker-compose or ECS task def).
CMD ["python", "main.py", "--prod"]
