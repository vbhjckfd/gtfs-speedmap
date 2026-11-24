"""Where a bus lane would pay: delayed stretches ranked by the bus time they cost.

A leg is hot when, over the whole weekday, it takes HOT_DELAY longer than in its
quickest hour, the free-flow time on empty roads. The quickest hour, not midday,
is the yardstick: a street jammed from morning to evening is slow at midday too,
and measured against midday it would look fine. Adjacent hot
legs are merged into one corridor along the busiest stream, since a lane on one
200 m block helps nobody; where routes join or split, the corridor follows the
leg with the most buses and the lighter branch starts its own. Corridors are
ranked by bus-minutes lost per weekday per km of lane: rides x median
seconds over free flow, so a slow street nobody drives loses to a slightly slower one that
carries forty buses an hour, and a short bottleneck beats a long mild street.

The same corridors ranked by delay alone (`order="delay"`) are the slowest
stretches: how much longer than on empty roads, for streets with at least
SLOW_MIN_BUSES buses an hour, since a crawl nobody rides is no one's problem.

Built once per month from the weekday ride payloads, and written beside them as
web/data/lanes-YYYY-MM.json and lanes-slow-YYYY-MM.json.
"""

from __future__ import annotations

from .traces import leg_line, metres

HOT_DELAY = 0.25
LANE_MIN_M = 300
TOP_N = 10
# Weekday buses an hour, all-day average, a stretch needs to rank as slowest.
SLOW_MIN_BUSES = 6
# Rides an hour needs before its time can be the free-flow baseline.
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


def _delay(leg: dict, by_hour: dict[int, dict], weekdays: int) -> dict | None:
    """What this leg costs over the weekday, or None if it is not delayed."""
    times = [t for payload in by_hour.values() if (t := _pooled(leg, payload))]
    trusted = [s for s, n in times if n >= BASELINE_MIN_RIDES]
    if not trusted:
        return None
    free_s = min(trusted)
    rides = sum(n for _, n in times)
    weighted = sum(s * n for s, n in times)
    if weighted / (free_s * rides) - 1 < HOT_DELAY:
        return None
    return {
        "lost_s": sum(max(0.0, s - free_s) * n for s, n in times) / weekdays,
        "buses": rides / weekdays / len(times),
        "actual": weighted,
        "baseline": free_s * rides,
    }


def rank_corridors(
    paths: list[dict],
    stops: dict,
    by_hour: dict[int, dict],
    weekdays: int,
    *,
    min_m: float = LANE_MIN_M,
    limit: int = TOP_N,
    order: str = "lost_per_km",
    min_buses: float = 0,
) -> list[dict]:
    """The worst delayed stretches, most bus-minutes lost per km first, or with
    `order="delay"` the most delayed first among those with `min_buses` an hour.

    `by_hour` is the weekday ride payload of each local hour, as the viewer loads it.
    """
    legs = _legs(paths, stops)
    hot = {k: {**leg, **r} for k, leg in legs.items() if (r := _delay(leg, by_hour, weekdays))}

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
    corridors = [c for c in corridors if c["buses"] >= min_buses]
    corridors.sort(key=lambda c: c[order], reverse=True)
    return corridors[:limit]
