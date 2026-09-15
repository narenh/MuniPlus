'use strict';
// Reading, validating and writing appdata/data.json.
//
// Hard errors are things that make the document self-inconsistent: a stop code
// under two stations, a duplicate id, a reference to something that is not
// there. Everything that is a judgement call is a warning, because curation
// belongs to the person: transfers are deliberately asymmetric, headings are
// sometimes line-relative rather than geometric, and isIsland is hand-authored
// and legitimately unset.

const fs = require('fs');
const path = require('path');

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
const MODES = ['indoor', 'street'];
const SF = { minLat: 37.69, maxLat: 37.84, minLon: -122.55, maxLon: -122.33 };

const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const serialise = doc => JSON.stringify(doc, null, 2) + '\n';

function writeAtomic(file, doc) {
  const text = serialise(doc);
  const tmp = path.join(path.dirname(file), `.data.json.${process.pid}.tmp`);
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, file);
  return text;
}

const isNum = v => typeof v === 'number' && Number.isFinite(v);

/**
 * A station is one of two shapes, and the shape is its own discriminator:
 * it either has `levels` (and `exits`), or a flat `platforms` list. There is
 * no `kind` field to contradict it - which matters because "underground" was
 * already wrong for Balboa Park, whose BART tracks are elevated.
 */
const isMultilevel = st => Array.isArray(st?.levels);

/** Every platform of a station, whichever shape it has. */
function platformsOf(st) {
  return isMultilevel(st) ? st.levels.flatMap(l => l.platforms || []) : (st.platforms || []);
}

/**
 * The station coordinate is always derived and never edited: from the exits at
 * a multilevel station, from the platforms at a street one. A multilevel
 * station's platform coordinates deliberately do NOT feed it - they may be
 * below street or above it, and what a rider walks to is a door.
 */
function derivedCoord(st) {
  const pts = isMultilevel(st)
    ? (st.exits || []).filter(e => isNum(e.latitude) && isNum(e.longitude))
    : (st.platforms || []).filter(p => isNum(p.latitude) && isNum(p.longitude));
  if (!pts.length) return { latitude: null, longitude: null };
  return {
    latitude: Math.round(pts.reduce((n, p) => n + p.latitude, 0) / pts.length * 1e6) / 1e6,
    longitude: Math.round(pts.reduce((n, p) => n + p.longitude, 0) / pts.length * 1e6) / 1e6,
  };
}

/** Rewrite every derived field in place. Called before any write. */
function applyDerived(doc) {
  const onLine = new Map();
  for (const l of doc.lines) for (const sid of l.stationIds || []) {
    if (!onLine.has(sid)) onLine.set(sid, new Set());
    onLine.get(sid).add(l.id);
  }
  for (const st of doc.stations) {
    const c = derivedCoord(st);
    st.latitude = c.latitude;
    st.longitude = c.longitude;
    const union = new Set();
    for (const p of platformsOf(st)) for (const l of p.lines || []) union.add(l);
    for (const l of onLine.get(st.id) || []) union.add(l);
    st.lines = [...union].sort();
  }
  return doc;
}

function validate(doc) {
  const errors = [], warnings = [];
  const E = (path, msg) => errors.push({ path, msg });
  const W = (path, msg) => warnings.push({ path, msg });

  if (!doc || typeof doc !== 'object') { E('', 'document is not an object'); return { errors, warnings }; }
  for (const k of ['lines', 'subways', 'stations']) {
    if (!Array.isArray(doc[k])) E(k, `"${k}" must be an array`);
  }
  if (errors.length) return { errors, warnings };

  const stationIds = new Set();
  const codeOwner = new Map();
  const lineIds = new Set(doc.lines.map(l => l.id));

  for (const st of doc.stations) {
    const at = `station ${st.id || '(no id)'}`;
    if (!st.id) { E(at, 'station has no id'); continue; }
    if (stationIds.has(st.id)) E(at, `duplicate station id "${st.id}"`);
    stationIds.add(st.id);
    if (!st.name || !String(st.name).trim()) E(at, 'station has an empty name');
    if (st.kind !== undefined) E(at, 'kind is derived from the shape and must not be stored');
    if (isMultilevel(st) && st.platforms !== undefined) {
      E(at, 'a station has levels or platforms, never both');
    }
    if (!isMultilevel(st) && st.platforms === undefined) {
      E(at, 'a station needs either levels or platforms');
    }

    const multilevel = isMultilevel(st);

    if (multilevel) {
      if (!st.levels.length) E(at, 'a multilevel station has no levels');
      if (!Array.isArray(st.exits)) E(at, 'a multilevel station has no exits array');

      const depths = new Set();
      for (const lv of st.levels || []) {
        const lat_ = `${at} level ${lv.id}`;
        // Depth is an ordinal, signed: 0 is street, negative is below it,
        // positive is above. Balboa Park's BART tracks are elevated, so a
        // positive level is not a mistake.
        if (!Number.isInteger(lv.id)) E(lat_, 'a level id must be an integer depth');
        if (depths.has(lv.id)) E(lat_, `two levels share depth ${lv.id}`);
        depths.add(lv.id);
        if (!lv.name || !String(lv.name).trim()) E(lat_, 'level has an empty name');
        if (lv.isIsland !== null && typeof lv.isIsland !== 'boolean') {
          E(lat_, 'isIsland must be true, false, or null when not yet authored');
        }
        if (!Array.isArray(lv.platforms)) E(lat_, 'level has no platforms array');
      }

      for (const ex of st.exits || []) {
        const eat = `${at} exit ${ex.id || '(no id)'}`;
        if (!ex.id) E(eat, 'exit has no id');
        if (!ex.name || !String(ex.name).trim()) E(eat, 'exit has an empty name');
        if (!depths.has(ex.level)) E(eat, `exit lands on level ${ex.level}, which this station does not have`);
        for (const f of ['stairs', 'escalator', 'elevator', 'closed']) {
          if (typeof ex[f] !== 'boolean') E(eat, `${f} must be true or false`);
        }
        if (ex.latitude !== null && !isNum(ex.latitude)) E(eat, 'latitude must be a number or null');
        if (ex.longitude !== null && !isNum(ex.longitude)) E(eat, 'longitude must be a number or null');
        if (!ex.stairs && !ex.escalator && !ex.elevator) {
          W(eat, 'exit has no stairs, escalator or elevator');
        }
      }
      const open = (st.exits || []).filter(e => !e.closed);
      if (!open.length && (st.exits || []).length) W(at, 'every exit is marked closed');
      if (!(st.exits || []).length) W(at, 'no exits yet, so this station has no coordinate');
      else if (!open.some(e => isNum(e.latitude))) W(at, 'no open exit has coordinates yet');
    } else {
      if (!st.platforms.length) E(at, 'station has no platforms');
      if (st.exits !== undefined) E(at, 'exits belong to a multilevel station');
    }

    // platforms, whichever shape holds them
    const seen = new Set();
    for (const p of platformsOf(st)) {
      const pat = `${at} platform ${p.id || '(no code)'}`;
      if (!/^\d{5}$/.test(String(p.id || ''))) E(pat, 'stop code must be 5 digits');
      if (seen.has(p.id)) E(pat, `stop code "${p.id}" is listed twice at this station`);
      seen.add(p.id);
      if (codeOwner.has(p.id) && codeOwner.get(p.id) !== st.id) {
        E(pat, `stop code "${p.id}" is already used by station "${codeOwner.get(p.id)}"`);
      }
      codeOwner.set(p.id, st.id);
      if (!HEADINGS.includes(p.heading)) E(pat, `heading must be one of ${HEADINGS.join(' / ')}`);
      if (!isNum(p.latitude) || !isNum(p.longitude)) E(pat, 'platform is missing coordinates');
      if (p.name !== null && typeof p.name !== 'string') E(pat, 'name must be a string or null');
      if (!Array.isArray(p.lines) || !p.lines.length) E(pat, 'platform serves no lines');
      for (const l of p.lines || []) if (!lineIds.has(l)) E(pat, `unknown line "${l}"`);
      for (const l of p.terminates || []) {
        if (!(p.lines || []).includes(l)) E(pat, `terminates lists "${l}", which does not serve this platform`);
      }
      if (multilevel && p.stopName !== undefined) W(pat, 'stopName is for street-level platforms');
    }

    if (isNum(st.latitude) && (st.latitude < SF.minLat || st.latitude > SF.maxLat ||
        st.longitude < SF.minLon || st.longitude > SF.maxLon)) {
      W(at, 'station coordinate is outside San Francisco');
    }
  }

  // cross references
  for (const st of doc.stations) {
    for (const t of st.transfers || []) {
      const tat = `station ${st.id}`;
      if (!stationIds.has(t.to)) E(tat, `transfer points at unknown station "${t.to}"`);
      if (!MODES.includes(t.mode)) E(tat, `transfer mode must be ${MODES.join(' or ')}`);
      if (t.mode === 'indoor') {
        const other = doc.stations.find(s => s.id === t.to);
        const back = (other?.transfers || []).find(x => x.to === st.id && x.mode === 'indoor');
        // You cannot build a passage you can only walk one way.
        if (!back) E(tat, `indoor transfer to "${t.to}" is not reciprocated; an indoor link must work both ways`);
        if (t.via !== undefined && !(st.levels || []).some(l => l.id === t.via)) {
          E(tat, `transfer via level ${t.via}, which this station does not have`);
        }
      }
    }
  }

  for (const ln of doc.lines) {
    const at = `line ${ln.id || '(no id)'}`;
    if (!ln.id) { E(at, 'line has no id'); continue; }
    if (!/^#[0-9A-Fa-f]{6}$/.test(ln.color || '')) E(at, 'color must be #RRGGBB');
    if (ln.hidden !== undefined && typeof ln.hidden !== 'boolean') E(at, 'hidden must be true or false');
    if (!Array.isArray(ln.stationIds) || ln.stationIds.length < 2) E(at, 'line needs at least two stations');
    for (const sid of ln.stationIds || []) {
      if (!stationIds.has(sid)) E(at, `stationIds references unknown station "${sid}"`);
      else {
        const st = doc.stations.find(s => s.id === sid);
        if (!platformsOf(st).some(p => (p.lines || []).includes(ln.id))) {
          W(`station ${sid}`, `is on line ${ln.id} but no platform there serves it`);
        }
      }
    }
  }
  for (const sub of doc.subways || []) {
    for (const sid of sub.stationIds || []) {
      if (!stationIds.has(sid)) E(`subway ${sub.id}`, `references unknown station "${sid}"`);
    }
  }
  return { errors, warnings };
}

function stats(doc) {
  return {
    stations: doc.stations.length,
    platforms: doc.stations.reduce((n, s) => n + platformsOf(s).length, 0),
    lines: doc.lines.length,
    subways: (doc.subways || []).length,
    multilevel: doc.stations.filter(isMultilevel).length,
    exits: doc.stations.reduce((n, s) => n + (s.exits || []).length, 0),
  };
}

module.exports = {
  read, serialise, writeAtomic, validate, stats, platformsOf, derivedCoord, applyDerived,
  isMultilevel, HEADINGS, MODES,
};
