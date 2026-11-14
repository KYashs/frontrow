"""Every minute: give back inventory from holds whose timer ran out.

Seats in an expired hold are *already* purchasable (the hold condition
accepts expired holds), so this is about GA counters, the live seat map and
the hold records. Release is conditional on the hold still being expired and
in the expected state, so the sweeper can never fight a checkout.
"""
import logging
import time

import boto3

from core import inventory, metrics, settings

log = logging.getLogger()
log.setLevel(logging.INFO)
ddb = boto3.client("dynamodb")


def handler(event, context):
    t = settings.tables()
    now = int(time.time())
    released = 0
    for status in ("active", "checkout"):
        for page in ddb.get_paginator("query").paginate(
            TableName=t.holds, IndexName="by_status",
            KeyConditionExpression="#st = :s AND expires_at < :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": {"S": status}, ":now": {"N": str(now)}},
        ):
            for it in page["Items"]:
                hold = inventory.get_hold(ddb, t, it["hold_id"]["S"])
                if hold and inventory.release(ddb, t, hold, "expired", [status], now=now, only_if_expired=True):
                    released += 1
            if context and context.get_remaining_time_in_millis() < 5000:
                break
    if released:
        metrics.emit("HoldsExpired", released, service="sweeper")
    log.info("released %d expired holds", released)
    return {"released": released}
