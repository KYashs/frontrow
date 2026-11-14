"""Checkout worker: SQS FIFO -> payment -> confirm in DynamoDB -> EventBridge.

Holds already settled who gets which seat. This worker turns a locked hold
into a sold order exactly once:
  * payment (simulated; the integration point for Razorpay/Stripe),
  * one transaction: seats held->sold, hold checkout->confirmed, order put,
  * publish OrderConfirmed / OrderFailed to the event bus. Email, analytics
    and anything else subscribe there; the worker doesn't know about them.
"""
import hashlib
import json
import logging
import os
import time

import boto3

from core import inventory, metrics, settings

log = logging.getLogger()
log.setLevel(logging.INFO)

ddb = boto3.client("dynamodb")
events = boto3.client("events")
BUS = os.environ.get("EVENT_BUS", "default")


def handler(event, context):
    records = event.get("Records", [])
    for i, record in enumerate(records):
        try:
            process(json.loads(record["body"]))
        except Exception:
            log.exception("checkout failed for %s", record.get("messageId"))
            # FIFO: fail this message and everything after it in the batch.
            return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records[i:]]}
    return {"batchItemFailures": []}


def process(msg):
    t = settings.tables()
    order_id = msg["order_id"]
    if ddb.get_item(TableName=t.orders, Key={"order_id": {"S": order_id}}, ConsistentRead=True).get("Item"):
        log.info("order %s already processed", order_id)
        return
    hold = inventory.get_hold(ddb, t, msg["hold_id"])
    if hold is None:
        log.warning("hold %s missing for order %s", msg["hold_id"], order_id)
        return

    if hold["status"] != "checkout" or hold.get("order_id") != order_id:
        return _fail(t, msg, hold, "HOLD_EXPIRED")

    if msg.get("payment") == "simulated_decline":
        inventory.release(ddb, t, hold, "failed", ["checkout"])
        metrics.emit("PaymentsDeclined", service="checkout")
        return _fail(t, msg, hold, "PAYMENT_DECLINED")

    now = int(time.time())
    ticket_code = hashlib.sha256(f"{order_id}:{hold['hold_id']}".encode()).hexdigest()[:10].upper()
    item = _order_item(msg, hold, "CONFIRMED", now)
    item["ticket_code"] = {"S": ticket_code}
    try:
        created = inventory.confirm(ddb, t, hold, item, now)
    except inventory.HoldNotActive:
        inventory.release(ddb, t, hold, "failed", ["checkout"])
        return _fail(t, msg, hold, "HOLD_LOST")
    if not created:
        return

    wait = now - int(msg.get("submitted_at", now))
    metrics.emit("OrdersConfirmed", service="checkout")
    metrics.emit("TicketsSold", hold["qty"], service="checkout")
    metrics.emit("CheckoutLatency", wait, service="checkout", unit="Seconds")
    _publish("OrderConfirmed", msg, hold, now, ticket_code=ticket_code)
    log.info("order %s confirmed (%s tickets)", order_id, hold["qty"])


def _order_item(msg, hold, status, now, reason=None):
    item = {
        "order_id": {"S": msg["order_id"]},
        "status": {"S": status},
        "hold_id": {"S": hold["hold_id"]},
        "event_id": {"S": hold["event_id"]},
        "email": {"S": msg["email"]},
        "total": {"N": str(hold["total"])},
        "currency": {"S": hold["currency"]},
        "qty": {"N": str(hold["qty"])},
        "lines": {"L": [{"M": {"section": {"S": l["section"]}, "tier": {"S": l["tier"]},
                               "qty": {"N": str(l["qty"])}, "price": {"N": str(l["price"])}}}
                        for l in hold["lines"]]},
        "processed_at": {"N": str(now)},
    }
    if hold["seats"]:
        item["seats"] = {"L": [{"S": s} for s in hold["seats"]]}
    if reason:
        item["reason"] = {"S": reason}
    return item


def _fail(t, msg, hold, reason):
    now = int(time.time())
    try:
        ddb.put_item(TableName=t.orders, Item=_order_item(msg, hold, "FAILED", now, reason),
                     ConditionExpression="attribute_not_exists(order_id)")
    except ddb.exceptions.ConditionalCheckFailedException:
        return
    metrics.emit("OrdersFailed", service="checkout", reason=reason)
    _publish("OrderFailed", msg, hold, now, reason=reason)
    log.info("order %s failed: %s", msg["order_id"], reason)


def _publish(detail_type, msg, hold, now, **extra):
    # No email/PII on the bus: subscribers that need it read the order record.
    detail = {
        "order_id": msg["order_id"], "event_id": hold["event_id"], "artist": msg["artist"],
        "title": msg["title"], "venue": msg["venue"], "city": msg["city"], "starts_at": msg["starts_at"],
        "status": "CONFIRMED" if detail_type == "OrderConfirmed" else "FAILED",
        "kind": hold["kind"], "qty": hold["qty"], "total": hold["total"], "currency": hold["currency"],
        "tiers": sorted({l["tier"] for l in hold["lines"]}),
        "sections": sorted({l["section"] for l in hold["lines"]}),
        "payment": msg.get("payment"), "at": now,
        "checkout_seconds": now - int(msg.get("submitted_at", now)),
        **extra,
    }
    events.put_events(Entries=[{"Source": "frontrow.checkout", "DetailType": detail_type,
                                "Detail": json.dumps(detail), "EventBusName": BUS}])
