"""The rules that make an on-sale safe."""
import time

import pytest

from conftest import EVENT

LINES = [{"section": "101", "tier": "L100", "qty": 1, "price": 2000}]


def inv():
    from core import inventory
    return inventory


def seat_status(ddb, t, section, sk):
    item = ddb.get_item(TableName=t.seats, Key={"pk": {"S": f"{EVENT}#{section}"}, "sk": {"S": sk}})["Item"]
    return item["status"]["S"]


def test_seeder_respects_end_stage_and_shards(aws, t):
    from core import inventory
    counts = inventory.availability(aws["ddb"], t, EVENT)
    assert counts == {"101": 12, "PIT": 40}  # 102 is behind the stage: never on sale
    shards = aws["ddb"].query(TableName=t.inventory, KeyConditionExpression="pk = :e AND begins_with(sk, :g)",
                              ExpressionAttributeValues={":e": {"S": EVENT}, ":g": {"S": "GA#PIT#"}})["Items"]
    assert sorted(int(s["available"]["N"]) for s in shards) == [10, 10, 10, 10]


def test_only_one_buyer_can_hold_a_seat(aws, t):
    i = inv()
    winners, losers = 0, 0
    for _ in range(25):
        try:
            i.hold_seats(aws["ddb"], t, EVENT, [("101", "R01#S01")], LINES, 2000, "INR")
            winners += 1
        except i.SeatsUnavailable as e:
            assert e.seats == ["101|R01#S01"]
            losers += 1
    assert (winners, losers) == (1, 24)


def test_multi_seat_hold_is_all_or_nothing(aws, t):
    i = inv()
    i.hold_seats(aws["ddb"], t, EVENT, [("101", "R02#S02")], LINES, 2000, "INR")
    with pytest.raises(i.SeatsUnavailable) as e:
        i.hold_seats(aws["ddb"], t, EVENT, [("101", "R02#S01"), ("101", "R02#S02")], LINES * 2, 4000, "INR")
    assert e.value.seats == ["101|R02#S02"]
    assert seat_status(aws["ddb"], t, "101", "R02#S01") == "available"


def test_expired_hold_can_be_taken_over(aws, t):
    i = inv()
    old = i.hold_seats(aws["ddb"], t, EVENT, [("101", "R03#S01")], LINES, 2000, "INR", now=1000)
    new = i.hold_seats(aws["ddb"], t, EVENT, [("101", "R03#S01")], LINES, 2000, "INR", now=int(time.time()))
    assert old["hold_id"] != new["hold_id"]
    # the stale hold can no longer check out
    with pytest.raises(i.HoldNotActive):
        i.begin_checkout(aws["ddb"], t, i.get_hold(aws["ddb"], t, old["hold_id"]), "x" * 32, now=1001)


def test_ga_sharded_counters_sell_exactly_capacity(aws, t):
    i = inv()
    lines = [{"section": "PIT", "tier": "PIT", "qty": 2, "price": 5000}]
    sold = 0
    while True:
        try:
            i.hold_ga(aws["ddb"], t, EVENT, "PIT", 2, lines, 10000, "INR")
            sold += 2
        except i.SoldOut:
            break
    assert sold == 40
    assert i.availability(aws["ddb"], t, EVENT)["PIT"] == 0


def test_release_returns_ga_inventory_once(aws, t):
    i = inv()
    lines = [{"section": "PIT", "tier": "PIT", "qty": 3, "price": 5000}]
    h = i.hold_ga(aws["ddb"], t, EVENT, "PIT", 3, lines, 15000, "INR")
    hold = i.get_hold(aws["ddb"], t, h["hold_id"])
    assert i.release(aws["ddb"], t, hold, "released", ["active"]) is True
    assert i.release(aws["ddb"], t, hold, "released", ["active"]) is False  # no double refund
    assert i.availability(aws["ddb"], t, EVENT)["PIT"] == 40


def test_checkout_and_confirm_flow(aws, t):
    i = inv()
    h = i.hold_seats(aws["ddb"], t, EVENT, [("101", "R01#S03"), ("101", "R01#S04")], LINES * 2, 4000, "INR")
    hold = i.get_hold(aws["ddb"], t, h["hold_id"])
    i.begin_checkout(aws["ddb"], t, hold, "a" * 32)
    hold = i.get_hold(aws["ddb"], t, h["hold_id"])
    assert hold["status"] == "checkout"
    order = {"order_id": {"S": "a" * 32}, "status": {"S": "CONFIRMED"}}
    assert i.confirm(aws["ddb"], t, hold, order) is True
    assert i.confirm(aws["ddb"], t, hold, order) is False  # redelivery is harmless
    assert seat_status(aws["ddb"], t, "101", "R01#S04") == "sold"


def test_sweeper_releases_only_expired_holds(aws, t):
    from conftest import load_function
    i = inv()
    fresh = i.hold_seats(aws["ddb"], t, EVENT, [("101", "R03#S03")], LINES, 2000, "INR")
    stale = i.hold_ga(aws["ddb"], t, EVENT, "PIT", 4, [{"section": "PIT", "tier": "PIT", "qty": 4, "price": 5000}],
                      20000, "INR", now=int(time.time()) - 3600)
    out = load_function("hold_sweeper").handler({}, None)
    assert out == {"released": 1}
    assert i.get_hold(aws["ddb"], t, stale["hold_id"])["status"] == "expired"
    assert i.get_hold(aws["ddb"], t, fresh["hold_id"])["status"] == "active"
    assert i.availability(aws["ddb"], t, EVENT)["PIT"] == 40
