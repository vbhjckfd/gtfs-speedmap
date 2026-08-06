"""Decode archived GTFS-RT FeedMessage protobufs into flat vehicle rows.

Unlike gtfs-eta's reader, this one keeps the **per-vehicle** timestamp
(`entity.vehicle.timestamp`) alongside the feed header timestamp. The Lviv feed
republishes vehicles whose own timestamp is hours stale (p99 ≈ 17 h against a
p50 of 14 s), so the two must be compared to tell an observation from a ghost.
"""

from __future__ import annotations

from typing import NamedTuple

from google.transit import gtfs_realtime_pb2


class VehicleRow(NamedTuple):
    feed_ts: int  # header timestamp, epoch seconds
    veh_ts: int  # this vehicle's own timestamp, epoch seconds
    vehicle_id: str
    trip_id: str
    route_id: str
    lat: float
    lon: float
    speed: float  # metres per second, as published


def parse_feed(data: bytes) -> list[VehicleRow]:
    """Parse one snapshot. Entities without a position or trip are skipped.

    Raises on malformed protobuf — the caller decides whether a bad object is
    fatal or just a lost snapshot.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(data)
    feed_ts = int(feed.header.timestamp)

    rows: list[VehicleRow] = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("position") or not v.HasField("trip"):
            continue
        pos = v.position
        trip = v.trip
        if not trip.trip_id or not trip.route_id:
            continue
        if not pos.HasField("speed"):
            continue

        rows.append(
            VehicleRow(
                feed_ts=feed_ts,
                # A missing vehicle timestamp can't be aged; treat it as fresh.
                veh_ts=int(v.timestamp) if v.timestamp else feed_ts,
                vehicle_id=v.vehicle.id if v.HasField("vehicle") else entity.id,
                trip_id=trip.trip_id,
                route_id=trip.route_id,
                lat=pos.latitude,
                lon=pos.longitude,
                speed=pos.speed,
            )
        )
    return rows
