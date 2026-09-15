'use strict';
// Reading, validating and writing appdata/data.json.
//
// Two rules matter more than the rest and are enforced as hard errors, because
// tools/build_bus_data.py fails the surface-transit build on both:
//   * a stop code may appear under exactly one station
//   * a station id must be unique
// Everything else is reported as a warning so curation judgement stays with the
// person: transferStations are deliberately asymmetric, and a curated heading is
// allowed to be line-relative rather than geometric.

const fs = require('fs');
const path = require('path');

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
const KINDS = ['underground', 'streetLevel'];

const SF = { minLat: 37.69, maxLat: 37.84, minLon: -122.55, maxLon: -122.33 };

function read(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

/** Byte-for-byte the format the repo already uses: 2-space indent, trailing \n. */
function serialise(doc) {
  return JSON.stringify(doc, null, 2) + '\n';
}

function writeAtomic(file, doc) {
  const text = serialise(doc);
  const tmp = path.join(path.dirname(file), `.data.json.${process.pid}.tmp`);
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, file);
  return text;
}

function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }

/** Structural check. Returns {errors, warnings}; errors block a save. */
function validate(doc) {
  const errors = [];
  const warnings = [];
  const E = (path, msg) => errors.push({ path, msg });
  const W = (path, msg) => warnings.push({ path, msg });

  if (!doc || typeof doc !== 'object') { E('', 'document is not an object'); return { errors, warnings }; }
  for (const key of ['lines', 'subways', 'stations']) {
    if (!Array.isArray(doc[key])) E(key, `"${key}" must be an array`);
  }
  if (errors.length) return { errors, warnings };

  const stationById = new Map();
  const codeOwner = new Map();

  for (const st of doc.stations) {
    const at = `station ${st.id || '(no id)'}`;
    if (!st.id) { E(at, 'station has no id'); continue; }
    if (stationById.has(st.id)) E(at, `duplicate station id "${st.id}"`);
    stationById.set(st.id, st);

    if (!st.name || !String(st.name).trim()) E(at, 'station has an empty name');
    if (!KINDS.includes(st.kind)) E(at, `kind must be one of ${KINDS.join(' / ')}`);
    if (!isNum(st.latitude) || !isNum(st.longitude)) E(at, 'station is missing coordinates');
    else if (st.latitude < SF.minLat || st.latitude > SF.maxLat ||
             st.longitude < SF.minLon || st.longitude > SF.maxLon) {
      W(at, 'station coordinate is outside San Francisco');
    }

    if (!Array.isArray(st.platforms) || st.platforms.length === 0) {
      E(at, 'station has no platforms');
    } else {
      const seen = new Set();
      for (const p of st.platforms) {
        const pat = `${at} platform ${p.id || '(no id)'}`;
        if (!p.id) { E(pat, 'platform has no stop code'); continue; }
        if (!/^\d{5}$/.test(String(p.id))) E(pat, 'stop code must be 5 digits');
        if (seen.has(p.id)) E(pat, `stop code "${p.id}" is listed twice on this station`);
        seen.add(p.id);
        if (codeOwner.has(p.id) && codeOwner.get(p.id) !== st.id) {
          E(pat, `stop code "${p.id}" is already used by station "${codeOwner.get(p.id)}" ` +
                 '- a code may belong to exactly one station');
        }
        codeOwner.set(p.id, st.id);
        if (!HEADINGS.includes(p.heading)) E(pat, `heading must be one of ${HEADINGS.join(' / ')}`);
        if (!isNum(p.latitude) || !isNum(p.longitude)) E(pat, 'platform is missing coordinates');
        if (p.name !== null && typeof p.name !== 'string') E(pat, 'name must be a string or null');
        if (p.stopName !== undefined && typeof p.stopName !== 'string') E(pat, 'stopName must be a string');
      }
    }
  }

  // Cross references
  for (const st of doc.stations) {
    for (const t of st.transferStations || []) {
      if (!stationById.has(t)) E(`station ${st.id}`, `transferStations points at unknown station "${t}"`);
    }
    for (const l of st.lines || []) {
      if (!doc.lines.some(x => x.id === l)) E(`station ${st.id}`, `lines lists unknown line "${l}"`);
    }
  }

  const lineIds = new Set();
  for (const ln of doc.lines) {
    const at = `line ${ln.id || '(no id)'}`;
    if (!ln.id) { E(at, 'line has no id'); continue; }
    if (lineIds.has(ln.id)) E(at, `duplicate line id "${ln.id}"`);
    lineIds.add(ln.id);
    if (!/^#[0-9A-Fa-f]{6}$/.test(ln.color || '')) E(at, 'color must be #RRGGBB');
    if (!Array.isArray(ln.stationIds) || ln.stationIds.length < 2) E(at, 'line needs at least two stations');
    else {
      for (const sid of ln.stationIds) {
        if (!stationById.has(sid)) E(at, `stationIds references unknown station "${sid}"`);
      }
      ln.stationIds.forEach((sid, i) => {
        if (i && sid === ln.stationIds[i - 1]) W(at, `station "${sid}" is listed twice in a row`);
      });
    }
  }

  for (const sub of doc.subways || []) {
    const at = `subway ${sub.id || '(no id)'}`;
    if (!sub.id) E(at, 'subway has no id');
    for (const sid of sub.stationIds || []) {
      if (!stationById.has(sid)) E(at, `stationIds references unknown station "${sid}"`);
    }
  }

  // Consistency between a line's stationIds and each station's own lines array.
  for (const ln of doc.lines) {
    for (const sid of ln.stationIds || []) {
      const st = stationById.get(sid);
      if (st && !(st.lines || []).includes(ln.id)) {
        W(`station ${sid}`, `is on line ${ln.id} but does not list ${ln.id} in its own "lines"`);
      }
    }
  }
  for (const st of doc.stations) {
    for (const l of st.lines || []) {
      const ln = doc.lines.find(x => x.id === l);
      if (ln && !(ln.stationIds || []).includes(st.id)) {
        W(`station ${st.id}`, `lists line ${l} but line ${l} does not include this station`);
      }
    }
  }

  return { errors, warnings };
}

function stats(doc) {
  return {
    stations: doc.stations.length,
    platforms: doc.stations.reduce((n, s) => n + s.platforms.length, 0),
    lines: doc.lines.length,
    subways: (doc.subways || []).length,
    underground: doc.stations.filter(s => s.kind === 'underground').length,
  };
}

module.exports = { read, serialise, writeAtomic, validate, stats, HEADINGS, KINDS };
