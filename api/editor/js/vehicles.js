// The live vehicle layer: where 511 says each in-service vehicle is, drawn over
// the network on both /editor/ and /map/. It is a view of `GET /api/vehicles`
// and nothing else, so it never touches the curation.
//
// Off by default: it polls, and an editor working through platforms does not
// need buses sliding under the cursor.

import {
  store, subscribe, lineById, allLines, lineMatches, filteringModes, esc, upstream,
  derivedStop, ownerOf, stationById,
} from './store.js';
import { api } from './api.js';
import { map, claimClicks, LAYER_IDS } from './map.js';

/** One per page: both share an origin, and turning vehicles on to look around
 *  /map/ is no reason for them to be on the next time someone edits. */
const storageKey = () => `muniplus.${store.publicMap ? 'map' : 'editor'}.layer.vehicles`;
/** The server refreshes from 511 every 180 s, so polling faster than this only
 *  re-reads the same positions; 30 s keeps a fresh fetch on screen within half a
 *  minute of the server having it. */
const POLL_MS = 30_000;
/** A request naming more lines than this asks for all of them instead: with the
 *  bus chip on it would list ~55 of SF's 68 lines, and trimming the other dozen
 *  saves nothing. What is drawn is filtered here either way. */
const MAX_REQUEST_LINES = 24;
/** A position more than this far behind the feed's newest is drawn faded. */
const STALE_S = 300;
const GLIDE_MS = 1000;
/** A vehicle that moved further than this between fetches has started another
 *  trip or lost its fix; gliding it across the city would draw a path it never
 *  took. 180 s at 20 m/s (a bus on a freeway stretch) is 3.6 km. */
const GLIDE_MAX_M = 3000;
const GREY = '#7c8598';

const STATUS = { stoppedAt: 'Stopped at', incomingAt: 'Arriving at', inTransitTo: 'Next stop' };

let res = null;           // the last good VehiclesResponse
let receivedAt = 0;       // performance.now() when `res` arrived
let problem = null;       // { text, title } shown in the layer control, or null
let timer = 0;
let inflight = null;      // AbortController of the fetch in progress
let asked = undefined;    // the `line` query of the last request: null = all lines
const shown = new Map();  // vehicle id -> [lon, lat] as currently drawn
let glide = null;         // { from: Map, to: Map, t0 } while markers are moving
let popup = null;         // { id, popup }

// ------------------------------------------------------------------- setup
export function initVehicles() {
  try { store.layers.vehicles = localStorage.getItem(storageKey()) === '1'; }
  catch { store.layers.vehicles = false; }

  addLayers();
  claimClicks(LAYER_IDS.vehicles);
  map.on('click', 'muni-vehicle', e => openPopup(e.features[0].properties.vid));
  map.on('mouseenter', 'muni-vehicle', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'muni-vehicle', () => { map.getCanvas().style.cursor = ''; });

  subscribe(what => {
    if (what === 'layers') {
      try { localStorage.setItem(storageKey(), store.layers.vehicles ? '1' : '0'); } catch { /* a convenience */ }
    }
    if (what === 'layers' || what === 'filter' || what === 'line' || what === 'all') sync();
  });
  document.addEventListener('visibilitychange', sync);
  sync();
}

/** Start, stop or re-aim polling to match the toggle, the tab and the filters. */
function sync() {
  const on = !!store.layers.vehicles;
  if (!on || document.hidden) {
    clearTimeout(timer); timer = 0;
    inflight?.abort(); inflight = null;
    if (!on) { closePopup(); problem = null; }
    renderNote();
    return;
  }
  // Chips and focus apply at once from what is already here; a fetch follows
  // only if the lines asked for have changed, or polling was stopped.
  draw();
  renderNote();
  if (!timer && !inflight) poll();
  else if (!sameLines(wanted(), asked)) { clearTimeout(timer); timer = 0; poll(); }
}

// ----------------------------------------------------------------- fetching
/**
 * The lines to ask for: the focused one, else every line the mode chips show,
 * else all (null). A hidden line (an owl copy of a day line) is asked for like
 * any other: it is left out of the app's line lists, but its buses are real and
 * on the street.
 */
function wanted() {
  if (store.activeLine) return [store.activeLine];
  if (!filteringModes()) return null;
  const ids = allLines().filter(l => lineMatches(l.id)).map(l => l.id);
  return ids.length > MAX_REQUEST_LINES ? null : ids;
}

const sameLines = (a, b) => (a === null || b === null || b === undefined)
  ? a === b : a.join(',') === b.join(',');

async function poll() {
  clearTimeout(timer); timer = 0;
  const lines = wanted();
  asked = lines;
  if (lines && !lines.length) {
    // Every mode chip is off: nothing to show and nothing to ask for.
    res = res && { ...res, vehicles: [] };
    draw();
    timer = setTimeout(poll, POLL_MS);
    return;
  }
  inflight?.abort();
  const ctl = new AbortController();
  inflight = ctl;
  try {
    const next = await api.vehicles(lines, ctl.signal);
    if (inflight !== ctl) return;
    res = next;
    receivedAt = performance.now();
    problem = feedLag(next);
    moveTo(next.vehicles);
  } catch (err) {
    if (err.name === 'AbortError' || inflight !== ctl) return;
    // Quiet by design: the layer is an overlay, and a server with realtime off
    // (no 511 key) is a normal configuration, not a fault to interrupt anyone for.
    problem = err.status === 503
      ? { text: 'Live positions are off here', title: err.message || 'This server is not polling 511.' }
      : err.status
        ? { text: `Vehicles unavailable (${err.status})`, title: err.message }
        : { text: 'Offline: retrying', title: 'Could not reach the server. The markers shown are from the last fetch that worked.' };
  } finally {
    if (inflight === ctl) {
      inflight = null;
      renderNote();
      if (store.layers.vehicles && !document.hidden) timer = setTimeout(poll, POLL_MS);
    }
  }
}

/**
 * The feed's own clock is its newest position report. The response's
 * `fetchedAt` is when the server fetched it, which is not the same thing: under
 * FIXTURES=1 the server re-reads positions recorded on 2026-09-22 at 14:51 and
 * stamps them with the wall clock, so against `fetchedAt` every vehicle would be
 * hours stale. A whole feed that has fallen behind the fetch is said once, here,
 * instead of by fading every marker.
 */
function feedClock(r) {
  let t = 0;
  for (const v of r?.vehicles || []) if (v.reportedAt > t) t = v.reportedAt;
  return t || r?.fetchedAt || 0;
}

function feedLag(r) {
  const clock = feedClock(r);
  if (!r.fetchedAt || !clock || r.fetchedAt - clock <= STALE_S) return null;
  return {
    text: `Positions ${ago(r.fetchedAt - clock)} old`,
    title: `511's newest position is from ${clockTime(clock)}, ${ago(r.fetchedAt - clock)} before the server fetched it at ${clockTime(r.fetchedAt)}.`,
  };
}

// ------------------------------------------------------------------ drawing
/** The response's vehicles that the chips and the focused line show. A focused
 *  line shows its vehicles alone; otherwise the chips decide, as for lines. */
function visible() {
  const all = res?.vehicles || [];
  if (store.activeLine) return all.filter(v => v.line === store.activeLine);
  return filteringModes() ? all.filter(v => lineMatches(v.line)) : all;
}

function features(pos) {
  const clock = feedClock(res);
  const feats = [];
  for (const v of visible()) {
    const at = pos.get(v.id) || [v.lon, v.lat];
    const ln = lineById(v.line);
    feats.push({
      type: 'Feature',
      properties: {
        vid: v.id,
        color: ln?.color || GREY,
        fg: ln?.textColor || '#FFFFFF',
        label: ln?.shortName || upstream(v.line),
        // Only a bearing 511 actually sent gets an arrow; null (about 1 in 5)
        // is a dot, never a guessed direction.
        arrow: v.bearing == null ? 0 : 1,
        bearing: v.bearing ?? 0,
        stale: clock - v.reportedAt > STALE_S ? 1 : 0,
      },
      geometry: { type: 'Point', coordinates: at },
    });
  }
  return { type: 'FeatureCollection', features: feats };
}

function draw(pos = shown) {
  const src = map?.getSource('vehicles');
  if (!src) return;
  src.setData(features(pos));
  updatePopup(pos);
}

/**
 * Glide every marker from where it is drawn to its new position. Positions
 * change at most every 180 s, so a jump is the only motion there is; a second
 * of easing makes it read as travel rather than as a flicker.
 */
function moveTo(vehicles) {
  const to = new Map(vehicles.map(v => [v.id, [v.lon, v.lat]]));
  const from = new Map();
  const cur = glide ? interpolate(performance.now()) : shown;
  for (const [id, at] of to) {
    const was = cur.get(id);
    if (was && (was[0] !== at[0] || was[1] !== at[1]) && metres(was, at) < GLIDE_MAX_M) from.set(id, was);
  }
  shown.clear();
  for (const [id, at] of to) shown.set(id, at);

  const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (!from.size || still || document.hidden) { glide = null; draw(); return; }
  glide = { from, to, t0: performance.now() };
  requestAnimationFrame(step);
}

function interpolate(now) {
  const { from, to, t0 } = glide;
  const t = Math.min(1, (now - t0) / GLIDE_MS);
  const e = 1 - (1 - t) ** 3;   // ease out: arrive gently
  const out = new Map(to);
  for (const [id, a] of from) {
    const b = to.get(id);
    out.set(id, [a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e]);
  }
  return out;
}

function step(now) {
  if (!glide) return;
  const done = now - glide.t0 >= GLIDE_MS;
  draw(done ? shown : interpolate(now));
  if (done) glide = null; else requestAnimationFrame(step);
}

const M_LAT = 110540, M_LON = 111320 * Math.cos(37.76 * Math.PI / 180);
const metres = (a, b) => Math.hypot((a[0] - b[0]) * M_LON, (a[1] - b[1]) * M_LAT);

// --------------------------------------------------------------- map layers
function addLayers() {
  makeImages();
  map.addSource('vehicles', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

  // Above the lines, stations and poles, below the candidate picker and the
  // station labels; the selected station is redrawn on top (map.js,
  // 'muni-station-sel') so a bus at the stop never hides what is being inspected.
  const before = map.getLayer('muni-station-sel-glow') ? 'muni-station-sel-glow' : undefined;
  map.addLayer({
    id: 'muni-vehicle', type: 'symbol', source: 'vehicles',
    layout: {
      visibility: 'none',
      'icon-image': ['case', ['==', ['get', 'arrow'], 1], 'vehicle-arrow', 'vehicle-dot'],
      // Small at city zoom, where a few hundred vehicles have to share the
      // screen with the network; big enough to carry the line's name from z14.
      'icon-size': ['interpolate', ['linear'], ['zoom'], 10, 0.13, 12, 0.2, 13, 0.3, 14, 0.5, 17, 0.66],
      'icon-rotate': ['get', 'bearing'],
      'icon-rotation-alignment': 'map',
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
      'text-field': ['step', ['zoom'], '', 14, ['get', 'label']],
      'text-font': ['Open Sans Bold', 'Arial Unicode MS Bold'],
      'text-size': ['interpolate', ['linear'], ['zoom'], 14, 8.5, 17, 11],
      'text-rotation-alignment': 'viewport',
      'text-allow-overlap': true,
      'text-ignore-placement': true,
    },
    paint: {
      'icon-color': ['get', 'color'],
      'icon-halo-color': '#ffffff',
      'icon-halo-width': ['interpolate', ['linear'], ['zoom'], 11, 0.6, 15, 1.4],
      'icon-opacity': ['case', ['==', ['get', 'stale'], 1], 0.45, 1],
      'text-color': ['get', 'fg'],
      'text-opacity': ['case', ['==', ['get', 'stale'], 1], 0.55, 1],
    },
  }, before);
}

/**
 * Two SDF shapes, tinted per line by `icon-color`: a disc, and the same disc
 * with a nose pointing at the bearing. The disc is centred in both, so rotating
 * the arrow turns it about the vehicle's position and a dot and an arrow of the
 * same line are the same size.
 *
 * Most SF buses share one blue with the lines they run on, so a marker needs an
 * outline to be seen at all, and an SDF halo is drawn from the distance field
 * outside the shape. A plain canvas fill has none (its alpha stops dead at the
 * edge), so the field is computed here, the way TinySDF encodes it.
 */
function makeImages() {
  const S = 64, R = 17, SPREAD = 8;
  const shape = nose => {
    const c = document.createElement('canvas');
    c.width = c.height = S;
    const g = c.getContext('2d');
    g.translate(S / 2, S / 2);
    g.fillStyle = '#fff';
    g.beginPath();
    g.arc(0, 0, R, 0, Math.PI * 2);
    g.fill();
    if (nose) {
      g.beginPath();
      g.moveTo(-R * 0.72, -R * 0.69);
      g.lineTo(0, -R - 11);
      g.lineTo(R * 0.72, -R * 0.69);
      g.closePath();
      g.fill();
    }
    const alpha = g.getImageData(0, 0, S, S).data;
    const inside = i => alpha[i * 4 + 3] > 127;
    const out = new Uint8ClampedArray(S * S * 4);
    for (let y = 0; y < S; y++) {
      for (let x = 0; x < S; x++) {
        const me = inside(y * S + x);
        // distance to the nearest pixel on the other side of the edge
        let best = SPREAD;
        for (let dy = -SPREAD; dy <= SPREAD; dy++) {
          const yy = y + dy;
          if (yy < 0 || yy >= S) continue;
          for (let dx = -SPREAD; dx <= SPREAD; dx++) {
            const xx = x + dx;
            if (xx < 0 || xx >= S || inside(yy * S + xx) === me) continue;
            const d = Math.hypot(dx, dy);
            if (d < best) best = d;
          }
        }
        const signed = me ? -(best - 0.5) : best - 0.5;
        const i = (y * S + x) * 4;
        out[i] = out[i + 1] = out[i + 2] = 255;
        // MapLibre's SDF scale: the edge at 0.75, one eighth less per pixel
        // outward, which is what its halo width is measured against
        out[i + 3] = 255 * Math.max(0, Math.min(1, 0.75 - signed / 8));
      }
    }
    return { width: S, height: S, data: out };
  };
  if (!map.hasImage('vehicle-dot')) map.addImage('vehicle-dot', shape(false), { sdf: true });
  if (!map.hasImage('vehicle-arrow')) map.addImage('vehicle-arrow', shape(true), { sdf: true });
}

// -------------------------------------------------------------------- popup
function openPopup(id) {
  closePopup();
  const v = res?.vehicles.find(x => x.id === id);
  if (!v) return;
  const p = new maplibregl.Popup({ closeButton: true, maxWidth: '290px', className: 'veh-pop', offset: 14 })
    .setLngLat(shown.get(id) || [v.lon, v.lat])
    .setHTML(popupHtml(v))
    .addTo(map);
  p.on('close', () => { if (popup?.popup === p) popup = null; });
  popup = { id, popup: p };
}

function closePopup() {
  const p = popup?.popup;
  popup = null;
  p?.remove();
}

/** Keep an open popup on its vehicle as it moves, and its details current. */
function updatePopup(pos) {
  if (!popup) return;
  const v = visible().find(x => x.id === popup.id);
  if (!v) { closePopup(); return; }
  popup.popup.setLngLat(pos.get(v.id) || [v.lon, v.lat]);
  if (!glide) popup.popup.setHTML(popupHtml(v));
}

function popupHtml(v) {
  const ln = lineById(v.line);
  const dir = (ln?.directions || []).find(d => d.direction === v.direction);
  const stop = v.stop ? derivedStop(v.stop) : null;
  const sid = v.stop ? ownerOf(v.stop) : null;
  const station = sid ? stationById(sid) : null;
  const stopName = stop?.name || (v.stop ? upstream(v.stop) : '');
  // The age now, not at the fetch: the server's fetch time plus the time since
  // this page received it, so a wrong clock in the browser cannot skew it.
  const age = res?.fetchedAt
    ? res.fetchedAt - v.reportedAt + Math.round((performance.now() - receivedAt) / 1000)
    : null;
  const stale = feedClock(res) - v.reportedAt > STALE_S;

  const where = v.stop ? `
    <div class="vp-row"><span>${esc(STATUS[v.status] || 'Next stop')}</span>
      <b>${esc(stopName)}</b>
      ${station && station.name !== stopName ? `<em>${esc(station.name)}</em>` : ''}
      <code>${esc(upstream(v.stop))}</code></div>` : '';

  return `
    <div class="vp-head">
      <span class="vp-bullet" style="background:${esc(ln?.color || GREY)};color:${esc(ln?.textColor || '#fff')}">${esc(ln?.shortName || upstream(v.line))}</span>
      <div class="vp-title">
        <b>${esc(ln?.name || upstream(v.line))}</b>
        <span>${dir?.headsign ? `→ ${esc(dir.headsign)}` : v.direction == null ? 'direction unknown' : `direction ${v.direction}`}</span>
      </div>
    </div>
    ${where}
    <div class="vp-meta">
      <span>Vehicle <code>${esc(upstream(v.id))}</code></span>
      <span class="${stale ? 'vp-stale' : ''}" title="Reported at ${esc(clockTime(v.reportedAt))}">${age == null ? '' : `reported ${esc(ago(age))} ago`}</span>
    </div>
    ${v.bearing == null ? '<div class="vp-note">511 sent no heading for this vehicle</div>' : ''}`;
}

// ---------------------------------------------------------------- the control
/** The quiet line beside the layer's button: set only when something is wrong. */
function renderNote() {
  const btn = document.querySelector('#map-tools [data-layer="vehicles"]');
  if (!btn) return;
  let note = btn.querySelector('.tool-note');
  if (!note) {
    note = document.createElement('span');
    note.className = 'tool-note';
    btn.appendChild(note);
  }
  const show = store.layers.vehicles && problem;
  note.hidden = !show;
  note.textContent = show ? problem.text : '';
  const base = btn.dataset.baseTitle || (btn.dataset.baseTitle = btn.title);
  const n = visible().length;
  btn.title = !store.layers.vehicles ? base
    : problem ? `${base}\n${problem.title}`
    : res ? `${base}\n${n} vehicle${n === 1 ? '' : 's'} shown, as of ${clockTime(feedClock(res))}`
    : base;
}

// ------------------------------------------------------------------ helpers
function ago(s) {
  s = Math.max(0, Math.round(s));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

const clockTime = t => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
