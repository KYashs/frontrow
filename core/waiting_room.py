"""Virtual waiting room for an on-sale.

Joining takes a numbered ticket from one of N sharded counters (so joins
aren't throttled on a single hot item). Admission needs no worker at all:

    admitted_upto = initial + rate_per_sec * (now - opens_at)

so the gate opens at a constant, configured rate that matches what the
hold/checkout path can absorb. Tickets and access passes are HMAC-signed,
so checking "is it my turn?" costs one config read (cached) and no writes.
"""
import random
import time

from core import settings, tokens

_CONFIG_CACHE = {}
_CACHE_SECONDS = 5


def get_config(ddb, t, event_id, now=None):
    now = now or time.time()
    hit = _CONFIG_CACHE.get(event_id)
    if hit and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    item = ddb.get_item(TableName=t.waiting,
                        Key={"pk": {"S": event_id}, "sk": {"S": "CONFIG"}}).get("Item")
    cfg = {"mode": "open", "opens_at": 0, "rate": 0, "initial": 0, "epoch": "0"}
    if item:
        cfg = {
            "mode": item["mode"]["S"],
            "opens_at": int(item["opens_at"]["N"]),
            "rate": float(item["rate"]["N"]),
            "initial": int(item["initial"]["N"]),
            "epoch": item["epoch"]["S"],
        }
    _CONFIG_CACHE[event_id] = (now, cfg)
    return cfg


def clear_cache():
    _CONFIG_CACHE.clear()


def admitted_upto(cfg, now):
    if cfg["mode"] == "open":
        return float("inf")
    if now < cfg["opens_at"]:
        return 0
    return cfg["initial"] + cfg["rate"] * (now - cfg["opens_at"])


def join(ddb, t, secret, event_id, now=None):
    now = now or time.time()
    cfg = get_config(ddb, t, event_id, now)
    shard = random.randrange(settings.QUEUE_SHARDS)
    resp = ddb.update_item(
        TableName=t.waiting, Key={"pk": {"S": event_id}, "sk": {"S": f"Q#{shard}"}},
        UpdateExpression="ADD n :one", ExpressionAttributeValues={":one": {"N": "1"}},
        ReturnValues="UPDATED_NEW")
    n = int(resp["Attributes"]["n"]["N"])
    position = (n - 1) * settings.QUEUE_SHARDS + shard + 1
    ticket = tokens.sign(secret, {"k": "ticket", "e": event_id, "ep": cfg["epoch"], "p": position,
                                  "exp": int(now) + 6 * 3600})
    return {"ticket": ticket, "position": position, **status(ddb, t, secret, event_id, ticket, now)}


def status(ddb, t, secret, event_id, ticket, now=None):
    now = now or time.time()
    cfg = get_config(ddb, t, event_id, now)
    payload = tokens.verify(secret, ticket, kind="ticket", now=now)
    if not payload or payload["e"] != event_id or payload["ep"] != cfg["epoch"]:
        return {"state": "invalid"}
    position = payload["p"]
    upto = admitted_upto(cfg, now)
    if position <= upto:
        return {"state": "admitted", "position": position,
                "access_token": access_token(secret, event_id, cfg["epoch"], now)}
    ahead = int(position - max(upto, 0))
    if now < cfg["opens_at"]:
        eta = (cfg["opens_at"] - now) + max(0, position - cfg["initial"]) / max(cfg["rate"], 1)
        state = "before_open"
    else:
        eta = ahead / max(cfg["rate"], 1)
        state = "waiting"
    return {"state": state, "position": position, "ahead": ahead, "eta_seconds": int(eta),
            "opens_at": cfg["opens_at"]}


def access_token(secret, event_id, epoch, now):
    return tokens.sign(secret, {"k": "access", "e": event_id, "ep": epoch,
                                "exp": int(now) + settings.ACCESS_TOKEN_SECONDS})


def check_access(ddb, t, secret, event_id, token, now=None):
    now = now or time.time()
    cfg = get_config(ddb, t, event_id, now)
    if cfg["mode"] == "open":
        return True
    payload = tokens.verify(secret, token or "", kind="access", now=now)
    return bool(payload and payload["e"] == event_id and payload["ep"] == cfg["epoch"])
