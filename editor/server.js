#!/usr/bin/env node
'use strict';
// Muni+ data.json editor - server.
//
// Deliberately dependency-free: `node server.js` is the whole runtime, which
// keeps the Coolify image to a base node layer plus git.
//
// NOTE ON ACCESS: this server has no authentication of its own. It is meant to
// sit behind Coolify's proxy with auth in front of it. Anything that can reach
// this port can commit to the repo.

const fs = require('fs');
const http = require('http');
const path = require('path');
const { URL } = require('url');

const { makeGit } = require('./lib/git');
const { readZipEntry, parseCsv } = require('./lib/gtfs');
const data = require('./lib/data');

const PORT = Number(process.env.PORT || 8787);
const HOST = process.env.HOST || '0.0.0.0';
const REPO_ROOT = path.resolve(process.env.REPO_ROOT || path.join(__dirname, '..'));
const DATA_REL = process.env.DATA_FILE || 'appdata/data.json';
const DATA_ABS = path.join(REPO_ROOT, DATA_REL);
const PUBLIC = path.join(__dirname, 'public');
const GTFS_URL = process.env.GTFS_URL || 'https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip';
const PUSH_BRANCH = process.env.GIT_BRANCH || '';
const AUTO_PUSH = /^(1|true|yes)$/i.test(process.env.GIT_PUSH || '');
const READ_ONLY = /^(1|true|yes)$/i.test(process.env.READ_ONLY || '');

const git = makeGit(REPO_ROOT, {
  authorName: process.env.GIT_AUTHOR_NAME,
  authorEmail: process.env.GIT_AUTHOR_EMAIL,
  remote: process.env.GIT_REMOTE || 'origin',
});

// ---------------------------------------------------------------- helpers
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
};

function send(res, code, body, headers = {}) {
  const buf = Buffer.isBuffer(body) ? body : Buffer.from(body);
  res.writeHead(code, {
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
    ...headers,
  });
  res.end(buf);
}

const json = (res, code, obj) =>
  send(res, code, JSON.stringify(obj), { 'Content-Type': 'application/json; charset=utf-8' });

function readBody(req, limit = 24 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', c => {
      size += c.length;
      if (size > limit) { reject(new Error('request body too large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

async function meta() {
  const available = await git.available();
  if (!available) return { git: false, branch: null, head: null, dirty: false, readOnly: READ_ONLY };
  const [branch, head, dirty] = await Promise.all([
    git.branch().catch(() => null),
    git.head().catch(() => null),
    git.isDirty(DATA_REL).catch(() => false),
  ]);
  return {
    git: true, branch, head, dirty,
    pushBranch: PUSH_BRANCH || branch,
    autoPush: AUTO_PUSH,
    readOnly: READ_ONLY,
    file: DATA_REL,
  };
}

// ------------------------------------------------------------ GTFS drift
let driftCache = { at: 0, payload: null };
const DRIFT_TTL = 10 * 60 * 1000;

async function fetchStops() {
  const res = await fetch(GTFS_URL);
  if (!res.ok) throw new Error(`feed responded ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  const entry = readZipEntry(buf, 'stops.txt');
  if (!entry) throw new Error('feed contains no stops.txt');
  const rows = parseCsv(entry.toString('utf8'));
  const byCode = new Map();
  for (const r of rows) {
    if (r.stop_code && !byCode.has(r.stop_code)) {
      byCode.set(r.stop_code, {
        code: r.stop_code,
        name: r.stop_name,
        lat: Number(r.stop_lat),
        lon: Number(r.stop_lon),
      });
    }
  }
  return byCode;
}

const M_PER_DEG_LAT = 110540;
const M_PER_DEG_LON = 111320 * Math.cos(37.76 * Math.PI / 180);
const metres = (aLat, aLon, bLat, bLon) =>
  Math.hypot((aLon - bLon) * M_PER_DEG_LON, (aLat - bLat) * M_PER_DEG_LAT);

async function computeDrift(doc) {
  const stops = await fetchStops();
  const platforms = [];
  for (const st of doc.stations) {
    for (const p of st.platforms) {
      const feed = stops.get(String(p.id));
      if (!feed) {
        platforms.push({ station: st.id, platform: p.id, kind: 'missing',
          detail: 'this stop code is not in the current SFMTA feed' });
        continue;
      }
      const d = metres(p.latitude, p.longitude, feed.lat, feed.lon);
      platforms.push({
        station: st.id, platform: p.id,
        kind: d > 25 ? 'moved' : 'ok',
        metres: Math.round(d),
        feedName: feed.name,
        feedLat: feed.lat, feedLon: feed.lon,
      });
    }
  }
  return {
    fetchedAt: new Date().toISOString(),
    feedStops: stops.size,
    platforms,
  };
}

// -------------------------------------------------------------- routing
async function api(req, res, url) {
  const route = `${req.method} ${url.pathname}`;

  if (route === 'GET /api/state') {
    const doc = data.read(DATA_ABS);
    return json(res, 200, {
      doc,
      meta: await meta(),
      stats: data.stats(doc),
      validation: data.validate(doc),
    });
  }

  if (route === 'GET /api/diff') {
    const diff = await git.diff(DATA_REL).catch(e => `diff unavailable: ${e.message}`);
    return json(res, 200, { diff });
  }

  if (route === 'GET /api/history') {
    const commits = await git.log(DATA_REL, 40).catch(() => []);
    return json(res, 200, { commits });
  }

  if (route === 'GET /api/drift') {
    const force = url.searchParams.get('force') === '1';
    if (!force && driftCache.payload && Date.now() - driftCache.at < DRIFT_TTL) {
      return json(res, 200, { ...driftCache.payload, cached: true });
    }
    try {
      const payload = await computeDrift(data.read(DATA_ABS));
      driftCache = { at: Date.now(), payload };
      return json(res, 200, { ...payload, cached: false });
    } catch (e) {
      return json(res, 502, { error: `could not reach the SFMTA feed: ${e.message}` });
    }
  }

  if (route === 'POST /api/validate') {
    const body = JSON.parse(await readBody(req));
    return json(res, 200, data.validate(body.doc));
  }

  if (route === 'PUT /api/doc' || route === 'POST /api/commit') {
    if (READ_ONLY) return json(res, 403, { error: 'editor is running in read-only mode' });

    const body = JSON.parse(await readBody(req));
    if (!body || typeof body.doc !== 'object') return json(res, 400, { error: 'missing doc' });

    const validation = data.validate(body.doc);
    if (validation.errors.length) {
      return json(res, 422, { error: 'document failed validation', validation });
    }

    data.writeAtomic(DATA_ABS, body.doc);

    if (route === 'PUT /api/doc') {
      return json(res, 200, { ok: true, meta: await meta(), validation });
    }

    const message = String(body.message || '').trim() || 'Update data.json from the editor';
    let commit = null, pushed = false, pushError = null;
    try {
      commit = await git.commit(DATA_REL, message);
    } catch (e) {
      return json(res, 500, { error: `commit failed: ${e.stderr || e.message}` });
    }
    if (commit && (body.push ?? AUTO_PUSH)) {
      try { await git.push(PUSH_BRANCH || await git.branch()); pushed = true; }
      catch (e) { pushError = e.stderr || e.message; }
    }
    return json(res, 200, { ok: true, commit, pushed, pushError, meta: await meta(), validation });
  }

  if (route === 'POST /api/discard') {
    if (READ_ONLY) return json(res, 403, { error: 'editor is running in read-only mode' });
    await git.discard(DATA_REL);
    const doc = data.read(DATA_ABS);
    return json(res, 200, { ok: true, doc, meta: await meta(), stats: data.stats(doc) });
  }

  return json(res, 404, { error: `no such endpoint: ${route}` });
}

function serveStatic(req, res, url) {
  let rel = decodeURIComponent(url.pathname);
  if (rel === '/' || rel === '') rel = '/index.html';
  const abs = path.join(PUBLIC, rel);
  if (!abs.startsWith(PUBLIC + path.sep) && abs !== path.join(PUBLIC, 'index.html')) {
    return send(res, 403, 'forbidden');
  }
  fs.readFile(abs, (err, buf) => {
    if (err) return send(res, 404, 'not found');
    send(res, 200, buf, { 'Content-Type': MIME[path.extname(abs)] || 'application/octet-stream' });
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (url.pathname.startsWith('/api/')) {
    api(req, res, url).catch(err => {
      console.error('[api]', err);
      json(res, 500, { error: err.message || String(err) });
    });
  } else {
    serveStatic(req, res, url);
  }
});

server.listen(PORT, HOST, async () => {
  const m = await meta();
  console.log(`Muni+ editor  http://${HOST}:${PORT}`);
  console.log(`  repo    ${REPO_ROOT}`);
  console.log(`  file    ${DATA_REL}`);
  console.log(`  git     ${m.git ? `${m.branch}${m.dirty ? ' (uncommitted changes)' : ''}` : 'unavailable'}`);
  console.log(`  push    ${AUTO_PUSH ? (PUSH_BRANCH || m.branch) : 'off'}`);
  if (READ_ONLY) console.log('  MODE    read-only');
});
