"""Test harness: moto fakes every AWS service, sqlite stands in for RDS, and a
tiny two-section venue keeps seeding instant."""
import importlib
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "app")]

REGION = "ap-south-1"
SENDER = "tickets@example.com"
EVENT = "test-show-2027"

TINY_VENUE = {
    "venue_id": "tiny", "name": "Tiny Arena", "city": "Testville", "country": "IN", "tz": "Asia/Kolkata",
    "viewBox": [0, 0, 100, 100], "decor": [], "capacity": {"seated": 12, "ga": 40, "total": 52},
    "sections": [
        {"id": "101", "label": "101", "level": "L100", "kind": "seated", "rows": 3, "seats_per_row": 4,
         "capacity": 12, "d": "M0,0 Z", "lx": 0, "ly": 0, "angle": 180},
        {"id": "102", "label": "102", "level": "L100", "kind": "seated", "rows": 2, "seats_per_row": 2,
         "capacity": 4, "d": "M0,0 Z", "lx": 0, "ly": 0, "angle": 0},
        {"id": "PIT", "label": "Pit", "level": "PIT", "kind": "ga", "capacity": 40,
         "d": "M0,0 Z", "lx": 0, "ly": 0},
    ],
}
TINY_EVENTS = {"events": [{
    "event_id": EVENT, "artist": "Test Artist", "title": "Live", "venue_id": "tiny",
    "starts_at": "2027-01-01T20:00:00+05:30", "currency": "INR", "genre": "Test", "art": ["#000", "#fff"],
    "tiers": [{"tier": "PIT", "name": "Pit", "price": 5000}, {"tier": "L100", "name": "100 Level", "price": 2000}],
    "killed": {"levels": ["L100"], "center": 0, "half_angle": 10},   # section 102 is behind the stage
    "onsale": {"mode": "open", "rate_per_sec": 10, "initial": 5},
}]}

TABLES = {
    "SEATS_TABLE": "t-seats", "INVENTORY_TABLE": "t-inventory", "HOLDS_TABLE": "t-holds",
    "ORDERS_TABLE": "t-orders", "WAITING_TABLE": "t-waiting", "CONNECTIONS_TABLE": "t-conns",
}


def _create_tables(ddb):
    def table(name, hash_, range_=None, gsi=None, stream=False):
        attrs = {hash_[0]: hash_[1]}
        schema = [{"AttributeName": hash_[0], "KeyType": "HASH"}]
        if range_:
            attrs[range_[0]] = range_[1]
            schema.append({"AttributeName": range_[0], "KeyType": "RANGE"})
        kwargs = {}
        if gsi:
            gname, gh, gr = gsi
            attrs[gh[0]] = gh[1]
            gschema = [{"AttributeName": gh[0], "KeyType": "HASH"}]
            if gr:
                attrs[gr[0]] = gr[1]
                gschema.append({"AttributeName": gr[0], "KeyType": "RANGE"})
            kwargs["GlobalSecondaryIndexes"] = [{"IndexName": gname, "KeySchema": gschema,
                                                 "Projection": {"ProjectionType": "ALL"}}]
        if stream:
            kwargs["StreamSpecification"] = {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"}
        ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": k, "AttributeType": v} for k, v in attrs.items()],
                         KeySchema=schema, **kwargs)

    table("t-seats", ("pk", "S"), ("sk", "S"), stream=True)
    table("t-inventory", ("pk", "S"), ("sk", "S"), stream=True)
    table("t-holds", ("hold_id", "S"), gsi=("by_status", ("status", "S"), ("expires_at", "N")))
    table("t-orders", ("order_id", "S"))
    table("t-waiting", ("pk", "S"), ("sk", "S"))
    table("t-conns", ("connection_id", "S"), gsi=("by_event", ("event_id", "S"), None))


def reload_core():
    import core.settings, core.catalog, core.keys, core.inventory, core.waiting_room  # noqa
    for m in (core.settings, core.catalog, core.keys, core.inventory, core.waiting_room):
        importlib.reload(m)
    core.waiting_room.clear_cache()


def load_function(name):
    path = os.path.join(ROOT, "functions", name, "handler.py")
    spec = importlib.util.spec_from_file_location(f"fn_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def aws(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "catalog").mkdir(parents=True)
    (data / "venues").mkdir()
    (data / "catalog" / "events.json").write_text(json.dumps(TINY_EVENTS))
    (data / "venues" / "tiny.json").write_text(json.dumps(TINY_VENUE))
    env = {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "CATALOG_DIR": str(data),
        "SENDER_EMAIL": SENDER, "SERVED_BY": "i-test", "TOKEN_SECRET": "test-secret",
        "DATABASE_URL": f"sqlite:///{tmp_path}/catalogue.db", "EVENT_BUS": "frontrow-test",
        "GA_SHARDS": "4", "HOLD_SECONDS": "480", **TABLES,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        _create_tables(ddb)
        boto3.client("ses", region_name=REGION).verify_email_identity(EmailAddress=SENDER)
        boto3.client("events", region_name=REGION).create_event_bus(Name="frontrow-test")
        q = boto3.client("sqs", region_name=REGION).create_queue(
            QueueName="t-checkout.fifo", Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"})
        monkeypatch.setenv("QUEUE_URL", q["QueueUrl"])
        reload_core()
        seeder = load_function("inventory_seeder")
        seeder.handler({}, None)
        yield {"ddb": ddb, "queue_url": q["QueueUrl"]}


@pytest.fixture
def t(aws):
    from core import settings
    return settings.tables()


@pytest.fixture
def api(aws):
    import aws as aws_mod, database, seed_catalog, main  # noqa: E401
    for m in (aws_mod, database, seed_catalog):
        importlib.reload(m)
    aws_mod.reset()
    database.reset()
    seed_catalog.seed()
    main = importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)
