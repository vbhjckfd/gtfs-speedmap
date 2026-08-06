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
  hour: document.getElementById("hour"),
  hourLabel: document.getElementById("hour-label"),
  allHours: document.getElementById("all-hours"),
  metric: document.getElementById("metric"),
  subtitle: document.getElementById("subtitle"),
  note: document.getElementById("note"),
  ramp: document.getElementById("ramp"),
  scaleLow: document.getElementById("scale-low"),
  scaleHigh: document.getElementById("scale-high"),
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

function speedColor(kmh) {
  const { low_kmh: low, high_kmh: high } = index.scale;
  const [r, g, b] = ramp((kmh - low) / (high - low));
  return `rgb(${r},${g},${b})`;
}

function paintLegend() {
  const stops = STOPS.map(([p]) => {
    const [r, g, b] = ramp(p);
    return `rgb(${r},${g},${b}) ${p * 100}%`;
  });
  els.ramp.style.background = `linear-gradient(90deg, ${stops.join(", ")})`;
  els.scaleLow.textContent = `≤ ${index.scale.low_kmh}`;
  els.scaleHigh.textContent = `≥ ${index.scale.high_kmh}`;
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
    ctx.globalAlpha = 0.85;

    for (let i = 0; i < lat.length; i++) {
      if (values[i] == null) continue;
      if (lat[i] < bounds.getSouth() || lat[i] > bounds.getNorth()) continue;
      if (lon[i] < bounds.getWest() || lon[i] > bounds.getEast()) continue;
      const p = map.latLngToContainerPoint([lat[i], lon[i]]);
      ctx.fillStyle = speedColor(values[i]);
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

function selectionKey() {
  const month = els.month.value;
  const hour = els.allHours.checked ? "all" : String(els.hour.value).padStart(2, "0");
  return `${month}-${hour}`;
}

/* ---------- shareable URL ---------- */

// The selection lives in the query string so a view can be linked. The
// canonical link stays on the bare homepage, so search engines index one page
// rather than a hundred near-identical ones.
function writeUrl() {
  const params = new URLSearchParams({
    month: els.month.value,
    hour: els.allHours.checked ? "all" : String(els.hour.value).padStart(2, "0"),
    stat: els.metric.value,
  });
  // replaceState, not pushState: dragging the slider must not bury the user's
  // previous page under twenty history entries.
  history.replaceState(null, "", `?${params}`);
}

function readUrl() {
  const params = new URLSearchParams(location.search);

  const month = params.get("month");
  if (month && [...els.month.options].some((o) => o.value === month)) {
    els.month.value = month;
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
      fetchSelection(`${els.month.value}-${String(hours[i]).padStart(2, "0")}`).catch(() => {});
    }
  });
}

function describe(count) {
  const month = els.month.selectedOptions[0].textContent;
  const when = els.allHours.checked
    ? "all hours"
    : `${String(els.hour.value).padStart(2, "0")}:00–${String(els.hour.value).padStart(2, "0")}:59`;
  const stat = els.metric.selectedOptions[0].textContent.toLowerCase();
  return `${month} · ${when} · ${count.toLocaleString()} cells · ${stat}`;
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

map.on("click", (e) => {
  if (!current) return;
  const metresPerDegLat = 111320;
  // A fixed metre tolerance is unclickable when zoomed in and grabs the wrong
  // cell when zoomed out, so allow a constant ~12 px of slop instead.
  const tolerance = Math.max(index.cell_size_m, metresPerPixel() * 12);
  let best = -1;
  let bestDist = Infinity;
  const cosLat = Math.cos((e.latlng.lat * Math.PI) / 180);

  for (let i = 0; i < current.lat.length; i++) {
    const dy = (current.lat[i] - e.latlng.lat) * metresPerDegLat;
    const dx = (current.lon[i] - e.latlng.lng) * metresPerDegLat * cosLat;
    const d = dx * dx + dy * dy;
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  if (best < 0 || Math.sqrt(bestDist) > tolerance) return;

  const median = current.med ? current.med[best] : null;
  const rows = [`average ${current.v[best].toFixed(1)} km/h`];
  if (median != null) rows.push(`median ${median.toFixed(1)} km/h`);

  L.popup()
    .setLatLng([current.lat[best], current.lon[best]])
    .setContent(
      `<div class="cell-popup"><b>${speedValues()[best].toFixed(1)} km/h</b><br>` +
        `${rows.join("<br>")}<br>${current.n[best].toLocaleString()} samples</div>`,
    )
    .openOn(map);
});

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

  for (const metric of index.metrics || [{ key: "v", label: "Average" }]) {
    const option = document.createElement("option");
    option.value = metric.key;
    option.textContent = metric.label;
    els.metric.append(option);
  }

  const hours = index.hours;
  els.hour.min = String(hours[0]);
  els.hour.max = String(hours[hours.length - 1]);
  els.hour.value = String(hours.includes(8) ? 8 : hours[0]);

  readUrl();
  paintLegend();
  syncHourLabel();
  els.note.textContent =
    `Buses only. Positions within ${index.stop_radius_m} m of a stop on the ` +
    `vehicle's own trip are excluded, so this is running speed. ` +
    `${index.days.first} … ${index.days.last}, ` +
    `${index.samples.toLocaleString()} samples.`;

  dots.addTo(map);
  await render();

  els.month.addEventListener("change", render);
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
    dots.redraw();
    if (current) els.subtitle.textContent = describe(current.lat.length);
  });
}

boot().catch((err) => {
  els.subtitle.textContent = `failed to load: ${err.message}`;
});
