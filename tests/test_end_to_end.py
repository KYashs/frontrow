"""Browse -> hold -> checkout -> worker -> stream processor, through the real API."""
import json

import boto3

from conftest import EVENT, REGION, load_function


def drain(queue_url):
    sqs = boto3.client("sqs", region_name=REGION)
    msgs = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
    return [{"messageId": m["MessageId"], "body": m["Body"]} for m in msgs]


def test_catalogue_from_rds(api):
    events = api.get("/api/events").json()["events"]
    assert len(events) == 1 and events[0]["capacity"] == 52
    assert api.get("/api/events").headers["cache-control"] == "public, max-age=30"
    ev = api.get(f"/api/events/{EVENT}").json()
    on_sale = {s["section_id"]: s["on_sale"] for s in ev["sections"]}
    assert on_sale == {"101": 1, "102": 0, "PIT": 1}


def test_seat_map_and_availability(api):
    seats = api.get(f"/api/events/{EVENT}/sections/101/seats").json()
    assert len(seats["seats"]) == 12 and seats["seats"][0] == ["R01#S01", "a"]
    avail = api.get(f"/api/events/{EVENT}/availability")
    assert avail.json()["available"] == {"101": 12, "PIT": 40}
    assert avail.headers["cache-control"] == "public, max-age=2"


def test_hold_validation(api):
    behind_stage = {"event_id": EVENT, "seats": [{"section": "102", "seat": "R01#S01"}]}
    assert api.post("/api/holds", json=behind_stage).status_code == 400
    no_such_seat = {"event_id": EVENT, "seats": [{"section": "101", "seat": "R09#S01"}]}
    assert api.post("/api/holds", json=no_such_seat).status_code == 400
    both = {"event_id": EVENT, "seats": [{"section": "101", "seat": "R01#S01"}], "ga": {"section": "PIT", "qty": 1}}
    assert api.post("/api/holds", json=both).status_code == 422


def test_seated_purchase_end_to_end(api, aws, t):
    r = api.post("/api/holds", json={"event_id": EVENT, "seats": [
        {"section": "101", "seat": "R02#S01"}, {"section": "101", "seat": "R02#S02"}]})
    assert r.status_code == 201, r.text
    hold = r.json()
    assert hold["total"] == 4000

    again = api.post("/api/holds", json={"event_id": EVENT, "seats": [{"section": "101", "seat": "R02#S02"}]})
    assert again.status_code == 409

    order = api.post("/api/orders", json={"hold_id": hold["hold_id"], "email": "fan@example.com"})
    assert order.status_code == 202
    oid = order.json()["order_id"]
    assert api.get(f"/api/orders/{oid}").json()["status"] == "PENDING"

    worker = load_function("checkout_worker")
    assert worker.handler({"Records": drain(aws["queue_url"])}, None) == {"batchItemFailures": []}
    done = api.get(f"/api/orders/{oid}").json()
    assert done["status"] == "CONFIRMED" and len(done["ticket_code"]) == 10
    assert done["seats"] == ["101|R02#S01", "101|R02#S02"]

    # second checkout of the same hold is refused
    assert api.post("/api/orders", json={"hold_id": hold["hold_id"], "email": "fan@example.com"}).status_code == 410

    # the notifier reads the order record (no PII travels on the bus)
    notifier = load_function("notifier")
    notifier.handler({"detail": {"order_id": oid, "artist": "Test Artist", "title": "Live", "venue": "Tiny Arena",
                                 "city": "Testville", "starts_at": "x", "currency": "INR", "total": 4000,
                                 "ticket_code": done["ticket_code"]}}, None)


def test_declined_payment_returns_inventory(api, aws):
    hold = api.post("/api/holds", json={"event_id": EVENT, "ga": {"section": "PIT", "qty": 4}}).json()
    assert api.get(f"/api/events/{EVENT}/availability").json()["available"]["PIT"] == 36
    order = api.post("/api/orders", json={"hold_id": hold["hold_id"], "email": "fan@example.com",
                                          "payment": "simulated_decline"}).json()
    load_function("checkout_worker").handler({"Records": drain(aws["queue_url"])}, None)
    result = api.get(f"/api/orders/{order['order_id']}").json()
    assert result["status"] == "FAILED" and result["reason"] == "PAYMENT_DECLINED"
    assert api.get(f"/api/events/{EVENT}/availability").json()["available"]["PIT"] == 40


def test_waiting_room_gates_holds(api, aws, t):
    from core import waiting_room
    aws["ddb"].put_item(TableName=t.waiting, Item={
        "pk": {"S": EVENT}, "sk": {"S": "CONFIG"}, "mode": {"S": "queue"}, "opens_at": {"N": "0"},
        "rate": {"N": "1000"}, "initial": {"N": "100"}, "epoch": {"S": "live"}})
    waiting_room.clear_cache()
    body = {"event_id": EVENT, "ga": {"section": "PIT", "qty": 1}}
    blocked = api.post("/api/holds", json=body)
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "WAITING_ROOM"
    joined = api.post(f"/api/waiting-room/{EVENT}/join").json()
    assert joined["state"] == "admitted"
    assert api.post("/api/holds", json={**body, "access_token": joined["access_token"]}).status_code == 201


def test_stream_processor_maintains_counters(aws, t):
    fn = load_function("stream_processor")
    arn = f"arn:aws:dynamodb:{REGION}:123456789012:table/{t.seats}/stream/x"

    def rec(before, after):
        img = lambda s: {"pk": {"S": f"{EVENT}#101"}, "sk": {"S": "R01#S01"}, "status": {"S": s}}  # noqa
        return {"eventName": "MODIFY", "eventSourceARN": arn,
                "dynamodb": {"OldImage": img(before), "NewImage": img(after)}}

    fn.handler({"Records": [rec("available", "held"), rec("available", "held"), rec("held", "sold"),
                            rec("held", "available")]}, None)
    from core import inventory
    assert inventory.availability(aws["ddb"], t, EVENT)["101"] == 11  # 12 - 1 - 1 + 1



def test_prometheus_metrics(api):
    from prometheus_client import REGISTRY
    ok = {"method": "GET", "route": "/api/events/{event_id}/availability", "status": "200"}
    before = REGISTRY.get_sample_value("frontrow_http_requests_total", ok) or 0
    api.get(f"/api/events/{EVENT}/availability")
    api.get("/api/holds/no-such-hold")
    assert REGISTRY.get_sample_value("frontrow_http_requests_total", ok) == before + 1
    body = api.get("/metrics").text
    assert 'route="/api/holds/{hold_id}",status="404"' in body      # route template, not the raw ID
    assert "no-such-hold" not in body
    assert 'route="/metrics"' not in body                           # scrapes don't count themselves
    assert "frontrow_http_request_duration_seconds_bucket" in body
