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

## Average or median

Each day writes two parquets: `data/agg/` keeps the running sums, `data/hist/` keeps a speed
histogram per cell in `HIST_BIN_KMH` bins. Both statistics ship in the same JSON, so the map's
Statistic dropdown repaints without refetching, and any other percentile can be added later without
touching R2 again.

The average is the default: it is time-weighted mean speed, the figure that corresponds to how long
a journey actually takes. The median ignores the tail of waits at lights and in queues, which makes
it a better read on free-flow conditions and a worse one on delay. Measured sample counts per cell:
a single month at a single hour has a median of 5 samples per cell and only 31% of cells at 15 or
more, so the median is firmest on the all-months views and on busy corridors — the popup shows the
sample count behind every cell.

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
| `HIST_BIN_KMH` | 1 | Histogram resolution, and so the median's resolution. |
| `DEPOT_MAX_KMH` | 4 | How slow a cell must be to be depot-like. |
| `DEPOT_MIN_SAMPLES` | 300 | How busy. |
| `DEPOT_MIN_HOURS` | 10 | In how many separate hours — what separates a yard from a jam. |
| `DEPOT_MIN_DIST_TO_STOP_M` | 120 | How far the nearest mid-route stop must be, so dwell is not mistaken for parking. |
| `DEPOT_PAD_M` | 40 | Grown onto each site's observed extent. |
| `SCALE_LOW_KMH` / `SCALE_HIGH_KMH` | 5 / 35 | Colour ramp ends. Measured cell speeds: p25 ≈ 13, p50 ≈ 21, p90 ≈ 41 km/h. |

Changing a filter means re-running `make ingest-all --force`; changing `MIN_SAMPLES` or the colour
scale only needs `make build`.

## Sharing a view

The month, hour and statistic live in the query string, so any view can be linked:

```
https://gtfs-speedmap.vbhjckfd.workers.dev/?month=2026-07&hour=08&stat=med
```

`month` takes `all` or `YYYY-MM`, `hour` takes `all` or `00`–`23`, `stat` takes `v` (average) or
`med` (median). Anything unrecognised falls back to the default rather than erroring. The canonical
link stays on the bare homepage on purpose — one page for search engines to index, not a hundred
near-identical ones. Slider moves use `replaceState`, so dragging it does not bury the previous page
under twenty history entries.

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
