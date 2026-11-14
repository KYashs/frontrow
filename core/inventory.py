"""Seat and GA inventory: the hold -> checkout -> confirm / release lifecycle.

Every state change is a DynamoDB conditional write, so correctness never
depends on timing, locks, or which server handled the request.

    seat:  available --hold--> held --confirm--> sold
                 ^               |
                 +---release-----+   (also: an expired hold counts as available)

    hold:  active --checkout--> checkout --confirm--> confirmed
             |                     |
             +----> expired / released / failed (inventory returned)
"""
import random
import time
import uuid

from botocore.exceptions import ClientError

from core import keys, settings


class SeatsUnavailable(Exception):
    def __init__(self, seats):
        super().__init__(f"seats unavailable: {seats}")
        self.seats = seats


class SoldOut(Exception):
    pass


class HoldNotActive(Exception):
    pass


class Retryable(Exception):
    """Contention that is not a business answer (e.g. TransactionConflict)."""


def _codes(err):
    return [r.get("Code", "None") for r in err.response.get("CancellationReasons", [])]


def _is_cancel(err):
    return err.response["Error"]["Code"] == "TransactionCanceledException"


def _check_retryable(codes):
    if any(c not in ("None", "ConditionalCheckFailed") for c in codes):
        raise Retryable(",".join(codes))


def new_id():
    return uuid.uuid4().hex


# ---------------------------------------------------------------- holds

def hold_seats(ddb, t, event_id, seats, lines, total, currency, now=None, hold_id=None):
    """Atomically hold specific seats. `seats` = [(section_id, seat_sk), ...]."""
    now = int(now or time.time())
    hold_id = hold_id or new_id()
    expires = now + settings.HOLD_SECONDS
    items = []
    for section_id, sk in seats:
        items.append({"Update": {
            "TableName": t.seats,
            "Key": {"pk": {"S": keys.seat_pk(event_id, section_id)}, "sk": {"S": sk}},
            "UpdateExpression": "SET #st = :held, hold_id = :hid, hold_expires = :exp",
            "ConditionExpression": "#st = :available OR (#st = :held AND hold_expires < :now)",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {
                ":held": {"S": "held"}, ":available": {"S": "available"},
                ":hid": {"S": hold_id}, ":exp": {"N": str(expires)}, ":now": {"N": str(now)},
            },
        }})
    items.append({"Put": {
        "TableName": t.holds,
        "Item": _hold_item(hold_id, event_id, "seated", lines, total, currency, now, expires,
                           seats=[f"{s}|{k}" for s, k in seats], qty=len(seats)),
        "ConditionExpression": "attribute_not_exists(hold_id)",
    }})
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as err:
        if not _is_cancel(err):
            raise
        codes = _codes(err)
        _check_retryable(codes)
        taken = [f"{seats[i][0]}|{seats[i][1]}" for i, c in enumerate(codes[: len(seats)])
                 if c == "ConditionalCheckFailed"]
        raise SeatsUnavailable(taken or [f"{s}|{k}" for s, k in seats])
    return {"hold_id": hold_id, "expires_at": expires}


def hold_ga(ddb, t, event_id, section_id, qty, lines, total, currency, now=None, hold_id=None):
    """Hold `qty` general-admission places from one of the zone's sharded counters.

    A single counter item caps out around 1,000 writes/s and serialises every
    buyer of a 15,000-capacity field. Splitting it into N shards spreads the
    writes; we try shards in random order until one has room.
    """
    now = int(now or time.time())
    hold_id = hold_id or new_id()
    expires = now + settings.HOLD_SECONDS
    shards = list(range(settings.GA_SHARDS))
    random.shuffle(shards)
    for shard in shards:
        items = [
            {"Update": {
                "TableName": t.inventory,
                "Key": {"pk": {"S": event_id}, "sk": {"S": keys.ga_sk(section_id, shard)}},
                "UpdateExpression": "SET available = available - :q",
                "ConditionExpression": "available >= :q",
                "ExpressionAttributeValues": {":q": {"N": str(qty)}},
            }},
            {"Put": {
                "TableName": t.holds,
                "Item": _hold_item(hold_id, event_id, "ga", lines, total, currency, now, expires,
                                   ga_section=section_id, ga_shard=shard, qty=qty),
                "ConditionExpression": "attribute_not_exists(hold_id)",
            }},
        ]
        try:
            ddb.transact_write_items(TransactItems=items)
            return {"hold_id": hold_id, "expires_at": expires, "shard": shard}
        except ClientError as err:
            if not _is_cancel(err):
                raise
            codes = _codes(err)
            _check_retryable(codes)
            if codes and codes[0] == "ConditionalCheckFailed":
                continue  # this shard is out, try the next one
            raise
    raise SoldOut(section_id)


def _hold_item(hold_id, event_id, kind, lines, total, currency, now, expires,
               seats=None, ga_section=None, ga_shard=None, qty=0):
    item = {
        "hold_id": {"S": hold_id},
        "event_id": {"S": event_id},
        "kind": {"S": kind},
        "status": {"S": "active"},
        "qty": {"N": str(qty)},
        "total": {"N": str(total)},
        "currency": {"S": currency},
        "lines": {"L": [{"M": {"section": {"S": l["section"]}, "tier": {"S": l["tier"]},
                               "qty": {"N": str(l["qty"])}, "price": {"N": str(l["price"])}}}
                        for l in lines]},
        "created_at": {"N": str(now)},
        "expires_at": {"N": str(expires)},
        "ttl": {"N": str(expires + 86400)},  # DynamoDB TTL tidies old holds after a day
    }
    if seats:
        item["seats"] = {"L": [{"S": s} for s in seats]}
    if ga_section is not None:
        item["ga_section"] = {"S": ga_section}
        item["ga_shard"] = {"N": str(ga_shard)}
    return item


def parse_hold(item):
    if not item:
        return None
    return {
        "hold_id": item["hold_id"]["S"],
        "event_id": item["event_id"]["S"],
        "kind": item["kind"]["S"],
        "status": item["status"]["S"],
        "qty": int(item["qty"]["N"]),
        "total": int(item["total"]["N"]),
        "currency": item["currency"]["S"],
        "lines": [{"section": l["M"]["section"]["S"], "tier": l["M"]["tier"]["S"],
                   "qty": int(l["M"]["qty"]["N"]), "price": int(l["M"]["price"]["N"])}
                  for l in item.get("lines", {}).get("L", [])],
        "seats": [s["S"] for s in item.get("seats", {}).get("L", [])],
        "ga_section": item.get("ga_section", {}).get("S"),
        "ga_shard": int(item["ga_shard"]["N"]) if "ga_shard" in item else None,
        "expires_at": int(item["expires_at"]["N"]),
        "order_id": item.get("order_id", {}).get("S"),
    }


def get_hold(ddb, t, hold_id):
    resp = ddb.get_item(TableName=t.holds, Key={"hold_id": {"S": hold_id}}, ConsistentRead=True)
    return parse_hold(resp.get("Item"))


def _seat_key(event_id, seat):
    section_id, sk = seat.split("|", 1)
    return {"pk": {"S": keys.seat_pk(event_id, section_id)}, "sk": {"S": sk}}


# ---------------------------------------------------------------- checkout

def begin_checkout(ddb, t, hold, order_id, now=None):
    """Lock the hold for payment: active -> checkout, and push every seat's
    expiry out by the checkout grace period so nobody can grab it while the
    payment is in flight."""
    now = int(now or time.time())
    extended = now + settings.CHECKOUT_GRACE_SECONDS
    items = [{"Update": {
        "TableName": t.holds,
        "Key": {"hold_id": {"S": hold["hold_id"]}},
        "UpdateExpression": "SET #st = :checkout, expires_at = :ext, order_id = :oid",
        "ConditionExpression": "#st = :active AND expires_at > :now",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":checkout": {"S": "checkout"}, ":active": {"S": "active"},
                                      ":ext": {"N": str(extended)}, ":now": {"N": str(now)},
                                      ":oid": {"S": order_id}},
    }}]
    for seat in hold["seats"]:
        items.append({"Update": {
            "TableName": t.seats,
            "Key": _seat_key(hold["event_id"], seat),
            "UpdateExpression": "SET hold_expires = :ext",
            "ConditionExpression": "hold_id = :hid AND #st = :held",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":ext": {"N": str(extended)}, ":hid": {"S": hold["hold_id"]},
                                          ":held": {"S": "held"}},
        }})
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as err:
        if not _is_cancel(err):
            raise
        _check_retryable(_codes(err))
        raise HoldNotActive(hold["hold_id"])
    return extended


def confirm(ddb, t, hold, order_item, now=None):
    """held -> sold for every seat, hold -> confirmed, order written: one transaction.
    Returns False if the order already exists (idempotent redelivery)."""
    now = int(now or time.time())
    items = []
    for seat in hold["seats"]:
        items.append({"Update": {
            "TableName": t.seats,
            "Key": _seat_key(hold["event_id"], seat),
            "UpdateExpression": "SET #st = :sold, order_id = :oid, sold_at = :now REMOVE hold_expires",
            "ConditionExpression": "hold_id = :hid AND #st = :held",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":sold": {"S": "sold"}, ":held": {"S": "held"},
                                          ":hid": {"S": hold["hold_id"]},
                                          ":oid": order_item["order_id"], ":now": {"N": str(now)}},
        }})
    items.append({"Update": {
        "TableName": t.holds,
        "Key": {"hold_id": {"S": hold["hold_id"]}},
        "UpdateExpression": "SET #st = :confirmed",
        "ConditionExpression": "#st = :checkout",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":confirmed": {"S": "confirmed"}, ":checkout": {"S": "checkout"}},
    }})
    items.append({"Put": {"TableName": t.orders, "Item": order_item,
                          "ConditionExpression": "attribute_not_exists(order_id)"}})
    try:
        ddb.transact_write_items(TransactItems=items,
                                 ClientRequestToken=order_item["order_id"]["S"][:36])
        return True
    except ClientError as err:
        if not _is_cancel(err):
            raise
        _check_retryable(_codes(err))
        existing = ddb.get_item(TableName=t.orders, Key={"order_id": order_item["order_id"]},
                                ConsistentRead=True).get("Item")
        if existing:
            return False
        raise HoldNotActive(hold["hold_id"])


def release(ddb, t, hold, new_status, from_statuses, now=None, only_if_expired=False):
    """Return a hold's inventory. Claiming the hold (status change) and the GA
    counter refund happen in one transaction, so a refund can never be applied
    twice. Seats are then freed one by one, conditional on still belonging to
    this hold (a seat someone re-held after expiry is left alone)."""
    now = int(now or time.time())
    names = {"#st": "status"}
    values = {":new": {"S": new_status}}
    conds = []
    for i, s in enumerate(from_statuses):
        values[f":f{i}"] = {"S": s}
        conds.append(f"#st = :f{i}")
    cond = "(" + " OR ".join(conds) + ")"
    if only_if_expired:
        cond += " AND expires_at < :now"
        values[":now"] = {"N": str(now)}
    items = [{"Update": {
        "TableName": t.holds, "Key": {"hold_id": {"S": hold["hold_id"]}},
        "UpdateExpression": "SET #st = :new", "ConditionExpression": cond,
        "ExpressionAttributeNames": names, "ExpressionAttributeValues": values,
    }}]
    if hold["kind"] == "ga":
        items.append({"Update": {
            "TableName": t.inventory,
            "Key": {"pk": {"S": hold["event_id"]},
                    "sk": {"S": keys.ga_sk(hold["ga_section"], hold["ga_shard"])}},
            "UpdateExpression": "ADD available :q",
            "ExpressionAttributeValues": {":q": {"N": str(hold["qty"])}},
        }})
    try:
        ddb.transact_write_items(TransactItems=items)
    except ClientError as err:
        if not _is_cancel(err):
            raise
        _check_retryable(_codes(err))
        return False  # someone else already moved this hold on
    for seat in hold["seats"]:
        try:
            ddb.update_item(
                TableName=t.seats, Key=_seat_key(hold["event_id"], seat),
                UpdateExpression="SET #st = :available REMOVE hold_id, hold_expires",
                ConditionExpression="hold_id = :hid AND #st = :held",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":available": {"S": "available"}, ":held": {"S": "held"},
                                           ":hid": {"S": hold["hold_id"]}})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    return True


# ---------------------------------------------------------------- reads

def section_seats(ddb, t, event_id, section_id, now=None):
    """Seat statuses for one section (a single-partition Query)."""
    now = int(now or time.time())
    out = []
    for page in ddb.get_paginator("query").paginate(
        TableName=t.seats,
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": keys.seat_pk(event_id, section_id)}},
        ProjectionExpression="sk, #st, hold_expires",
        ExpressionAttributeNames={"#st": "status"},
    ):
        for it in page["Items"]:
            status = it["status"]["S"]
            if status == "held" and int(it.get("hold_expires", {}).get("N", "0")) < now:
                status = "available"  # expired hold, not yet swept
            out.append((it["sk"]["S"], status))
    return out


def availability(ddb, t, event_id):
    """{section_id: available} from the inventory table (seated counts are
    maintained by the stream processor; GA counts are the sum of shards)."""
    totals = {}
    for page in ddb.get_paginator("query").paginate(
        TableName=t.inventory, KeyConditionExpression="pk = :e",
        ExpressionAttributeValues={":e": {"S": event_id}},
    ):
        for it in page["Items"]:
            kind, section = keys.parse_inventory_sk(it["sk"]["S"])
            if kind:
                totals[section] = totals.get(section, 0) + int(it.get("available", {}).get("N", "0"))
    return totals
