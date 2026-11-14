"""Table names and tunables, read from the environment."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Tables:
    seats: str
    inventory: str
    holds: str
    orders: str
    waiting: str
    connections: str


def tables():
    p = os.getenv("TABLE_PREFIX", "frontrow")
    return Tables(
        seats=os.getenv("SEATS_TABLE", f"{p}-seats"),
        inventory=os.getenv("INVENTORY_TABLE", f"{p}-inventory"),
        holds=os.getenv("HOLDS_TABLE", f"{p}-holds"),
        orders=os.getenv("ORDERS_TABLE", f"{p}-orders"),
        waiting=os.getenv("WAITING_TABLE", f"{p}-waiting-room"),
        connections=os.getenv("CONNECTIONS_TABLE", f"{p}-ws-connections"),
    )


REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "ap-south-1"))
HOLD_SECONDS = int(os.getenv("HOLD_SECONDS", "480"))          # 8 minutes to pay
CHECKOUT_GRACE_SECONDS = int(os.getenv("CHECKOUT_GRACE_SECONDS", "300"))
MAX_TICKETS = int(os.getenv("MAX_TICKETS_PER_ORDER", "8"))
GA_SHARDS = int(os.getenv("GA_SHARDS", "8"))                  # sharded counters per GA zone
QUEUE_SHARDS = int(os.getenv("QUEUE_SHARDS", "10"))           # sharded waiting-room counters
ACCESS_TOKEN_SECONDS = int(os.getenv("ACCESS_TOKEN_SECONDS", "900"))
