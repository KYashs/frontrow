"""DynamoDB key design.

seats      pk = "<event>#<section>"   sk = "R05#S12"
           Partitioning by event+section (not just event) spreads an on-sale
           across ~100-200 partition keys instead of one hot key.
inventory  pk = event_id              sk = "SEC#<section>" (seated count, derived)
                                       sk = "GA#<section>#<shard>" (authoritative GA counters)
holds      pk = hold_id               GSI by_status: status + expires_at (sweeper)
orders     pk = order_id
waiting    pk = event_id              sk = "CONFIG" | "Q#<shard>"
"""
import re

SEAT_SK_RE = re.compile(r"^R(\d{2})#S(\d{2})$")


def seat_pk(event_id, section_id):
    return f"{event_id}#{section_id}"


def split_seat_pk(pk):
    event_id, section_id = pk.rsplit("#", 1)
    return event_id, section_id


def seat_sk(row, num):
    return f"R{int(row):02d}#S{int(num):02d}"


def parse_seat_sk(sk):
    m = SEAT_SK_RE.match(sk)
    if not m:
        raise ValueError(f"bad seat key {sk!r}")
    return int(m.group(1)), int(m.group(2))


def section_sk(section_id):
    return f"SEC#{section_id}"


def ga_sk(section_id, shard):
    return f"GA#{section_id}#{shard}"


def parse_inventory_sk(sk):
    """-> (kind, section_id). kind is 'seated' or 'ga'."""
    if sk.startswith("SEC#"):
        return "seated", sk[4:]
    if sk.startswith("GA#"):
        return "ga", sk[3:].rsplit("#", 1)[0]
    return None, None


def shard_capacities(capacity, shards):
    base, extra = divmod(capacity, shards)
    return [base + (1 if i < extra else 0) for i in range(shards)]
