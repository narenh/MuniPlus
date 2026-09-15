// Single source of truth. Every mutation goes through edit(), which snapshots
// for undo and notifies subscribers. The document is kept as plain JSON so that
// what we POST back is exactly what the server writes.

const clone = o => JSON.parse(JSON.stringify(o));

export const store = {
  doc: null,
  base: null,           // the document as last read from disk - the diff baseline
  meta: {},
  stats: {},
  drift: null,          // code -> drift record
  driftAt: null,

  activeLine: '',       // '' = all lines
  selStation: null,     // station id
  selPlatform: null,    // stop code
  validation: { errors: [], warnings: [] },

  layers: { platforms: true, labels: true, transfers: true, drift: false },

  _undo: [], _redo: [], _subs: new Set(), _max: 120,
};

export function subscribe(fn) { store._subs.add(fn); return () => store._subs.delete(fn); }

export function emit(what = 'all') {
  for (const fn of store._subs) fn(what);
}

export function load({ doc, meta, stats, validation }) {
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
  const before = clone(store.doc);
  mutate(store.doc);
  store._undo.push({ label, doc: before });
  if (store._undo.length > store._max) store._undo.shift();
  store._redo.length = 0;
  revalidate();
  emit('doc');
}

export function undo() {
  const step = store._undo.pop();
  if (!step) return null;
  store._redo.push({ label: step.label, doc: clone(store.doc) });
  store.doc = step.doc;
  revalidate();
  emit('doc');
  return step.label;
}

export function redo() {
  const step = store._redo.pop();
  if (!step) return null;
  store._undo.push({ label: step.label, doc: clone(store.doc) });
  store.doc = step.doc;
  revalidate();
  emit('doc');
  return step.label;
}

export const canUndo = () => store._undo.length > 0;
export const canRedo = () => store._redo.length > 0;

// ------------------------------------------------------------------ lookups
export const stationById = id => store.doc?.stations.find(s => s.id === id) || null;
export const lineById = id => store.doc?.lines.find(l => l.id === id) || null;

export function platformByCode(code) {
  for (const st of store.doc.stations) {
    const p = st.platforms.find(x => String(x.id) === String(code));
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

// ------------------------------------------------------------------ validation
// Mirrors editor/lib/data.js closely enough to give instant feedback; the
// server re-validates before writing, and the server is the authority.
export function revalidate() {
  const doc = store.doc;
  const errors = [], warnings = [];
  const E = (path, msg) => errors.push({ path, msg });
  const W = (path, msg) => warnings.push({ path, msg });

  const ids = new Set(), owner = new Map();
  for (const st of doc.stations) {
    const at = `station ${st.id || '(no id)'}`;
    if (!st.id) { E(at, 'station has no id'); continue; }
    if (ids.has(st.id)) E(at, `duplicate station id "${st.id}"`);
    ids.add(st.id);
    if (!st.name?.trim()) E(at, 'station has an empty name');
    if (!st.platforms?.length) E(at, 'station has no platforms');
    const seen = new Set();
    for (const p of st.platforms || []) {
      const pat = `${at} · ${p.id || '(no code)'}`;
      if (!/^\d{5}$/.test(String(p.id || ''))) E(pat, 'stop code must be 5 digits');
      if (seen.has(p.id)) E(pat, `stop code "${p.id}" is listed twice here`);
      seen.add(p.id);
      if (owner.has(p.id) && owner.get(p.id) !== st.id) {
        E(pat, `stop code "${p.id}" is already used by "${owner.get(p.id)}"`);
      }
      owner.set(p.id, st.id);
      if (!Number.isFinite(p.latitude) || !Number.isFinite(p.longitude)) {
        E(pat, 'platform is missing coordinates');
      }
    }
  }
  for (const st of doc.stations) {
    for (const t of st.transferStations || []) {
      if (!ids.has(t)) E(`station ${st.id}`, `transfer points at unknown station "${t}"`);
    }
  }
  for (const ln of doc.lines) {
    for (const sid of ln.stationIds || []) {
      if (!ids.has(sid)) E(`line ${ln.id}`, `references unknown station "${sid}"`);
      else {
        const st = doc.stations.find(s => s.id === sid);
        if (st && !(st.lines || []).includes(ln.id)) {
          W(`station ${sid}`, `is on ${ln.id} but does not list ${ln.id} in its own lines`);
        }
      }
    }
    if ((ln.stationIds || []).length < 2) E(`line ${ln.id}`, 'line needs at least two stations');
  }
  for (const sub of doc.subways || []) {
    for (const sid of sub.stationIds || []) {
      if (!ids.has(sid)) E(`subway ${sub.id}`, `references unknown station "${sid}"`);
    }
  }
  store.validation = { errors, warnings };
  return store.validation;
}

// ------------------------------------------------------------------ diffing
const round6 = n => Math.round(n * 1e6) / 1e6;

/** Human-readable change list between `base` and `doc`. Drives the save sheet. */
export function changes() {
  const out = [];
  if (!store.base || !store.doc) return out;

  const A = new Map(store.base.stations.map(s => [s.id, s]));
  const B = new Map(store.doc.stations.map(s => [s.id, s]));

  for (const [id, b] of B) {
    const a = A.get(id);
    if (!a) { out.push({ k: 'add', t: b.name, d: `New station <code>${id}</code> with ${b.platforms.length} platform(s)` }); continue; }

    if (a.name !== b.name) out.push({ k: 'edit', t: b.name, d: `Renamed from <code>${esc(a.name)}</code>` });
    if (a.kind !== b.kind) out.push({ k: 'edit', t: b.name, d: `Kind <code>${a.kind}</code> → <code>${b.kind}</code>` });
    if (round6(a.latitude) !== round6(b.latitude) || round6(a.longitude) !== round6(b.longitude)) {
      out.push({ k: 'move', t: b.name, d: `Station moved ${dist(a, b)} m` });
    }
    const at = (a.transferStations || []).join(), bt = (b.transferStations || []).join();
    if (at !== bt) out.push({ k: 'edit', t: b.name, d: `Transfers now <code>${bt || 'none'}</code>` });
    const al = (a.lines || []).join(), bl = (b.lines || []).join();
    if (al !== bl) out.push({ k: 'edit', t: b.name, d: `Lines <code>${al || 'none'}</code> → <code>${bl || 'none'}</code>` });

    const ap = new Map(a.platforms.map(p => [String(p.id), p]));
    const bp = new Map(b.platforms.map(p => [String(p.id), p]));
    for (const [code, p] of bp) {
      const q = ap.get(code);
      if (!q) { out.push({ k: 'add', t: b.name, d: `Added platform <code>${code}</code> (${p.heading})` }); continue; }
      if (round6(q.latitude) !== round6(p.latitude) || round6(q.longitude) !== round6(p.longitude)) {
        out.push({ k: 'move', t: `${b.name} · ${code}`, d: `Pole moved ${dist(q, p)} m to <code>${p.latitude}, ${p.longitude}</code>` });
      }
      if (q.heading !== p.heading) out.push({ k: 'edit', t: `${b.name} · ${code}`, d: `Heading <code>${q.heading}</code> → <code>${p.heading}</code>` });
      if ((q.name ?? null) !== (p.name ?? null)) out.push({ k: 'edit', t: `${b.name} · ${code}`, d: `Label → <code>${esc(p.name ?? 'null')}</code>` });
      if ((q.stopName ?? '') !== (p.stopName ?? '')) out.push({ k: 'edit', t: `${b.name} · ${code}`, d: `Stop name → <code>${esc(p.stopName ?? '')}</code>` });
    }
    for (const code of ap.keys()) if (!bp.has(code)) out.push({ k: 'del', t: b.name, d: `Removed platform <code>${code}</code>` });
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
