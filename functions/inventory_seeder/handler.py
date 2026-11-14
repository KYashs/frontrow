"""One-off: create DynamoDB inventory for every event in the catalogue.

  * one item per seat in every on-sale seated section (~150k items),
  * a seated availability counter per section,
  * GA zones split into sharded counters,
  * the waiting-room config for the on-sale.

Invoked by Terraform after the tables exist. Idempotent per event (a META
marker); pass {"force": true, "events": [...]} to rebuild an event.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

from core import catalog, keys, settings

log = logging.getLogger()
log.setLevel(logging.INFO)
ddb = boto3.client("dynamodb", config=Config(max_pool_connections=32, retries={"mode": "adaptive", "max_attempts": 10}))


def _batch_write(table, items):
    pending = {table: [{"PutRequest": {"Item": it}} for it in items]}
    delay = 0.05
    while pending:
        resp = ddb.batch_write_item(RequestItems=pending)
        pending = resp.get("UnprocessedItems") or {}
        if pending:
            time.sleep(delay)
            delay = min(delay * 2, 2)


def seed_event(t, ev, force=False):
    event_id = ev["event_id"]
    marker = ddb.get_item(TableName=t.inventory, Key={"pk": {"S": event_id}, "sk": {"S": "META"}}).get("Item")
    if marker and not force:
        return {"event_id": event_id, "skipped": True}
    now = int(time.time())
    seats, counters = [], []
    for s in catalog.event_sections(event_id).values():
        if not s["on_sale"]:
            continue
        if s["kind"] == "seated":
            pk = keys.seat_pk(event_id, s["section_id"])
            for sk in catalog.seat_keys(s):
                seats.append({"pk": {"S": pk}, "sk": {"S": sk}, "status": {"S": "available"}})
            counters.append({"pk": {"S": event_id}, "sk": {"S": keys.section_sk(s["section_id"])},
                             "available": {"N": str(s["capacity"])}, "capacity": {"N": str(s["capacity"])}})
        else:
            for shard, cap in enumerate(keys.shard_capacities(s["capacity"], settings.GA_SHARDS)):
                counters.append({"pk": {"S": event_id}, "sk": {"S": keys.ga_sk(s["section_id"], shard)},
                                 "available": {"N": str(cap)}, "capacity": {"N": str(cap)}})

    chunks = [seats[i:i + 25] for i in range(0, len(seats), 25)]
    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(lambda c: _batch_write(t.seats, c), chunks))
    for i in range(0, len(counters), 25):
        _batch_write(t.inventory, counters[i:i + 25])

    onsale = ev.get("onsale", {"mode": "open", "rate_per_sec": 0, "initial": 0})
    ddb.put_item(TableName=t.waiting, Item={
        "pk": {"S": event_id}, "sk": {"S": "CONFIG"}, "mode": {"S": onsale["mode"]},
        "opens_at": {"N": str(now)}, "rate": {"N": str(onsale["rate_per_sec"])},
        "initial": {"N": str(onsale["initial"])}, "epoch": {"S": str(now)}})
    ddb.put_item(TableName=t.inventory, Item={
        "pk": {"S": event_id}, "sk": {"S": "META"}, "seeded_at": {"N": str(now)},
        "seats": {"N": str(len(seats))}})
    log.info("seeded %s: %d seats, %d counters", event_id, len(seats), len(counters))
    return {"event_id": event_id, "seats": len(seats), "counters": len(counters)}


def handler(event, context):
    event = event or {}
    t = settings.tables()
    wanted = set(event.get("events") or [e["event_id"] for e in catalog.events()])
    results = [seed_event(t, ev, force=bool(event.get("force"))) for ev in catalog.events()
               if ev["event_id"] in wanted]
    return {"results": results}
