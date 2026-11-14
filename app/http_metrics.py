"""Prometheus RED metrics for the API.

Kept out of main.py so the metric objects are created once per process, even
when main is reloaded (the test suite does this for every test).
Labels use the route template (/api/holds/{hold_id}), never the raw path, so
IDs can't explode the series count.
"""
from prometheus_client import Counter, Histogram

REQUESTS = Counter("frontrow_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("frontrow_http_request_duration_seconds", "HTTP request latency", ["method", "route"],
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5))
