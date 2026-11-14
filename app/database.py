"""RDS PostgreSQL access (catalogue: venues, events, prices, section tiers).

Credentials come from the RDS-managed Secrets Manager secret through a VPC
interface endpoint. DATABASE_URL overrides this for local runs and tests.
"""
import json
import os
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

import aws

_engine = None
_lock = threading.Lock()


def _url():
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    creds = json.loads(aws.secrets().get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    return URL.create("postgresql+psycopg2", username=creds["username"], password=creds["password"],
                      host=os.environ["DB_HOST"], port=int(os.getenv("DB_PORT", "5432")),
                      database=os.getenv("DB_NAME", "frontrow"), query={"sslmode": "require"})


def engine():
    global _engine
    with _lock:
        if _engine is None:
            url = _url()
            kwargs = {"pool_pre_ping": True}
            if not str(url).startswith("sqlite"):
                kwargs.update(pool_size=8, max_overflow=8, pool_recycle=1800)
            _engine = create_engine(url, **kwargs)
        return _engine


def reset():
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def query(sql, **params):
    """Read rows as dicts; rebuild the pool once if the rotated secret changed."""
    for attempt in (1, 2):
        try:
            with engine().connect() as conn:
                return [dict(r._mapping) for r in conn.execute(text(sql), params)]
        except OperationalError:
            if attempt == 2:
                raise
            reset()
