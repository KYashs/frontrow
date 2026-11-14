#!/usr/bin/env python3
"""Rebuild an event's inventory from scratch (every seat back on sale) by
invoking the seeder Lambda with force=true. Use between demos, when no
holds are active (an outstanding GA hold would be refunded on top of the reset).

    pip install boto3
    python3 scripts/reset_event.py --event kendrick-lamar-metlife-2027
"""
import argparse
import json

import boto3


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    p.add_argument("--function", default="frontrow-inventory-seeder")
    p.add_argument("--region", default="us-east-1")
    a = p.parse_args()
    lam = boto3.client("lambda", region_name=a.region)
    resp = lam.invoke(FunctionName=a.function, Payload=json.dumps({"events": [a.event], "force": True}).encode())
    print(json.loads(resp["Payload"].read()))
    print("Note: seats are reset to available; section counters are rewritten to full capacity.")


if __name__ == "__main__":
    main()
