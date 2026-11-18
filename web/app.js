/* Lviv bus ride times.
 *
 * Every stop-to-stop leg of the bus network, drawn along the route's own line
 * and coloured by how much longer it takes at the chosen hour than at its own
 * quickest hour of the day. The legs are painted onto one canvas: a few
 * thousand Leaflet polylines would each be a DOM path, redrawn on every pan.
 */

const DATA = "data";
const LVIV = [49.8397, 24.0297];
// The network with a margin round it. A shared link outside it is a typo, not
// a view, and panning past it only finds blank map.
const BOUNDS = L.latLngBounds([49.55, 23.6], [50.1, 24.45]);
// A leg this much slower than its off-peak is drawn fully red.
const DELAY_HIGH = 1.0;
// Click slop around a drawn leg, in pixels.
const HIT_PX = 8;
const CACHE = new Map();

let index = null;
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

// 0 = as quick as the leg ever gets (green), DELAY_HIGH and up = red.
function delayColor(delay) {
  const [r, g, b] = ramp(1 - delay / DELAY_HIGH);
  return `rgb(${r},${g},${b})`;
}

function paintLegend() {
  const stops = STOPS.map(([p]) => {
    const [r, g, b] = ramp(1 - p);
    return `rgb(${r},${g},${b}) ${p * 100}%`;
  });
  els.ramp.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  els.scaleLow.textContent = "+0%";
  els.scaleUnit.textContent = t("longer");
  els.scaleHigh.textContent = `≥ +${Math.round(DELAY_HIGH * 100)}%`;
}

function formatPercent(delay) {
  return `+${Math.round(Math.max(0, delay) * 100)}%`;
}

/* ---------- map ---------- */

// The panel occupies the top-left corner, where Leaflet puts the zoom buttons
// by default.
const map = L.map("map", {
  preferCanvas: true,
  // Leaflet's tile fade-in sticks part-way on protomaps-leaflet's async tiles.
  fadeAnimation: false,
  zoomControl: false,
  maxBounds: BOUNDS,
  minZoom: 10,
}).setView(LVIV, 13);
const zoomControl = L.control.zoom({ position: "topright" }).addTo(map);

// Same basemap as lad.lviv.ua: the Protomaps basemap drawn by protomaps-leaflet from
// one .pmtiles archive on our own R2 bucket. The archive covers only the Lviv metro
// area (TILE_BOUNDS, its extract bbox), so nothing is drawn or fetched past it.
const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const TILE_BOUNDS = [[49.68, 23.75], [50.0, 24.35]];
if (window.protomapsL) {
  protomapsL
    .leafletLayer({
      url: "https://tiles.lad.lviv.ua/lviv-basemap-20260828.pmtiles",
      flavor: "light",
      lang: "uk",
      maxDataZoom: 15,
      bounds: TILE_BOUNDS,
      attribution: `${OSM_ATTRIBUTION} · <a href="https://protomaps.com">Protomaps</a>`,
    })
    .addTo(map);
} else {
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: `${OSM_ATTRIBUTION} contributors`,
  }).addTo(map);
}

const METRES_PER_DEG_LAT = 111320;

function metresBetween(lat1, lon1, lat2, lon2) {
  const dy = (lat1 - lat2) * METRES_PER_DEG_LAT;
  const dx = (lon1 - lon2) * METRES_PER_DEG_LAT * Math.cos((lat1 * Math.PI) / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

/* ---------- loaded month ---------- */

// One month and day type at a time: its route geometry, every hour's leg
// times (the off-peak needs them all), and the whole-day file. The ruler reads
// the same state.
const RIDES = {
  key: null, // `${month}-${daytype}` this state belongs to
  paths: null,
  hourly: null, // Map "HH" → rides payload
  whole: null,
  legs: null, // stop pairs built from `paths`
  highlight: null,
  expanded: false,
};

function monthKey() {
  return `${els.month.value}-${els.daytype.value}`;
}

function hourKey() {
  return els.allHours.checked ? "all" : String(els.hour.value).padStart(2, "0");
}

// The payload for the hour on screen, or null while it loads.
function currentRides() {
  if (RIDES.key !== monthKey()) return null;
  return hourKey() === "all" ? RIDES.whole : RIDES.hourly.get(hourKey()) || null;
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

async function loadMonth() {
  const key = monthKey();
  if (RIDES.key === key) return;
  const hours = index.hours.map((h) => String(h).padStart(2, "0"));
  const [paths, whole, ...hourly] = await Promise.all([
    fetchSelection(`paths-${els.month.value}`),
    fetchSelection(`rides-${key}-all`),
    // allSettled: an hour that fails to load drops out of the off-peak
    // comparison rather than blanking the map.
    ...hours.map((h) =>
      fetchSelection(`rides-${key}-${h}`).then(
        (v) => v,
        () => null,
      ),
    ),
  ]);
  if (key !== monthKey()) return; // a newer selection won
  if (RIDES.paths !== paths) {
    RIDES.paths = paths; // routeLine reads it while the legs are built
    RIDES.legs = buildLegs(paths);
  }
  RIDES.whole = whole;
  RIDES.hourly = new Map(hours.flatMap((h, i) => (hourly[i] ? [[h, hourly[i]]] : [])));
  RIDES.key = key;
  RIDES.expanded = false;
}

/* ---------- legs ---------- */

/**
 * Every distinct stop-to-stop pair in the month's network, each with the line
 * it is drawn along and the route legs that drive it.
 *
 * Several routes share most of their legs through the centre; pooled, they are
 * one stretch of street with one ride time, which is what the map is about.
 * The line is cut from the first route's own shape between the two stops.
 */
function buildLegs(paths) {
  const byPair = new Map();
  paths.routes.forEach((route, r) => {
    for (let i = 0; i + 1 < route.path.length; i++) {
      const a = route.path[i];
      const b = route.path[i + 1];
      const pair = `${a}>${b}`;
      let leg = byPair.get(pair);
      if (!leg) {
        leg = { a, b, points: legLine(paths, route, i), members: [] };
        if (leg.points.length < 2) continue;
        leg.box = L.latLngBounds(leg.points);
        byPair.set(pair, leg);
      }
      leg.members.push({ r, i });
    }
  });
  return [...byPair.values()];
}

function legLine(paths, route, i) {
  const stops = paths.stops;
  const straight = [stops[route.path[i]], stops[route.path[i + 1]]]
    .filter(Boolean)
    .map((s) => [s[0], s[1]]);
  if (!route.shape || !route.dist) return straight;
  const { points, cum } = routeLine(route);
  const from = route.dist[i];
  const to = route.dist[i + 1];
  if (!(to > from)) return straight;
  const slice = [pointAlong(points, cum, from)];
  for (let k = 0; k < points.length; k++) {
    if (cum[k] > from && cum[k] < to) slice.push(points[k]);
  }
  slice.push(pointAlong(points, cum, to));
  return slice;
}

/**
 * One leg's ride time in a payload: the n-weighted mean of the chosen statistic
 * across every route that drives it.
 *
 * shortcut: a pooled median is approximated by the n-weighted mean of the
 * per-route medians; exact would need the per-route histograms shipped, which
 * is only worth it if routes sharing a leg turn out to differ a lot.
 */
function legTime(leg, data, metric) {
  if (!data) return null;
  let sum = 0;
  let n = 0;
  for (const { r, i } of leg.members) {
    const route = RIDES.paths.routes[r];
    const legs = data[`${route.route}|${route.dir}`];
    const v = legs && legs[metric] && legs[metric][i];
    if (v == null) continue;
    const w = legs.n[i] || 0;
    sum += v * w;
    n += w;
  }
  return n ? { seconds: sum / n, n } : null;
}

// The leg as it goes when the road is at its emptiest: its quickest hour with
// enough rides to be believed. Cached on the leg per month and statistic.
function legOffPeak(leg, metric) {
  const key = `${RIDES.key}|${metric}`;
  if (leg.offKey === key) return leg.off;
  let best = null;
  for (const [hour, data] of RIDES.hourly) {
    const t = legTime(leg, data, metric);
    if (!t || t.n < OFFPEAK_MIN_RIDES) continue;
    if (!best || t.seconds < best.seconds) best = { seconds: t.seconds, hour };
  }
  leg.offKey = key;
  leg.off = best;
  return best;
}

/* ---------- canvas ---------- */

const LegLayer = L.Layer.extend({
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

  // Thin when zoomed out, so a corridor of shared legs stays legible.
  _widthPx() {
    return Math.max(1.5, Math.min(5, map.getZoom() - 10.5));
  },

  /**
   * Paints every leg in view and keeps its screen line for the click test.
   *
   * Right-hand traffic: each leg is nudged to the right of its own direction,
   * so the two directions of a street lie side by side instead of on top of
   * each other.
   */
  redraw() {
    const ctx = this._ctx;
    if (!ctx) return;
    const size = map.getSize();
    ctx.clearRect(0, 0, size.x, size.y);
    this.drawn = [];
    const data = currentRides();
    if (!data) return;
    const metric = els.metric.value;
    const width = this._widthPx();
    const shift = width * 0.8;
    const bounds = map.getBounds().pad(0.05);
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    for (const leg of RIDES.legs) {
      if (!bounds.intersects(leg.box)) continue;
      const t = legTime(leg, data, metric);
      if (!t) continue;
      const off = legOffPeak(leg, metric);
      const screen = offsetLine(
        leg.points.map((p) => map.latLngToContainerPoint(p)),
        shift,
      );
      ctx.strokeStyle = off ? delayColor(t.seconds / off.seconds - 1) : "#9aa5b1";
      ctx.beginPath();
      screen.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.stroke();
      this.drawn.push({ leg, screen });
    }
  },
});

// Each vertex moved `shift` px along the right-hand normal of its segment.
function offsetLine(pts, shift) {
  return pts.map((p, k) => {
    const a = pts[Math.max(0, k - 1)];
    const b = pts[Math.min(pts.length - 1, k + 1)];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    // Screen y grows downwards, so (−dy, dx) is the right-hand side.
    return { x: p.x - (dy / len) * shift, y: p.y + (dx / len) * shift };
  });
}

function distanceToSegment(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2)) : 0;
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

function nearestLeg(point) {
  let best = null;
  let bestPx = HIT_PX;
  for (const { leg, screen } of legsLayer.drawn || []) {
    for (let k = 0; k + 1 < screen.length; k++) {
      const d = distanceToSegment(point, screen[k], screen[k + 1]);
      if (d < bestPx) {
        bestPx = d;
        best = leg;
      }
    }
  }
  return best;
}

const legsLayer = new LegLayer();

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
    lang: LANG,
    // The viewport too, so a link can point at one junction rather than the
    // whole city.
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

// Day counts for the month on screen, so "Weekdays" says how many weekdays that
// month holds rather than the whole archive.
function labelDaytypes() {
  const month = index.months.find((m) => m.key === els.month.value);
  for (const option of els.daytype.options) {
    const daytype = index.daytypes.find((d) => d.key === option.value);
    const days = month?.daytypes?.[option.value] ?? daytype.days;
    option.textContent = `${t("daytype")[option.value] ?? daytype.label} (${t("dayCount", { n: days })})`;
  }
}

// The newest month the data covers from its first day to its last. The month
// still being collected is partial, and so is the one collection started in;
// either would make a thin default. Falls back to the newest month.
function lastFullMonth() {
  const full = index.months.filter((m) => {
    const [y, mo] = m.key.split("-").map(Number);
    const first = `${m.key}-01`;
    const last = `${m.key}-${String(new Date(Date.UTC(y, mo, 0)).getUTCDate()).padStart(2, "0")}`;
    return first >= index.days.first && last <= index.days.last;
  });
  const pick = full.length ? full : index.months;
  return pick[pick.length - 1].key;
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
  if (
    params.get("z") &&
    Number.isFinite(zoom) &&
    Number.isFinite(lat) &&
    Number.isFinite(lon) &&
    BOUNDS.contains([lat, lon])
  ) {
    // Not animated: render() writes the URL straight after this, and mid-
    // animation the map still reports the old view, which would overwrite the
    // link's own viewport with the default one.
    map.setView([lat, lon], Math.min(19, Math.max(10, zoom)), { animate: false });
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

function describe() {
  const parts = [els.month.selectedOptions[0].textContent];
  parts.push(els.daytype.selectedOptions[0].textContent);
  const hour = String(els.hour.value).padStart(2, "0");
  parts.push(els.allHours.checked ? t("allHours") : `${hour}:00–${hour}:59`);
  parts.push(t("legsInView", { n: num((legsLayer.drawn || []).length) }));
  parts.push(els.metric.selectedOptions[0].textContent.toLocaleLowerCase());
  return parts.join(" · ");
}

function repaint() {
  legsLayer.redraw();
  els.subtitle.textContent = describe();
  // A new hour is a new set of times, so a line already on the map is timed
  // again against them.
  if (RULER.on) rulerRedraw();
}

async function render() {
  const seq = ++requestSeq;
  writeUrl();
  // An open popup describes one leg of the old selection.
  map.closePopup();
  if (RIDES.key !== monthKey()) els.subtitle.textContent = t("loading");
  try {
    await loadMonth();
    if (seq !== requestSeq) return;
    repaint();
  } catch (err) {
    if (seq !== requestSeq) return;
    RIDES.key = null;
    legsLayer.redraw();
    els.subtitle.textContent = t("noData", { msg: err.message });
  }
}

function syncHourLabel() {
  const disabled = els.allHours.checked;
  els.hour.disabled = disabled;
  els.hourLabel.textContent = disabled
    ? t("all")
    : `${String(els.hour.value).padStart(2, "0")}:00`;
}

/* ---------- popup ---------- */

// Ride time by hour for one leg, bar height the time and colour its delay, so a
// red bar in the sparkline means the same thing as a red leg on the map.
function sparkline(leg, metric, off) {
  const hours = index.hours;
  const values = hours.map((h) => {
    const t = legTime(leg, RIDES.hourly.get(String(h).padStart(2, "0")), metric);
    return t ? t.seconds : null;
  });
  const known = values.filter((v) => v != null);
  if (!known.length) return "";
  const top = Math.max(...known);
  const selected = els.allHours.checked ? -1 : Number(els.hour.value);
  const width = 8;
  const gap = 1.6;
  const height = 34;
  const bars = values
    .map((value, i) => {
      const x = (i * (width + gap)).toFixed(1);
      if (value == null) {
        return `<rect x="${x}" y="${height - 1}" width="${width}" height="1" fill="#c9d0d9"/>`;
      }
      const h = Math.max(1.5, (value / top) * height);
      const fill = off ? delayColor(value / off.seconds - 1) : "#9aa5b1";
      const marked = hours[i] === selected;
      return (
        `<rect x="${x}" y="${(height - h).toFixed(1)}" width="${width}" height="${h.toFixed(1)}"` +
        ` fill="${fill}"${marked ? ' stroke="#16202a" stroke-width="1.2"' : ""}>` +
        `<title>${hours[i]}:00 · ${formatDuration(value)}</title></rect>`
      );
    })
    .join("");
  const span = values.length * (width + gap) - gap;
  return (
    `<svg class="spark" viewBox="0 0 ${span} ${height + 12}" width="${span}" ` +
    `height="${height + 12}" role="img" aria-label="${t("sparkLabel")}">` +
    `${bars}<text x="0" y="${height + 10}" class="spark-tick">${hours[0]}:00</text>` +
    `<text x="${span}" y="${height + 10}" text-anchor="end" class="spark-tick">` +
    `${hours[hours.length - 1]}:00</text></svg>`
  );
}

function popupContent(leg) {
  const metric = els.metric.value;
  const data = currentRides();
  const time = legTime(leg, data, metric);
  const off = legOffPeak(leg, metric);
  const stops = RIDES.paths.stops;
  const name = (id) => (stops[id] ? stops[id][2] : id);
  const label = els.metric.selectedOptions[0].textContent.toLocaleLowerCase();
  const offRow = off
    ? `<span>${t("offPeakAt", { hour: off.hour })}</span><span>${formatDuration(off.seconds)}</span>` +
      (time
        ? `<span>${t("delay")}</span><span>${formatPercent(time.seconds / off.seconds - 1)}</span>`
        : "")
    : `<span>${t("offPeak")}</span><span>${t("tooFew")}</span>`;
  // Each route on its own: a shared leg can hide one route that stops longer.
  const routes = leg.members
    .map(({ r, i }) => {
      const route = RIDES.paths.routes[r];
      const legs = data && data[`${route.route}|${route.dir}`];
      const v = legs && legs[metric] && legs[metric][i];
      return v == null
        ? ""
        : `<span>${route.name}</span><span>${formatDuration(v)} · ${t("rides", { n: legs.n[i] })}</span>`;
    })
    .join("");
  return (
    `<div class="cell-popup"><b>${time ? formatDuration(time.seconds) : "—"}</b>` +
    `<div class="stat-label">${name(leg.a)} → ${name(leg.b)}<br>` +
    `${t("legSummary", { label, n: time ? num(time.n) : 0 })}</div>` +
    `<div class="stats">${offRow}${routes}</div>` +
    `<div class="spark-slot">${sparkline(leg, metric, off)}</div></div>`
  );
}

map.on("click", (e) => {
  if (RULER.on) {
    rulerAddPoint(e.latlng);
    return;
  }
  const leg = nearestLeg(map.latLngToContainerPoint(e.latlng));
  if (!leg) return;
  L.popup().setLatLng(e.latlng).setContent(popupContent(leg)).openOn(map);
});


/* ---------- ruler ---------- */

// Two ends, and the buses that run between them. The line drawn on the map is
// a sight-line, not a route: what is measured is what the vehicles did.
const RULER = {
  on: false,
  points: [],
  vertices: [],
  line: null,
  arrow: null,
  layer: null,
};

function formatDuration(seconds) {
  const total = Math.round(seconds);
  if (total < 60) return `${total} ${t("sec")}`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} ${t("min")} ${String(total % 60).padStart(2, "0")} ${t("sec")}`;
  return `${Math.floor(minutes / 60)} ${t("hr")} ${String(minutes % 60).padStart(2, "0")} ${t("min")}`;
}

/* ---------- real rides ---------- */

// Of the buses that actually run past both ends, how long did they take
// between them — from stop-to-stop times measured on the archived vehicles
// themselves, dwell and lights included.
// How far a clicked point may be from the route's own line before that route is
// not really passing it. This is distance to the street the bus drives, not to a
// stop, so it is far tighter than a walking radius would be — at 250 m a route
// on the next street over qualified, and then reported its own longer way round.
const RIDE_NEAR_M = 150;
// And how far round the houses it may go between the two points, against the
// straight line between them.
const RIDE_DETOUR_MAX = 2.0;
const RULER_PANE = "rulerPane";
const RIDE_ROWS = 4;
// An hour counts as a stretch's off-peak only on this many rides: a quiet
// 05:00 with three buses in it is a lucky draw, not the road at its emptiest.
const OFFPEAK_MIN_RIDES = 10;

/**
 * The quickest hour of the day over the same stretch, on the same statistic —
 * the ride as it goes when the road is at its emptiest. Null until every hour
 * has loaded, or when no hour has enough rides to be believed.
 */
function offPeak(key, stopDist, from, to, metric) {
  let best = null;
  for (const [hour, data] of RIDES.hourly) {
    const legs = data[key];
    if (!legs) continue;
    const timed = timeAlongRoute(legs, stopDist, from, to, metric);
    if (!timed || timed.observations < OFFPEAK_MIN_RIDES) continue;
    if (!best || timed.seconds < best.seconds) best = { seconds: timed.seconds, hour };
  }
  return best;
}

function formatDelta(seconds) {
  const total = Math.round(Math.abs(seconds));
  const sign = seconds < 0 ? "−" : "+";
  return `${sign} ${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * The line a route drives and where each of its stops sits along it.
 *
 * Routes carry the schedule's own shape. The handful whose stops could not be
 * matched to one fall back to the chain of their stops, which is the same idea
 * at lower resolution: a line to measure position along.
 */
function routeLine(route) {
  if (route.line) return route.line;
  const stops = RIDES.paths.stops;
  let points = route.shape;
  let stopDist = route.dist;
  if (!points || !stopDist) {
    points = route.path.map((id) => stops[id]).filter(Boolean);
    stopDist = [0];
    for (let i = 1; i < points.length; i++) {
      stopDist.push(
        stopDist[i - 1] +
          metresBetween(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]),
      );
    }
  }
  // Cumulative metres at each vertex, so a projection can be turned into a
  // distance along the route in one step.
  const cum = [0];
  for (let i = 1; i < points.length; i++) {
    cum.push(
      cum[i - 1] +
        metresBetween(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]),
    );
  }
  route.line = { points, cum, stopDist };
  return route.line;
}

/**
 * Every place along a route's line that a point could be standing beside.
 *
 * More than one, because a route that runs out along a street and back down it
 * passes the same spot twice, and the nearest of the two passes is not
 * necessarily the one that gets you where you are going.
 */
function projectOnRoute(route, latlng, maxOff) {
  const { points, cum } = routeLine(route);
  const scale = Math.cos((latlng.lat * Math.PI) / 180) * METRES_PER_DEG_LAT;
  const py = latlng.lat * METRES_PER_DEG_LAT;
  const px = latlng.lng * scale;
  const places = [];
  let previous = Infinity;
  let pending = null;

  for (let i = 0; i < points.length - 1; i++) {
    const ay = points[i][0] * METRES_PER_DEG_LAT;
    const ax = points[i][1] * scale;
    const by = points[i + 1][0] * METRES_PER_DEG_LAT;
    const bx = points[i + 1][1] * scale;
    const dx = bx - ax;
    const dy = by - ay;
    const length2 = dx * dx + dy * dy;
    let t = length2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / length2;
    t = Math.max(0, Math.min(1, t));
    const offX = px - (ax + t * dx);
    const offY = py - (ay + t * dy);
    const off = Math.sqrt(offX * offX + offY * offY);
    const along = cum[i] + t * Math.sqrt(length2);

    // Keep each local minimum: the point where the line stops approaching and
    // starts leaving again is one pass of the route past this spot.
    if (off <= previous) {
      pending = off <= maxOff ? { along, off } : null;
    } else if (pending) {
      places.push(pending);
      pending = null;
    }
    previous = off;
  }
  if (pending) places.push(pending);
  return places;
}

/**
 * Time to cover the stretch between two distances along a route.
 *
 * The measurements are stop to stop, because that is where a vehicle's position
 * stream can be timed reliably; the answer is for the two points asked about.
 * A leg the stretch only partly covers is charged in proportion, which assumes
 * an even pace across one leg — a 460 m block at the median.
 */
function timeAlongRoute(legs, stopDist, from, to, metric) {
  const values = legs[metric] || legs.med;
  const charged = [];
  const known = values.filter((v) => v != null);
  if (!known.length) return null;
  const fill = known.reduce((sum, v) => sum + v, 0) / known.length;

  let seconds = 0;
  let holes = 0;
  let covered = 0;
  let measured = 0;
  for (let i = 0; i < values.length && i + 1 < stopDist.length; i++) {
    const start = stopDist[i];
    const end = stopDist[i + 1];
    const span = end - start;
    if (span <= 0) continue;
    const overlap = Math.min(end, to) - Math.max(start, from);
    if (overlap <= 0) continue;
    const fraction = overlap / span;
    if (values[i] == null) {
      holes += 1;
      seconds += fill * fraction;
    } else {
      seconds += values[i] * fraction;
      measured += 1;
    }
    charged.push(legs.n[i] || 0);
    covered += 1;
  }
  // A stretch where nothing at all was observed is not a measurement of it,
  // however confidently the route's average leg could fill it in.
  if (!measured) return null;
  charged.sort((x, y) => x - y);
  return {
    seconds,
    holes,
    legs: covered,
    // The typical leg of this stretch, over exactly the legs that were charged
    // for it — the weakest one was reporting a whole busy ride as "0 rides".
    observations: charged[Math.floor(charged.length / 2)],
  };
}

/**
 * Every route-direction running from A to B, timed on its own observations.
 *
 * A route counts only if both ends are near stops on its path *and* they are
 * in path order — the same pair of points is a different journey, on a
 * different set of legs, in the other direction.
 */
function rideOptions(a, b) {
  const data = currentRides();
  if (!data) return [];
  const metric = els.metric.value;
  const out = [];

  for (const route of RIDES.paths.routes) {
    const key = `${route.route}|${route.dir}`;
    const legs = data[key];
    if (!legs) continue;
    // Both points have to be on this route's street, and in the order it drives
    // them: the same two points are a different journey the other way round.
    const starts = projectOnRoute(route, a, RIDE_NEAR_M);
    const ends = projectOnRoute(route, b, RIDE_NEAR_M);
    if (!starts.length || !ends.length) continue;

    // Of the passes that work, the shortest way round is the journey being
    // asked about; a longer one is the route going somewhere else first.
    let from = null;
    let to = null;
    for (const start of starts) {
      for (const end of ends) {
        if (end.along <= start.along) continue;
        if (!from || end.along - start.along < to.along - from.along) {
          from = start;
          to = end;
        }
      }
    }
    if (!from) continue;
    // A route that takes half the city to cover a straight kilometre is not a
    // way of making this journey, whatever its clock says.
    const direct = metresBetween(a.lat, a.lng, b.lat, b.lng);
    if (to.along - from.along > direct * RIDE_DETOUR_MAX + RIDE_NEAR_M * 2) continue;

    const { stopDist } = routeLine(route);
    const timed = timeAlongRoute(legs, stopDist, from.along, to.along, metric);
    if (!timed) continue;

    // Which stops the stretch spans, for naming where a rider would get on and
    // off — the time itself is between the points, not between these.
    let board = 0;
    let alight = stopDist.length - 1;
    for (let i = 0; i < stopDist.length; i++) {
      if (stopDist[i] <= from.along) board = i;
      if (stopDist[i] <= to.along) alight = i;
    }
    out.push({
      route,
      seconds: timed.seconds,
      metres: to.along - from.along,
      stops: Math.max(0, alight - board),
      holes: timed.holes,
      legs: timed.legs,
      observations: timed.observations,
      off: Math.max(from.off, to.off),
      from: from.along,
      to: to.along,
      board,
      alight,
      offpeak: offPeak(key, stopDist, from.along, to.along, metric),
    });
  }
  return out.sort((x, y) => x.seconds - y.seconds);
}

/** The [lat, lon] sitting `along` metres into a polyline. */
function pointAlong(points, cum, along) {
  for (let i = 0; i < points.length - 1; i++) {
    if (cum[i + 1] < along) continue;
    const span = cum[i + 1] - cum[i];
    const t = span === 0 ? 0 : (along - cum[i]) / span;
    return [
      points[i][0] + (points[i + 1][0] - points[i][0]) * t,
      points[i][1] + (points[i + 1][1] - points[i][1]) * t,
    ];
  }
  return points[points.length - 1];
}

function highlightRide(option) {
  if (RIDES.highlight) {
    RULER.layer.removeLayer(RIDES.highlight);
    RIDES.highlight = null;
  }
  if (!option) return;
  // The stretch being timed, cut out of the route's own line at the two
  // distances rather than at the stops either side of them.
  const { points, cum } = routeLine(option.route);
  const slice = [];
  for (let i = 0; i < points.length; i++) {
    if (cum[i] > option.from && cum[i] < option.to) slice.push(points[i]);
  }
  slice.unshift(pointAlong(points, cum, option.from));
  slice.push(pointAlong(points, cum, option.to));
  RIDES.highlight = L.polyline(slice, {
    pane: RULER_PANE,
    color: "#3b6ea5",
    weight: 5,
    opacity: 0.8,
    interactive: false,
  }).addTo(RULER.layer);
}

function rideRow(option, i) {
  const stops = RIDES.paths.stops;
  const board = stops[option.route.path[option.board]];
  const alight = stops[option.route.path[option.alight]];
  const km = (option.metres / 1000).toLocaleString(locale(), {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  const offpeak = option.offpeak;
  const hour = offpeak && `${String(offpeak.hour).padStart(2, "0")}:00`;
  const delta =
    offpeak && offpeak.hour !== hourKey()
      ? ` <span class="ride-delta" title="${t("againstOffPeak", { hour })}">` +
        `${formatDelta(option.seconds - offpeak.seconds)}</span>`
      : "";
  const note = !offpeak
    ? ""
    : offpeak.hour === hourKey()
      ? ` · ${t("quickestHour")}`
      : ` · ${t("offPeakTime", { hour, time: formatDuration(offpeak.seconds) })}`;
  return (
    `<button type="button" class="ride-row" data-ride="${i}">` +
    `<span class="ride-name">${option.route.name}</span>` +
    `<span class="ride-time">${formatDuration(option.seconds)}${delta}</span>` +
    `<span class="ride-detail">${board ? board[2] : "?"} → ${alight ? alight[2] : "?"}<br>` +
    `${km} ${t("km")} · ${t("ridesSeen", { n: option.observations })}` +
    // Only when part of the total is filled in rather than measured.
    `${option.holes ? ` · ${t("estimated", { holes: option.holes, legs: option.legs })}` : ""}` +
    `${note}</span>` +
    `</button>`
  );
}

function ridesReadout() {
  if (!index.rides) {
    return `<div class="ride-empty">${t("noRideTimes")}</div>`;
  }
  if (RULER.points.length === 0) return `<div class="ride-empty">${t("clickFirst")}</div>`;
  if (RULER.points.length === 1) {
    return `<div class="ride-empty">${t("clickSecond")}</div>`;
  }
  // Stale figures from the previous hour are worse than none while the new
  // ones are on their way.
  if (!currentRides()) {
    return `<div class="ride-empty">${t("looking")}</div>`;
  }

  const options = rideOptions(RULER.points[0], RULER.points[RULER.points.length - 1]);
  if (!options.length) {
    return `<div class="ride-empty">${t("noRoute", { m: RIDE_NEAR_M })}</div>`;
  }
  // The slower ones are still ways of making the journey, so the tail folds
  // away rather than disappearing — the box opens short, not short of answers.
  const limit = RIDES.expanded ? options.length : RIDE_ROWS;
  const shown = options.slice(0, limit).map(rideRow).join("");
  const rest = options.length - limit;
  const source = els.metric.selectedOptions[0].textContent.toLocaleLowerCase();
  const more =
    rest > 0
      ? `<button type="button" class="ride-more" data-expand="1">` +
        `${t("showSlower", { n: rest })}</button>`
      : RIDES.expanded && options.length > RIDE_ROWS
        ? `<button type="button" class="ride-more" data-expand="0">${t("showFewer")}</button>`
        : "";
  return (
    `<div class="ride-head">${t("onTheBus")} <span>${t("ofObserved", { source })}</span></div>` +
    shown +
    more
  );
}

// Which way you are travelling decides which routes can carry you, so the line
// carries an arrowhead at its midpoint rather than leaving the reader to infer
// the direction from the order they happened to click in.
function rulerArrow() {
  if (RULER.arrow) {
    RULER.layer.removeLayer(RULER.arrow);
    RULER.arrow = null;
  }
  if (RULER.points.length < 2) return;
  const [from, to] = [RULER.points[0], RULER.points[RULER.points.length - 1]];
  const mid = L.latLng((from.lat + to.lat) / 2, (from.lng + to.lng) / 2);
  const dy = (to.lat - from.lat) * METRES_PER_DEG_LAT;
  const dx = (to.lng - from.lng) * METRES_PER_DEG_LAT * Math.cos((mid.lat * Math.PI) / 180);
  const degrees = (Math.atan2(dx, dy) * 180) / Math.PI; // 0 = north, clockwise
  RULER.arrow = L.marker(mid, {
    interactive: false,
    keyboard: false,
    icon: L.divIcon({
      className: "ruler-arrow",
      iconSize: [18, 18],
      html:
        `<svg viewBox="0 0 18 18" width="18" height="18" ` +
        `style="transform: rotate(${degrees.toFixed(1)}deg)">` +
        `<path d="M9 1 L15 15 L9 11.5 L3 15 Z" fill="#16202a"/></svg>`,
    }),
  }).addTo(RULER.layer);
}

function rulerRedraw() {
  if (RULER.line) RULER.line.setLatLngs(RULER.points);
  rulerArrow();
  // Re-rendering the list drops the active row, so the stretch it drew goes
  // with it rather than hanging over a line that has since moved.
  highlightRide(null);
  els.rides.innerHTML = ridesReadout();
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
    link.title = t("rulerTitle");
    link.setAttribute("role", "button");
    link.setAttribute("aria-pressed", "false");
    link.setAttribute("aria-label", t("rulerLabel"));
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
  // The legs are a canvas inside the overlay pane, and whatever is appended
  // there last wins — which was the canvas, burying the ruler's own lines under
  // the network. Its own pane above it settles it.
  map.createPane(RULER_PANE).style.zIndex = 620;
  RULER.layer = L.layerGroup().addTo(map);
  RULER.line = L.polyline([], {
    pane: RULER_PANE,
    color: "#16202a",
    weight: 3,
    opacity: 0.85,
    dashArray: "6 5",
    interactive: false,
  }).addTo(RULER.layer);
  new RulerControl({ position: "topright" }).addTo(map);

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

/* ---------- language ---------- */

// Everything app.js writes in words, written again in the current language.
// The page's own markup is i18n.js's.
function relabel() {
  for (const option of els.month.options) {
    const month = index.months.find((m) => m.key === option.value);
    // The running month says how far through it the data reaches; a finished
    // one says how many days it holds.
    option.textContent = `${monthName(month.key)} (${month.part ?? t("dayCount", { n: month.days })})`;
  }
  for (const option of els.metric.options) {
    option.textContent = t("metric")[option.value] ?? option.value;
  }
  labelDaytypes();
  paintLegend();
  syncHourLabel();
  els.note.textContent = t("note", {
    first: index.days.first,
    last: index.days.last,
    n: num(index.samples),
  });
  const zoom = zoomControl.getContainer();
  zoom.querySelector(".leaflet-control-zoom-in").title = t("zoomIn");
  zoom.querySelector(".leaflet-control-zoom-out").title = t("zoomOut");
  els.rulerButton.title = t("rulerTitle");
  els.rulerButton.setAttribute("aria-label", t("rulerLabel"));
  map.closePopup();
  if (RULER.on) els.rides.innerHTML = ridesReadout();
  if (currentRides()) els.subtitle.textContent = describe();
}

/* ---------- boot ---------- */

async function boot() {
  index = await fetch(`${DATA}/index.json`).then((r) => r.json());

  for (const month of index.months) {
    const option = document.createElement("option");
    option.value = month.key;
    // The running month says how far through it the data reaches; a finished
    // one says how many days it holds.
    els.month.append(option);
  }
  els.month.value = lastFullMonth();

  for (const daytype of index.daytypes) {
    const option = document.createElement("option");
    option.value = daytype.key;
    els.daytype.append(option);
  }
  // Weekdays by default: mixing in weekends blurs the rush hours most people
  // open the map to see. Falls back to all days if the split is missing.
  els.daytype.value = index.daytypes.some((d) => d.key === "wd") ? "wd" : "all";

  for (const metric of index.rides.metrics) {
    const option = document.createElement("option");
    option.value = metric.key;
    els.metric.append(option);
  }
  // Median by default: how a leg usually goes, not dragged by the odd long
  // wait. Only the average adds up along a journey, which the ruler says.
  if (index.rides.metrics.some((m) => m.key === "med")) els.metric.value = "med";

  const hours = index.hours;
  els.hour.min = String(hours[0]);
  els.hour.max = String(hours[hours.length - 1]);
  els.hour.value = String(currentHour());

  readUrl();
  rulerBoot();
  relabel();

  legsLayer.addTo(map);
  await render();

  els.month.addEventListener("change", () => {
    labelDaytypes();
    render();
  });
  els.daytype.addEventListener("change", render);
  els.hour.addEventListener("input", () => {
    syncHourLabel();
    render();
  });
  els.allHours.addEventListener("change", () => {
    syncHourLabel();
    render();
  });
  // Every hour and statistic is already loaded, so this is a repaint.
  els.metric.addEventListener("change", () => {
    writeUrl();
    map.closePopup();
    repaint();
  });

  for (const button of document.querySelectorAll("[data-lang]")) {
    button.addEventListener("click", () => {
      setLang(button.dataset.lang);
      relabel();
      writeUrl();
    });
  }

  // Panning is not a data change: the URL and the leg count are rewritten,
  // and not on every frame of a drag.
  let pending = null;
  map.on("moveend", () => {
    clearTimeout(pending);
    pending = setTimeout(() => {
      writeUrl();
      if (currentRides()) els.subtitle.textContent = describe();
    }, 300);
  });
}

boot().catch((err) => {
  els.subtitle.textContent = t("failed", { msg: err.message });
});
