"""Cloudflare R2 access for the bucket written by gtfs-collector.

Layout (see ~/Projects/gtfs-collector/README.md):

    raw/YYYY-MM-DD/YYYY-MM-DDTHH:mm:ss.sssZ.pb   GTFS-RT FeedMessage snapshots
    static/YYYY-MM-DD/static.zip                 GTFS static as of that day
"""

from __future__ import annotations

import os

import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

RAW_PREFIX = "raw/"
STATIC_PREFIX = "static/"


def make_client():
    account = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        # The ingest fans out to WORKERS threads sharing one client; the default
        # pool of 10 would serialise them.
        config=Config(max_pool_connections=64, retries={"max_attempts": 5, "mode": "standard"}),
    )


def bucket() -> str:
    return os.environ.get("R2_BUCKET", "gtfs-lviv")


def _dates_under(client, prefix: str) -> list[str]:
    """Return the YYYY-MM-DD directory names directly under a prefix, sorted."""
    dates: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix, Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            name = common["Prefix"][len(prefix) :].rstrip("/")
            if len(name) == 10 and name[4] == "-" and name[7] == "-":
                dates.append(name)
    return sorted(dates)


def raw_dates(client) -> list[str]:
    return _dates_under(client, RAW_PREFIX)


def static_dates(client) -> list[str]:
    return _dates_under(client, STATIC_PREFIX)


def snapshot_keys(client, date_str: str) -> list[str]:
    """All .pb keys for one day, sorted (which is chronological)."""
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=f"{RAW_PREFIX}{date_str}/"):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".pb"):
                keys.append(obj["Key"])
    return sorted(keys)


def get_bytes(client, key: str) -> bytes:
    return client.get_object(Bucket=bucket(), Key=key)["Body"].read()


def static_date_for(date_str: str, available: list[str]) -> str | None:
    """The static archive in effect on `date_str`: the newest one dated <= it.

    Falls back to the oldest archive when the day predates every archive, which
    happens for the earliest raw days (static archiving started later).
    """
    if not available:
        return None
    eligible = [d for d in available if d <= date_str]
    return eligible[-1] if eligible else available[0]
