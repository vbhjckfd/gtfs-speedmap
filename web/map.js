/* The map both pages draw on: Lviv, its basemap, and the geometry helpers
 * they share. Loaded after Leaflet and before the page's own script.
 */

const LVIV = [49.8397, 24.0297];
// The network with a margin round it. A shared link outside it is a typo, not
// a view, and panning past it only finds blank map.
const BOUNDS = L.latLngBounds([49.55, 23.6], [50.1, 24.45]);

// Same basemap as lad.lviv.ua: the Protomaps basemap drawn by protomaps-leaflet from
// one .pmtiles archive on our own R2 bucket. The archive covers only the Lviv metro
// area (TILE_BOUNDS, its extract bbox), so nothing is drawn or fetched past it.
const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
const TILE_BOUNDS = [[49.68, 23.75], [50.0, 24.35]];

// The panel occupies the top-left corner, where Leaflet puts the zoom buttons
// by default.
function makeMap() {
  const map = L.map("map", {
    preferCanvas: true,
    // Leaflet's tile fade-in sticks part-way on protomaps-leaflet's async tiles.
    fadeAnimation: false,
    zoomControl: false,
    maxBounds: BOUNDS,
    minZoom: 10,
  }).setView(LVIV, 13);
  const zoomControl = L.control.zoom({ position: "topright" }).addTo(map);
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
  return { map, zoomControl };
}

const METRES_PER_DEG_LAT = 111320;

function metresBetween(lat1, lon1, lat2, lon2) {
  const dy = (lat1 - lat2) * METRES_PER_DEG_LAT;
  const dx = (lon1 - lon2) * METRES_PER_DEG_LAT * Math.cos((lat1 * Math.PI) / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

// An arrowhead halfway from `from` to `to`, pointing along it.
function arrowMarker(from, to) {
  [from, to] = [L.latLng(from), L.latLng(to)];
  const mid = L.latLng((from.lat + to.lat) / 2, (from.lng + to.lng) / 2);
  const dy = (to.lat - from.lat) * METRES_PER_DEG_LAT;
  const dx = (to.lng - from.lng) * METRES_PER_DEG_LAT * Math.cos((mid.lat * Math.PI) / 180);
  const degrees = (Math.atan2(dx, dy) * 180) / Math.PI; // 0 = north, clockwise
  return L.marker(mid, {
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
  });
}

/* ---------- panel ---------- */

// On a phone the panel folds away to its title, and stays folded across visits.
const PANEL_KEY = "panelCollapsed";

function panelCollapse(collapsed) {
  const toggle = document.getElementById("panel-toggle");
  document.getElementById("panel").classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-expanded", String(!collapsed));
  const label = t(collapsed ? "showControls" : "hideControls");
  toggle.title = label;
  toggle.setAttribute("aria-label", label);
}

function panelBoot() {
  let saved = false;
  try {
    saved = localStorage.getItem(PANEL_KEY) === "1";
  } catch {}
  panelCollapse(saved);
  document.getElementById("panel-toggle").addEventListener("click", () => {
    const collapsed = !document.getElementById("panel").classList.contains("collapsed");
    panelCollapse(collapsed);
    try {
      localStorage.setItem(PANEL_KEY, collapsed ? "1" : "0");
    } catch {}
  });
}
