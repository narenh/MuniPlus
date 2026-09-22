// The map. A desaturated dark basemap with the Muni network drawn on top:
// a glow pass and a solid pass per line, transfer arcs, station nodes and
// platform poles. Nothing here moves a coordinate: they come from 511.

import { store, select, stationById, linesOf, platformsOf, positionOf } from './store.js';

const STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';
// A walking path is meaningless at city zoom; it only means something once the
// blocks it crosses are on screen.
const TRANSFER_MINZOOM = 13.6;
// Where poles stop being a cluster of dots and become individually legible.
const POLE_MINZOOM = 15;
const SF = { center: [-122.4355, 37.7605], zoom: 11.9 };

export let map = null;
let litLink = null;           // a transfer chip is being hovered in the inspector
let hoverId = null;
const listeners = { pick: [], hover: [] };

export function onPick(fn) { listeners.pick.push(fn); }

// ---------------------------------------------------------------- geometry
const HEADING_DEG = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };

function routeFeatures() {
  const feats = [];
  for (const ln of store.doc.lines) {
    const coords = ln.stationIds
      .map(id => stationById(id))
      .filter(Boolean)
      .map(positionOf)
      .filter(Boolean);
    if (coords.length < 2) continue;
    const active = !store.activeLine || store.activeLine === ln.id;
    feats.push({
      type: 'Feature',
      properties: { id: ln.id, color: ln.color, active: active ? 1 : 0, name: ln.name },
      geometry: { type: 'LineString', coordinates: coords },
    });
  }
  // draw the active line last so it sits on top
  return { type: 'FeatureCollection', features: feats.sort((a, b) => a.properties.active - b.properties.active) };
}

/**
 * Transfers as walking paths. A transfer is one-way unless the other station
 * lists it back, and that asymmetry is deliberate in data.json, so each pair is
 * drawn once with an arrowhead at whichever end(s) it actually points to:
 * one chevron for a one-way link, one at each end for a mutual one.
 *
 * Straight lines are the honest shape here. data.json records that a walk
 * exists, never its route, and this is an editor - inventing a path through the
 * streets would be drawing a claim the file does not make.
 */
export function transferLinks() {
  const links = new Map();      // "a|b" with a < b  ->  { a, b, fwd, rev, indoor }
  for (const st of store.doc.stations) {
    for (const t of st.transfers || []) {
      if (!stationById(t.to)) continue;
      const [a, b] = st.id < t.to ? [st.id, t.to] : [t.to, st.id];
      const key = `${a}|${b}`;
      const rec = links.get(key) || { a, b, fwd: false, rev: false, indoor: false };
      if (st.id === a) rec.fwd = true; else rec.rev = true;   // fwd means a -> b
      if (t.mode === 'indoor') rec.indoor = true;
      links.set(key, rec);
    }
  }
  return links;
}

const indoorLink = (a, b) => {
  const k = a < b ? `${a}|${b}` : `${b}|${a}`;
  return transferLinks().get(k)?.indoor;
};

function transferFeatures() {
  const lines = [], heads = [];
  const sel = store.selStation;

  for (const { a, b, fwd, rev } of transferLinks().values()) {
    const pa = positionOf(stationById(a)), pb = positionOf(stationById(b));
    if (!pa || !pb) continue;
    const both = fwd && rev ? 1 : 0;
    const touches = sel === a || sel === b ? 1 : 0;

    lines.push({
      type: 'Feature',
      properties: { a, b, both, touches,
                    lit: isLit(a, b) ? 1 : 0,
                    indoor: indoorLink(a, b) ? 1 : 0,
                    metres: Math.round(metresBetween(pa, pb)) },
      geometry: { type: 'LineString', coordinates: [pa, pb] },
    });
    const lit = isLit(a, b) ? 1 : 0;
    if (fwd) heads.push(chevron(pa, pb, both, touches, lit));
    if (rev) heads.push(chevron(pb, pa, both, touches, lit));
  }
  return {
    lines: { type: 'FeatureCollection', features: lines },
    heads: { type: 'FeatureCollection', features: heads },
  };
}

const isLit = (a, b) =>
  !!litLink && ((litLink.a === a && litLink.b === b) || (litLink.a === b && litLink.b === a));

/** Light one transfer path while its chip is hovered in the inspector. */
export function highlightLink(a, b) {
  const next = a && b ? { a, b } : null;
  const same = (!next && !litLink) || (next && litLink && next.a === litLink.a && next.b === litLink.b);
  if (same) return;
  litLink = next;
  refresh('transfers');
}

/** An arrowhead just short of the station it points at, clear of the dot. */
function chevron(from, to, both, touches, lit) {
  const t = 0.8;
  return {
    type: 'Feature',
    properties: { both, touches, lit, bearing: bearingDeg(from, to) },
    geometry: {
      type: 'Point',
      coordinates: [from[0] + (to[0] - from[0]) * t, from[1] + (to[1] - from[1]) * t],
    },
  };
}

const M_LAT = 110540, M_LON = 111320 * Math.cos(37.76 * Math.PI / 180);
const metresBetween = (a, b) => Math.hypot((a[0] - b[0]) * M_LON, (a[1] - b[1]) * M_LAT);
/** Bearing in degrees clockwise from north, for icon-rotate. */
const bearingDeg = (a, b) =>
  (Math.atan2((b[0] - a[0]) * M_LON, (b[1] - a[1]) * M_LAT) * 180 / Math.PI + 360) % 360;

function stationFeatures() {
  const active = store.activeLine;
  return {
    type: 'FeatureCollection',
    features: store.doc.stations.map(s => {
      const ls = linesOf(s.id);
      const on = !active || ls.some(l => l.id === active);
      const primary = (active && on ? ls.find(l => l.id === active) : ls[0]) || null;
      const at = positionOf(s);
      if (!at) return null;
      return {
        type: 'Feature',
        id: hashId(s.id),
        properties: {
          sid: s.id,
          name: s.name,
          color: primary?.color || '#7c8598',
          active: on ? 1 : 0,
          interchange: ls.length > 1 ? 1 : 0,
          selected: store.selStation === s.id ? 1 : 0,
          nplat: platformsOf(s).length,
        },
        geometry: { type: 'Point', coordinates: at },
      };
    }).filter(Boolean),
  };
}

/** A platform with no coordinate (not in 511's snapshot) has nothing to draw. */
const hasPosition = p => Number.isFinite(p.latitude) && Number.isFinite(p.longitude);

function platformFeatures() {
  const active = store.activeLine;
  const feats = [];
  for (const s of store.doc.stations) {
    const ls = linesOf(s.id);
    const on = !active || ls.some(l => l.id === active);
    const primary = (active && on ? ls.find(l => l.id === active) : ls[0]) || null;
    for (const p of platformsOf(s)) {
      if (!hasPosition(p)) continue;
      feats.push({
        type: 'Feature',
        id: hashId(String(p.id)),
        properties: {
          code: String(p.id),
          sid: s.id,
          heading: p.heading,
          bearing: HEADING_DEG[p.heading] ?? 0,
          color: primary?.color || '#7c8598',
          active: on ? 1 : 0,
          selected: store.selPlatform === String(p.id) ? 1 : 0,
          label: p.stopName || s.name,
        },
        geometry: { type: 'Point', coordinates: [p.longitude, p.latitude] },
      });
    }
  }
  return { type: 'FeatureCollection', features: feats };
}

/**
 * A short leader from each station's centre to each of its poles.
 *
 * An underground station puts every pole within metres of the centre, so
 * without these the poles are an indistinguishable pile on top of the station
 * dot, and nothing shows which station a stray pole belongs to.
 */
function leaderFeatures() {
  const active = store.activeLine;
  const feats = [];
  for (const s of store.doc.stations) {
    const ls = linesOf(s.id);
    const on = !active || ls.some(l => l.id === active);
    const primary = (active && on ? ls.find(l => l.id === active) : ls[0]) || null;
    const at = positionOf(s);
    if (!at) continue;
    for (const p of platformsOf(s)) {
      if (!hasPosition(p)) continue;
      feats.push({
        type: 'Feature',
        properties: {
          color: primary?.color || '#7c8598',
          selected: store.selStation === s.id ? 1 : 0,
          active: on ? 1 : 0,
        },
        geometry: { type: 'LineString', coordinates: [at, [p.longitude, p.latitude]] },
      });
    }
  }
  return { type: 'FeatureCollection', features: feats };
}

/** Stable small int id, because MapLibre feature-state needs numeric ids. */
const idCache = new Map();
let idSeq = 1;
function hashId(key) {
  if (!idCache.has(key)) idCache.set(key, idSeq++);
  return idCache.get(key);
}

// ------------------------------------------------------------------- setup
export async function initMap(container) {
  map = new maplibregl.Map({
    container,
    style: STYLE,
    center: SF.center,
    zoom: SF.zoom,
    pitch: 0,
    bearing: 0,
    antialias: true,
    attributionControl: { compact: true },
    maxZoom: 20,
  });

  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: 'metric' }), 'bottom-left');

  await new Promise(res => map.on('load', res));

  desaturateBasemap();
  makeArrowImage();
  addSources();
  addLayers();
  wireInteractions();

  return map;
}

/** Push the basemap back so the network reads as the foreground. */
function desaturateBasemap() {
  for (const layer of map.getStyle().layers) {
    if (layer.id.startsWith('muni-')) continue;
    try {
      if (layer.type === 'symbol') {
        map.setPaintProperty(layer.id, 'text-opacity', 0.42);
        map.setPaintProperty(layer.id, 'icon-opacity', 0.3);
      } else if (layer.type === 'line') {
        map.setPaintProperty(layer.id, 'line-opacity', 0.5);
      } else if (layer.type === 'fill') {
        map.setPaintProperty(layer.id, 'fill-opacity', 0.65);
      }
    } catch { /* some layers have data-driven values we should not clobber */ }
  }
}

/** A small triangular arrow drawn to a canvas, used for platform headings. */
function makeArrowImage() {
  const S = 36, c = document.createElement('canvas');
  c.width = c.height = S;
  const g = c.getContext('2d');
  g.translate(S / 2, S / 2);
  g.beginPath();
  g.moveTo(0, -14); g.lineTo(7.5, 2.5); g.lineTo(0, -1.5); g.lineTo(-7.5, 2.5);
  g.closePath();
  g.fillStyle = '#fff';
  g.fill();
  map.addImage('heading-arrow', { width: S, height: S, data: g.getImageData(0, 0, S, S).data }, { sdf: true });

  // An open chevron for transfer direction. Deliberately a different shape from
  // the solid heading arrow, so a walking path never reads as a platform heading.
  const c2 = document.createElement('canvas');
  c2.width = c2.height = S;
  const h = c2.getContext('2d');
  h.translate(S / 2, S / 2);
  h.strokeStyle = '#fff';
  h.lineWidth = 3.8;
  h.lineCap = 'round';
  h.lineJoin = 'round';
  h.beginPath();
  h.moveTo(-7.5, 5.5); h.lineTo(0, -5.5); h.lineTo(7.5, 5.5);
  h.stroke();
  map.addImage('transfer-arrow', { width: S, height: S, data: h.getImageData(0, 0, S, S).data }, { sdf: true });
}

function addSources() {
  map.addSource('routes', { type: 'geojson', data: routeFeatures() });
  const tf = transferFeatures();
  map.addSource('transfers', { type: 'geojson', data: tf.lines });
  map.addSource('transfer-heads', { type: 'geojson', data: tf.heads });
  map.addSource('stations', { type: 'geojson', data: stationFeatures() });
  map.addSource('platforms', { type: 'geojson', data: platformFeatures() });
  map.addSource('leaders', { type: 'geojson', data: leaderFeatures() });
}

const dimmed = (on, a, b) => ['case', ['==', ['get', 'active'], 1], a, b];

function addLayers() {
  // --- route glow, then casing, then the line itself
  map.addLayer({
    id: 'muni-route-glow', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 10, 11, 14, 24, 18, 44],
      'line-blur': ['interpolate', ['linear'], ['zoom'], 10, 8, 16, 24],
      'line-opacity': dimmed(true, 0.55, 0.05),
    },
  });
  map.addLayer({
    id: 'muni-route-case', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#05060a',
      'line-width': ['interpolate', ['linear'], ['zoom'], 10, 4.6, 14, 8.5, 18, 15],
      'line-opacity': dimmed(true, 0.9, 0.2),
    },
  });
  map.addLayer({
    id: 'muni-route', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 10, 2.6, 14, 5.4, 18, 10],
      'line-opacity': dimmed(true, 1, 0.16),
    },
  });

  // --- transfer walking paths, only once the walk is legible
  const lit = (yes, no) => ['case', ['==', ['get', 'lit'], 1], yes, no];
  const byState = (selected, other) => lit(1, ['case', ['==', ['get', 'touches'], 1], selected, other]);
  // A lit link ignores the zoom fade, so hovering its chip always shows the path.
  const fadeIn = (selected, other) => ['interpolate', ['linear'], ['zoom'],
    TRANSFER_MINZOOM, lit(1, 0),
    TRANSFER_MINZOOM + 0.7, byState(selected, other)];

  map.addLayer({
    id: 'muni-transfer', type: 'line', source: 'transfers',
    filter: ['!=', ['get', 'indoor'], 1],
    // Visibility is driven entirely by the opacity ramp below, not by minzoom,
    // so a link lit from the inspector shows its line and its arrowheads
    // together at any zoom.
    layout: { 'line-cap': 'round' },
    paint: {
      'line-color': lit('#ffffff', ['case', ['==', ['get', 'touches'], 1], '#d8e6ff', '#8fa3ca']),
      'line-width': ['interpolate', ['linear'], ['zoom'],
        14, lit(4, 1.6), 18, lit(4.5, 3)],
      'line-opacity': fadeIn(0.95, 0.42),
      // dotted for a street walk; solid for an indoor passage, because you
      // never surface and it is not a walk across the city
      'line-dasharray': [0.1, 2.4],
    },
  });
  // An in-station passage, drawn the way metro maps draw one: a solid white
  // connector with a black casing, so it reads as structure rather than as a
  // route or a street walk. The casing goes first so it sits underneath.
  map.addLayer({
    id: 'muni-transfer-indoor-case', type: 'line', source: 'transfers',
    filter: ['==', ['get', 'indoor'], 1],
    layout: { 'line-cap': 'round' },
    paint: {
      'line-color': '#05060a',
      'line-width': ['interpolate', ['linear'], ['zoom'], 14, lit(9, 6.5), 18, lit(12, 9.5)],
      'line-opacity': fadeIn(1, 0.7),
    },
  });
  map.addLayer({
    id: 'muni-transfer-indoor', type: 'line', source: 'transfers',
    filter: ['==', ['get', 'indoor'], 1],
    layout: { 'line-cap': 'round' },
    paint: {
      'line-color': '#ffffff',
      'line-width': ['interpolate', ['linear'], ['zoom'], 14, lit(5, 3.2), 18, lit(7, 5)],
      'line-opacity': fadeIn(1, 0.72),
    },
  });
  map.addLayer({
    id: 'muni-transfer-head', type: 'symbol', source: 'transfer-heads',
    layout: {
      'icon-image': 'transfer-arrow',
      'icon-size': ['interpolate', ['linear'], ['zoom'],
        14, lit(0.62, 0.4), 18, lit(0.85, 0.7)],
      'icon-rotate': ['get', 'bearing'],
      'icon-rotation-alignment': 'map',
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
    paint: {
      'icon-color': lit('#ffffff', ['case', ['==', ['get', 'touches'], 1], '#eef4ff', '#8fa3ca']),
      'icon-opacity': fadeIn(1, 0.55),
      'icon-halo-color': '#05060a',
      'icon-halo-width': 1.3,
    },
  });
  map.addLayer({
    id: 'muni-transfer-label', type: 'symbol', source: 'transfers',
    minzoom: 15,
    filter: ['==', ['get', 'touches'], 1],
    layout: {
      'symbol-placement': 'line-center',
      'text-field': ['concat',
        ['case', ['==', ['get', 'both'], 1], 'both ways · ', 'one way · '],
        ['to-string', ['get', 'metres']], ' m'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': 10.5,
      'text-offset': [0, -1],
      'text-keep-upright': true,
    },
    paint: {
      'text-color': '#d8e6ff',
      'text-halo-color': '#05060a',
      'text-halo-width': 1.8,
    },
  });

  // --- station nodes
  map.addLayer({
    id: 'muni-station-glow', type: 'circle', source: 'stations',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 9, 15, 21, 18, 32],
      'circle-color': ['get', 'color'],
      'circle-opacity': ['case', ['==', ['get', 'selected'], 1], 0.75, 0],
      'circle-blur': 0.55,
    },
  });
  map.addLayer({
    id: 'muni-station', type: 'circle', source: 'stations',
    paint: {
      // Metro convention: a station served by more than one line is a white
      // disc with a black ring; a single-line stop is a small dot ringed in
      // that line's colour. Interchanges are drawn larger, as on a real map.
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        11, ['case', ['==', ['get', 'interchange'], 1], 5, 3.2],
        14, ['case', ['==', ['get', 'interchange'], 1], 8, 5.2],
        18, ['case', ['==', ['get', 'interchange'], 1], 14, 9]],
      'circle-color': ['case',
        ['==', ['get', 'interchange'], 1], '#ffffff', '#0a0c12'],
      'circle-stroke-width': ['case',
        ['==', ['get', 'selected'], 1], 4,
        ['boolean', ['feature-state', 'hover'], false], 3.2, 2.4],
      'circle-stroke-color': ['case',
        // a white ring on a white disc would have no edge, so a selected
        // interchange keeps its black ring and is marked by the glow beneath
        ['==', ['get', 'interchange'], 1], '#05060a',
        ['==', ['get', 'selected'], 1], '#ffffff',
        ['get', 'color']],
      'circle-opacity': dimmed(true, 1, 0.3),
      'circle-stroke-opacity': dimmed(true, 1, 0.28),
    },
  });

  // --- leaders from a station to each of its poles
  map.addLayer({
    id: 'muni-leader', type: 'line', source: 'leaders',
    minzoom: POLE_MINZOOM,
    layout: { 'line-cap': 'round' },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 15, 1, 19, 2],
      'line-opacity': ['interpolate', ['linear'], ['zoom'],
        POLE_MINZOOM, 0,
        POLE_MINZOOM + 0.8, ['case',
          ['==', ['get', 'selected'], 1], 0.9,
          ['==', ['get', 'active'], 1], 0.32, 0.08]],
      'line-dasharray': [1.5, 1.5],
    },
  });

  // --- platform poles
  map.addLayer({
    id: 'muni-platform-halo', type: 'circle', source: 'platforms',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 4, 16, 13, 19, 20],
      'circle-color': ['get', 'color'],
      'circle-opacity': ['case',
        ['==', ['get', 'selected'], 1], 0.35,
        ['boolean', ['feature-state', 'hover'], false], 0.26, 0.1],
      'circle-blur': 0.35,
    },
  });
  map.addLayer({
    id: 'muni-platform', type: 'circle', source: 'platforms',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        12, 2.4, 14, 4, 16, 6.5, 19, 11],
      'circle-color': '#0a0c12',
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'],
        14, ['case', ['==', ['get', 'selected'], 1], 3, 2],
        19, ['case', ['==', ['get', 'selected'], 1], 5, 3.5]],
      'circle-stroke-color': ['case',
        ['==', ['get', 'selected'], 1], '#ffffff',
        ['get', 'color']],
      'circle-opacity': dimmed(true, 1, 0.25),
      'circle-stroke-opacity': dimmed(true, 1, 0.3),
    },
  });
  map.addLayer({
    id: 'muni-platform-arrow', type: 'symbol', source: 'platforms',
    minzoom: 14.5,
    layout: {
      'icon-image': 'heading-arrow',
      'icon-size': ['interpolate', ['linear'], ['zoom'], 14.5, 0.3, 19, 0.62],
      'icon-rotate': ['get', 'bearing'],
      'icon-rotation-alignment': 'map',
      'icon-offset': [0, -26],
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
    paint: {
      'icon-color': '#ffffff',
      'icon-opacity': dimmed(true, 0.92, 0.12),
      'icon-halo-color': '#05060a',
      'icon-halo-width': 1.4,
    },
  });

  // --- the stop code, which is the thing you actually need to read off a pole
  map.addLayer({
    id: 'muni-platform-label', type: 'symbol', source: 'platforms',
    minzoom: 15.6,
    layout: {
      'text-field': ['get', 'code'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 15.6, 9.5, 19, 12.5],
      'text-offset': [0, -1.4],
      'text-anchor': 'bottom',
      'text-padding': 2,
      'text-optional': true,
    },
    paint: {
      'text-color': ['case', ['==', ['get', 'selected'], 1], '#ffffff', ['get', 'color']],
      'text-halo-color': '#05060a',
      'text-halo-width': 1.8,
      'text-opacity': dimmed(true, 1, 0.2),
    },
  });

  map.addLayer({
    id: 'muni-label', type: 'symbol', source: 'stations',
    minzoom: 12.2,
    layout: {
      'text-field': ['get', 'name'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 12.2, 10, 16, 13, 19, 15],
      'text-offset': [0, 1.15],
      'text-anchor': 'top',
      'text-max-width': 9,
      'text-padding': 3,
      'text-optional': true,
    },
    paint: {
      'text-color': '#eaeef7',
      'text-halo-color': '#05060a',
      'text-halo-width': 1.6,
      'text-halo-blur': 0.4,
      'text-opacity': dimmed(true, 1, 0.22),
    },
  });
}

// ------------------------------------------------------------ interactions
function wireInteractions() {
  const SRC = { 'muni-station': 'stations', 'muni-platform': 'platforms' };

  for (const layer of Object.keys(SRC)) {
    const src = SRC[layer];

    map.on('mousemove', layer, e => {
      map.getCanvas().style.cursor = 'pointer';
      const f = e.features[0];
      if (hoverId && (hoverId.id !== f.id || hoverId.src !== src)) {
        map.setFeatureState({ source: hoverId.src, id: hoverId.id }, { hover: false });
      }
      hoverId = { src, id: f.id };
      map.setFeatureState({ source: src, id: f.id }, { hover: true });
      showCoords(f.properties.code
        ? `${f.properties.label} · ${f.properties.code} · ${f.properties.heading}`
        : f.properties.name);
    });

    map.on('mouseleave', layer, () => {
      map.getCanvas().style.cursor = '';
      if (hoverId) map.setFeatureState({ source: hoverId.src, id: hoverId.id }, { hover: false });
      hoverId = null;
      hideCoords();
    });
  }

  map.on('click', 'muni-station', e => {
    const sid = e.features[0].properties.sid;
    select(sid, null);
    listeners.pick.forEach(f => f(sid, null));
  });

  map.on('click', 'muni-platform', e => {
    e.originalEvent.stopPropagation();
    const { sid, code } = e.features[0].properties;
    select(sid, code);
    listeners.pick.forEach(f => f(sid, code));
  });

  map.on('click', e => {
    const hits = map.queryRenderedFeatures(e.point,
      { layers: ['muni-station', 'muni-platform'] });
    if (!hits.length) { select(null, null); listeners.pick.forEach(f => f(null, null)); }
  });
}

// ------------------------------------------------------------------- HUD
const hud = () => document.getElementById('hud-coords');
export function showCoords(text) {
  const el = hud();
  document.getElementById('hud-coords-text').textContent = text;
  el.classList.add('show');
}
export function hideCoords() { hud()?.classList.remove('show'); }

export function hint(text, ms = 2400) {
  const el = document.getElementById('hud-hint');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(hint._t);
  hint._t = setTimeout(() => el.classList.remove('show'), ms);
}

// ---------------------------------------------------------------- refresh
export function refresh(which = 'all') {
  if (!map || !map.getSource('routes')) return;
  if (which === 'all' || which === 'routes') map.getSource('routes').setData(routeFeatures());
  if (which === 'all' || which === 'transfers') {
    const tf = transferFeatures();
    map.getSource('transfers').setData(tf.lines);
    map.getSource('transfer-heads').setData(tf.heads);
  }
  if (which === 'all' || which === 'stations') map.getSource('stations').setData(stationFeatures());
  if (which === 'all' || which === 'platforms') {
    map.getSource('platforms').setData(platformFeatures());
    map.getSource('leaders').setData(leaderFeatures());
  }
}

export function applyLayerToggles() {
  if (!map?.getLayer('muni-platform')) return;
  const L = store.layers;
  for (const id of ['muni-platform', 'muni-platform-halo', 'muni-platform-arrow',
                    'muni-platform-label', 'muni-leader']) {
    map.setLayoutProperty(id, 'visibility', L.platforms ? 'visible' : 'none');
  }
  map.setLayoutProperty('muni-label', 'visibility', L.labels ? 'visible' : 'none');
  for (const id of ['muni-transfer', 'muni-transfer-indoor', 'muni-transfer-indoor-case',
                    'muni-transfer-head', 'muni-transfer-label']) {
    map.setLayoutProperty(id, 'visibility', L.transfers ? 'visible' : 'none');
  }
}

// ------------------------------------------------------------------ camera
function padding() {
  const strip = document.getElementById('strip');
  const insp = document.getElementById('inspector');
  return {
    left: 24 + (strip && !strip.classList.contains('closed') ? 330 : 0),
    right: 24 + (insp && !insp.classList.contains('closed') ? 384 : 0),
    top: 24, bottom: 24,
  };
}

export function flyToStation(id, zoom = 16.4) {
  const s = stationById(id);
  const at = positionOf(s);
  if (!at || !map) return;
  map.easeTo({
    center: at,
    zoom: Math.max(map.getZoom(), zoom),
    duration: 900,
    padding: padding(),
    essential: true,
  });
}

export function fitLine(lineId) {
  const ln = store.doc.lines.find(l => l.id === lineId);
  if (!ln || !map) return;
  const pts = ln.stationIds.map(stationById).filter(Boolean).map(positionOf).filter(Boolean);
  if (!pts.length) return;
  const b = new maplibregl.LngLatBounds();
  for (const at of pts) b.extend(at);
  map.fitBounds(b, { padding: padding(), duration: 1100, maxZoom: 15.5 });
}

export function fitAll() {
  if (!map) return;
  const b = new maplibregl.LngLatBounds();
  let any = false;
  for (const s of store.doc.stations) {
    const at = positionOf(s);
    if (at) { b.extend(at); any = true; }
  }
  if (any) map.fitBounds(b, { padding: padding(), duration: 1000 });
}

export function resetNorth() {
  map?.easeTo({ bearing: 0, pitch: 0, duration: 700 });
}
