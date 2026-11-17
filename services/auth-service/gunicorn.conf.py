"""Gunicorn configuration (12-factor: everything tunable via env)."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = 20
keepalive = 5
max_requests = 2000
max_requests_jitter = 200
accesslog = None  # access logs are emitted by RequestContextMiddleware as JSON
errorlog = "-"
forwarded_allow_ips = "*"


def child_exit(server, worker):  # noqa: ARG001 - gunicorn hook signature
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
