"""Where a bus lane would pay: delayed stretches ranked by the bus time they cost.

A leg is hot when, in the weekday rush hours, it takes HOT_DELAY longer than at
weekday midday. Midday, not the quickest hour, is the yardstick: a lane cannot
give buses empty night roads, only take back what rush traffic adds. Adjacent hot
legs are merged into one corridor along the busiest stream, since a lane on one
200 m block helps nobody; where routes join or split, the corridor follows the
leg with the most buses and the lighter branch starts its own. Corridors are
ranked by bus-minutes lost per weekday rush per km of lane: rides x median
seconds over midday, so a slow street nobody drives loses to a slightly slower one that
carries forty buses an hour, and a short bottleneck beats a long mild street.

Built once per month from the weekday ride payloads, and written beside them as
web/data/lanes-YYYY-MM.json.
"""

from __future__ import annotations

from .traces import leg_line, metres

# Local hours, weekday rush: 07:00-09:00 and 16:00-18:00.
RUSH_HOURS = (7, 8, 16, 17)
HOT_DELAY = 0.25
LANE_MIN_M = 300
TOP_N = 10
# Local hours whose pooled ride time is the baseline, and the rides it needs.
MIDDAY_HOURS = (10, 11, 12, 13, 14)
BASELINE_MIN_RIDES = 10


def _legs(paths: list[dict], stops: dict) -> dict[str, dict]:
    """Every stop pair once, with the route legs that drive it, its length and line."""
    legs: dict[str, dict] = {}
    for route in paths:
        key = f"{route['route']}|{route['dir']}"
        dist = route.get("dist")
        for i, (a, b) in enumerate(zip(route["path"], route["path"][1:])):
            leg = legs.get(f"{a}>{b}")
            measured = bool(dist) and dist[i + 1] > dist[i]
            shaped = measured and bool(route.get("shape"))
            # Some routes in the feed have no shape: the first route seen with
            # one draws the leg, not a straight line across the blocks.
            if leg is None or (shaped and not leg["shaped"]):
                if measured:
                    length = dist[i + 1] - dist[i]
                elif a in stops and b in stops:
                    length = metres(stops[a], stops[b])
                else:
                    continue
                geometry = {"length": length, "line": leg_line(route, i, stops), "shaped": shaped}
                if leg is None:
                    leg = legs[f"{a}>{b}"] = {"a": a, "b": b, "members": [], "names": set()}
                leg.update(geometry)
            leg["members"].append((key, i))
            leg["names"].add(route["name"])
    return legs


def _pooled(leg: dict, payload: dict) -> tuple[float, int] | None:
    """The leg's typical ride time and ride count across the routes that drive it.

    Medians, not means: a few runs that swallowed a terminal layover or a
    breakdown add many minutes each and would make a quiet leg look jammed.
    Routes are pooled by a ride-weighted mean of their medians.
    """
    total = n = 0
    for key, i in leg["members"]:
        route = payload.get(key)
        if not route or route["med"][i] is None:
            continue
        total += route["med"][i] * route["n"][i]
        n += route["n"][i]
    return (total / n, n) if n else None


def _rush(leg: dict, by_hour: dict[int, dict], weekdays: int) -> dict | None:
    """What this leg costs in the rush hours, or None if it is not delayed."""
    mid = [t for h in MIDDAY_HOURS if h in by_hour and (t := _pooled(leg, by_hour[h]))]
    mid_n = sum(n for _, n in mid)
    if mid_n < BASELINE_MIN_RIDES:
        return None
    off_s = sum(t * n for t, n in mid) / mid_n

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
    """The worst delayed stretches, most bus-minutes lost per km first.

    `by_hour` is the weekday ride payload of each local hour, as the viewer loads it.
    """
    legs = _legs(paths, stops)
    hot = {k: {**leg, **r} for k, leg in legs.items() if (r := _rush(leg, by_hour, weekdays))}

    # A corridor runs on through a stop along the busiest stream: only the busiest
    # arriving leg carries on, into the busiest leaving one, so each leg has one
    # predecessor at most and corridors never overlap. shortcut: a loop of hot
    # legs has no start, so it is skipped; upgrade if a ring route ever tops the list.
    leaving: dict[str, list[str]] = {}
    arriving: dict[str, list[str]] = {}
    for k, leg in hot.items():
        leaving.setdefault(leg["a"], []).append(k)
        arriving.setdefault(leg["b"], []).append(k)

    def busiest(keys: list[str]) -> str:
        return max(keys, key=lambda k: hot[k]["buses"])

    def follows(k: str) -> str | None:
        leg = hot[k]
        onward = [n for n in leaving.get(leg["b"], ()) if hot[n]["b"] != leg["a"]]  # no U-turns
        if not onward or busiest(arriving[leg["b"]]) != k:
            return None
        return busiest(onward)

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
        lost_min = sum(m["lost_s"] for m in members) / 60
        corridors.append(
            {
                "from": stops.get(members[0]["a"], [0, 0, members[0]["a"]])[2],
                "to": stops.get(members[-1]["b"], [0, 0, members[-1]["b"]])[2],
                "legs": chain,
                # Drawn as is by /suggestions, which loads no route geometry of its own.
                "lines": [[[round(c, 5) for c in p] for p in m["line"]] for m in members],
                "length_m": round(length),
                "routes": sorted(set().union(*(m["names"] for m in members))),
                "lost_min": round(lost_min, 1),
                "lost_per_km": round(lost_min / length * 1000, 1),
                "buses": round(sum(m["buses"] for m in members) / len(members), 1),
                "delay": round(
                    sum(m["actual"] for m in members) / sum(m["baseline"] for m in members) - 1, 2
                ),
            }
        )
    corridors.sort(key=lambda c: c["lost_per_km"], reverse=True)
    return corridors[:limit]
