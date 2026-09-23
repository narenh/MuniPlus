// The map. A desaturated dark basemap with the Muni network drawn on top:
// a glow pass and a solid pass per line, transfer arcs, station nodes and
// platform poles. Nothing here moves a coordinate: they come from 511, by way
// of the server's `derived` values.

import {
  store, select, stationById, stationIds, linesOf, lineById, allLines, platformsOf,
  stationPos, platformPos, derivedPlatform, stationMatches, platformMatches,
  lineMatches, upstream, metresBetween, unclaimedNear,
} from './store.js';

const STYLE = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';
// A walking path is meaningless at city zoom; it only means something once the
// blocks it crosses are on screen.
const TRANSFER_MINZOOM = 13.6;
// Where poles stop being a cluster of dots and become individually legible.
const POLE_MINZOOM = 15;
const SF = { center: [-122.4355, 37.7605], zoom: 11.9 };
const GREY = '#7c8598';

export let map = null;
let litLink = null;           // a transfer chip is being hovered in the inspector
let hoverId = null;
const listeners = { pick: [], candidate: [] };

export function onPick(fn) { listeners.pick.push(fn); }
/** A candidate stop was clicked while the add-platform picker is open. */
export function onCandidate(fn) { listeners.candidate.push(fn); }

// ---------------------------------------------------------------- geometry
const HEADING_DEG = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };

/**
 * Each line as the path its most-run pattern per direction drives: the GTFS
 * shape, so it follows the street and the curves of the track. Until the shapes
 * arrive, or for a direction the feed has no shape for, the line is drawn
 * through the platforms it stops at instead, which cuts corners but is never
 * missing. Hidden lines (owl copies of a day line) are left off unless focused,
 * as the app leaves them off.
 */
function lineFeatures() {
  const feats = [];
  for (const ln of allLines()) {
    const focused = store.activeLine === ln.id;
    if (!focused && (ln.hidden || !lineMatches(ln.id))) continue;
    const active = !store.activeLine || focused;
    for (const dir of ln.directions || []) {
      const coords = store.shapes[dir.shape] || dir.platforms.map(platformPos).filter(Boolean);
      if (coords.length < 2) continue;
      feats.push({
        type: 'Feature',
        properties: { id: ln.id, dir: dir.direction, color: ln.color || GREY, active: active ? 1 : 0, name: ln.name },
        geometry: { type: 'LineString', coordinates: coords },
      });
    }
  }
  // draw the active line last so it sits on top
  return { type: 'FeatureCollection', features: feats.sort((a, b) => a.properties.active - b.properties.active) };
}

/**
 * Transfers as walking paths. A transfer is one-way unless the other station
 * lists it back, and that asymmetry is deliberate in the curation, so each pair
 * is drawn once with an arrowhead at whichever end(s) it actually points to:
 * one chevron for a one-way link, one at each end for a mutual one.
 *
 * Straight lines are the honest shape here. The curation records that a walk
 * exists, never its path, and this is an editor - inventing a path through the
 * streets would be drawing a claim the data does not make.
 */
export function transferLinks() {
  const links = new Map();      // "a|b" with a < b  ->  { a, b, fwd, rev, indoor }
  for (const sid of stationIds()) {
    for (const t of stationById(sid).transfers || []) {
      if (!stationById(t.to)) continue;
      const [a, b] = sid < t.to ? [sid, t.to] : [t.to, sid];
      const key = `${a}|${b}`;
      const rec = links.get(key) || { a, b, fwd: false, rev: false, indoor: false };
      if (sid === a) rec.fwd = true; else rec.rev = true;   // fwd means a -> b
      if (t.mode === 'indoor') rec.indoor = true;
      links.set(key, rec);
    }
  }
  return links;
}

function transferFeatures() {
  const lines = [], heads = [];
  const sel = store.selStation;

  for (const { a, b, fwd, rev, indoor } of transferLinks().values()) {
    if (!shown(a) && !shown(b)) continue;
    const pa = stationPos(a), pb = stationPos(b);
    if (!pa || !pb) continue;
    const both = fwd && rev ? 1 : 0;
    const touches = sel === a || sel === b ? 1 : 0;
    const lit = isLit(a, b) ? 1 : 0;

    lines.push({
      type: 'Feature',
      properties: { a, b, both, touches, lit, indoor: indoor ? 1 : 0, metres: metresBetween(pa, pb) },
      geometry: { type: 'LineString', coordinates: [pa, pb] },
    });
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
/** Bearing in degrees clockwise from north, for icon-rotate. */
const bearingDeg = (a, b) =>
  (Math.atan2((b[0] - a[0]) * M_LON, (b[1] - a[1]) * M_LAT) * 180 / Math.PI + 360) % 360;

/** Whether the filters show a station. The selected one always shows, so a
 *  filter never hides what the inspector is editing. */
const shown = sid => sid === store.selStation || stationMatches(sid);

/** The colour a station or pole is drawn in: the focused line's if it is on
 *  it, else its first line's. */
function tint(lines, on) {
  const active = store.activeLine;
  const primary = (active && on ? lines.find(l => l.id === active) : lines[0]) || null;
  return primary?.color || GREY;
}

function stationFeatures() {
  const active = store.activeLine;
  const feats = [];
  for (const sid of stationIds()) {
    if (!shown(sid)) continue;
    const at = stationPos(sid);
    if (!at) continue;
    const s = stationById(sid);
    const ls = linesOf(sid);
    const on = !active || ls.some(l => l.id === active);
    feats.push({
      type: 'Feature',
      id: hashId(sid),
      properties: {
        sid,
        name: s.name,
        color: tint(ls, on),
        active: on ? 1 : 0,
        interchange: ls.length > 1 ? 1 : 0,
        selected: store.selStation === sid ? 1 : 0,
        // a platform 511 no longer lists: flagged, never removed automatically
        stale: platformsOf(s).some(p => !derivedPlatform(p.id)?.live) ? 1 : 0,
      },
      geometry: { type: 'Point', coordinates: at },
    });
  }
  return { type: 'FeatureCollection', features: feats };
}

/** Poles of shown stations whose own lines pass the mode chips. A platform with
 *  no coordinate (not in 511's snapshot) has nothing to draw. */
function* shownPoles() {
  const active = store.activeLine;
  for (const sid of stationIds()) {
    if (!shown(sid)) continue;
    const ls = linesOf(sid);
    const on = !active || ls.some(l => l.id === active);
    const color = tint(ls, on);
    for (const p of platformsOf(stationById(sid))) {
      const at = platformPos(p.id);
      if (!at) continue;
      if (sid !== store.selStation && !platformMatches(p.id)) continue;
      yield { sid, p, at, on, color };
    }
  }
}

function platformFeatures() {
  const feats = [];
  for (const { sid, p, at, on, color } of shownPoles()) {
    feats.push({
      type: 'Feature',
      id: hashId(p.id),
      properties: {
        pid: p.id,
        code: upstream(p.id),
        sid,
        heading: p.heading,
        bearing: HEADING_DEG[p.heading] ?? 0,
        color,
        active: on ? 1 : 0,
        selected: store.selPlatform === p.id ? 1 : 0,
        label: derivedPlatform(p.id)?.stopName || stationById(sid).name,
      },
      geometry: { type: 'Point', coordinates: at },
    });
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
  const feats = [];
  for (const { sid, at, on, color } of shownPoles()) {
    const centre = stationPos(sid);
    if (!centre) continue;
    feats.push({
      type: 'Feature',
      properties: { color, selected: store.selStation === sid ? 1 : 0, active: on ? 1 : 0 },
      geometry: { type: 'LineString', coordinates: [centre, at] },
    });
  }
  return { type: 'FeatureCollection', features: feats };
}

/**
 * Unclaimed snapshot stops near a station, while its add-platform picker is
 * open. They are the only stops a platform can be added from, and which one is
 * right is usually obvious only on the map.
 */
let candidateFor = null;
export const CANDIDATE_LIMIT = 40;

export function showCandidates(sid) {
  candidateFor = sid;
  refresh('candidates');
}

function candidateFeatures() {
  if (!candidateFor || !map) return { type: 'FeatureCollection', features: [] };
  const at = stationPos(candidateFor) || map.getCenter().toArray();
  return {
    type: 'FeatureCollection',
    features: unclaimedNear(at, CANDIDATE_LIMIT).map(c => ({
      type: 'Feature',
      id: hashId(c.id),
      properties: { pid: c.id, code: upstream(c.id), label: c.p.stopName || '' },
      geometry: { type: 'Point', coordinates: [c.p.lon, c.p.lat] },
    })),
  };
}

/**
 * One stop ringed on the map: an issue about a 511 stop no station owns
 * (`unassigned-stop`) has no pole to select, so without this the camera would
 * fly to an empty street.
 */
let spotPid = null;
export function spotlight(pid) {
  if (spotPid === pid) return;
  spotPid = pid;
  refresh('spot');
}

function spotFeatures() {
  const at = spotPid && platformPos(spotPid);
  return {
    type: 'FeatureCollection',
    features: at ? [{ type: 'Feature', properties: { code: upstream(spotPid) }, geometry: { type: 'Point', coordinates: at } }] : [],
  };
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
  map.addSource('lines', { type: 'geojson', data: lineFeatures() });
  const tf = transferFeatures();
  map.addSource('transfers', { type: 'geojson', data: tf.lines });
  map.addSource('transfer-heads', { type: 'geojson', data: tf.heads });
  map.addSource('stations', { type: 'geojson', data: stationFeatures() });
  map.addSource('platforms', { type: 'geojson', data: platformFeatures() });
  map.addSource('leaders', { type: 'geojson', data: leaderFeatures() });
  map.addSource('candidates', { type: 'geojson', data: candidateFeatures() });
  map.addSource('spot', { type: 'geojson', data: spotFeatures() });
}

const dimmed = (on, a, b) => ['case', ['==', ['get', 'active'], 1], a, b];
const isSel = ['==', ['get', 'selected'], 1];
const isHover = ['boolean', ['feature-state', 'hover'], false];

/**
 * Station nodes. Metro convention: a station served by more than one line is a
 * white disc with a black ring; a single-line stop is a small dot ringed in
 * that line's colour. Interchanges are drawn larger, as on a real map.
 *
 * At city zoom SF's 1,805 stations sit closer together than the dots were
 * wide, so the lines drowned under a blanket of rings. Below z14 the dots and
 * their rings shrink with the zoom, down to specks along the lines at z10;
 * from z14 up they are the size they always were.
 */
const STATION_PAINT = {
  'circle-radius': ['interpolate', ['linear'], ['zoom'],
    10, ['case', ['==', ['get', 'interchange'], 1], 1.8, 1.2],
    12, ['case', ['==', ['get', 'interchange'], 1], 2.8, 1.8],
    13, ['case', ['==', ['get', 'interchange'], 1], 4.6, 3.1],
    14, ['case', ['==', ['get', 'interchange'], 1], 8, 5.2],
    18, ['case', ['==', ['get', 'interchange'], 1], 14, 9]],
  'circle-color': ['case',
    ['==', ['get', 'interchange'], 1], '#ffffff', '#0a0c12'],
  'circle-stroke-width': ['interpolate', ['linear'], ['zoom'],
    10, ['case', isSel, 1.6, 0.6],
    12, ['case', isSel, 2.4, isHover, 1.6, 0.9],
    14, ['case', isSel, 4, isHover, 3.2, 2.4]],
  'circle-stroke-color': ['case',
    // a white ring on a white disc would have no edge, so a selected
    // interchange keeps its black ring and is marked by the glow beneath
    ['==', ['get', 'interchange'], 1], '#05060a',
    isSel, '#ffffff',
    ['get', 'color']],
  'circle-opacity': dimmed(true, 1, 0.3),
  'circle-stroke-opacity': dimmed(true, 1, 0.28),
};

function addLayers() {
  // --- line glow, then casing, then the line itself
  map.addLayer({
    id: 'muni-line-glow', type: 'line', source: 'lines',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': ['interpolate', ['linear'], ['zoom'], 10, 11, 14, 24, 18, 44],
      'line-blur': ['interpolate', ['linear'], ['zoom'], 10, 8, 16, 24],
      'line-opacity': dimmed(true, 0.55, 0.05),
    },
  });
  map.addLayer({
    id: 'muni-line-case', type: 'line', source: 'lines',
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#05060a',
      'line-width': ['interpolate', ['linear'], ['zoom'], 10, 4.6, 14, 8.5, 18, 15],
      'line-opacity': dimmed(true, 0.9, 0.2),
    },
  });
  map.addLayer({
    id: 'muni-line', type: 'line', source: 'lines',
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
  // line or a street walk. The casing goes first so it sits underneath.
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
  // A station holding a platform 511 no longer lists gets an amber ring, so a
  // snapshot refresh that drops stops is visible at a glance on the map.
  map.addLayer({
    id: 'muni-station-stale', type: 'circle', source: 'stations',
    filter: ['==', ['get', 'stale'], 1],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2.6, 12, 4, 14, 11, 18, 18],
      'circle-color': 'rgba(0,0,0,0)',
      'circle-stroke-color': '#fbbf24',
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 10, 1, 13, 2],
      'circle-opacity': dimmed(true, 1, 0.3),
      'circle-stroke-opacity': dimmed(true, 0.95, 0.3),
    },
  });
  map.addLayer({ id: 'muni-station', type: 'circle', source: 'stations', paint: STATION_PAINT });

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
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 1.2, 13, 4, 16, 13, 19, 20],
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
      // Shrunk and faded at city zoom for the same reason as the stations:
      // 3,000 poles at their street-level size bury the lines.
      'circle-radius': ['interpolate', ['linear'], ['zoom'],
        11, 0.8, 13, 2, 14, 4, 16, 6.5, 19, 11],
      'circle-color': '#0a0c12',
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'],
        11, 0.5,
        13, ['case', isSel, 2, 1.1],
        14, ['case', isSel, 3, 2],
        19, ['case', isSel, 5, 3.5]],
      'circle-stroke-color': ['case',
        ['==', ['get', 'selected'], 1], '#ffffff',
        ['get', 'color']],
      'circle-opacity': ['interpolate', ['linear'], ['zoom'],
        12, dimmed(true, 0.5, 0.12), 14, dimmed(true, 1, 0.25)],
      'circle-stroke-opacity': ['interpolate', ['linear'], ['zoom'],
        12, dimmed(true, 0.5, 0.12), 14, dimmed(true, 1, 0.3)],
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

  // --- the selected station again, over the poles and the live vehicles
  // (vehicles.js inserts its layer just below these), so whatever is being
  // inspected is never hidden by a bus stopped on it
  map.addLayer({
    id: 'muni-station-sel-glow', type: 'circle', source: 'stations', filter: isSel,
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 9, 15, 21, 18, 32],
      'circle-color': ['get', 'color'],
      'circle-opacity': 0.75,
      'circle-blur': 0.55,
    },
  });
  map.addLayer({ id: 'muni-station-sel', type: 'circle', source: 'stations', filter: isSel, paint: STATION_PAINT });

  // --- candidate stops for the add-platform picker: hollow and dashed, so
  // they never read as poles a station already has
  map.addLayer({
    id: 'muni-candidate', type: 'circle', source: 'candidates',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 3, 16, 7, 19, 11],
      'circle-color': 'rgba(0,0,0,0)',
      'circle-stroke-color': '#60a5fa',
      'circle-stroke-width': ['case', ['boolean', ['feature-state', 'hover'], false], 3, 2],
    },
  });
  map.addLayer({
    id: 'muni-candidate-label', type: 'symbol', source: 'candidates',
    minzoom: 14.5,
    layout: {
      'text-field': ['get', 'code'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': 11,
      'text-offset': [0, 1.2],
      'text-anchor': 'top',
      'text-optional': true,
    },
    paint: { 'text-color': '#93c5fd', 'text-halo-color': '#05060a', 'text-halo-width': 1.8 },
  });

  map.addLayer({
    id: 'muni-spot', type: 'circle', source: 'spot',
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 8, 16, 14, 19, 20],
      'circle-color': 'rgba(251,191,36,.18)',
      'circle-stroke-color': '#fbbf24',
      'circle-stroke-width': 2.5,
    },
  });
  map.addLayer({
    id: 'muni-spot-label', type: 'symbol', source: 'spot',
    layout: {
      'text-field': ['get', 'code'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': 12,
      'text-offset': [0, 1.6],
      'text-anchor': 'top',
      'text-allow-overlap': true,
    },
    paint: { 'text-color': '#fcd34d', 'text-halo-color': '#05060a', 'text-halo-width': 1.8 },
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
  const SRC = { 'muni-station': 'stations', 'muni-platform': 'platforms', 'muni-candidate': 'candidates' };

  for (const layer of Object.keys(SRC)) {
    const src = SRC[layer];

    map.on('mousemove', layer, e => {
      map.getCanvas().style.cursor = 'pointer';
      const f = e.features[0];
      if (f.id != null) {
        if (hoverId && (hoverId.id !== f.id || hoverId.src !== src)) {
          map.setFeatureState({ source: hoverId.src, id: hoverId.id }, { hover: false });
        }
        hoverId = { src, id: f.id };
        map.setFeatureState({ source: src, id: f.id }, { hover: true });
      }
      const p = f.properties;
      showCoords(src === 'candidates' ? `${p.label} · ${p.code} · unclaimed`
        : p.code ? `${p.label} · ${p.code} · ${p.heading}`
        : p.name);
    });

    map.on('mouseleave', layer, () => {
      map.getCanvas().style.cursor = '';
      if (hoverId) map.setFeatureState({ source: hoverId.src, id: hoverId.id }, { hover: false });
      hoverId = null;
      hideCoords();
    });
  }

  map.on('click', 'muni-candidate', e => {
    e.originalEvent.stopPropagation();
    const pid = e.features[0].properties.pid;
    listeners.candidate.forEach(f => f(pid));
  });

  map.on('click', 'muni-station', e => {
    if (hitOverlay(e)) return;
    const sid = e.features[0].properties.sid;
    select(sid, null);
    listeners.pick.forEach(f => f(sid, null));
  });

  map.on('click', 'muni-platform', e => {
    if (hitOverlay(e)) return;
    e.originalEvent.stopPropagation();
    const { sid, pid } = e.features[0].properties;
    select(sid, pid);
    listeners.pick.forEach(f => f(sid, pid));
  });

  map.on('click', e => {
    const layers = ['muni-station', 'muni-platform', 'muni-candidate', ...claimed]
      .filter(id => map.getLayer(id));
    const hits = map.queryRenderedFeatures(e.point, { layers });
    if (!hits.length) { select(null, null); listeners.pick.forEach(f => f(null, null)); }
  });
}

/** Layers drawn over the stations that handle their own clicks (the live
 *  vehicles). A click on one neither selects what is under it nor clears the
 *  selection. */
const claimed = [];
export function claimClicks(layerIds) { claimed.push(...layerIds); }

/** A candidate or a claimed overlay drawn over a station or pole takes the
 *  click. A hidden layer renders nothing, so it hits nothing. */
function hitOverlay(e) {
  const layers = [...(candidateFor ? ['muni-candidate'] : []), ...claimed].filter(id => map.getLayer(id));
  return layers.length > 0 && map.queryRenderedFeatures(e.point, { layers }).length > 0;
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
  if (!map || !map.getSource('lines')) return;
  const all = which === 'all';
  if (all || which === 'lines') map.getSource('lines').setData(lineFeatures());
  if (all || which === 'transfers') {
    const tf = transferFeatures();
    map.getSource('transfers').setData(tf.lines);
    map.getSource('transfer-heads').setData(tf.heads);
  }
  if (all || which === 'stations') map.getSource('stations').setData(stationFeatures());
  if (all || which === 'platforms') {
    map.getSource('platforms').setData(platformFeatures());
    map.getSource('leaders').setData(leaderFeatures());
  }
  if (all || which === 'candidates') map.getSource('candidates').setData(candidateFeatures());
  if (all || which === 'spot') map.getSource('spot').setData(spotFeatures());
}

/**
 * Map layers the tool buttons switch, by `store.layers` key. A new overlay (the
 * live vehicle layer) adds its layer ids here and a button in main.js's
 * LAYER_TOOLS; nothing else needs to know about it.
 */
export const LAYER_IDS = {
  platforms: ['muni-platform', 'muni-platform-halo', 'muni-platform-arrow',
              'muni-platform-label', 'muni-leader'],
  labels: ['muni-label'],
  transfers: ['muni-transfer', 'muni-transfer-indoor', 'muni-transfer-indoor-case',
              'muni-transfer-head', 'muni-transfer-label'],
  vehicles: ['muni-vehicle'],
};

export function applyLayerToggles() {
  if (!map?.getLayer('muni-platform')) return;
  for (const [key, ids] of Object.entries(LAYER_IDS)) {
    for (const id of ids) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', store.layers[key] ? 'visible' : 'none');
    }
  }
}

// ------------------------------------------------------------------ camera
function padding() {
  // On a phone both panels are sheets over the bottom of the map (app.css,
  // "Phone"), so what they hide is the bottom, not a side. Whether one is open
  // comes from the state, not its class: a tap that focuses a line fits the
  // camera at once, and the sheet only opens on the next frame's render. Its
  // height is the cap, which its content always reaches (a line's stops, a
  // station's platform cards) and which is known before it has rendered.
  if (narrow()) {
    const stripOpen = !!lineById(store.activeLine);
    const inspOpen = !!stationById(store.selStation) || (store.lineInspector && stripOpen && !store.publicMap);
    const h = document.getElementById('body')?.clientHeight || window.innerHeight;
    const sheet = Math.max(stripOpen ? SHEET.strip : 0, inspOpen ? SHEET.inspector : 0) * h;
    return { left: 16, right: 16, top: 16, bottom: 16 + sheet };
  }
  const strip = document.getElementById('strip');
  const insp = document.getElementById('inspector');
  return {
    left: 24 + (strip && !strip.classList.contains('closed') ? 330 : 0),
    right: 24 + (insp && !insp.classList.contains('closed') ? 384 : 0),
    top: 24, bottom: 24,
  };
}

/** The width at which app.css turns the panels into bottom sheets, and their
 *  max-height there, as a share of the area below the top bar. */
export const narrow = () => window.matchMedia('(max-width: 640px)').matches;
const SHEET = { strip: 0.44, inspector: 0.58 };

export function flyTo(at, zoom = 16.4) {
  if (!at || !map) return;
  map.easeTo({
    center: at,
    zoom: Math.max(map.getZoom(), zoom),
    duration: 900,
    padding: padding(),
    essential: true,
  });
}

export const flyToStation = (id, zoom) => flyTo(stationPos(id), zoom);

export function fitLine(lineId) {
  const ln = lineById(lineId);
  if (!ln || !map) return;
  const pts = (ln.directions || []).flatMap(d => d.platforms.map(platformPos)).filter(Boolean);
  if (!pts.length) return;
  const b = new maplibregl.LngLatBounds();
  for (const at of pts) b.extend(at);
  map.fitBounds(b, { padding: padding(), duration: 1100, maxZoom: 15.5 });
}

export function fitAll() {
  if (!map) return;
  const b = new maplibregl.LngLatBounds();
  let any = false;
  for (const sid of stationIds()) {
    const at = stationPos(sid);
    if (at) { b.extend(at); any = true; }
  }
  if (any) map.fitBounds(b, { padding: padding(), duration: 1000 });
}

export function mapCentre() {
  return map ? map.getCenter().toArray() : null;
}

export function resetNorth() {
  map?.easeTo({ bearing: 0, pitch: 0, duration: 700 });
}
