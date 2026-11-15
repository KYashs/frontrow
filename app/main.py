"""FrontRow API - FastAPI on EKS behind the ALB.

One uvicorn worker per pod; Kubernetes scales pods, so Prometheus metrics stay
per-process and need no multiprocess mode. /metrics is outside /api, so
CloudFront never routes it: only Prometheus inside the cluster can scrape it.
Without CloudFront (cloudfront_enabled = false) the ALB blocks /metrics instead,
and these pods also serve the static site (bottom of this file).

Read path (cacheable at CloudFront for a few seconds):
  GET  /api/events                               RDS
  GET  /api/events/{id}                          RDS (event, tiers, section pricing)
  GET  /api/events/{id}/availability             DynamoDB inventory counters
  GET  /api/events/{id}/sections/{sid}/seats     DynamoDB (one partition)
On-sale path (never cached):
  POST /api/waiting-room/{id}/join               DynamoDB sharded counter
  GET  /api/waiting-room/{id}/status             stateless HMAC check
  POST /api/holds                                DynamoDB conditional transaction
  DELETE /api/holds/{id}                         release inventory
  POST /api/orders                               lock hold + SQS FIFO
  GET  /api/orders/{id}                          DynamoDB
"""
import json
import logging
import os
import socket
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator, model_validator

import aws
import database
from http_metrics import LATENCY, REQUESTS
from core import catalog, inventory, keys, metrics, settings, waiting_room

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("frontrow")


def _instance_id():
    try:
        tok = urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=0.5).read().decode()
        return urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": tok}), timeout=0.5).read().decode()
    except Exception:
        return socket.gethostname()


SERVED_BY = os.getenv("SERVED_BY") or _instance_id()
QUEUE_URL = os.getenv("QUEUE_URL", "")

app = FastAPI(title="FrontRow API", version="2.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

@app.middleware("http")
async def observability(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - t0
    route = getattr(request.scope.get("route"), "path", "unmatched") or "/static"  # mounts have path ""
    if route != "/metrics":
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(request.method, route).observe(elapsed)
    response.headers["X-Served-By"] = SERVED_BY
    response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.1f}"
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def cached(response: Response, seconds, backend):
    # CloudFront's cache policy honours origin Cache-Control, so hot reads are
    # absorbed at the edge during a stampede.
    response.headers["Cache-Control"] = f"public, max-age={seconds}"
    response.headers["X-Backend"] = backend


def T():
    return settings.tables()


# ---------------------------------------------------------------- health

@app.get("/api/health")
def health():
    return {"status": "ok", "served_by": SERVED_BY}


@app.get("/api/health/deep")
def deep_health():
    database.query("SELECT 1 AS ok")
    aws.dynamodb().describe_table(TableName=T().seats)
    return {"status": "ok", "rds": "ok", "dynamodb": "ok", "served_by": SERVED_BY}


# ---------------------------------------------------------------- catalogue (RDS)

@app.get("/api/events")
def list_events(response: Response):
    rows = database.query("""
        SELECT e.event_id, e.artist, e.title, e.starts_at, e.currency, e.genre, e.art1, e.art2,
               v.venue_id, v.name AS venue, v.city, v.country, v.tz,
               MIN(t.price) AS from_price,
               (SELECT SUM(capacity) FROM event_sections s WHERE s.event_id = e.event_id AND s.on_sale = 1) AS capacity
        FROM events e
        JOIN venues v ON v.venue_id = e.venue_id
        JOIN price_tiers t ON t.event_id = e.event_id
        GROUP BY e.event_id, e.artist, e.title, e.starts_at, e.currency, e.genre, e.art1, e.art2,
                 v.venue_id, v.name, v.city, v.country, v.tz
        ORDER BY e.starts_at""")
    cached(response, 30, "RDS")
    return {"events": rows}


def _event(event_id):
    rows = database.query("""
        SELECT e.event_id, e.artist, e.title, e.starts_at, e.currency, e.genre, e.art1, e.art2,
               v.venue_id, v.name AS venue, v.city, v.country, v.tz
        FROM events e JOIN venues v ON v.venue_id = e.venue_id WHERE e.event_id = :e""", e=event_id)
    if not rows:
        raise HTTPException(404, "Event not found")
    return rows[0]


@app.get("/api/events/{event_id}")
def get_event(event_id: str, response: Response):
    ev = _event(event_id)
    ev["tiers"] = database.query(
        "SELECT tier, name, price FROM price_tiers WHERE event_id = :e ORDER BY price DESC", e=event_id)
    ev["sections"] = database.query(
        "SELECT section_id, label, kind, tier, on_sale, capacity, rows_count, seats_per_row "
        "FROM event_sections WHERE event_id = :e", e=event_id)
    cached(response, 30, "RDS")
    return ev


# ---------------------------------------------------------------- live inventory (DynamoDB)

@app.get("/api/events/{event_id}/availability")
def get_availability(event_id: str, response: Response):
    if catalog.event(event_id) is None:
        raise HTTPException(404, "Event not found")
    counts = inventory.availability(aws.dynamodb(), T(), event_id)
    cfg = waiting_room.get_config(aws.dynamodb(), T(), event_id)
    cached(response, 2, "DynamoDB")
    return {"event_id": event_id, "available": counts, "total_available": sum(counts.values()),
            "onsale": {"mode": cfg["mode"], "opens_at": cfg["opens_at"]}, "as_of": int(time.time())}


@app.get("/api/events/{event_id}/sections/{section_id}/seats")
def get_section_seats(event_id: str, section_id: str, response: Response):
    sec = catalog.event_sections(event_id).get(section_id)
    if not sec or sec["kind"] != "seated":
        raise HTTPException(404, "Seated section not found")
    seats = inventory.section_seats(aws.dynamodb(), T(), event_id, section_id)
    cached(response, 2, "DynamoDB")
    return {"section_id": section_id, "rows": sec["rows"], "seats_per_row": sec["seats_per_row"],
            "seats": [[sk, st[0]] for sk, st in seats]}  # a=available h=held s=sold


# ---------------------------------------------------------------- waiting room

@app.post("/api/waiting-room/{event_id}/join")
def join_queue(event_id: str):
    if catalog.event(event_id) is None:
        raise HTTPException(404, "Event not found")
    result = waiting_room.join(aws.dynamodb(), T(), aws.token_secret(), event_id)
    metrics.emit("QueueJoins")
    return result


@app.get("/api/waiting-room/{event_id}/status")
def queue_status(event_id: str, ticket: str = Query(..., max_length=2048)):
    return waiting_room.status(aws.dynamodb(), T(), aws.token_secret(), event_id, ticket)


# ---------------------------------------------------------------- holds

class SeatRef(BaseModel):
    section: str = Field(max_length=32)
    seat: str = Field(pattern=r"^R\d{2}#S\d{2}$")


class GARequest(BaseModel):
    section: str = Field(max_length=32)
    qty: int = Field(ge=1, le=settings.MAX_TICKETS)


class HoldRequest(BaseModel):
    event_id: str = Field(max_length=64)
    access_token: Optional[str] = Field(default=None, max_length=2048)
    seats: List[SeatRef] = Field(default_factory=list, max_length=settings.MAX_TICKETS)
    ga: Optional[GARequest] = None

    @model_validator(mode="after")
    def one_kind(self):
        if bool(self.seats) == bool(self.ga):
            raise ValueError("send either seats or ga")
        if len({(s.section, s.seat) for s in self.seats}) != len(self.seats):
            raise ValueError("duplicate seats")
        return self


def _prices(event_id, section_ids):
    rows = database.query(
        "SELECT s.section_id, s.kind, s.tier, s.on_sale, s.rows_count, s.seats_per_row, t.price "
        "FROM event_sections s JOIN price_tiers t ON t.event_id = s.event_id AND t.tier = s.tier "
        "WHERE s.event_id = :e", e=event_id)
    return {r["section_id"]: r for r in rows if r["section_id"] in section_ids}


@app.post("/api/holds", status_code=201)
def create_hold(req: HoldRequest):
    ev = _event(req.event_id)
    ddb, t = aws.dynamodb(), T()
    if not waiting_room.check_access(ddb, t, aws.token_secret(), req.event_id, req.access_token):
        metrics.emit("HoldsRejectedNoAccess")
        raise HTTPException(403, {"message": "Join the waiting room first", "code": "WAITING_ROOM"})

    wanted = [s.section for s in req.seats] if req.seats else [req.ga.section]
    sections = _prices(req.event_id, set(wanted))
    for sid in wanted:
        sec = sections.get(sid)
        if not sec or not sec["on_sale"]:
            raise HTTPException(400, {"message": f"Section {sid} is not on sale"})

    try:
        if req.seats:
            refs = []
            for s in req.seats:
                sec = sections[s.section]
                row, num = keys.parse_seat_sk(s.seat)
                if sec["kind"] != "seated" or row > sec["rows_count"] or num > sec["seats_per_row"]:
                    raise HTTPException(400, {"message": f"No such seat {s.section} {s.seat}"})
                refs.append((s.section, s.seat))
            lines = {}
            for sid, _ in refs:
                sec = sections[sid]
                line = lines.setdefault(sid, {"section": sid, "tier": sec["tier"], "qty": 0, "price": sec["price"]})
                line["qty"] += 1
            lines = list(lines.values())
            total = sum(l["qty"] * l["price"] for l in lines)
            hold = inventory.hold_seats(ddb, t, req.event_id, refs, lines, total, ev["currency"])
        else:
            sec = sections[req.ga.section]
            if sec["kind"] != "ga":
                raise HTTPException(400, {"message": "Pick seats for a seated section"})
            lines = [{"section": req.ga.section, "tier": sec["tier"], "qty": req.ga.qty, "price": sec["price"]}]
            total = req.ga.qty * sec["price"]
            hold = inventory.hold_ga(ddb, t, req.event_id, req.ga.section, req.ga.qty, lines, total, ev["currency"])
    except inventory.SeatsUnavailable as e:
        metrics.emit("HoldConflicts")
        raise HTTPException(409, {"message": "Someone just grabbed some of those seats", "seats": e.seats})
    except inventory.SoldOut:
        metrics.emit("HoldConflicts")
        raise HTTPException(409, {"message": "That area just sold out"})
    except inventory.Retryable:
        raise HTTPException(503, {"message": "Busy, please retry"})

    metrics.emit("HoldsCreated")
    return {**hold, "lines": lines, "total": total, "currency": ev["currency"], "served_by": SERVED_BY}


@app.get("/api/holds/{hold_id}")
def get_hold(hold_id: str):
    hold = inventory.get_hold(aws.dynamodb(), T(), hold_id)
    if not hold:
        raise HTTPException(404, "Hold not found")
    return hold


@app.delete("/api/holds/{hold_id}")
def cancel_hold(hold_id: str):
    ddb, t = aws.dynamodb(), T()
    hold = inventory.get_hold(ddb, t, hold_id)
    if not hold:
        raise HTTPException(404, "Hold not found")
    released = inventory.release(ddb, t, hold, "released", ["active"])
    if released:
        metrics.emit("HoldsReleased", reason="cancelled")
    return {"hold_id": hold_id, "released": released}


# ---------------------------------------------------------------- orders

class OrderRequest(BaseModel):
    hold_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    payment: str = Field(default="simulated_success", pattern=r"^simulated_(success|decline)$")


@app.post("/api/orders", status_code=202)
def create_order(req: OrderRequest):
    ddb, t = aws.dynamodb(), T()
    hold = inventory.get_hold(ddb, t, req.hold_id)
    if not hold:
        raise HTTPException(404, "Hold not found")
    order_id = inventory.new_id()
    try:
        inventory.begin_checkout(ddb, t, hold, order_id)
    except inventory.HoldNotActive:
        raise HTTPException(410, {"message": "Your hold expired - pick your seats again", "code": "HOLD_EXPIRED"})

    ev = catalog.event(hold["event_id"])
    venue = catalog.venue(ev["venue_id"])
    aws.sqs().send_message(
        QueueUrl=QUEUE_URL,
        MessageGroupId=hold["hold_id"],
        MessageDeduplicationId=order_id,
        MessageBody=json.dumps({
            "order_id": order_id, "hold_id": hold["hold_id"], "email": req.email, "payment": req.payment,
            "event_id": hold["event_id"], "artist": ev["artist"], "title": ev["title"],
            "venue": venue["name"], "city": venue["city"], "starts_at": ev["starts_at"],
            "submitted_at": int(time.time()),
        }))
    metrics.emit("CheckoutsStarted")
    return {"order_id": order_id, "status": "PENDING", "served_by": SERVED_BY}


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    item = aws.dynamodb().get_item(TableName=T().orders, Key={"order_id": {"S": order_id}},
                                   ConsistentRead=True).get("Item")
    if not item:
        return {"order_id": order_id, "status": "PENDING"}
    return {
        "order_id": order_id,
        "status": item["status"]["S"],
        "reason": item.get("reason", {}).get("S"),
        "ticket_code": item.get("ticket_code", {}).get("S"),
        "total": int(item["total"]["N"]),
        "currency": item["currency"]["S"],
        "seats": [s["S"] for s in item.get("seats", {}).get("L", [])],
        "lines": [{"section": l["M"]["section"]["S"], "tier": l["M"]["tier"]["S"],
                   "qty": int(l["M"]["qty"]["N"])} for l in item.get("lines", {}).get("L", [])],
    }


@app.exception_handler(ClientError)
def aws_error(request, exc):
    code = exc.response["Error"]["Code"]
    log.exception("AWS error %s on %s", code, request.url.path)
    status = 503 if "Throttl" in code or code == "ProvisionedThroughputExceededException" else 500
    return JSONResponse({"detail": {"message": "Temporary problem, please retry", "code": code}}, status_code=status)


# ---------------------------------------------------------------- static site
# With CloudFront the site comes from S3 and only /api/* reaches the pods.
# Without it (Terraform cloudfront_enabled = false) the ALB sends everything
# here, so the pods also serve the site, venue maps and runtime config.
# Registered last, so every route above takes precedence.
def _first_dir(name: str) -> Optional[Path]:
    here = Path(__file__).resolve().parent
    return next((d for d in (here / name, here.parent / name) if d.is_dir()), None)


@app.get("/config.json", include_in_schema=False)
def site_config():
    return {"ws_url": os.getenv("WS_URL", ""), "region": settings.REGION}


if (_venues := _first_dir("venues")) is not None:
    app.mount("/venues", StaticFiles(directory=_venues), name="venues")
if (_site := _first_dir("frontend")) is not None:
    app.mount("/", StaticFiles(directory=_site, html=True), name="site")

