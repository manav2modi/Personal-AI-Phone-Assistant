"""Gunicorn config — auto-loaded by gunicorn even without CLI flags."""

worker_class = "gthread"
workers = 2
threads = 4
timeout = 120
