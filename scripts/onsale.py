#!/usr/bin/env python3
"""Start a fresh on-sale for an event: waiting room on, gate opens in N
seconds, admits R people per second. Everyone holding an old pass is sent
back to the queue (new epoch).

    pip install boto3
    python3 scripts/onsale.py --event skrillex-delhi-2027 --opens-in 60 --rate 20 --initial 50
    python3 scripts/onsale.py --event skrillex-delhi-2027 --open     # turn the waiting room off

Runs from your laptop with your AWS credentials (DynamoDB is a regional API).
"""
import argparse
import time

import boto3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    p.add_argument("--table", default="frontrow-waiting-room")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--opens-in", type=int, default=60, help="seconds until the gate opens")
    p.add_argument("--rate", type=float, default=20, help="people admitted per second")
    p.add_argument("--initial", type=int, default=50, help="admitted the moment it opens")
    p.add_argument("--open", action="store_true", help="disable the waiting room")
    a = p.parse_args()

    ddb = boto3.client("dynamodb", region_name=a.region)
    now = int(time.time())
    ddb.put_item(TableName=a.table, Item={
        "pk": {"S": a.event}, "sk": {"S": "CONFIG"},
        "mode": {"S": "open" if a.open else "queue"},
        "opens_at": {"N": str(now + a.opens_in)}, "rate": {"N": str(a.rate)},
        "initial": {"N": str(a.initial)}, "epoch": {"S": f"{now}"},
    })
    for shard in range(10):
        ddb.put_item(TableName=a.table, Item={"pk": {"S": a.event}, "sk": {"S": f"Q#{shard}"}, "n": {"N": "0"}})
    if a.open:
        print(f"{a.event}: waiting room off")
    else:
        print(f"{a.event}: gate opens in {a.opens_in}s, admitting {a.initial} then {a.rate}/s. "
              "API pods pick this up within 5 seconds.")


if __name__ == "__main__":
    main()
