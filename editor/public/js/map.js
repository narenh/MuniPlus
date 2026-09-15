// The map. A desaturated dark basemap with the Muni network drawn on top:
// a glow pass and a solid pass per line, transfer arcs, station nodes and
// draggable platform poles.

import { store, edit, select, stationById, linesOf, emit } from './store.js';

const STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';
const SF = { center: [-122.4355, 37.7605], zoom: 11.9 };

export let map = null;
let dragging = null;          // { code, station }
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
      .map(s => [s.longitude, s.latitude]);
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

function transferFeatures() {
  const feats = [];
  for (const st of store.doc.stations) {
    for (const t of st.transferStations || []) {
      const to = stationById(t);
      if (!to) continue;
      feats.push({
        type: 'Feature',
        properties: { from: st.id, to: t },
        geometry: { type: 'LineString', coordinates: arc([st.longitude, st.latitude], [to.longitude, to.latitude]) },
      });
    }
  }
  return { type: 'FeatureCollection', features: feats };
}

/** A gentle quadratic bow so two transfer links between the same pair don't overlap. */
function arc(a, b, bend = 0.22) {
  const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const cx = mx - dy * bend, cy = my + dx * bend;
  const out = [];
  for (let i = 0; i <= 18; i++) {
    const t = i / 18, u = 1 - t;
    out.push([u * u * a[0] + 2 * u * t * cx + t * t * b[0],
              u * u * a[1] + 2 * u * t * cy + t * t * b[1]]);
  }
  return out;
}

function stationFeatures() {
  const active = store.activeLine;
  return {
    type: 'FeatureCollection',
    features: store.doc.stations.map(s => {
      const ls = linesOf(s.id);
      const on = !active || ls.some(l => l.id === active);
      const primary = (active && on ? ls.find(l => l.id === active) : ls[0]) || null;
      return {
        type: 'Feature',
        id: hashId(s.id),
        properties: {
          sid: s.id,
          name: s.name,
          kind: s.kind,
          color: primary?.color || '#7c8598',
          active: on ? 1 : 0,
          interchange: ls.length > 1 ? 1 : 0,
          selected: store.selStation === s.id ? 1 : 0,
          nplat: s.platforms.length,
        },
        geometry: { type: 'Point', coordinates: [s.longitude, s.latitude] },
      };
    }),
  };
}

function platformFeatures() {
  const active = store.activeLine;
  const feats = [];
  for (const s of store.doc.stations) {
    const ls = linesOf(s.id);
    const on = !active || ls.some(l => l.id === active);
    const primary = (active && on ? ls.find(l => l.id === active) : ls[0]) || null;
    for (const p of s.platforms) {
      const d = store.drift?.get(String(p.id));
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
          drift: d && d.kind !== 'ok' ? 1 : 0,
          label: p.stopName || s.name,
        },
        geometry: { type: 'Point', coordinates: [p.longitude, p.latitude] },
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
}

function addSources() {
  map.addSource('routes', { type: 'geojson', data: routeFeatures() });
  map.addSource('transfers', { type: 'geojson', data: transferFeatures() });
  map.addSource('stations', { type: 'geojson', data: stationFeatures() });
  map.addSource('platforms', { type: 'geojson', data: platformFeatures() });
}

const dimmed = (on, a, b) => ['case', ['==', ['get', 'active'], 1], a, b];

function addLayers() {
  // --- transfer arcs
  map.addLayer({
    id: 'muni-transfer', type: 'line', source: 'transfers',
    layout: { 'line-cap': 'round' },
    paint: {
      'line-color': '#8ea0c4',
      'line-width': ['interpolate', ['linear'], ['zoom'], 11, 1, 16, 2],
      'line-opacity': 0.5,
      'line-dasharray': [1.5, 2],
    },
  });

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

  // --- platform poles
  map.addLayer({
    id: 'muni-platform-halo', type: 'circle', source: 'platforms',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 4, 16, 13, 19, 20],
      'circle-color': ['case', ['==', ['get', 'drift'], 1], '#fbbf24', ['get', 'color']],
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
        ['==', ['get', 'drift'], 1], '#fbbf24',
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

  // --- station nodes
  map.addLayer({
    id: 'muni-station-glow', type: 'circle', source: 'stations',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 7, 15, 17, 18, 26],
      'circle-color': ['get', 'color'],
      'circle-opacity': ['case', ['==', ['get', 'selected'], 1], 0.5, 0],
      'circle-blur': 0.6,
    },
  });
  map.addLayer({
    id: 'muni-station', type: 'circle', source: 'stations',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        11, ['case', ['==', ['get', 'kind'], 'underground'], 5, 3.4],
        14, ['case', ['==', ['get', 'kind'], 'underground'], 7.5, 5.4],
        18, ['case', ['==', ['get', 'kind'], 'underground'], 13, 9.5]],
      'circle-color': ['case',
        ['==', ['get', 'kind'], 'underground'], '#ffffff',
        ['==', ['get', 'interchange'], 1], '#e9edf6',
        '#0a0c12'],
      'circle-stroke-width': ['case',
        ['==', ['get', 'selected'], 1], 3.5,
        ['boolean', ['feature-state', 'hover'], false], 3, 2.4],
      'circle-stroke-color': ['case',
        ['==', ['get', 'selected'], 1], '#ffffff', ['get', 'color']],
      'circle-opacity': dimmed(true, 1, 0.3),
      'circle-stroke-opacity': dimmed(true, 1, 0.28),
    },
  });

  // --- labels
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
  const hoverable = ['muni-station', 'muni-platform'];

  for (const layer of hoverable) {
    const src = layer === 'muni-station' ? 'stations' : 'platforms';

    map.on('mousemove', layer, e => {
      if (dragging) return;
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

  // ---- drag a pole to reposition it
  map.on('mousedown', 'muni-platform', e => {
    if (e.originalEvent.button !== 0) return;
    e.preventDefault();
    const { sid, code } = e.features[0].properties;
    dragging = { sid, code, moved: false };
    map.dragPan.disable();
    map.getCanvas().style.cursor = 'grabbing';
    select(sid, code);
    listeners.pick.forEach(f => f(sid, code));
  });

  map.on('mousemove', e => {
    if (!dragging) return;
    dragging.moved = true;
    const st = stationById(dragging.sid);
    const p = st?.platforms.find(x => String(x.id) === dragging.code);
    if (!p) return;
    p.latitude = round6(e.lngLat.lat);
    p.longitude = round6(e.lngLat.lng);
    refresh('platforms');
    showCoords(`${dragging.code}  ${p.latitude.toFixed(6)}, ${p.longitude.toFixed(6)}`);
  });

  map.on('mouseup', () => {
    if (!dragging) return;
    const { sid, code, moved } = dragging;
    dragging = null;
    map.dragPan.enable();
    map.getCanvas().style.cursor = '';
    hideCoords();
    if (!moved) return;

    // The live drag mutated the doc directly for responsiveness; replay it as a
    // single undoable step so one drag is one undo.
    const st = stationById(sid);
    const p = st.platforms.find(x => String(x.id) === code);
    const lat = p.latitude, lon = p.longitude;
    edit(`Move pole ${code}`, d => {
      const s = d.stations.find(x => x.id === sid);
      const q = s.platforms.find(x => String(x.id) === code);
      q.latitude = lat; q.longitude = lon;
    });
  });

  map.on('click', e => {
    const hits = map.queryRenderedFeatures(e.point, { layers: ['muni-station', 'muni-platform'] });
    if (!hits.length) { select(null, null); listeners.pick.forEach(f => f(null, null)); }
  });
}

const round6 = n => Math.round(n * 1e6) / 1e6;

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
  if (which === 'all' || which === 'transfers') map.getSource('transfers').setData(transferFeatures());
  if (which === 'all' || which === 'stations') map.getSource('stations').setData(stationFeatures());
  if (which === 'all' || which === 'platforms') map.getSource('platforms').setData(platformFeatures());
}

export function applyLayerToggles() {
  if (!map?.getLayer('muni-platform')) return;
  const L = store.layers;
  for (const id of ['muni-platform', 'muni-platform-halo', 'muni-platform-arrow']) {
    map.setLayoutProperty(id, 'visibility', L.platforms ? 'visible' : 'none');
  }
  map.setLayoutProperty('muni-label', 'visibility', L.labels ? 'visible' : 'none');
  map.setLayoutProperty('muni-transfer', 'visibility', L.transfers ? 'visible' : 'none');
  map.setPaintProperty('muni-platform-halo', 'circle-opacity', L.drift
    ? ['case', ['==', ['get', 'drift'], 1], 0.55, 0.06]
    : ['case', ['==', ['get', 'selected'], 1], 0.35,
       ['boolean', ['feature-state', 'hover'], false], 0.26, 0.1]);
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
  if (!s || !map) return;
  map.easeTo({
    center: [s.longitude, s.latitude],
    zoom: Math.max(map.getZoom(), zoom),
    duration: 900,
    padding: padding(),
    essential: true,
  });
}

export function fitLine(lineId) {
  const ln = store.doc.lines.find(l => l.id === lineId);
  if (!ln || !map) return;
  const pts = ln.stationIds.map(stationById).filter(Boolean);
  if (!pts.length) return;
  const b = new maplibregl.LngLatBounds();
  for (const s of pts) b.extend([s.longitude, s.latitude]);
  map.fitBounds(b, { padding: padding(), duration: 1100, maxZoom: 15.5 });
}

export function fitAll() {
  if (!map) return;
  const b = new maplibregl.LngLatBounds();
  for (const s of store.doc.stations) b.extend([s.longitude, s.latitude]);
  map.fitBounds(b, { padding: padding(), duration: 1000 });
}

export function resetNorth() {
  map?.easeTo({ bearing: 0, pitch: 0, duration: 700 });
}
