"""DynamoDB Streams -> live seat map.

Two streams feed this function:
  seats table      every status change. We aggregate per (event, section)
                   into one counter update and broadcast seat-level changes.
  inventory table  counter changes (seated counts we just wrote, GA shard
                   decrements from holds). Broadcast as availability deltas.

Broadcasting goes to every WebSocket connection watching that event via the
API Gateway Management API. Gone connections are cleaned up as we go.
"""
import json
import logging
import os
from collections import defaultdict

import boto3

from core import keys, metrics, settings

log = logging.getLogger()
log.setLevel(logging.INFO)

ddb = boto3.client("dynamodb")
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")
_mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT) if WS_ENDPOINT else None
CHUNK = 250  # keep each WebSocket frame well under API Gateway's 32 KB frame limit


def _status(image):
    return image.get("status", {}).get("S") if image else None


def handler(event, context):
    t = settings.tables()
    seat_deltas = defaultdict(int)          # (event, section) -> +/- available
    seat_changes = defaultdict(list)        # event -> [[section, sk, s]]
    avail_deltas = defaultdict(lambda: defaultdict(int))  # event -> section -> delta

    for rec in event.get("Records", []):
        if rec.get("eventName") != "MODIFY":
            continue
        img = rec["dynamodb"]
        old, new = img.get("OldImage", {}), img.get("NewImage", {})
        source = rec.get("eventSourceARN", "")
        if f"table/{t.seats}/" in source:
            before, after = _status(old), _status(new)
            if before == after:
                continue
            event_id, section_id = keys.split_seat_pk(new["pk"]["S"])
            seat_deltas[(event_id, section_id)] += (after == "available") - (before == "available")
            seat_changes[event_id].append([section_id, new["sk"]["S"], after[0]])
        elif f"table/{t.inventory}/" in source:
            kind, section_id = keys.parse_inventory_sk(new["sk"]["S"])
            if not kind or "available" not in old or "available" not in new:
                continue
            d = int(new["available"]["N"]) - int(old["available"]["N"])
            if d:
                avail_deltas[new["pk"]["S"]][section_id] += d

    for (event_id, section_id), d in seat_deltas.items():
        if d:
            ddb.update_item(TableName=t.inventory,
                            Key={"pk": {"S": event_id}, "sk": {"S": keys.section_sk(section_id)}},
                            UpdateExpression="ADD available :d", ExpressionAttributeValues={":d": {"N": str(d)}})

    sent = 0
    for event_id, changes in seat_changes.items():
        for i in range(0, len(changes), CHUNK):
            sent += broadcast(t, event_id, {"type": "seats", "items": changes[i:i + CHUNK]})
    for event_id, sections in avail_deltas.items():
        sent += broadcast(t, event_id, {"type": "avail", "items": [[s, d] for s, d in sections.items()]})
    if sent:
        metrics.emit("WsMessagesSent", sent, service="realtime")


def broadcast(t, event_id, message):
    if _mgmt is None:
        return 0
    data = json.dumps(message, separators=(",", ":")).encode()
    sent = 0
    paginator = ddb.get_paginator("query")
    for page in paginator.paginate(TableName=t.connections, IndexName="by_event",
                                   KeyConditionExpression="event_id = :e",
                                   ExpressionAttributeValues={":e": {"S": event_id}},
                                   ProjectionExpression="connection_id"):
        for it in page["Items"]:
            cid = it["connection_id"]["S"]
            try:
                _mgmt.post_to_connection(ConnectionId=cid, Data=data)
                sent += 1
            except _mgmt.exceptions.GoneException:
                ddb.delete_item(TableName=t.connections, Key={"connection_id": {"S": cid}})
            except Exception:
                log.warning("post to %s failed", cid, exc_info=True)
    return sent
