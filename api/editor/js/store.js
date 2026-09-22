// Single source of truth. Every mutation goes through edit(), which snapshots
// for undo and notifies subscribers. The document is kept as plain JSON so that
// what we POST back is exactly what the server writes.

const clone = o => JSON.parse(JSON.stringify(o));

export const store = {
  doc: null,
  base: null,           // the document as last read from disk - the diff baseline
  meta: {},
  stats: {},

  activeLine: '',       // '' = all lines
  selStation: null,     // station id
  selPlatform: null,    // stop code
  validation: { errors: [], warnings: [] },   // as the server last reported it

  layers: { platforms: true, labels: true, transfers: true },

  readOnly: false,
  readOnlyReason: null,
  onBlocked: null,          // set by main.js so a blocked edit can explain itself

  _undo: [], _redo: [], _subs: new Set(), _max: 120,
};

export function subscribe(fn) { store._subs.add(fn); return () => store._subs.delete(fn); }

export function emit(what = 'all') {
  for (const fn of store._subs) fn(what);
}

export function load({ doc, meta, stats, validation }) {
  store.readOnly = !!meta?.readOnly;
  store.readOnlyReason = meta?.readOnlyReason || null;
  applyDerived(doc);
  store.doc = doc;
  store.base = clone(doc);
  store.meta = meta || {};
  store.stats = stats || {};
  store.validation = validation || { errors: [], warnings: [] };
  store._undo = []; store._redo = [];
  emit('all');
}

/**
 * Apply a mutation. `label` is what shows up in the change list.
 * The mutator receives the live document and edits it in place.
 */
export function edit(label, mutate) {
  if (store.readOnly) { store.onBlocked?.(); return false; }
  const before = clone(store.doc);
  mutate(store.doc);
  applyDerived(store.doc);
  store._undo.push({ label, doc: before });
  if (store._undo.length > store._max) store._undo.shift();
  store._redo.length = 0;
  validate();
  emit('doc');
  return true;
}

export function undo() {
  if (store.readOnly) return null;
  const step = store._undo.pop();
  if (!step) return null;
  store._redo.push({ label: step.label, doc: clone(store.doc) });
  store.doc = step.doc;
  validate();
  emit('doc');
  return step.label;
}

export function redo() {
  if (store.readOnly) return null;
  const step = store._redo.pop();
  if (!step) return null;
  store._undo.push({ label: step.label, doc: clone(store.doc) });
  store.doc = step.doc;
  validate();
  emit('doc');
  return step.label;
}

export const canUndo = () => store._undo.length > 0;
export const canRedo = () => store._redo.length > 0;

// ------------------------------------------------------------------ validation
/**
 * Runs after every change to the document. Validation belongs to the server:
 * one Python validator serves the loader and the editor, with no copy in the
 * browser to fall out of step with it (api/PLAN.md, "validation"). Until the
 * call below exists, `store.validation` keeps describing the document as
 * load() received it.
 *
 * SERVER SEAM: POST the curation to `api/validate` here (ValidateRequest ->
 * ValidateResponse in api/app/models/editor.py), store the returned
 * `validation` (and `derived`), then emit('doc') so the chrome redraws.
 */
function validate() {}

// ------------------------------------------------------------------ shape
/** A station's platforms: always one flat list. Null-safe, for ids that point
 *  at a station which no longer exists. */
export const platformsOf = st => st?.platforms || [];

const isNum = v => typeof v === 'number' && Number.isFinite(v);

/** Station coordinates are derived and never edited: the centroid of its
 *  platforms that have one. */
export function derivedCoord(st) {
  const pts = platformsOf(st).filter(p => isNum(p.latitude) && isNum(p.longitude));
  if (!pts.length) return { latitude: null, longitude: null };
  const r = n => Math.round(n * 1e6) / 1e6;
  return {
    latitude: r(pts.reduce((n, p) => n + p.latitude, 0) / pts.length),
    longitude: r(pts.reduce((n, p) => n + p.longitude, 0) / pts.length),
  };
}

export const hasCoord = st => isNum(st?.latitude) && isNum(st?.longitude);

/** `[lng, lat]` for MapLibre, or null for a station with no coordinate, which
 *  is then left off the map rather than drawn somewhere it is not. */
export const positionOf = st => (hasCoord(st) ? [st.longitude, st.latitude] : null);

/** Rewrite every derived field in place. Run after each edit. */
export function applyDerived(doc) {
  const onLine = new Map();
  for (const l of doc.lines) for (const sid of l.stationIds || []) {
    if (!onLine.has(sid)) onLine.set(sid, new Set());
    onLine.get(sid).add(l.id);
  }
  for (const st of doc.stations) {
    const c = derivedCoord(st);
    st.latitude = c.latitude;
    st.longitude = c.longitude;
    const u = new Set();
    for (const p of platformsOf(st)) for (const l of p.lines || []) u.add(l);
    for (const l of onLine.get(st.id) || []) u.add(l);
    st.lines = [...u].sort();
  }
}

// ------------------------------------------------------------------ lookups
export const stationById = id => store.doc?.stations.find(s => s.id === id) || null;
export const lineById = id => store.doc?.lines.find(l => l.id === id) || null;

export function platformByCode(code) {
  for (const st of store.doc.stations) {
    const p = platformsOf(st).find(x => String(x.id) === String(code));
    if (p) return { station: st, platform: p };
  }
  return null;
}

export function linesOf(stationId) {
  return store.doc.lines.filter(l => l.stationIds.includes(stationId));
}

export function colorOf(lineId) {
  return lineById(lineId)?.color || '#6ea8fe';
}

// ------------------------------------------------------------------ selection
export function select(stationId, platformCode = null) {
  store.selStation = stationId;
  store.selPlatform = platformCode;
  emit('selection');
}

export function setActiveLine(id) {
  store.activeLine = id;
  emit('line');
}

export function toggleLayer(k) {
  store.layers[k] = !store.layers[k];
  emit('layers');
}

// ------------------------------------------------------------------ diffing
/** Human-readable change list between `base` and `doc`. Drives the save sheet. */
export function changes() {
  const out = [];
  if (!store.base || !store.doc) return out;
  const A = new Map(store.base.stations.map(s => [s.id, s]));
  const B = new Map(store.doc.stations.map(s => [s.id, s]));

  for (const [id, b] of B) {
    const a = A.get(id);
    const T = b.name;
    if (!a) { out.push({ k: 'add', t: T, d: `New station <code>${id}</code>` }); continue; }

    if (a.name !== b.name) out.push({ k: 'edit', t: T, d: `Renamed from <code>${esc(a.name)}</code>` });

    // platforms
    const ap = new Map(platformsOf(a).map(p => [String(p.id), p]));
    const bp = new Map(platformsOf(b).map(p => [String(p.id), p]));
    for (const [code, p] of bp) {
      const q = ap.get(code);
      if (!q) { out.push({ k: 'add', t: T, d: `Added platform <code>${code}</code> (${p.heading})` }); continue; }
      if (q.heading !== p.heading) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Heading <code>${q.heading}</code> → <code>${p.heading}</code>` });
      if ((q.name ?? null) !== (p.name ?? null)) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Label → <code>${esc(p.name ?? 'none')}</code>` });
    }
    for (const code of ap.keys()) if (!bp.has(code)) out.push({ k: 'del', t: T, d: `Removed platform <code>${code}</code>` });

    // transfers
    const key = ts => (ts || []).map(t => `${t.to}:${t.mode}`).sort().join(',');
    if (key(a.transfers) !== key(b.transfers)) {
      out.push({ k: 'edit', t: T, d: `Transfers → <code>${(b.transfers || []).map(t => `${t.to} (${t.mode})`).join(', ') || 'none'}</code>` });
    }
    if ((a.transferAgencies || []).join() !== (b.transferAgencies || []).join()) {
      out.push({ k: 'edit', t: T, d: `Agencies → <code>${(b.transferAgencies || []).join(' ') || 'none'}</code>` });
    }
  }
  for (const [id, a] of A) if (!B.has(id)) out.push({ k: 'del', t: a.name, d: `Deleted station <code>${id}</code>` });

  const LA = new Map(store.base.lines.map(l => [l.id, l]));
  for (const l of store.doc.lines) {
    const a = LA.get(l.id);
    if (!a) { out.push({ k: 'add', t: l.name, d: 'New line' }); continue; }
    if (a.stationIds.join() !== l.stationIds.join()) {
      const added = l.stationIds.filter(x => !a.stationIds.includes(x));
      const gone = a.stationIds.filter(x => !l.stationIds.includes(x));
      let d = 'Stop order changed';
      if (added.length) d = `Added ${added.map(x => `<code>${x}</code>`).join(', ')}`;
      if (gone.length) d = (added.length ? d + '; ' : '') + `Removed ${gone.map(x => `<code>${x}</code>`).join(', ')}`;
      out.push({ k: 'edit', t: l.name, d });
    }
    if (a.name !== l.name) out.push({ k: 'edit', t: l.name, d: `Renamed from <code>${esc(a.name)}</code>` });
    if (a.color !== l.color) out.push({ k: 'edit', t: l.name, d: `Colour <code>${a.color}</code> → <code>${l.color}</code>` });
    if (!!a.hidden !== !!l.hidden) out.push({ k: 'edit', t: l.name, d: l.hidden ? 'Hidden from the UI' : 'Shown in the UI' });
  }
  for (const sub of store.doc.subways || []) {
    const a = (store.base.subways || []).find(s => s.id === sub.id);
    if (a && a.stationIds.join() !== sub.stationIds.join()) {
      out.push({ k: 'edit', t: sub.name, d: 'Subway station list changed' });
    }
  }
  return out;
}

export const isDirty = () => JSON.stringify(store.base) !== JSON.stringify(store.doc);

const M_LAT = 110540, M_LON = 111320 * Math.cos(37.76 * Math.PI / 180);
function dist(a, b) {
  return Math.round(Math.hypot((a.longitude - b.longitude) * M_LON, (a.latitude - b.latitude) * M_LAT));
}
export { dist as metresBetween };

export function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
