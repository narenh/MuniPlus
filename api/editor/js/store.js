// Single source of truth. The editor holds what the server sends in
// `EditorState` (api/app/models/editor.py) and edits exactly one part of it:
// `curation`, the hand-curated layer, which is also what a save posts back.
// Everything else (`derived`, `validation`) is the server's to compute. After
// every edit the curation goes to `api/validate` and both are replaced with the
// answer, so the browser never re-implements a rule it could get wrong.
//
// Every mutation goes through edit(), which snapshots for undo and notifies
// subscribers.

import { api } from './api.js';

const clone = o => JSON.parse(JSON.stringify(o));

/** The server's line order puts these first, in this order (MODE_ORDER in
 *  app/data/network.py); a mode another operator adds later sorts after them. */
export const MODES = ['metro', 'streetcar', 'cableway', 'bus'];

export const store = {
  // --- EditorState
  version: null,        // sf-transit commit the curation started from (baseVersion)
  readOnly: false,
  readOnlyReason: null,
  publicMap: false,     // served at /map/: no editorial chrome at all
  curation: null,       // edited in place
  base: null,           // curation as last loaded or saved: the change-list baseline
  snapshots: {},
  derived: { stations: {}, platforms: {}, lines: {} },
  shapes: {},           // GET /api/shapes, keyed by the shape id a direction names
  validation: null,     // null on /map
  repo: null,           // null on /map

  // --- view state
  activeLine: '',       // '' = all lines
  activeDir: 0,         // index into the active line's directions, for ↑/↓
  lineInspector: false, // the inspector shows the active line, not a station
  selStation: null,     // station id
  selPlatform: null,    // full platform id, "SF:16992"
  modesOff: new Set(),  // modes the chips have switched off; empty = no filter
  unverifiedOnly: false,
  layers: { platforms: true, labels: true, transfers: true, vehicles: false },

  // --- validate round trip
  checking: false,      // a validate call is pending or in flight
  checkError: null,     // the last validate call failed; derived may be stale

  onBlocked: null,      // set by main.js so a blocked edit can explain itself

  _undo: [], _redo: [], _subs: new Set(), _max: 120, _rev: 0,
};

export function subscribe(fn) { store._subs.add(fn); return () => store._subs.delete(fn); }

export function emit(what = 'all') {
  for (const fn of store._subs) fn(what);
}

export function load(state) {
  store.version = state.version;
  store.readOnly = !!state.readOnly;
  store.readOnlyReason = state.readOnlyReason || null;
  // The public map is read-only by purpose, where the editor is read-only by
  // circumstance (no password, no checkout), so it is told apart by the reason
  // app/editor/state.py gives, or by the path should that wording ever change.
  store.publicMap = store.readOnlyReason === 'public map' || location.pathname.startsWith('/map');
  store.curation = state.curation;
  store.base = clone(state.curation);
  store.snapshots = state.snapshots || {};
  store.derived = state.derived;
  store.validation = state.validation || null;
  store.repo = state.repo || null;
  store._undo = []; store._redo = [];
  store._rev++;
  emit('all');
}

/** Replace the curation wholesale (a restored draft), keeping `base` as given. */
export function adopt({ curation, base = store.base, version = store.version }) {
  store.curation = clone(curation);
  store.base = clone(base);
  store.version = version;
  store._undo = []; store._redo = [];
  store._rev++;
  scheduleValidate(0);
  emit('doc');
}

/**
 * Apply a mutation. `label` is what the undo toast shows. The mutator receives
 * the live curation and edits it in place.
 */
export function edit(label, mutate) {
  if (store.readOnly) { store.onBlocked?.(); return false; }
  const before = clone(store.curation);
  mutate(store.curation);
  clearVerifiedWherePlatformsChanged(before.stations.stations, store.curation.stations.stations);
  store._undo.push({ label, curation: before });
  if (store._undo.length > store._max) store._undo.shift();
  store._redo.length = 0;
  store._rev++;
  scheduleValidate();
  emit('doc');
  return true;
}

/**
 * `verified` says a person checked this station's platforms on the map. Once
 * the platform list changes that is no longer true, so the date goes. Done here
 * rather than in each handler so no path that touches platforms can forget it.
 * Renames, headings and notes leave it alone (PLAN.md, "verification").
 */
function clearVerifiedWherePlatformsChanged(before, after) {
  const ids = st => (st.platforms || []).map(p => p.id).sort().join(',');
  for (const [sid, st] of Object.entries(after)) {
    const was = before[sid];
    if (was && st.verified && ids(was) !== ids(st)) delete st.verified;
  }
}

export function undo() {
  if (store.readOnly) return null;
  const step = store._undo.pop();
  if (!step) return null;
  store._redo.push({ label: step.label, curation: clone(store.curation) });
  store.curation = step.curation;
  store._rev++;
  scheduleValidate();
  emit('doc');
  return step.label;
}

export function redo() {
  if (store.readOnly) return null;
  const step = store._redo.pop();
  if (!step) return null;
  store._undo.push({ label: step.label, curation: clone(store.curation) });
  store.curation = step.curation;
  store._rev++;
  scheduleValidate();
  emit('doc');
  return step.label;
}

export const canUndo = () => store._undo.length > 0;
export const canRedo = () => store._redo.length > 0;

// ------------------------------------------------------------------ validation
// Validation and derived values belong to the server: one Python validator
// serves the loader and the editor, with no copy in the browser to fall out of
// step with it (PLAN.md, "validation"). A burst of edits (typing through a
// form) makes one call, and an answer that arrives after a newer edit is
// dropped, so what is shown always describes the latest curation.

const VALIDATE_DELAY = 300;
let validateTimer = 0;
let validateSeq = 0;
let inflight = null;      // { seq, ctl, promise }

function scheduleValidate(delay = VALIDATE_DELAY) {
  if (store.readOnly) return;
  store.checking = true;
  clearTimeout(validateTimer);
  validateTimer = setTimeout(runValidate, delay);
}

function runValidate() {
  validateTimer = 0;
  const seq = ++validateSeq;
  inflight?.ctl.abort();
  const ctl = new AbortController();
  const promise = api.validate(store.curation, ctl.signal).then(res => {
    if (seq !== validateSeq) return;
    store.validation = res.validation;
    store.derived = res.derived;
    store.checkError = null;
  }).catch(err => {
    if (seq !== validateSeq || err.name === 'AbortError') return;
    // A 422 here means the curation does not even parse (a blank name that got
    // past the inspector). Keep the last good derived values and say so.
    store.checkError = err.body?.detail
      ? detailText(err.body.detail)
      : err.message;
  }).finally(() => {
    if (seq !== validateSeq) return;
    store.checking = false;
    inflight = null;
    emit('derived');
  });
  inflight = { seq, ctl, promise };
  return promise;
}

/** Run any pending validation now and wait for it: the save sheet must not
 *  show errors for a curation that has since changed. */
export async function validateNow() {
  if (store.readOnly) return;
  if (validateTimer) { clearTimeout(validateTimer); return runValidate(); }
  if (inflight) return inflight.promise;
}

/** FastAPI's 422 `detail` list, as one line. */
export function detailText(detail) {
  if (!Array.isArray(detail)) return String(detail);
  return detail.map(d => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
}

// ------------------------------------------------------------------ ids
/** "SF:16992" -> "16992": what is printed on the pole and what a person reads.
 *  The full id stays in the data. */
export const upstream = id => {
  const s = String(id ?? '');
  const i = s.indexOf(':');
  return i < 0 ? s : s.slice(i + 1);
};

// ------------------------------------------------------------------ lookups
export const stations = () => store.curation?.stations.stations || {};
export const stationIds = () => Object.keys(stations());
export const stationById = id => (id ? stations()[id] || null : null);
/** A station's platforms: always one flat list. Null-safe, for ids that point
 *  at a station which no longer exists. */
export const platformsOf = st => st?.platforms || [];

export const derivedStation = id => store.derived.stations[id] || null;
export const derivedPlatform = id => store.derived.platforms[id] || null;
export const lineById = id => store.derived.lines[id] || null;
export const allLines = () => Object.values(store.derived.lines);
export const lineOverride = id => store.curation?.lines?.[id] || null;
export const snapshotLine = id => {
  for (const snap of Object.values(store.snapshots)) {
    const l = snap.lines?.[id];
    if (l) return l;
  }
  return null;
};

/** The lines serving a station, in the server's line order. */
export function linesOf(stationId) {
  return (derivedStation(stationId)?.lines || []).map(lineById).filter(Boolean);
}

export function colorOf(lineId) {
  return lineById(lineId)?.color || '#6ea8fe';
}

const isNum = v => typeof v === 'number' && Number.isFinite(v);

/** `[lng, lat]` for MapLibre, or null for a station with no live platform,
 *  which is then left off the map rather than drawn somewhere it is not. */
export function stationPos(id) {
  const d = derivedStation(id);
  return d && isNum(d.lat) && isNum(d.lon) ? [d.lon, d.lat] : null;
}

export function platformPos(id) {
  const d = derivedPlatform(id);
  return d && isNum(d.lat) && isNum(d.lon) ? [d.lon, d.lat] : null;
}

/** The station that owns each platform. The server's rule for a platform two
 *  stations claim (invalid, but possible mid-edit) is that the first station by
 *  id wins, and this follows it so the strip agrees with the directions. */
let ownerCache = { rev: -1, map: new Map() };
export function ownerOf(pid) {
  if (ownerCache.rev !== store._rev) {
    const map = new Map();
    for (const sid of stationIds().sort()) {
      for (const p of platformsOf(stations()[sid])) if (!map.has(p.id)) map.set(p.id, sid);
    }
    ownerCache = { rev: store._rev, map };
  }
  return ownerCache.map.get(pid) || null;
}

export function platformByCode(pid) {
  const sid = ownerOf(pid);
  if (!sid) return null;
  const st = stationById(sid);
  return { sid, station: st, platform: platformsOf(st).find(p => p.id === pid) };
}

/**
 * Snapshot stops no station has claimed, nearest first: the only things a
 * platform can be added from. Ignored stops are left out, because assigning
 * one is a validation error; un-ignoring is the review queue's job.
 */
export function unclaimedNear(at, limit = Infinity) {
  const ignored = store.curation.ignored || {};
  const out = [];
  for (const [pid, p] of Object.entries(store.derived.platforms)) {
    if (!p.live || ownerOf(pid) || ignored[pid] || !isNum(p.lat)) continue;
    out.push({ id: pid, p, metres: at ? metresBetween(at, [p.lon, p.lat]) : 0 });
  }
  out.sort((a, b) => a.metres - b.metres || a.id.localeCompare(b.id));
  return out.slice(0, limit);
}

// ------------------------------------------------------------------ filters
/** Modes present in the data, in line order, with the four SF modes always
 *  listed so the chips do not move around between datasets. */
export function knownModes() {
  const seen = new Set(MODES);
  for (const l of allLines()) seen.add(l.mode);
  return [...seen];
}

export const modeOn = m => !store.modesOff.has(m);
export const filteringModes = () => store.modesOff.size > 0;

/** A station matches the mode chips if any of its modes is on. A station with
 *  no modes (no live platform, or no line serving one) only shows when no mode
 *  is filtered out, because it belongs to none of them. */
export function stationMatches(id) {
  const st = stationById(id);
  if (!st) return false;
  if (store.unverifiedOnly && st.verified) return false;
  if (!filteringModes()) return true;
  return (derivedStation(id)?.modes || []).some(modeOn);
}

export function platformMatches(pid) {
  if (!filteringModes()) return true;
  return (derivedPlatform(pid)?.lines || []).some(l => modeOn(lineById(l)?.mode));
}

export const lineMatches = id => modeOn(lineById(id)?.mode);

export function setModeOn(mode, on) {
  if (on) store.modesOff.delete(mode); else store.modesOff.add(mode);
  emit('filter');
}

export function setUnverifiedOnly(on) {
  store.unverifiedOnly = on;
  emit('filter');
}

// ------------------------------------------------------------------ verification
/** Today in the browser's time zone, as `YYYY-MM-DD`. Local, not UTC: someone
 *  verifying at 6 pm in San Francisco means today, not tomorrow. */
export function today() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function verifiedCount() {
  let n = 0, of = 0;
  for (const st of Object.values(stations())) { of++; if (st.verified) n++; }
  return { n, of };
}

/** The nearest unverified station to `at` that the active filters show (and,
 *  with a line focused, that is on that line), skipping `except`. */
export function nextUnverified(at, except = null) {
  const onLine = store.activeLine ? new Set(derivedStationsOnLine(store.activeLine)) : null;
  let best = null, bestD = Infinity;
  for (const [sid, st] of Object.entries(stations())) {
    if (st.verified || sid === except) continue;
    if (!stationMatches(sid)) continue;
    if (onLine && !onLine.has(sid)) continue;
    const pos = stationPos(sid);
    if (!pos) continue;
    const d = metresBetween(at, pos);
    if (d < bestD) { best = sid; bestD = d; }
  }
  return best;
}

export function derivedStationsOnLine(lineId) {
  const out = [];
  for (const d of lineById(lineId)?.directions || []) out.push(...d.stations);
  return [...new Set(out)];
}

// ------------------------------------------------------------------ selection
export function select(stationId, platformId = null) {
  store.selStation = stationId;
  store.selPlatform = platformId;
  if (stationId) store.lineInspector = false;
  emit('selection');
}

export function setActiveLine(id) {
  if (store.activeLine !== id) store.activeDir = 0;
  store.activeLine = id;
  if (!id) store.lineInspector = false;
  emit('line');
}

export function toggleLayer(k) {
  store.layers[k] = !store.layers[k];
  emit('layers');
}

// ------------------------------------------------------------------ diffing
/** Canonical JSON: keys sorted, and "not stated" (null, false, []) dropped, so
 *  a field cleared in the editor compares equal to one never written. */
export function canon(v) {
  return JSON.stringify(v, (k, x) => {
    if (x && typeof x === 'object' && !Array.isArray(x)) {
      const o = {};
      for (const key of Object.keys(x).sort()) {
        const y = x[key];
        if (y === null || y === undefined || y === false || (Array.isArray(y) && !y.length)) continue;
        o[key] = y;
      }
      return o;
    }
    return x;
  });
}

export const isDirty = () => !!store.base && canon(store.base) !== canon(store.curation);

/** "Not stated" in any of its spellings: absent, null, false or empty. */
const blank = v => (v === undefined || v === null || v === false || (Array.isArray(v) && !v.length) ? null : v);
const same = (a, b) => canon(blank(a)) === canon(blank(b));
const code = s => `<code>${esc(s)}</code>`;
const list = xs => (xs || []).length ? xs.map(code).join(' ') : code('none');

/**
 * The change list between `base` and `curation`, in words. Drives the save
 * sheet and the unsaved-change count. Each entry is `{k, t, d}`: kind (add,
 * edit, del), a title (whose part before " · " names the thing changed) and an
 * HTML description.
 */
export function changes() {
  const out = [];
  if (!store.base || !store.curation) return out;
  const A = store.base.stations.stations, B = store.curation.stations.stations;
  const push = (k, t, d) => out.push({ k, t, d });

  for (const [id, b] of Object.entries(B)) {
    const a = A[id];
    const T = b.name || id;
    if (!a) {
      push('add', T, `New station ${code(id)} with ${list(platformsOf(b).map(p => upstream(p.id)))}`);
      continue;
    }
    if (a.name !== b.name) push('edit', T, `Renamed from ${code(a.name)}`);

    const ap = new Map(platformsOf(a).map(p => [p.id, p]));
    const bp = new Map(platformsOf(b).map(p => [p.id, p]));
    for (const [pid, p] of bp) {
      const q = ap.get(pid);
      const P = `${T} · ${upstream(pid)}`;
      if (!q) { push('add', T, `Added platform ${code(upstream(pid))} (${esc(p.heading)})`); continue; }
      if (q.heading !== p.heading) push('edit', P, `Heading ${code(q.heading)} → ${code(p.heading)}`);
      if (!same(q.name, p.name)) push('edit', P, `Signage → ${code(p.name || 'none')}`);
      if (!same(q.note, p.note)) push('edit', P, p.note ? `Note → ${code(p.note)}` : 'Note removed');
    }
    for (const pid of ap.keys()) if (!bp.has(pid)) push('del', T, `Removed platform ${code(upstream(pid))}`);

    if (!same(a.note, b.note)) push('edit', T, b.note ? `Note → ${code(b.note)}` : 'Note removed');
    if (!same(a.verified, b.verified)) {
      push('edit', T, b.verified ? `Verified ${code(b.verified)}` : `No longer verified (was ${code(a.verified)})`);
    }

    const tkey = ts => (ts || []).map(t => `${t.to}:${t.mode}:${t.note || ''}`).sort().join(',');
    if (tkey(a.transfers) !== tkey(b.transfers)) {
      push('edit', T, `Transfers → ${list((b.transfers || []).map(t => `${t.to} (${t.mode})`))}`);
    }
    if (!same([...(a.transferAgencies || [])].sort(), [...(b.transferAgencies || [])].sort())) {
      push('edit', T, `Transfer agencies → ${list(b.transferAgencies)}`);
    }
    if (!same(a.formerIds, b.formerIds)) push('edit', T, `Former ids → ${list(b.formerIds)}`);
  }
  for (const [id, a] of Object.entries(A)) if (!B[id]) push('del', a.name, `Deleted station ${code(id)}`);

  const SA = store.base.stations.subways || {}, SB = store.curation.stations.subways || {};
  for (const [id, b] of Object.entries(SB)) {
    const a = SA[id];
    if (!a) { push('add', b.name, `New subway ${code(id)}`); continue; }
    if (a.name !== b.name) push('edit', b.name, `Subway renamed from ${code(a.name)}`);
    if (!same(a.stations, b.stations)) push('edit', b.name, `Subway stations → ${list(b.stations)}`);
  }
  for (const [id, a] of Object.entries(SA)) if (!SB[id]) push('del', a.name, `Deleted subway ${code(id)}`);

  const LA = store.base.lines || {}, LB = store.curation.lines || {};
  for (const id of new Set([...Object.keys(LA), ...Object.keys(LB)])) {
    const a = LA[id] || {}, b = LB[id] || {};
    const T = lineById(id)?.name || id;
    for (const f of LINE_FIELDS) {
      if (same(a[f], b[f])) continue;
      const v = b[f];
      const shown = f === 'hidden' ? (v ? 'hidden' : 'shown')
        : f === 'replaces' ? (v || []).join(' ') || 'nothing'
        : v ?? '511 default';
      push('edit', `${T} · line`, `${LINE_LABEL[f]} → ${code(shown)}`);
    }
  }

  const IA = store.base.ignored || {}, IB = store.curation.ignored || {};
  for (const [pid, b] of Object.entries(IB)) {
    const a = IA[pid];
    if (!a) push('add', `Ignored · ${upstream(pid)}`, `Ignored: ${esc(b.note)}`);
    else if (a.note !== b.note) push('edit', `Ignored · ${upstream(pid)}`, `Note → ${code(b.note)}`);
  }
  for (const pid of Object.keys(IA)) if (!IB[pid]) push('del', `Ignored · ${upstream(pid)}`, 'No longer ignored');

  return out;
}

/** The fields of a line override, in the order the inspector shows them. */
export const LINE_FIELDS = ['name', 'color', 'textColor', 'mode', 'hidden', 'replaces', 'note'];
const LINE_LABEL = {
  name: 'Name', color: 'Colour', textColor: 'Text colour', mode: 'Mode',
  hidden: 'Visibility', replaces: 'Replaces', note: 'Note',
};

/**
 * Set one field of a line's override, or remove it when `value` is empty.
 * lines.json holds overrides only, so a cleared field loses its key and an
 * override with nothing left in it loses its entry.
 */
export function setLineOverride(curation, lineId, field, value) {
  const all = curation.lines || (curation.lines = {});
  const o = all[lineId] || {};
  const empty = value === null || value === undefined || value === '' || value === false
    || (Array.isArray(value) && !value.length);
  if (empty) delete o[field]; else o[field] = value;
  for (const [k, v] of Object.entries(o)) {
    if (v === null || v === false || (Array.isArray(v) && !v.length)) delete o[k];
  }
  if (Object.keys(o).length) all[lineId] = o; else delete all[lineId];
}

// ------------------------------------------------------------------ geometry
const M_LAT = 110540, M_LON = 111320 * Math.cos(37.76 * Math.PI / 180);
/** Metres between two `[lng, lat]` points, flat-earth: fine across a city. */
export function metresBetween(a, b) {
  return Math.round(Math.hypot((a[0] - b[0]) * M_LON, (a[1] - b[1]) * M_LAT));
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
