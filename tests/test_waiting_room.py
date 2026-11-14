"""Waiting room admission is a pure function of time and position."""
from conftest import EVENT

SECRET = "test-secret"


def configure(ddb, t, mode="queue", opens_at=1000, rate=10, initial=5, epoch="e1"):
    from core import waiting_room
    ddb.put_item(TableName=t.waiting, Item={
        "pk": {"S": EVENT}, "sk": {"S": "CONFIG"}, "mode": {"S": mode}, "opens_at": {"N": str(opens_at)},
        "rate": {"N": str(rate)}, "initial": {"N": str(initial)}, "epoch": {"S": epoch}})
    waiting_room.clear_cache()


def test_positions_are_unique_across_shards(aws, t):
    from core import waiting_room
    configure(aws["ddb"], t)
    positions = [waiting_room.join(aws["ddb"], t, SECRET, EVENT, now=900)["position"] for _ in range(60)]
    assert len(set(positions)) == 60


def test_admission_opens_at_the_configured_rate(aws, t):
    from core import waiting_room
    configure(aws["ddb"], t, opens_at=1000, rate=10, initial=5)
    ticket = waiting_room.tokens.sign(SECRET, {"k": "ticket", "e": EVENT, "ep": "e1", "p": 55})
    assert waiting_room.status(aws["ddb"], t, SECRET, EVENT, ticket, now=999)["state"] == "before_open"
    waiting_room.clear_cache()
    mid = waiting_room.status(aws["ddb"], t, SECRET, EVENT, ticket, now=1003)
    assert mid["state"] == "waiting" and mid["ahead"] == 20     # 55 - (5 + 10*3)
    waiting_room.clear_cache()
    done = waiting_room.status(aws["ddb"], t, SECRET, EVENT, ticket, now=1005)  # 5 + 50 = 55
    assert done["state"] == "admitted"
    assert waiting_room.check_access(aws["ddb"], t, SECRET, EVENT, done["access_token"], now=1006)


def test_forged_and_stale_tokens_are_rejected(aws, t):
    from core import waiting_room
    configure(aws["ddb"], t, epoch="e1")
    forged = waiting_room.tokens.sign("not-the-secret", {"k": "access", "e": EVENT, "ep": "e1", "exp": 9e9})
    assert not waiting_room.check_access(aws["ddb"], t, SECRET, EVENT, forged, now=1000)
    old = waiting_room.access_token(SECRET, EVENT, "e0", 1000)  # issued for a previous on-sale
    assert not waiting_room.check_access(aws["ddb"], t, SECRET, EVENT, old, now=1000)
    assert not waiting_room.check_access(aws["ddb"], t, SECRET, EVENT, None, now=1000)
