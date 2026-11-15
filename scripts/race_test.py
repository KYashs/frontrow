#!/usr/bin/env python3
"""The double-selling test. N buyers go through the waiting room and try to
hold the SAME seat at the same instant. Exactly one may win.

    python3 scripts/race_test.py --url https://dxxxx.cloudfront.net \
        --event kendrick-lamar-metlife-2027 --section 113 --seat R10#S07 --buyers 100

Then the winner checks out, and we confirm the order and the seat state.
Standard library only.
"""
import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def call(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers.get("X-Served-By")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers.get("X-Served-By")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--event", default="kendrick-lamar-metlife-2027")
    p.add_argument("--section", default="113")
    p.add_argument("--seat", default="R10#S07")
    p.add_argument("--buyers", type=int, default=100)
    p.add_argument("--email", default="", help="checkout the winner and send the ticket here (SES-verified)")
    a = p.parse_args()
    base = a.url.rstrip("/") + "/api"

    print(f"{a.buyers} buyers joining the waiting room for {a.event} ...")
    def admit(_):
        _, j, _ = call(base, f"/waiting-room/{a.event}/join", "POST")
        for _ in range(120):
            if j.get("state") == "admitted":
                return j["access_token"]
            time.sleep(1)
            _, j, _ = call(base, f"/waiting-room/{a.event}/status?ticket={urllib.request.quote(j['ticket'])}")
        return None

    with ThreadPoolExecutor(max_workers=min(a.buyers, 100)) as pool:
        tokens = list(pool.map(admit, range(a.buyers)))
    print(f"admitted: {sum(1 for t in tokens if t)}/{a.buyers}")

    barrier = threading.Barrier(a.buyers)
    retries = Counter()
    def grab(token):
        barrier.wait()  # everyone clicks at once
        body = {"event_id": a.event, "access_token": token, "seats": [{"section": a.section, "seat": a.seat}]}
        # 503 = DynamoDB TransactionConflict: another transaction on this seat was
        # in flight. Not an answer yet, so retry like a real client until 201 or 409.
        for attempt in range(6):
            res = call(base, "/holds", "POST", body)
            if res[0] != 503:
                return res
            retries["503"] += 1
            time.sleep(0.05 * 2 ** attempt + random.random() * 0.05)
        return res

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.buyers) as pool:
        results = list(pool.map(grab, tokens))
    print(f"{a.buyers} hold attempts answered in {time.time() - t0:.2f}s")
    codes = Counter(code for code, _, _ in results)
    servers = Counter(s for _, _, s in results)
    print("final answers:", dict(codes), "(201 = held, 409 = someone else got it)")
    print(f"busy retries along the way: {retries['503']} (503 = DynamoDB TransactionConflict, retried)")
    print("spread across API pods:", dict(servers))

    winners = [body for code, body, _ in results if code == 201]
    if len(winners) != 1:
        print(f"FAIL: {len(winners)} buyers hold {a.section} {a.seat}" if winners else
              "No winner. Is the seat already sold? Pick another with --seat.")
        return 1
    print(f"PASS: exactly one buyer holds section {a.section} seat {a.seat}")

    if a.email:
        hold = winners[0]
        _, order, _ = call(base, "/orders", "POST", {"hold_id": hold["hold_id"], "email": a.email})
        for _ in range(60):
            _, o, _ = call(base, f"/orders/{order['order_id']}")
            if o["status"] != "PENDING":
                break
            time.sleep(1)
        print(f"checkout: {o['status']}  ticket code {o.get('ticket_code')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
