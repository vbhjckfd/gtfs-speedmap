/* /suggestions: the stretches where a bus lane would win back the most bus
 * time, one month at a time. /slowest, the same page with <body data-list="slow">:
 * the stretches most delayed against empty roads. Each corridor carries its own
 * drawn legs, so the page needs neither the paths file nor the ride-time slots.
 */

const SLOW = document.body.dataset.list === "slow";

const { map } = makeMap();
panelBoot();

const els = {
  month: document.getElementById("month"),
  list: document.getElementById("lanes-list"),
  back: document.getElementById("back-link"),
  other: document.getElementById("other-link"),
};

let index = null;
let corridors = [];
const drawn = L.layerGroup().addTo(map);
const picked = L.layerGroup().addTo(map);
// Zoomed out, a corridor is shorter than its arrows and they would hide it.
const arrows = L.layerGroup();
const ARROWS_FROM_ZOOM = 14;
map.on("zoomend", () => {
  if (map.getZoom() >= ARROWS_FROM_ZOOM) arrows.addTo(map);
  else arrows.remove();
});

function writeUrl() {
  const query = `?month=${els.month.value}&lang=${LANG}`;
  history.replaceState(null, "", query);
  els.back.href = `./${query}`;
  els.other.href = `${SLOW ? "suggestions" : "slowest"}${query}`;
}

function fillMonths(selected) {
  els.month.innerHTML = index.months
    .map((m) => `<option value="${m.key}">${monthName(m.key)}</option>`)
    .join("");
  els.month.value = selected;
}

// A shared link's month, else the newest month not still being collected.
function startMonth() {
  const asked = new URLSearchParams(location.search).get("month");
  if (index.months.some((m) => m.key === asked)) return asked;
  const whole = index.months.filter((m) => !m.part);
  return (whole.length ? whole : index.months).at(-1).key;
}

function row(c, i) {
  const km = (c.length_m / 1000).toLocaleString(locale(), {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  const routes = c.routes.slice(0, 6).join(", ") + (c.routes.length > 6 ? "…" : "");
  const delay = `+${Math.round(c.delay * 100)}%`;
  const perKm = t("lanesPerKm", { min: Math.round(c.lost_per_km) });
  return (
    `<button type="button" class="ride-row" data-lane="${i}">` +
    `<span class="ride-name">${i + 1}</span>` +
    `<span class="ride-time">${SLOW ? delay : perKm}</span>` +
    `<span class="ride-detail">${c.from} → ${c.to}<br>` +
    `${km} ${t("km")} · ${t("lanesLost", { min: Math.round(c.lost_min) })} · ` +
    `${t("lanesBuses", { n: Math.round(c.buses) })} · ` +
    `${SLOW ? perKm : delay} · ${routes}</span>` +
    `</button>`
  );
}

function renderList() {
  els.list.innerHTML = corridors.length
    ? corridors.map(row).join("")
    : `<p class="ride-empty">${t("lanesNone")}</p>`;
}

// Fit `bounds` into the part of the map the panel leaves free: beside it on a
// wide screen, below it on a phone.
function fit(bounds, options) {
  const panel = document.getElementById("panel").getBoundingClientRect();
  const beside = panel.right < map.getSize().x * 0.6;
  const pad = options.padding;
  map.fitBounds(bounds, {
    maxZoom: options.maxZoom,
    paddingTopLeft: beside ? [panel.right + pad, pad] : [pad, panel.bottom + pad],
    paddingBottomRight: [pad, pad],
  });
}

// The way buses run along a leg, at its middle vertex pair.
function legArrow(leg) {
  const mid = Math.floor((leg.length - 1) / 2);
  return arrowMarker(leg[mid], leg[mid + 1]);
}

function draw() {
  drawn.clearLayers();
  arrows.clearLayers();
  picked.clearLayers();
  corridors.forEach((c, i) => {
    const line = L.polyline(c.lines, { color: "#c0392b", weight: 6, opacity: 0.8 });
    line.on("click", () => pick(i));
    drawn.addLayer(line);
    for (const leg of c.lines) arrows.addLayer(legArrow(leg));
  });
  if (corridors.length) {
    fit(L.featureGroup(drawn.getLayers()).getBounds(), { padding: 30 });
  }
}

function pick(i) {
  picked.clearLayers();
  for (const b of els.list.querySelectorAll(".ride-row")) {
    b.classList.toggle("active", Number(b.dataset.lane) === i);
  }
  const lines = corridors[i].lines;
  L.polyline(lines, { color: "#16202a", weight: 9, opacity: 0.9 }).addTo(picked);
  L.polyline(lines, { color: "#e74c3c", weight: 5 }).addTo(picked);
  for (const leg of lines) legArrow(leg).addTo(picked);
  fit(L.polyline(lines).getBounds(), { maxZoom: 16, padding: 60 });
}

async function load() {
  writeUrl();
  const month = els.month.value;
  try {
    const res = await fetch(`data/lanes-${SLOW ? "slow-" : ""}${month}.json`);
    corridors = res.ok ? await res.json() : [];
  } catch {
    corridors = [];
  }
  if (month !== els.month.value) return; // picked another month meanwhile
  renderList();
  draw();
}

els.month.addEventListener("change", load);
els.list.addEventListener("click", (e) => {
  const button = e.target.closest("[data-lane]");
  if (button) pick(Number(button.dataset.lane));
});
for (const button of document.querySelectorAll("[data-lang]")) {
  button.addEventListener("click", () => {
    setLang(button.dataset.lang);
    panelCollapse(document.getElementById("panel").classList.contains("collapsed"));
    fillMonths(els.month.value);
    renderList();
    writeUrl();
  });
}

(async () => {
  index = await (await fetch("data/index.json")).json();
  fillMonths(startMonth());
  load();
})();
