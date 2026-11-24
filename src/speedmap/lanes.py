"""Where a bus lane would pay: delayed stretches ranked by the bus time they cost.

A leg is hot when, in the weekday rush hours, it takes HOT_DELAY longer than its
own quickest trusted hour — the same yardstick the map colours by. Adjacent hot
legs (same street, no fork between) are merged into one corridor, since a lane
on one 200 m block helps nobody. Corridors are ranked by bus-minutes lost per
weekday rush: rides x seconds over off-peak, so a slow street nobody drives
loses to a slightly slower one that carries forty buses an hour.

Built once per month from the weekday ride payloads, and written beside them as
web/data/lanes-YYYY-MM.json.
"""

from __future__ import annotations

import math

# Local hours, weekday rush: 07:00-09:00 and 16:00-18:00.
RUSH_HOURS = (7, 8, 16, 17)
HOT_DELAY = 0.25
LANE_MIN_M = 300
TOP_N = 10
# Rides pooled over the month a leg needs for its quickest hour to count.
# Mirrors OFFPEAK_MIN_RIDES in web/app.js.
OFFPEAK_MIN_RIDES = 10


def _metres(a: list, b: list) -> float:
    dy = (a[0] - b[0]) * 111320
    dx = (a[1] - b[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def _legs(paths: list[dict], stops: dict) -> dict[str, dict]:
    """Every stop pair once, with the route legs that drive it and its length."""
    legs: dict[str, dict] = {}
    for route in paths:
        key = f"{route['route']}|{route['dir']}"
        dist = route.get("dist")
        for i, (a, b) in enumerate(zip(route["path"], route["path"][1:])):
            leg = legs.get(f"{a}>{b}")
            if leg is None:
                if dist and dist[i + 1] > dist[i]:
                    length = dist[i + 1] - dist[i]
                elif a in stops and b in stops:
                    length = _metres(stops[a], stops[b])
                else:
                    continue
                leg = legs[f"{a}>{b}"] = {"a": a, "b": b, "length": length, "members": [], "names": set()}
            leg["members"].append((key, i))
            leg["names"].add(route["name"])
    return legs


def _pooled(leg: dict, payload: dict) -> tuple[float, int] | None:
    """The leg's mean ride time and ride count across the routes that drive it."""
    total = n = 0
    for key, i in leg["members"]:
        route = payload.get(key)
        if not route or route["avg"][i] is None:
            continue
        total += route["avg"][i] * route["n"][i]
        n += route["n"][i]
    return (total / n, n) if n else None


def _rush(leg: dict, by_hour: dict[int, dict], weekdays: int) -> dict | None:
    """What this leg costs in the rush hours, or None if it is not delayed."""
    off = [t for p in by_hour.values() if (t := _pooled(leg, p)) and t[1] >= OFFPEAK_MIN_RIDES]
    if not off:
        return None
    off_s = min(t for t, _ in off)

    lost = rides = weighted = hours = 0.0
    for hour in RUSH_HOURS:
        t = _pooled(leg, by_hour[hour]) if hour in by_hour else None
        if not t:
            continue
        hours += 1
        rides += t[1]
        weighted += t[0] * t[1]
        lost += max(0.0, t[0] - off_s) * t[1]
    if not rides or weighted / (off_s * rides) - 1 < HOT_DELAY:
        return None
    return {
        "lost_s": lost / weekdays,
        "buses": rides / weekdays / hours,
        "actual": weighted,
        "baseline": off_s * rides,
    }


def rank_corridors(
    paths: list[dict],
    stops: dict,
    by_hour: dict[int, dict],
    weekdays: int,
    *,
    min_m: float = LANE_MIN_M,
    limit: int = TOP_N,
) -> list[dict]:
    """The worst delayed stretches, most bus-minutes lost first.

    `by_hour` is the weekday ride payload of each local hour, as the viewer loads it.
    """
    legs = _legs(paths, stops)
    hot = {k: {**leg, **r} for k, leg in legs.items() if (r := _rush(leg, by_hour, weekdays))}

    # A corridor runs through a stop only where exactly one hot leg arrives and
    # one leaves; at a fork it ends. shortcut: a loop of hot legs has no start, so
    # it is skipped; upgrade if a ring route ever tops the list.
    leaving: dict[str, list[str]] = {}
    arriving: dict[str, list[str]] = {}
    for k, leg in hot.items():
        leaving.setdefault(leg["a"], []).append(k)
        arriving.setdefault(leg["b"], []).append(k)

    def follows(k: str) -> str | None:
        stop = hot[k]["b"]
        if len(leaving.get(stop, ())) == 1 and len(arriving[stop]) == 1:
            return leaving[stop][0]
        return None

    nexts = {k: follows(k) for k in hot}
    continued = {n for n in nexts.values() if n}

    corridors = []
    for start in (k for k in hot if k not in continued):
        chain = [start]
        while (nxt := nexts[chain[-1]]) and nxt not in chain:
            chain.append(nxt)
        members = [hot[k] for k in chain]
        length = sum(m["length"] for m in members)
        if length < min_m:
            continue
        corridors.append(
            {
                "from": stops.get(members[0]["a"], [0, 0, members[0]["a"]])[2],
                "to": stops.get(members[-1]["b"], [0, 0, members[-1]["b"]])[2],
                "legs": chain,
                "length_m": round(length),
                "routes": sorted(set().union(*(m["names"] for m in members))),
                "lost_min": round(sum(m["lost_s"] for m in members) / 60, 1),
                "buses": round(sum(m["buses"] for m in members) / len(members), 1),
                "delay": round(
                    sum(m["actual"] for m in members) / sum(m["baseline"] for m in members) - 1, 2
                ),
            }
        )
    corridors.sort(key=lambda c: c["lost_min"], reverse=True)
    return corridors[:limit]
