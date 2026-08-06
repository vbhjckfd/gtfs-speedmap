# gtfs-speedmap

An OSM map of **average bus speed** in Lviv, one dot per 25 m cell, coloured green (fast) to red
(slow), with an hour-of-day slider and a month picker.

Built from the GTFS-RT vehicle-position snapshots that
[`gtfs-collector`](../gtfs-collector) archives to Cloudflare R2.

```
R2  raw/YYYY-MM-DD/*.pb          GTFS-RT snapshots, every ~10 s
R2  static/YYYY-MM-DD/static.zip GTFS schedule as archived that day
        │  python -m speedmap.aggregate --all
        ▼
data/agg/YYYY-MM-DD.parquet      per-day sums, keyed (month, hour, cell)
        │  python -m speedmap.build_web
        ▼
web/data/{month}-{hour}.json     what the map fetches
        │  make deploy
        ▼
Cloudflare Worker (assets-only)
```

## What counts as a sample

Every GTFS-RT entity is a candidate; it survives only if all of the following hold.

| Filter | Why |
|---|---|
| Route is a **bus** | `route_type == 3` **and** the short name does not start with `Тр`. `route_type` alone is not enough: Lviv trolleybuses (`Тр22`…`Тр38`) are published as `route_type=3`, and trams are `route_type=0`. |
| Vehicle timestamp is fresh | The feed republishes vehicles whose own `timestamp` is hours old (p50 = 14 s but p99 ≈ 17 h). Entities more than `STALE_MAX_S` behind the feed header are ghosts. |
| `(vehicle_id, timestamp)` not seen before | The collector polls every 10 s and the feed repeats unchanged entities, so a parked bus would otherwise be counted many times over. |
| Speed in `0 … SPEED_MAX_MPS` | 25 m/s = 90 km/h; above that is GPS noise for a city bus. |
| Inside the Lviv bbox | Drops stray coordinates. |
| **Not within `STOP_RADIUS_M` of a stop on its own trip** | Makes the map show *running* speed instead of dwell time. |

That last filter is the point of the whole thing. The stop set is scoped to the vehicle's own trip —
and when the trip_id is not in the schedule any more, to its route **and direction**. The two
directions of a Lviv route share **zero** stop_ids, and their nearest opposite-direction stops sit a
median of 70 m apart, so the stop across the street never blanks out the lane running past it.

Speed is `position.speed` straight from the feed (m/s), which the Lviv feed populates on every
entity.

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env    # R2 credentials — same bucket and keys as gtfs-eta
```

## Use

```bash
make ingest DATE=2026-07-15   # aggregate one day
make ingest-all               # every day in R2; resumable, skips days already done
make build                    # merge into web/data/*.json
make serve                    # http://localhost:8000
make test
make deploy                   # build + publish as a Cloudflare Worker (needs Node >=22: nvm use)
make update                   # ingest new days, rebuild, deploy
```

`web/data/*.json` is generated and git-ignored, so the deploy uploads whatever the last `make build`
produced. `make deploy` runs `build` first to keep those in step.

`make ingest-all` re-reads only the days with no `data/agg/*.parquet` yet, so a new day costs one
run of a couple of minutes. Pass `--force` to redo days after changing a filter.

## Tuning

Every knob lives in `src/speedmap/config.py` and reads an env var of the same name.

| Setting | Default | Effect |
|---|---|---|
| `CELL_SIZE_M` | 25 | Dot spacing. Smaller = finer, more cells, bigger JSON. |
| `STOP_RADIUS_M` | 40 | How much road either side of a stop is treated as dwell. |
| `STALE_MAX_S` | 120 | Ghost-entity cutoff. |
| `SPEED_MAX_MPS` | 25 | Upper sanity bound. |
| `MIN_SAMPLES` | 5 | Cells with fewer samples are not published. |
| `SCALE_LOW_KMH` / `SCALE_HIGH_KMH` | 5 / 35 | Colour ramp ends. Measured cell speeds: p25 ≈ 13, p50 ≈ 21, p90 ≈ 41 km/h. |

Changing a filter means re-running `make ingest-all --force`; changing `MIN_SAMPLES` or the colour
scale only needs `make build`.

## Known artefacts

- Dense red clusters appear at depots and terminal layovers (e.g. near Skniliv). Those buses are
  stationary but not near a stop of their trip, so they pass every filter. They are real
  observations, just not congestion.
- Hours 00:00–04:00 are absent: the collector does not poll then.
- `static/` archives only start 2026-07-31, so earlier days are matched against the oldest archive.
  About 35% of May trip_ids no longer exist there, which is what the route+direction fallback covers.
