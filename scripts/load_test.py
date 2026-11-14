#!/usr/bin/env python3
"""Ticket-drop stampede of browsing traffic: watch CloudFront absorb the
cacheable reads, the ALB spread the rest, and Kubernetes add API pods.

    python3 scripts/load_test.py --url https://dxxxx.cloudfront.net --workers 200 --minutes 10

Add --bust-cache to defeat the 2-second edge cache and push load to the API pods and DynamoDB.
From a single IP you will hit the WAF rate limit (that's the point of it);
raise waf_api_rate_limit in terraform/environments/prod.tfvars (via a pull request)
for a pure scaling test.
Standard library only.
"""
import argparse
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

EVENTS = {
    # event -> seated sections whose seat grids we fetch
    "fred-again-mumbai-2027": ["D-L1", "B-L2", "PLATINUM"],
    "skrillex-delhi-2027": ["A10", "B12", "A20"],
    "the-weeknd-sofi-2027": ["A3", "140", "512"],
    "kendrick-lamar-metlife-2027": ["113", "140", "338"],
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--workers", type=int, default=100)
    p.add_argument("--minutes", type=float, default=5)
    p.add_argument("--bust-cache", action="store_true")
    a = p.parse_args()
    base = a.url.rstrip("/") + "/api"
    stop = time.time() + a.minutes * 60
    lock = threading.Lock()
    lat, codes, cache, servers = [], Counter(), Counter(), Counter()

    def worker():
        while time.time() < stop:
            ev = random.choice(list(EVENTS))
            sec = random.choice(EVENTS[ev])
            path = random.choice([f"/events/{ev}", f"/events/{ev}/availability", f"/events/{ev}/sections/{sec}/seats"])
            if a.bust_cache:
                path += f"?r={random.random()}"
            t0 = time.time()
            try:
                with urllib.request.urlopen(base + path, timeout=20) as r:
                    r.read()
                    code, hit, srv = r.status, (r.headers.get("X-Cache") or "").split(" ")[0], r.headers.get("X-Served-By")
            except urllib.error.HTTPError as e:
                code, hit, srv = e.code, (e.headers.get("X-Cache") or "").split(" ")[0], None
            except Exception:
                code, hit, srv = "error", "", None
            with lock:
                lat.append(time.time() - t0)
                codes[code] += 1
                cache[hit or "n/a"] += 1
                if srv:
                    servers[srv] += 1

    for _ in range(a.workers):
        threading.Thread(target=worker, daemon=True).start()
    while time.time() < stop:
        time.sleep(10)
        with lock:
            if lat:
                s = sorted(lat)
                p95 = s[max(0, int(len(s) * 0.95) - 1)]
                print(f"[{time.strftime('%H:%M:%S')}] {len(s)/10:.0f} req/s  p50 {statistics.median(s)*1000:.0f}ms  "
                      f"p95 {p95*1000:.0f}ms  status {dict(codes)}  edge {dict(cache)}  API pods seen {len(servers)}")
            lat.clear(); codes.clear(); cache.clear()
    print("requests per API pod:", dict(servers))


if __name__ == "__main__":
    main()
