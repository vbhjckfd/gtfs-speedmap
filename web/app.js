/* Lviv bus speed map.
 *
 * Draws one dot per 25 m cell on a canvas overlay. Leaflet's own vector layer
 * creates a path per marker, which stalls well below the ~40k cells a busy
 * hour produces, so the dots are painted straight onto a single canvas.
 */

const DATA = "data";
const LVIV = [49.8397, 24.0297];
const DOT_MIN_PX = 2.0;
const CACHE = new Map();

let index = null;
let current = null; // { lat, lon, v, n } for the visible selection
let requestSeq = 0;

const els = {
  month: document.getElementById("month"),
  daytype: document.getElementById("daytype"),
  hour: document.getElementById("hour"),
  hourLabel: document.getElementById("hour-label"),
  allHours: document.getElementById("all-hours"),
  metric: document.getElementById("metric"),
  subtitle: document.getElementById("subtitle"),
  note: document.getElementById("note"),
  ramp: document.getElementById("ramp"),
  scaleLow: document.getElementById("scale-low"),
  scaleHigh: document.getElementById("scale-high"),
  scaleUnit: document.getElementById("scale-unit"),
  ruler: document.getElementById("ruler"),
  rides: document.getElementById("rides"),
  rulerMetric: document.getElementById("ruler-metric"),
  rulerClear: document.getElementById("ruler-clear"),
  rulerButton: null, // the map control, created below
};

/* ---------- colour ---------- */

// Red (slow) → amber → green (fast), interpolated in RGB.
const STOPS = [
  [0.0, [214, 40, 40]],
  [0.5, [240, 166, 32]],
  [1.0, [46, 158, 74]],
];

function ramp(t) {
  const x = Math.max(0, Math.min(1, t));
  for (let i = 1; i < STOPS.length; i++) {
    const [p1, c1] = STOPS[i - 1];
    const [p2, c2] = STOPS[i];
    if (x <= p2) {
      const f = (x - p1) / (p2 - p1);
      return [
        Math.round(c1[0] + (c2[0] - c1[0]) * f),
        Math.round(c1[1] + (c2[1] - c1[1]) * f),
        Math.round(c1[2] + (c2[2] - c1[2]) * f),
      ];
    }
  }
  return STOPS[STOPS.length - 1][1];
}

// Each metric carries its own domain, because km/h, a ratio and a spread
// cannot share one scale. `invert` is for metrics whose high end is the bad
// end — a wide p85−p15 spread means unpredictable, so it colours red.
function activeMetric() {
  return index.metrics.find((m) => m.key === els.metric.value) || index.metrics[0];
}

function metricColor(value, metric) {
  const { low, high } = metric.scale;
  const t = (value - low) / (high - low);
  const [r, g, b] = ramp(metric.invert ? 1 - t : t);
  return `rgb(${r},${g},${b})`;
}

// The sparkline is always km/h whatever metric is on screen, so it keeps its
// own scale rather than borrowing the active one.
const KMH_METRIC = { scale: { low: 5, high: 35 }, invert: false, factor: 1, decimals: 1 };

function speedColor(kmh) {
  return metricColor(kmh, KMH_METRIC);
}

function format(value, metric) {
  if (value == null || Number.isNaN(value)) return "—";
  return (value * metric.factor).toFixed(metric.decimals);
}

function paintLegend() {
  const metric = activeMetric();
  const stops = STOPS.map(([p]) => {
    const [r, g, b] = ramp(metric.invert ? 1 - p : p);
    return `rgb(${r},${g},${b}) ${p * 100}%`;
  });
  els.ramp.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  els.scaleLow.textContent = `≤ ${format(metric.scale.low, metric)}`;
  els.scaleHigh.textContent = `≥ ${format(metric.scale.high, metric)}`;
  els.scaleUnit.textContent = metric.unit;
}

/* ---------- map ---------- */

// The panel occupies the top-left corner, where Leaflet puts the zoom buttons
// by default.
const map = L.map("map", { preferCanvas: true, zoomControl: false }).setView(LVIV, 13);
L.control.zoom({ position: "topright" }).addTo(map);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

function metresPerPixel() {
  const centre = map.getCenter();
  return (
    (40075016.686 * Math.cos((centre.lat * Math.PI) / 180)) /
    (256 * Math.pow(2, map.getZoom()))
  );
}

const DotLayer = L.Layer.extend({
  onAdd(m) {
    this._canvas = L.DomUtil.create("canvas", "leaflet-zoom-animated");
    this._ctx = this._canvas.getContext("2d");
    m.getPanes().overlayPane.appendChild(this._canvas);
    m.on("moveend zoomend resize", this._reset, this);
    if (m.options.zoomAnimation && L.Browser.any3d) {
      m.on("zoomanim", this._animateZoom, this);
    }
    this._reset();
  },

  onRemove(m) {
    L.DomUtil.remove(this._canvas);
    m.off("moveend zoomend resize", this._reset, this);
    m.off("zoomanim", this._animateZoom, this);
  },

  _animateZoom(e) {
    const scale = map.getZoomScale(e.zoom, map.getZoom());
    const offset = map._latLngBoundsToNewLayerBounds(map.getBounds(), e.zoom, e.center).min;
    L.DomUtil.setTransform(this._canvas, offset, scale);
  },

  _reset() {
    const size = map.getSize();
    const corner = map.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(this._canvas, corner);
    const dpr = window.devicePixelRatio || 1;
    this._canvas.width = size.x * dpr;
    this._canvas.height = size.y * dpr;
    this._canvas.style.width = `${size.x}px`;
    this._canvas.style.height = `${size.y}px`;
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.redraw();
  },

  // A cell is `cell_size_m` across; draw it at its true ground size so dots
  // merge into corridors when zoomed out and separate when zoomed in.
  _radiusPx() {
    return Math.max(DOT_MIN_PX, index.cell_size_m / metresPerPixel() / 2);
  },

  redraw() {
    const ctx = this._ctx;
    if (!ctx) return;
    const size = map.getSize();
    ctx.clearRect(0, 0, size.x, size.y);
    if (!current) return;

    const r = this._radiusPx();
    const bounds = map.getBounds().pad(0.05);
    const { lat, lon } = current;
    const values = speedValues();
    const metric = activeMetric();
    ctx.globalAlpha = 0.85;

    for (let i = 0; i < lat.length; i++) {
      if (values[i] == null) continue;
      if (lat[i] < bounds.getSouth() || lat[i] > bounds.getNorth()) continue;
      if (lon[i] < bounds.getWest() || lon[i] > bounds.getEast()) continue;
      const p = map.latLngToContainerPoint([lat[i], lon[i]]);
      ctx.fillStyle = metricColor(values[i], metric);
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  },
});

const dots = new DotLayer();

/* ---------- selection ---------- */

// The chosen statistic ships in the same file as every other one, so switching
// between them never refetches. Falls back to the average for data built before
// histograms existed.
function speedValues(payload) {
  const source = payload || current;
  if (!source) return [];
  return source[els.metric.value] || source.v;
}

function hourKey() {
  return els.allHours.checked ? "all" : String(els.hour.value).padStart(2, "0");
}

function selectionKey() {
  return `${els.month.value}-${els.daytype.value}-${hourKey()}`;
}

function profileKey() {
  return `profile-${els.month.value}-${els.daytype.value}`;
}

/* ---------- shareable URL ---------- */

// The selection lives in the query string so a view can be linked. The
// canonical link stays on the bare homepage, so search engines index one page
// rather than a hundred near-identical ones.
function writeUrl() {
  const centre = map.getCenter();
  const params = new URLSearchParams({
    month: els.month.value,
    days: els.daytype.value,
    hour: hourKey(),
    stat: els.metric.value,
    // The viewport too, so a link can point at one junction rather than the
    // whole city. 5 dp is well under a cell.
    z: String(map.getZoom()),
    lat: centre.lat.toFixed(5),
    lon: centre.lng.toFixed(5),
  });
  // replaceState, not pushState: dragging the slider must not bury the user's
  // previous page under twenty history entries.
  history.replaceState(null, "", `?${params}`);
}

// The hour bins are local to the feed's city, so "now" means now *there* —
// a visitor in another timezone still lands on the hour Lviv is living.
function currentHour() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: index.timezone,
    hour: "numeric",
    hour12: false,
  }).formatToParts(new Date());
  const hour = Number(parts.find((p) => p.type === "hour").value);
  // The collector sleeps 00:00–04:00, so those hours have no data at all;
  // land on the nearest hour that does.
  return index.hours.reduce(
    (best, h) => (Math.abs(h - hour) < Math.abs(best - hour) ? h : best),
    index.hours[0],
  );
}

function readUrl() {
  const params = new URLSearchParams(location.search);

  const month = params.get("month");
  if (month && [...els.month.options].some((o) => o.value === month)) {
    els.month.value = month;
  }

  const daytype = params.get("days");
  if (daytype && [...els.daytype.options].some((o) => o.value === daytype)) {
    els.daytype.value = daytype;
  }

  const zoom = Number(params.get("z"));
  const lat = Number(params.get("lat"));
  const lon = Number(params.get("lon"));
  if (Number.isFinite(zoom) && Number.isFinite(lat) && Number.isFinite(lon) && params.get("z")) {
    map.setView([lat, lon], zoom);
  }

  const hour = params.get("hour");
  if (hour === "all") {
    els.allHours.checked = true;
  } else if (hour !== null && index.hours.includes(Number(hour))) {
    els.hour.value = String(Number(hour));
  }

  const stat = params.get("stat");
  if (stat && [...els.metric.options].some((o) => o.value === stat)) {
    els.metric.value = stat;
  }
}

async function fetchSelection(key) {
  if (CACHE.has(key)) return CACHE.get(key);
  const promise = fetch(`${DATA}/${key}.json`)
    .then((r) => {
      if (!r.ok) throw new Error(`${key}: HTTP ${r.status}`);
      return r.json();
    })
    .catch((err) => {
      CACHE.delete(key);
      throw err;
    });
  CACHE.set(key, promise);
  return promise;
}

function prefetchNeighbours() {
  if (els.allHours.checked) return;
  const hours = index.hours;
  const at = hours.indexOf(Number(els.hour.value));
  [at - 1, at + 1].forEach((i) => {
    if (i >= 0 && i < hours.length) {
      const hour = String(hours[i]).padStart(2, "0");
      fetchSelection(`${els.month.value}-${els.daytype.value}-${hour}`).catch(() => {});
    }
  });
}

function describe(count) {
  const parts = [els.month.selectedOptions[0].textContent];
  // "All days" is the default, so saying so is noise; a restriction is not.
  if (els.daytype.value !== "all") parts.push(els.daytype.selectedOptions[0].textContent);
  const hour = String(els.hour.value).padStart(2, "0");
  parts.push(els.allHours.checked ? "all hours" : `${hour}:00–${hour}:59`);
  parts.push(`${count.toLocaleString()} cells`);
  parts.push(els.metric.selectedOptions[0].textContent.toLowerCase());
  return parts.join(" · ");
}

async function render() {
  const key = selectionKey();
  const seq = ++requestSeq;
  writeUrl();
  try {
    const payload = await fetchSelection(key);
    if (seq !== requestSeq) return; // a newer selection won
    current = payload;
    dots.redraw();
    els.subtitle.textContent = describe(payload.lat.length);
  } catch (err) {
    if (seq !== requestSeq) return;
    current = null;
    dots.redraw();
    els.subtitle.textContent = `no data for this selection (${err.message})`;
  }
  // A new hour is a new set of speeds, so a line already on the map is timed
  // again against them.
  if (RULER.on) rulerRedraw();
  prefetchNeighbours();
}

function syncHourLabel() {
  const disabled = els.allHours.checked;
  els.hour.disabled = disabled;
  els.hourLabel.textContent = disabled
    ? "all"
    : `${String(els.hour.value).padStart(2, "0")}:00`;
}

/* ---------- popup ---------- */

const METRES_PER_DEG_LAT = 111320;

// A popup click can afford to scan all ~40k cells; the ruler cannot, since it
// looks up a cell every 12 m along the line. Cells are bucketed once per
// payload and the lookup walks outwards a ring of buckets at a time.
const BUCKET_M = 100;
const GRIDS = new WeakMap();

function gridOf(source) {
  const cached = GRIDS.get(source);
  if (cached) return cached;
  const latStep = BUCKET_M / METRES_PER_DEG_LAT;
  const lonStep = latStep / Math.cos((LVIV[0] * Math.PI) / 180);
  const buckets = new Map();
  for (let i = 0; i < source.lat.length; i++) {
    const key = `${Math.floor(source.lat[i] / latStep)},${Math.floor(source.lon[i] / lonStep)}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(i);
    else buckets.set(key, [i]);
  }
  const grid = { latStep, lonStep, buckets };
  GRIDS.set(source, grid);
  return grid;
}

/**
 * Index of the point in a {lat, lon} pair of arrays nearest to `latlng`.
 * `maxDistance` bounds how far out to search; without it the search runs until
 * it finds something, which on an empty selection means the whole grid. It
 * bounds the search, not the answer — a hit slightly beyond it can still come
 * back, so callers decide what counts as near enough by checking `distance`.
 * Index is -1 and distance Infinity when the search found nothing at all.
 */
function nearestIndex(source, latlng, maxDistance) {
  const grid = gridOf(source);
  const cosLat = Math.cos((latlng.lat * Math.PI) / 180);
  const gy = Math.floor(latlng.lat / grid.latStep);
  const gx = Math.floor(latlng.lng / grid.lonStep);
  const rings = Number.isFinite(maxDistance) ? Math.ceil(maxDistance / BUCKET_M) + 1 : 64;
  let best = -1;
  let bestDist = Infinity;

  for (let ring = 0; ring <= rings; ring++) {
    // Nothing in this ring can be nearer than (ring − 1) buckets, so a hit
    // already inside that radius cannot be beaten by going further out.
    if (best >= 0 && Math.sqrt(bestDist) <= (ring - 1) * BUCKET_M) break;
    for (let dy = -ring; dy <= ring; dy++) {
      for (let dx = -ring; dx <= ring; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue; // interior already done
        const bucket = grid.buckets.get(`${gy + dy},${gx + dx}`);
        if (!bucket) continue;
        for (const i of bucket) {
          const my = (source.lat[i] - latlng.lat) * METRES_PER_DEG_LAT;
          const mx = (source.lon[i] - latlng.lng) * METRES_PER_DEG_LAT * cosLat;
          const d = mx * mx + my * my;
          if (d < bestDist) {
            bestDist = d;
            best = i;
          }
        }
      }
    }
  }
  return { index: best, distance: Math.sqrt(bestDist) };
}

// Speed by hour for one cell, drawn as bars on the same colour ramp as the map
// so a red bar in the sparkline means the same thing as a red dot on it.
function sparkline(values, hours, selected) {
  const known = values.filter((v) => v >= 0);
  if (!known.length) return "";
  const top = Math.max(...known);
  const width = 8;
  const gap = 1.6;
  const height = 34;
  const bars = values
    .map((value, i) => {
      const x = (i * (width + gap)).toFixed(1);
      if (value < 0) {
        return `<rect x="${x}" y="${height - 1}" width="${width}" height="1" fill="#c9d0d9"/>`;
      }
      const h = Math.max(1.5, (value / top) * height);
      const marked = hours[i] === selected;
      return (
        `<rect x="${x}" y="${(height - h).toFixed(1)}" width="${width}" height="${h.toFixed(1)}"` +
        ` fill="${speedColor(value)}"${marked ? ' stroke="#16202a" stroke-width="1.2"' : ""}/>`
      );
    })
    .join("");
  const span = values.length * (width + gap) - gap;
  return (
    `<svg class="spark" viewBox="0 0 ${span} ${height + 12}" width="${span}" ` +
    `height="${height + 12}" role="img" aria-label="Speed by hour of day">` +
    `${bars}<text x="0" y="${height + 10}" class="spark-tick">${hours[0]}:00</text>` +
    `<text x="${span}" y="${height + 10}" text-anchor="end" class="spark-tick">` +
    `${hours[hours.length - 1]}:00</text></svg>`
  );
}

async function cellProfile(latlng) {
  const profile = await fetchSelection(profileKey());
  if (!profile.lat.length) return "";
  const hit = nearestIndex(profile, latlng, index.cell_size_m);
  if (hit.index < 0 || hit.distance > index.cell_size_m) return "";
  const width = profile.hours.length;
  const values = profile.q.slice(hit.index * width, (hit.index + 1) * width);
  const selected = els.allHours.checked ? -1 : Number(els.hour.value);
  return sparkline(values, profile.hours, selected);
}

function popupContent(i, spark) {
  const metric = activeMetric();
  const value = speedValues()[i];
  const rows = index.metrics
    .filter((m) => m.key !== metric.key && current[m.key])
    .map((m) => `<span>${m.label}</span><span>${format(current[m.key][i], m)} ${m.unit}</span>`)
    .join("");
  return (
    `<div class="cell-popup"><b>${format(value, metric)} ${metric.unit}</b>` +
    `<div class="stat-label">${metric.label}</div>` +
    `<div class="stats">${rows}` +
    `<span>Samples</span><span>${current.n[i].toLocaleString()}</span></div>` +
    `<div class="spark-slot">${spark}</div></div>`
  );
}

map.on("click", (e) => {
  if (RULER.on) {
    rulerAddPoint(e.latlng);
    return;
  }
  if (!current) return;
  // A fixed metre tolerance is unclickable when zoomed in and grabs the wrong
  // cell when zoomed out, so allow a constant ~12 px of slop instead.
  const tolerance = Math.max(index.cell_size_m, metresPerPixel() * 12);
  const hit = nearestIndex(current, e.latlng, tolerance);
  if (hit.index < 0 || hit.distance > tolerance) return;

  const at = L.latLng(current.lat[hit.index], current.lon[hit.index]);
  const popup = L.popup()
    .setLatLng(at)
    .setContent(popupContent(hit.index, "speed by hour…"))
    .openOn(map);

  // The profile file is a megabyte or so, so it loads on the first click and is
  // reused after that; the popup opens immediately either way. It has to go
  // back through setContent rather than patching the DOM — Leaflet re-renders
  // the popup from the string it was given, so a patched node is wiped on the
  // next update and the popup never resizes around it.
  cellProfile(at)
    .then((svg) => {
      if (map.hasLayer(popup)) {
        popup.setContent(popupContent(hit.index, svg || "no hourly profile here"));
      }
    })
    .catch(() => {});
});

/* ---------- ruler ---------- */

// Two ends, and the buses that run between them. The line drawn on the map is
// a sight-line, not a route: what is measured is what the vehicles did.
const RULER = {
  on: false,
  points: [],
  vertices: [],
  line: null,
  layer: null,
};

function formatDuration(seconds) {
  const total = Math.round(seconds);
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} min ${String(total % 60).padStart(2, "0")} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
}

/* ---------- real rides ---------- */

// The drawn line above is an integral of the speed field: it prices a shape,
// not a journey, and by construction it cannot see a bus standing at a stop.
// This block answers the other question — of the buses that actually run past
// both ends, how long did they take between them — from stop-to-stop times
// measured on the archived vehicles themselves, dwell and lights included.
const RIDES = {
  paths: null,
  data: null,
  key: null,
  highlight: null,
  loading: false,
  expanded: false,
};

// How far a clicked point may be from a stop before that route is not really
// serving it. Roughly a five-minute walk.
const RIDE_NEAR_M = 500;
const RIDE_ROWS = 4;

function ridesKey() {
  return `rides-${els.month.value}-${els.daytype.value}-${hourKey()}`;
}

// Median or average of the observed leg times, whichever the dropdown says.
// The median is the default: journey times are right-skewed, so one bus that
// broke down moves the average and leaves the median alone.
function rideMetric() {
  return els.rulerMetric.value === "avg" ? "avg" : "med";
}

function metresBetween(lat1, lon1, lat2, lon2) {
  const dy = (lat1 - lat2) * METRES_PER_DEG_LAT;
  const dx = (lon1 - lon2) * METRES_PER_DEG_LAT * Math.cos((lat1 * Math.PI) / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

// Walking speed, for ranking routes by the whole journey. 5 km/h is the usual
// planning figure and is close enough for a few hundred metres.
const WALK_KMH = 5;

function walkSeconds(metres) {
  return metres / ((WALK_KMH * 1000) / 3600);
}

/** Where along a route's path a clicked point boards, or -1 if it does not. */
function boardingStop(path, latlng) {
  const stops = RIDES.paths.stops;
  let best = -1;
  let bestDist = RIDE_NEAR_M;
  for (let i = 0; i < path.length; i++) {
    const stop = stops[path[i]];
    if (!stop) continue;
    const d = metresBetween(latlng.lat, latlng.lng, stop[0], stop[1]);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return { index: best, distance: bestDist };
}

/**
 * Every route-direction running from A to B, timed on its own observations.
 *
 * A route counts only if both ends are near stops on its path *and* they are
 * in path order — the same pair of points is a different journey, on a
 * different set of legs, in the other direction.
 */
function rideOptions(a, b) {
  if (!RIDES.paths || !RIDES.data) return [];
  const metric = rideMetric();
  const out = [];

  for (const route of RIDES.paths.routes) {
    const legs = RIDES.data[`${route.route}|${route.dir}`];
    if (!legs) continue;
    const from = boardingStop(route.path, a);
    const to = boardingStop(route.path, b);
    if (from.index < 0 || to.index < 0 || from.index >= to.index) continue;

    const values = legs[metric] || legs.med;
    const slice = values.slice(from.index, to.index);
    const known = slice.filter((v) => v != null);
    if (!known.length) continue;
    // A leg nobody was observed on takes this route's own average leg instead
    // of vanishing, which would make a gappy route look like the fast one.
    const fill = known.reduce((sum, v) => sum + v, 0) / known.length;
    out.push({
      route,
      seconds: slice.reduce((sum, v) => sum + (v == null ? fill : v), 0),
      stops: to.index - from.index,
      holes: slice.length - known.length,
      observations: Math.min(...legs.n.slice(from.index, to.index)),
      walk: from.distance + to.distance,
      from: from.index,
      to: to.index,
    });
  }
  // Ordered by the whole journey, not the bus part of it: a route two minutes
  // quicker that leaves you 600 m further from the door is not quicker.
  return out.sort((x, y) => x.seconds + walkSeconds(x.walk) - (y.seconds + walkSeconds(y.walk)));
}

function highlightRide(option) {
  if (RIDES.highlight) {
    RULER.layer.removeLayer(RIDES.highlight);
    RIDES.highlight = null;
  }
  if (!option) return;
  const stops = RIDES.paths.stops;
  const points = option.route.path
    .slice(option.from, option.to + 1)
    .map((id) => stops[id])
    .filter(Boolean)
    .map((s) => [s[0], s[1]]);
  RIDES.highlight = L.polyline(points, {
    color: "#3b6ea5",
    weight: 5,
    opacity: 0.8,
    interactive: false,
  }).addTo(RULER.layer);
}

function rideRow(option, i) {
  const stops = RIDES.paths.stops;
  const board = stops[option.route.path[option.from]];
  const alight = stops[option.route.path[option.to]];
  const walk = Math.round(option.walk);
  return (
    `<button type="button" class="ride-row" data-ride="${i}">` +
    `<span class="ride-name">${option.route.name}</span>` +
    `<span class="ride-time">${formatDuration(option.seconds)}</span>` +
    `<span class="ride-detail">${board ? board[2] : "?"} → ${alight ? alight[2] : "?"}<br>` +
    `${option.stops} stop${option.stops === 1 ? "" : "s"} · ${walk} m walk ` +
    `(${formatDuration(walkSeconds(option.walk))}) · ${option.observations} rides seen` +
    `${option.holes ? ` · ${option.holes} leg(s) unobserved` : ""}</span>` +
    `</button>`
  );
}

function ridesReadout() {
  if (!index.rides) {
    return `<div class="ride-empty">This build carries no measured ride times.</div>`;
  }
  if (RULER.points.length === 0) return `<div class="ride-empty">Click the map to set where you are.</div>`;
  if (RULER.points.length === 1) {
    return `<div class="ride-empty">Click again for where you are going. Drag either end to move it.</div>`;
  }
  // Stale figures from the previous hour are worse than none while the new
  // ones are on their way.
  if (!RIDES.paths || !RIDES.data || RIDES.key !== ridesKey()) {
    return `<div class="ride-empty">looking for buses…</div>`;
  }

  const options = rideOptions(RULER.points[0], RULER.points[RULER.points.length - 1]);
  if (!options.length) {
    return (
      `<div class="ride-empty">No bus runs from the first end to the last one — ` +
      `no route has a stop within ${RIDE_NEAR_M} m of both, in that order.</div>`
    );
  }
  // The slower ones are still ways of making the journey, so the tail folds
  // away rather than disappearing — the box opens short, not short of answers.
  const limit = RIDES.expanded ? options.length : RIDE_ROWS;
  const shown = options.slice(0, limit).map(rideRow).join("");
  const rest = options.length - limit;
  const source = rideMetric() === "avg" ? "average" : "median";
  const more =
    rest > 0
      ? `<button type="button" class="ride-more" data-expand="1">` +
        `Show ${rest} slower route${rest === 1 ? "" : "s"}</button>`
      : RIDES.expanded && options.length > RIDE_ROWS
        ? `<button type="button" class="ride-more" data-expand="0">Show fewer</button>`
        : "";
  return (
    `<div class="ride-head">On the bus <span>${source} of observed rides</span></div>` +
    shown +
    more
  );
}

// The route list and the geometry are only fetched once the ruler is used, and
// the geometry only ever once — it does not change with the selection.
async function loadRides() {
  const key = ridesKey();
  // Dragging a point redraws on every frame; without the flag each frame would
  // queue another pair of loads for the same selection.
  RIDES.loading = true;
  try {
    const [paths, data] = await Promise.all([
      RIDES.paths ? RIDES.paths : fetchSelection("paths"),
      RIDES.key === key && RIDES.data ? RIDES.data : fetchSelection(key),
    ]);
    RIDES.paths = paths;
    RIDES.data = data;
    RIDES.key = key;
  } finally {
    RIDES.loading = false;
  }
}

function rulerRedraw() {
  if (RULER.line) RULER.line.setLatLngs(RULER.points);
  // Re-rendering the list drops the active row, so the stretch it drew goes
  // with it rather than hanging over a line that has since moved.
  highlightRide(null);
  els.rides.innerHTML = ridesReadout();
  if (index.rides && RULER.points.length >= 2 && !RIDES.loading && RIDES.key !== ridesKey()) {
    // Fetched on demand, and only the first time a line is drawn — the ride
    // files and the route geometry are dead weight for a visitor who only ever
    // looks at the map.
    loadRides()
      .then(() => {
        els.rides.innerHTML = ridesReadout();
      })
      .catch(() => {
        els.rides.innerHTML = `<div class="ride-empty">ride times unavailable</div>`;
      });
  }
}

function rulerAddPoint(latlng) {
  // A journey has two ends. A third click is the start of the next one rather
  // than a waypoint on this one — nothing in between is measured anyway, since
  // the routes are timed stop to stop along their own paths.
  if (RULER.points.length >= 2) rulerClear();
  const at = RULER.points.length;
  RULER.points.push(latlng);

  const marker = L.marker(latlng, {
    draggable: true,
    keyboard: false,
    icon: L.divIcon({ className: "ruler-vertex", iconSize: [11, 11] }),
  }).addTo(RULER.layer);
  // Grabbing a point must move it, not drop a new one underneath it.
  marker.on("click", (e) => L.DomEvent.stop(e));
  marker.on("drag", () => {
    RULER.points[at] = marker.getLatLng();
    rulerRedraw();
  });
  RULER.vertices.push(marker);
  rulerRedraw();
}

function rulerUndo() {
  const marker = RULER.vertices.pop();
  if (!marker) return;
  RULER.layer.removeLayer(marker);
  RULER.points.pop();
  rulerRedraw();
}

function rulerClear() {
  RULER.vertices.forEach((marker) => RULER.layer.removeLayer(marker));
  RULER.vertices = [];
  RULER.points = [];
  RIDES.expanded = false; // a new journey opens on its best few again
  highlightRide(null);
  rulerRedraw();
}

function rulerToggle(on) {
  RULER.on = on;
  els.ruler.hidden = !on;
  document.body.classList.toggle("ruler-on", on);
  els.rulerButton.classList.toggle("active", on);
  els.rulerButton.setAttribute("aria-pressed", String(on));
  // A double click is how a line gets finished elsewhere; here it must not zoom.
  if (on) {
    map.doubleClickZoom.disable();
    map.closePopup();
  } else {
    map.doubleClickZoom.enable();
    rulerClear();
  }
}

const RulerControl = L.Control.extend({
  onAdd() {
    const container = L.DomUtil.create("div", "leaflet-bar ruler-toggle");
    const link = L.DomUtil.create("a", "", container);
    link.href = "#";
    link.title = "Measure ride time between two points";
    link.setAttribute("role", "button");
    link.setAttribute("aria-pressed", "false");
    link.setAttribute("aria-label", "Measure ride time");
    link.innerHTML =
      '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">' +
      '<path fill="none" stroke="currentColor" stroke-width="1.3" ' +
      'd="M1.7 10.3 10.3 1.7l4 4-8.6 8.6z"/>' +
      '<path fill="none" stroke="currentColor" stroke-width="1.1" ' +
      'd="M4.5 8.4 6.1 10M6.7 6.2 8.3 7.8M8.9 4l1.6 1.6"/></svg>';
    L.DomEvent.on(link, "click", (e) => {
      L.DomEvent.stop(e);
      rulerToggle(!RULER.on);
    });
    L.DomEvent.disableClickPropagation(container);
    els.rulerButton = link;
    return container;
  },
});

function rulerBoot() {
  RULER.layer = L.layerGroup().addTo(map);
  RULER.line = L.polyline([], {
    color: "#16202a",
    weight: 3,
    opacity: 0.85,
    dashArray: "6 5",
    interactive: false,
  }).addTo(RULER.layer);
  new RulerControl({ position: "topright" }).addTo(map);

  for (const metric of (index.rides && index.rides.metrics) || []) {
    const option = document.createElement("option");
    option.value = metric.key;
    option.textContent = metric.label;
    els.rulerMetric.append(option);
  }
  els.rulerMetric.value = "med";

  els.rulerMetric.addEventListener("change", rulerRedraw);
  els.rulerClear.addEventListener("click", rulerClear);

  // Picking a route draws the stretch of it being timed, which is the only way
  // to see that the fast one is fast because it runs a different street.
  els.rides.addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-expand]");
    if (toggle) {
      RIDES.expanded = toggle.dataset.expand === "1";
      els.rides.innerHTML = ridesReadout();
      return;
    }
    const row = e.target.closest("[data-ride]");
    if (!row) return;
    const options = rideOptions(RULER.points[0], RULER.points[RULER.points.length - 1]);
    const chosen = options[Number(row.dataset.ride)];
    const already = row.classList.contains("active");
    els.rides.querySelectorAll(".ride-row").forEach((r) => r.classList.remove("active"));
    if (already || !chosen) {
      highlightRide(null);
      return;
    }
    row.classList.add("active");
    highlightRide(chosen);
  });
  document.addEventListener("keydown", (e) => {
    if (!RULER.on) return;
    if (e.key === "Escape") {
      if (RULER.points.length) rulerClear();
      else rulerToggle(false);
    } else if (e.key === "Backspace" || e.key === "Delete") {
      e.preventDefault();
      rulerUndo();
    }
  });
  rulerRedraw();
}

/* ---------- boot ---------- */

async function boot() {
  index = await fetch(`${DATA}/index.json`).then((r) => r.json());

  for (const month of index.months) {
    const option = document.createElement("option");
    option.value = month.key;
    option.textContent = `${month.label} (${month.days}d)`;
    els.month.append(option);
  }
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = `All months (${index.days.count}d)`;
  els.month.append(all);
  els.month.value = "all";

  for (const daytype of index.daytypes) {
    const option = document.createElement("option");
    option.value = daytype.key;
    option.textContent = `${daytype.label} (${daytype.days}d)`;
    els.daytype.append(option);
  }
  els.daytype.value = "all";

  for (const metric of index.metrics) {
    const option = document.createElement("option");
    option.value = metric.key;
    option.textContent = metric.label;
    els.metric.append(option);
  }

  KMH_METRIC.scale = { low: index.scale.low_kmh, high: index.scale.high_kmh };

  const hours = index.hours;
  els.hour.min = String(hours[0]);
  els.hour.max = String(hours[hours.length - 1]);
  els.hour.value = String(currentHour());

  readUrl();
  paintLegend();
  syncHourLabel();
  rulerBoot();
  els.note.textContent =
    `Buses only. Positions within ${index.stop_radius_m} m of a stop on the ` +
    `vehicle's own trip are excluded, so this is running speed. ` +
    `${index.days.first} … ${index.days.last}, ` +
    `${index.samples.toLocaleString()} samples.`;

  dots.addTo(map);
  await render();

  els.month.addEventListener("change", render);
  els.daytype.addEventListener("change", render);
  els.hour.addEventListener("input", () => {
    syncHourLabel();
    render();
  });
  els.allHours.addEventListener("change", () => {
    syncHourLabel();
    render();
  });
  // Every statistic is already in the loaded payload, so this is a repaint.
  els.metric.addEventListener("change", () => {
    writeUrl();
    paintLegend();
    dots.redraw();
    if (current) els.subtitle.textContent = describe(current.lat.length);
  });

  // Panning is not a data change, so it only needs the URL rewritten — and not
  // on every frame of a drag.
  let pending = null;
  map.on("moveend", () => {
    clearTimeout(pending);
    pending = setTimeout(writeUrl, 300);
  });
}

boot().catch((err) => {
  els.subtitle.textContent = `failed to load: ${err.message}`;
});
