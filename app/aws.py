"""Shared boto3 clients (clients are thread-safe) and secrets."""
import json
import os
from functools import lru_cache

import boto3
from botocore.config import Config

from core import settings

_cfg = Config(retries={"mode": "adaptive", "max_attempts": 5}, max_pool_connections=50)


@lru_cache(maxsize=None)
def dynamodb():
    return boto3.client("dynamodb", region_name=settings.REGION, config=_cfg)


@lru_cache(maxsize=None)
def sqs():
    return boto3.client("sqs", region_name=settings.REGION, config=_cfg)


@lru_cache(maxsize=None)
def secrets():
    return boto3.client("secretsmanager", region_name=settings.REGION, config=_cfg)


@lru_cache(maxsize=None)
def token_secret():
    """HMAC key for waiting-room tickets and access passes (Secrets Manager)."""
    if os.getenv("TOKEN_SECRET"):
        return os.environ["TOKEN_SECRET"]
    value = secrets().get_secret_value(SecretId=os.environ["TOKEN_SECRET_ARN"])["SecretString"]
    try:
        return json.loads(value)["key"]
    except (ValueError, KeyError):
        return value


def reset():
    for fn in (dynamodb, sqs, secrets, token_secret):
        fn.cache_clear()
