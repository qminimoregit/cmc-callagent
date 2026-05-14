#!/usr/bin/env python
# main.py
"""
Entry point for the Nimali trilingual call agent.

Development (single worker, auto-reload):
    uv run python main.py

Production (Gunicorn + Uvicorn workers — handles concurrent calls):
    uv run python main.py --prod

Or via environment variable:
    ENVIRONMENT=production uv run python main.py
"""

import os
import sys

def run_dev():
    """Single-worker Uvicorn with auto-reload for local development."""
    import uvicorn
    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )

def run_prod():
    """
    Gunicorn with Uvicorn workers for production.
    Reads WEB_CONCURRENCY env var (default: 4 workers).
    Formula: (2 × CPU cores) + 1  →  4 is safe for 1-2 core VMs.
    """
    import multiprocessing
    workers = int(os.getenv("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 8)))

    # Use Gunicorn programmatically
    from gunicorn.app.base import BaseApplication

    class StandaloneApp(BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    from src.server import app as fastapi_app

    options = {
        "bind": "0.0.0.0:8000",
        "workers": workers,
        "worker_class": "uvicorn.workers.UvicornWorker",
        "timeout": 120,            # allow long STT/LLM/TTS chains
        "keepalive": 5,
        "max_requests": 1000,      # recycle workers to prevent memory leaks
        "max_requests_jitter": 100,
        "accesslog": "-",
        "errorlog": "-",
        "loglevel": "info",
        "preload_app": True,       # load app once in master → shared memory
    }

    print(f"[Nimali] Starting production server with {workers} workers…")
    StandaloneApp(fastapi_app, options).run()


if __name__ == "__main__":
    is_prod = (
        "--prod" in sys.argv
        or os.getenv("ENVIRONMENT", "").lower() == "production"
    )
    if is_prod:
        run_prod()
    else:
        run_dev()
