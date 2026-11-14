"""API Gateway WebSocket routes: $connect, $disconnect, $default.

Browsers connect with ?event=<event_id>; we remember which event each
connection watches so the stream processor can fan out seat changes.
"""
import re
import time

import boto3

from core import metrics, settings

ddb = boto3.client("dynamodb")
EVENT_RE = re.compile(r"^[a-z0-9-]{3,64}$")


def handler(event, context):
    ctx = event["requestContext"]
    route, cid = ctx["routeKey"], ctx["connectionId"]
    t = settings.tables()
    if route == "$connect":
        event_id = (event.get("queryStringParameters") or {}).get("event", "")
        if not EVENT_RE.match(event_id):
            return {"statusCode": 400, "body": "event query parameter required"}
        ddb.put_item(TableName=t.connections, Item={
            "connection_id": {"S": cid}, "event_id": {"S": event_id},
            "connected_at": {"N": str(int(time.time()))},
            "ttl": {"N": str(int(time.time()) + 2 * 3600)},  # API Gateway caps connections at 2h
        })
        metrics.emit("WsConnects", service="realtime")
    elif route == "$disconnect":
        ddb.delete_item(TableName=t.connections, Key={"connection_id": {"S": cid}})
    return {"statusCode": 200, "body": "ok"}
