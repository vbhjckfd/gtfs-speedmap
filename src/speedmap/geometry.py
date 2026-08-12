"""Polyline helpers for laying times out along a route rather than at its stops.

Leg times are measured between stops, because a stop is the one landmark a
vehicle's position stream can be timed against reliably. But a rider does not
board at a stop id — they board where they are standing, and what they want to
know is the time between two arbitrary points. That needs the route's shape:
somewhere to project both points onto, and a distance along it to place them.

Everything here works in projected metres (UTM 35N), so a distance is a
distance.
"""

from __future__ import annotations

import math


def cumulative(points: list[tuple[float, float]]) -> list[float]:
    """Distance along the polyline at each vertex, starting at zero."""
    out = [0.0]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        out.append(out[-1] + math.hypot(x2 - x1, y2 - y1))
    return out


def _perpendicular(point, start, end) -> float:
    (px, py), (ax, ay), (bx, by) = point, start, end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(points: list[tuple[float, float]], tolerance: float) -> list[int]:
    """Indices of the vertices Douglas–Peucker keeps at `tolerance` metres.

    Indices rather than points, so the caller can carry the lat/lon it started
    with instead of unprojecting what comes back.
    """
    if len(points) < 3:
        return list(range(len(points)))

    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, worst_at = 0.0, first
        for i in range(first + 1, last):
            d = _perpendicular(points[i], points[first], points[last])
            if d > worst:
                worst, worst_at = d, i
        if worst > tolerance:
            keep.add(worst_at)
            stack.append((first, worst_at))
            stack.append((worst_at, last))
    return sorted(keep)


def _offsets(
    path: list[tuple[float, float]], cum: list[float], x: float, y: float
) -> list[tuple[float, float]]:
    """(distance along, distance from) for the nearest point of each segment."""
    out = []
    for i in range(len(path) - 1):
        (ax, ay), (bx, by) = path[i], path[i + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else ((x - ax) * dx + (y - ay) * dy) / length2
        t = max(0.0, min(1.0, t))
        off = math.hypot(x - (ax + t * dx), y - (ay + t * dy))
        out.append((cum[i] + t * math.sqrt(length2), off))
    return out


def project(
    path: list[tuple[float, float]], cum: list[float], x: float, y: float
) -> tuple[float, float]:
    """Nearest point on the polyline to (x, y): (distance along, distance from)."""
    if len(path) < 2:
        return 0.0, math.inf
    return min(_offsets(path, cum, x, y), key=lambda pair: pair[1])


def candidates(
    path: list[tuple[float, float]],
    cum: list[float],
    x: float,
    y: float,
    max_off: float,
    limit: int = 6,
) -> list[tuple[float, float]]:
    """Every place along the path this point could plausibly sit.

    A route that runs out along a street and back down it passes the same stop
    twice, so the nearest point is not always the right one — the caller picks
    between these by which choice keeps the stops in order.
    """
    offsets = _offsets(path, cum, x, y)
    local = [
        offsets[i]
        for i in range(len(offsets))
        if offsets[i][1] <= max_off
        and (i == 0 or offsets[i - 1][1] >= offsets[i][1])
        and (i == len(offsets) - 1 or offsets[i + 1][1] >= offsets[i][1])
    ]
    return sorted(local, key=lambda pair: pair[1])[:limit]


def match_stops(
    path: list[tuple[float, float]],
    cum: list[float],
    stops: list[tuple[float, float]],
    max_off: float,
    slack: float = 80.0,
) -> list[float] | None:
    """Distance along the path of each stop, in order, or None if they do not fit.

    Stops are matched to the shape as a sequence rather than one at a time: the
    assignment must increase along the route, and among those that do, the one
    that sits closest to the stops wins. Matching each stop independently puts a
    stop on whichever pass of a doubled-back route happens to be nearer, and
    forcing each one to come after the last lets a single stop that sits a few
    metres behind its predecessor throw the rest of the route onto the return
    leg — measured on Lviv's А58, a 23 km jump between two adjacent stops.

    `slack` is how far back a stop may sit from the one before it and still be
    the next stop: a pair inside a tight turn can project a few metres the wrong
    way, and refusing those outright loses the whole route. Small inversions are
    flattened afterwards; a jump back onto an earlier pass is far outside it.
    """
    if len(path) < 2 or not stops:
        return None

    # Viterbi over (stop, candidate place), cost = how far off the shape it sits.
    best: list[tuple[float, float, int]] = []  # (cost, along, back-pointer)
    trellis: list[list[tuple[float, float]]] = []
    for x, y in stops:
        places = candidates(path, cum, x, y, max_off)
        if not places:
            return None
        trellis.append(places)

    previous: list[tuple[float, float, int]] = [
        (off, along, -1) for along, off in trellis[0]
    ]
    back: list[list[int]] = [[-1] * len(trellis[0])]
    for step in range(1, len(trellis)):
        current: list[tuple[float, float, int]] = []
        pointers: list[int] = []
        for along, off in trellis[step]:
            options = [
                (cost + off, at)
                for at, (cost, previous_along, _) in enumerate(previous)
                if along > previous_along - slack
            ]
            if not options:
                current.append((math.inf, along, -1))
                pointers.append(-1)
                continue
            cost, at = min(options)
            current.append((cost, along, at))
            pointers.append(at)
        previous = current
        back.append(pointers)
        if all(math.isinf(cost) for cost, _, _ in previous):
            return None

    best = min(previous, key=lambda item: item[0])
    if math.isinf(best[0]):
        return None

    at = previous.index(best)
    out = []
    for step in range(len(trellis) - 1, -1, -1):
        out.append(trellis[step][at][0])
        at = back[step][at]
        if at < 0 and step > 0:
            return None
    out.reverse()

    # Flatten the inversions `slack` allowed, so the distances a caller slices
    # between are never out of order.
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1])
    return out
