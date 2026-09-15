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
  selExit: null,        // exit id, within selStation
  validation: { errors: [], warnings: [] },

  layers: { platforms: true, labels: true, transfers: true, drift: false },

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
  revalidate();
  emit('doc');
  return true;
}

export function undo() {
  if (store.readOnly) return null;
  const step = store._undo.pop();
  if (!step) return null;
  store._redo.push({ label: step.label, doc: clone(store.doc) });
  store.doc = step.doc;
  revalidate();
  emit('doc');
  return step.label;
}

export function redo() {
  if (store.readOnly) return null;
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

// ------------------------------------------------------------------ shape
/**
 * A station is one of two shapes, and the shape is its own discriminator: it
 * either has `levels` (and `exits`) or a flat `platforms` list. There is no
 * stored `kind`, because a stored one can contradict the shape - and
 * "underground" was already wrong for stations whose levels go up.
 */
export const hasLevels = st => Array.isArray(st?.levels);

/** Every platform of a station, whichever shape holds it. */
export function platformsOf(st) {
  if (!st) return [];
  return hasLevels(st) ? st.levels.flatMap(l => l.platforms || []) : (st.platforms || []);
}

/** The level a platform sits on, or null on the surface. */
export function levelOf(st, code) {
  if (!hasLevels(st)) return null;
  return st.levels.find(l => (l.platforms || []).some(p => String(p.id) === String(code))) || null;
}

const isNum = v => typeof v === 'number' && Number.isFinite(v);

/**
 * Station coordinates are derived and never edited: exits underground,
 * platforms on the surface. Underground platform coordinates never feed it -
 * they are below ground, and what a rider walks to is a door.
 */
export function derivedCoord(st) {
  const pts = hasLevels(st)
    ? (st.exits || []).filter(e => isNum(e.latitude) && isNum(e.longitude))
    : (st.platforms || []).filter(p => isNum(p.latitude) && isNum(p.longitude));
  if (!pts.length) return { latitude: null, longitude: null };
  const r = n => Math.round(n * 1e6) / 1e6;
  return {
    latitude: r(pts.reduce((n, p) => n + p.latitude, 0) / pts.length),
    longitude: r(pts.reduce((n, p) => n + p.longitude, 0) / pts.length),
  };
}

/** Where to point the camera: the real coordinate, or the platforms if a
 *  station has no exits yet. Never stored - this is for the editor only. */
export function anchorOf(st) {
  if (isNum(st?.latitude)) return [st.longitude, st.latitude];
  const ps = platformsOf(st).filter(p => isNum(p.latitude));
  if (!ps.length) return null;
  return [ps.reduce((n, p) => n + p.longitude, 0) / ps.length,
          ps.reduce((n, p) => n + p.latitude, 0) / ps.length];
}

export const hasCoord = st => isNum(st?.latitude) && isNum(st?.longitude);

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
export function select(stationId, platformCode = null, exitId = null) {
  store.selStation = stationId;
  store.selPlatform = platformCode;
  store.selExit = exitId;
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
  const lineIds = new Set(doc.lines.map(l => l.id));

  for (const st of doc.stations) {
    const at = `station ${st.id || '(no id)'}`;
    if (!st.id) { E(at, 'station has no id'); continue; }
    if (ids.has(st.id)) E(at, `duplicate station id "${st.id}"`);
    ids.add(st.id);
    if (!st.name?.trim()) E(at, 'station has an empty name');

    if (st.kind !== undefined) E(at, 'kind is derived from the shape and must not be stored');
    if (hasLevels(st) && st.platforms !== undefined) E(at, 'a station has levels or platforms, never both');

    if (hasLevels(st)) {
      if (!st.levels.length) E(at, 'a levelled station has no levels');
      const depths = new Set();
      for (const lv of st.levels || []) {
        const la = `${at} · level ${lv.id}`;
        // signed ordinal: 0 street, negative below, positive above (elevated)
        if (!Number.isInteger(lv.id)) E(la, 'level id must be an integer depth');
        if (depths.has(lv.id)) E(la, `two levels share depth ${lv.id}`);
        depths.add(lv.id);
        if (!lv.name?.trim()) E(la, 'level has an empty name');
      }
      for (const ex of st.exits || []) {
        const ea = `${at} · exit ${ex.id || '(no id)'}`;
        if (!ex.name?.trim()) E(ea, 'exit has an empty name');
        if (!depths.has(ex.level)) E(ea, `exit lands on level ${ex.level}, which does not exist here`);
        if (!ex.stairs && !ex.escalator && !ex.elevator) W(ea, 'no stairs, escalator or elevator');
      }
      if (!(st.exits || []).length) W(at, 'no exits yet, so this station has no coordinate');
    } else {
      if (!st.platforms?.length) E(at, 'station has no platforms');
    }

    const seen = new Set();
    for (const p of platformsOf(st)) {
      const pa = `${at} · ${p.id || '(no code)'}`;
      if (!/^\d{5}$/.test(String(p.id || ''))) E(pa, 'stop code must be 5 digits');
      if (seen.has(p.id)) E(pa, `stop code "${p.id}" is listed twice here`);
      seen.add(p.id);
      if (owner.has(p.id) && owner.get(p.id) !== st.id) {
        E(pa, `stop code "${p.id}" is already used by "${owner.get(p.id)}"`);
      }
      owner.set(p.id, st.id);
      if (!Number.isFinite(p.latitude) || !Number.isFinite(p.longitude)) {
        E(pa, 'platform is missing coordinates');
      }
      if (!p.lines?.length) E(pa, 'platform serves no lines');
      for (const l of p.lines || []) if (!lineIds.has(l)) E(pa, `unknown line "${l}"`);
      for (const l of p.terminates || []) {
        if (!(p.lines || []).includes(l)) E(pa, `terminates lists "${l}", which does not stop here`);
      }
    }
  }

  for (const st of doc.stations) {
    for (const t of st.transfers || []) {
      const ta = `station ${st.id}`;
      if (!ids.has(t.to)) E(ta, `transfer points at unknown station "${t.to}"`);
      if (!['indoor', 'street'].includes(t.mode)) E(ta, 'transfer mode must be indoor or street');
      if (t.mode === 'indoor') {
        const other = doc.stations.find(x => x.id === t.to);
        const back = (other?.transfers || []).find(x => x.to === st.id && x.mode === 'indoor');
        if (!back) E(ta, `indoor transfer to "${t.to}" is not reciprocated`);
        if (t.via !== undefined && !(st.levels || []).some(l => l.id === t.via)) {
          E(ta, `transfer via level ${t.via}, which does not exist here`);
        }
      }
    }
  }

  for (const ln of doc.lines) {
    if ((ln.stationIds || []).length < 2) E(`line ${ln.id}`, 'line needs at least two stations');
    for (const sid of ln.stationIds || []) {
      if (!ids.has(sid)) E(`line ${ln.id}`, `references unknown station "${sid}"`);
      else if (!platformsOf(doc.stations.find(s => s.id === sid)).some(p => (p.lines || []).includes(ln.id))) {
        W(`station ${sid}`, `is on ${ln.id} but no platform there serves it`);
      }
    }
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
const round6 = n => (n === null || n === undefined ? null : Math.round(n * 1e6) / 1e6);

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
    if (hasLevels(a) !== hasLevels(b)) {
      out.push({ k: 'edit', t: T, d: hasLevels(b) ? 'Converted to levels and exits' : 'Converted to street platforms' });
    }

    // levels
    const AL = new Map((a.levels || []).map(l => [l.id, l]));
    const BL = new Map((b.levels || []).map(l => [l.id, l]));
    for (const [d_, l] of BL) {
      const q = AL.get(d_);
      if (!q) { out.push({ k: 'add', t: T, d: `Level <code>${d_}</code> “${esc(l.name)}”` }); continue; }
      if (q.name !== l.name) out.push({ k: 'edit', t: T, d: `Level <code>${d_}</code> renamed to “${esc(l.name)}”` });
      if ((q.agency || null) !== (l.agency || null)) out.push({ k: 'edit', t: T, d: `Level <code>${d_}</code> agency → <code>${esc(l.agency || 'none')}</code>` });
      if (q.isIsland !== l.isIsland) out.push({ k: 'edit', t: T, d: `Level <code>${d_}</code> isIsland → <code>${l.isIsland === null ? 'unset' : l.isIsland}</code>` });
    }
    for (const d_ of AL.keys()) if (!BL.has(d_)) out.push({ k: 'del', t: T, d: `Removed level <code>${d_}</code>` });

    // exits
    const AE = new Map((a.exits || []).map(e => [e.id, e]));
    const BE = new Map((b.exits || []).map(e => [e.id, e]));
    for (const [eid, e] of BE) {
      const q = AE.get(eid);
      if (!q) { out.push({ k: 'add', t: T, d: `Exit “${esc(e.name)}”` }); continue; }
      if (q.name !== e.name) out.push({ k: 'edit', t: T, d: `Exit renamed to “${esc(e.name)}”` });
      if (round6(q.latitude) !== round6(e.latitude) || round6(q.longitude) !== round6(e.longitude)) {
        out.push({ k: 'move', t: `${T} · ${esc(e.name)}`,
          d: e.latitude === null ? 'Exit coordinate cleared'
             : `Exit moved to <code>${e.latitude}, ${e.longitude}</code>` });
      }
      if (q.level !== e.level) out.push({ k: 'edit', t: T, d: `Exit “${esc(e.name)}” now lands on level <code>${e.level}</code>` });
      if (!!q.closed !== !!e.closed) out.push({ k: 'edit', t: T, d: `Exit “${esc(e.name)}” ${e.closed ? 'marked closed' : 'reopened'}` });
      for (const f of ['stairs', 'escalator', 'elevator']) {
        if (!!q[f] !== !!e[f]) out.push({ k: 'edit', t: T, d: `Exit “${esc(e.name)}” ${f} → <code>${!!e[f]}</code>` });
      }
    }
    for (const [eid, e] of AE) if (!BE.has(eid)) out.push({ k: 'del', t: T, d: `Removed exit “${esc(e.name)}”` });

    // platforms
    const ap = new Map(platformsOf(a).map(p => [String(p.id), p]));
    const bp = new Map(platformsOf(b).map(p => [String(p.id), p]));
    for (const [code, p] of bp) {
      const q = ap.get(code);
      if (!q) { out.push({ k: 'add', t: T, d: `Added platform <code>${code}</code> (${p.heading})` }); continue; }
      if (round6(q.latitude) !== round6(p.latitude) || round6(q.longitude) !== round6(p.longitude)) {
        out.push({ k: 'move', t: `${T} · ${code}`, d: `Pole moved ${dist(q, p)} m to <code>${p.latitude}, ${p.longitude}</code>` });
      }
      if (q.heading !== p.heading) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Heading <code>${q.heading}</code> → <code>${p.heading}</code>` });
      if ((q.name ?? null) !== (p.name ?? null)) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Label → <code>${esc(p.name ?? 'none')}</code>` });
      if ((q.stopName ?? '') !== (p.stopName ?? '')) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Stop name → <code>${esc(p.stopName ?? '')}</code>` });
      if ((q.lines || []).join() !== (p.lines || []).join()) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Lines → <code>${(p.lines || []).join(' ') || 'none'}</code>` });
      if ((q.terminates || []).join() !== (p.terminates || []).join()) out.push({ k: 'edit', t: `${T} · ${code}`, d: `Terminates → <code>${(p.terminates || []).join(' ') || 'none'}</code>` });
    }
    for (const code of ap.keys()) if (!bp.has(code)) out.push({ k: 'del', t: T, d: `Removed platform <code>${code}</code>` });

    // transfers
    const key = ts => (ts || []).map(t => `${t.to}:${t.mode}${t.via !== undefined ? ':' + t.via : ''}`).sort().join(',');
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
