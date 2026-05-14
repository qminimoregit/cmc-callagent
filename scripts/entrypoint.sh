#!/usr/bin/env bash
# scripts/entrypoint.sh
# Fetches Google Cloud credentials from AWS Secrets Manager at container startup,
# then hands off to the main process (uvicorn).
set -e

if [ -n "$GOOGLE_CREDENTIALS_SECRET_ARN" ]; then
    echo "[entrypoint] Fetching Google credentials from Secrets Manager..."
    aws secretsmanager get-secret-value \
        --secret-id "$GOOGLE_CREDENTIALS_SECRET_ARN" \
        --query SecretString \
        --output text \
        > /tmp/google-credentials.json
    export GOOGLE_APPLICATION_CREDENTIALS=/tmp/google-credentials.json
    echo "[entrypoint] Google credentials written to /tmp/google-credentials.json"
else
    echo "[entrypoint] GOOGLE_CREDENTIALS_SECRET_ARN not set — skipping Secrets Manager fetch."
    echo "[entrypoint] Ensure GOOGLE_APPLICATION_CREDENTIALS is set manually for local dev."
fi

exec "$@"
