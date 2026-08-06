# gtfs-speedmap

An OSM map of **bus running speed** in Lviv, one dot per 25 m cell, coloured green (fast) to red
(slow), with an hour-of-day slider, a month picker and a weekday/weekend split.

Built from the GTFS-RT vehicle-position snapshots that
[`gtfs-collector`](../gtfs-collector) archives to Cloudflare R2.

```
R2  raw/YYYY-MM-DD/*.pb           GTFS-RT snapshots, every ~10 s
R2  static/YYYY-MM-DD/static.zip  GTFS schedule as archived that day
        │  python -m speedmap.aggregate --all
        ▼
data/agg/YYYY-MM-DD.parquet       per-day sums, keyed (month, hour, cell)
data/hist/YYYY-MM-DD.parquet      per-day speed histograms, same key + bin
        │  python -m speedmap.sync push     ⇄  R2 derived/{agg,hist}/
        │  python -m speedmap.build_web
        ▼
web/data/{month}-{days}-{hour}.json   what the map fetches
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
| Not inside a discovered **depot** | Parked buses, not slow traffic. See below. |
| Not within `TERMINAL_RADIUS_M` of the **first or last stop** of its trip | Layover at the end of a run, where buses often stand well past the stop itself. |
| **Not within `STOP_RADIUS_M` of a stop on its own trip** | Makes the map show *running* speed instead of dwell time. |

That last filter is the point of the whole thing. The stop set is scoped to the vehicle's own trip —
and when the trip_id is not in the schedule any more, to its route **and direction**. The two
directions of a Lviv route share **zero** stop_ids, and their nearest opposite-direction stops sit a
median of 70 m apart, so the stop across the street never blanks out the lane running past it.

Speed is `position.speed` straight from the feed (m/s), which the Lviv feed populates on every
entity.

## Finding the depots

GTFS publishes stops, not depots or off-street layover yards, and those are where a bus spends
hours doing nothing. They are mined out of the aggregates instead, by
[`depots.py`](src/speedmap/depots.py): cells that are motionless (≤ `DEPOT_MAX_KMH`), busy
(≥ `DEPOT_MIN_SAMPLES`), more than `DEPOT_MIN_DIST_TO_STOP_M` from any **mid-route** stop, and — the
part that matters — still motionless across at least `DEPOT_MIN_HOURS` *separate hours of the day*.
A junction that jams at rush hour fails that last test; a yard full of parked buses passes it at
05:00 and at 23:00 alike. Neighbouring cells are clustered into one site, and the site radius is its
observed extent plus `DEPOT_PAD_M`.

Terminal stops deliberately do not shield a cell from being called a depot, and only the **nearest**
stop is consulted. The layover sprawls past the terminal stop by more than any fixed radius covers:
the forecourt of the Двірцевий bus station, the far end of Городоцька, and the ends of the Рясне-2,
Голоско, Сихів and Ряшівська routes all sit at 0–1.5 km/h for fifteen-plus hours a day. Requiring
distance from *every* stop hid exactly the sites worth finding — one terminus stayed hidden even
after that was fixed, because an unrelated route's ordinary stop happened to sit 102 m away. Dwell
at an ordinary stop is already handled by `STOP_RADIUS_M`.

Over 88 days this finds **78 sites holding ~4.4M samples**. Spot-checked against OpenStreetMap, they
are what they claim to be: the two largest are tagged `amenity=parking` (Збиральна, Авіаційна), and
the rest are route termini and the bus station.

```bash
python -m speedmap.depots           # discover, writes data/depots.json
python -m speedmap.depots --list    # show what is on file
python -m speedmap.depots --merge   # keep known sites, add newly found ones
```

Discovery has to run against aggregates built **without** the mask, or the evidence for a site has
already been filtered away — plain `discover` refuses to overwrite an existing `data/depots.json`
for that reason. Use `--merge` to widen the mask from already-masked aggregates: it keeps every
known site and adds only what the new pass turns up. The order is: ingest, discover, then
`ingest-all --force`.

The mask is applied twice: `aggregate.py` drops the samples at ingest, and `build_web.py` drops
whole cells that land inside a zone. The second pass is what makes a widened mask visible after a
`make build` of a few seconds, instead of another couple of hours over R2. Re-ingesting afterwards
is still worth doing — it also clears those samples out of neighbouring cells' histograms — but it
is no longer the thing standing between a fix and a deploy.

## Weekdays and weekends

Averaging the whole week together hides the rush hour. City-wide mean speed, km/h:

| hour | weekday | Sat | Sun |
|---|---|---|---|
| 07 | 20.6 | 22.7 | 23.3 |
| 08 | 18.4 | 21.9 | 22.7 |
| 12 | 18.1 | 19.1 | 20.6 |
| 17 | **16.6** | 21.1 | **21.4** |
| 22 | 28.2 | 27.4 | 28.6 |

Without the split, 17:00 is a blend of a 16.6 km/h jam and a 21.4 km/h empty street. Saturday and
Sunday differ by at most 1.5 km/h at any hour, against a 4–5 km/h weekday gap, so they share one
"weekend" bucket rather than thinning the data three ways.

The day type comes from the aggregate's filename, and the histograms already hold the full
distribution, so none of this needed another pass over R2 — `build_web.py` reads the same parquets
and buckets them differently.

## Which statistic

Each day writes two parquets: `data/agg/` keeps the running sums, `data/hist/` keeps a speed
histogram per cell in `HIST_BIN_KMH` bins. Every statistic ships in the same JSON, so the Statistic
dropdown repaints without refetching.

| Statistic | What it answers |
|---|---|
| Average | Time-weighted mean — the figure that matches how long a journey takes. |
| Median | Ignores the tail of waits at lights; a better read on conditions, a worse one on delay. |
| Slow day (p15) / Fast day (p85) | The bad and good ends of the same distribution. |
| Unreliability (p85 − p15) | How unpredictable a stretch is, which is not the same as how slow. |
| **% of free-flow** | Median over that cell's *own* p85 across the whole archive. |

The last one is the reason the others are not enough. Absolute speed tells you where the road is
slow; it cannot tell you where there is congestion. Measured across the archive, a cell's median
speed and its share of its own free-flow correlate at **r = 0.07** — they are very nearly
independent. A narrow old-town lane doing 20 km/h at 03:00 is not congested; a ring road down to
22 km/h from 40 is. The reference is global on purpose: derive it per selection and the colours
shift meaninglessly as the slider moves. Cells whose reference is under `REL_MIN_FF_KMH` report
nothing, because a ratio against a 5 km/h free-flow is noise over noise.

Sample counts per cell: a single month at a single hour has a median of 5 samples and only 31% of
cells at 15 or more, so the percentiles are firmest on the all-months views and on busy corridors —
the popup shows the sample count behind every cell, alongside its speed hour by hour.

## Keeping it current

Updating is manual, roughly monthly:

```bash
make update   # ingest the new days, rebuild, deploy
```

`ingest-all` only reads days with no `data/agg/*.parquet` yet, so a month costs a few minutes even
though the archive is 88 days deep. The map carries its own date range in the panel, so a stale
deploy says so rather than pretending to be current.

`data/` is git-ignored and is the only copy of ~330 MB that takes a couple of hours to rebuild from
scratch. `speedmap.sync` can mirror it into the same bucket under `derived/agg/` and
`derived/hist/`, which is worth doing before changing machines:

```bash
make push     # upload aggregates missing remotely
make pull     # download aggregates missing locally
```

Both are deliberately off the `make update` path — on the machine that already holds `data/` there
is nothing to fetch, and a first push is a 330 MB upload that should be a decision rather than a
side effect. The parquets are immutable once written, so either direction is a filename comparison
and nothing more.

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
| `TERMINAL_RADIUS_M` | 120 | The same, for the first and last stop of a trip. |
| `STALE_MAX_S` | 120 | Ghost-entity cutoff. |
| `SPEED_MAX_MPS` | 25 | Upper sanity bound. |
| `MIN_SAMPLES` | 5 | Cells with fewer samples are not published. |
| `HIST_BIN_KMH` | 1 | Histogram resolution, and so every percentile's resolution. |
| `FREE_FLOW_Q` | 0.85 | Which percentile counts as a cell's free-flow reference. |
| `REL_MIN_FF_KMH` | 8 | Below this reference speed, "% of free-flow" reports nothing. |
| `PROFILE_MIN_SAMPLES` | 3 | Per-hour floor for the popup sparkline (see below). |
| `PROFILE_MIN_HOURS` | 6 | Cells measured in fewer hours get no sparkline at all. |
| `DEPOT_MAX_KMH` | 4 | How slow a cell must be to be depot-like. |
| `DEPOT_MIN_SAMPLES` | 300 | How busy. |
| `DEPOT_MIN_HOURS` | 10 | In how many separate hours — what separates a yard from a jam. |
| `DEPOT_MIN_DIST_TO_STOP_M` | 120 | How far the nearest mid-route stop must be, so dwell is not mistaken for parking. |
| `DEPOT_PAD_M` | 40 | Grown onto each site's observed extent. |
| `SCALE_LOW_KMH` / `SCALE_HIGH_KMH` | 5 / 35 | Colour ramp ends. Measured cell speeds: p25 ≈ 13, p50 ≈ 21, p90 ≈ 41 km/h. |

Changing a filter means re-running `make ingest-all --force`; changing `MIN_SAMPLES`, a percentile
or the colour scale only needs `make build`.

`PROFILE_MIN_SAMPLES` is lower than `MIN_SAMPLES` because the sparkline splits a cell 19 ways: at
the map's own floor, half of all cells end up with three bars or fewer, which reads as "no buses ran
at 14:00" rather than "not measured". Dropping the floor to 3 lifts the median cell from 6 populated
hours to 9, and cells that still cannot fill `PROFILE_MIN_HOURS` get no sparkline — a two-bar chart
misleads more than it tells.

## Sharing a view

The whole selection lives in the query string, viewport included, so a link can point at one
junction rather than the whole city:

```
https://gtfs-speedmap.vbhjckfd.workers.dev/?month=2026-07&days=wd&hour=08&stat=rel&z=16&lat=49.8408&lon=24.0219
```

`month` takes `all` or `YYYY-MM`; `days` takes `all`, `wd` or `we`; `hour` takes `all` or `00`–`23`;
`stat` takes `v`, `med`, `p15`, `p85`, `spread` or `rel`. Anything unrecognised falls back to the
default rather than erroring, and a link written before an axis existed still opens — `days`
defaults to `all`. The canonical link stays on the bare homepage on purpose — one page for search
engines to index, not a thousand near-identical ones. Slider moves and pans use `replaceState`, so
dragging does not bury the previous page under twenty history entries.

With no query string at all, the map opens on the current hour **in Kyiv**, not the visitor's own
timezone, clamped to the nearest hour the collector actually polls.

## Known artefacts

- A depot that opened after the aggregates were last mined will show up as a red knot until
  `python -m speedmap.depots` is run again on unmasked data.
- Hours 00:00–04:00 are absent: the collector does not poll then.
- `static/` archives only start 2026-07-31, so earlier days are matched against the oldest archive.
  About 35% of May trip_ids no longer exist there, which is what the route+direction fallback covers.

## Licence

[WTFPL](LICENSE). Do what the fuck you want to.

The data behind it is not mine to relicense: vehicle positions come from the Lviv operator's
GTFS-RT feed, and the basemap is © OpenStreetMap contributors.

## Contact

[github.com/vbhjckfd/gtfs-speedmap](https://github.com/vbhjckfd/gtfs-speedmap) — issues and pull
requests welcome.
