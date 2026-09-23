// Refreshing the 511 snapshot: fetch, read the drift, then commit (or not).
// The server side is app/editor/refresh.py. It shows in the review panel's
// second tab, so the map stays in view while the drift is read.
//
// A fetch costs two of the 511 key's calls (PLAN.md, "511 budget"), so it is
// never one click. The commit reloads the whole EditorState, because the
// snapshot is under every derived value, so it is refused while there are
// unsaved curation edits: the reload would throw them away.

import {
  store, esc, upstream, lineById, stationById, ownerOf, platformPos, isDirty,
  changes, select, setActiveLine,
} from './store.js';
import { api } from './api.js';
import { flyTo, flyToStation, fitLine, spotlight, spotlightAt, hint } from './map.js';
import { toast } from './ui.js';

const CALLS_PER_REFRESH = 2;    // refresh.py's CALLS_PER_REFRESH
const MAX_ITEMS = 200;          // per drift section; a first snapshot lists every stop

/** 'idle' | 'confirm' | 'fetching' | 'report' | 'committing' */
let phase = 'idle';
let result = null;              // SnapshotFetchResponse being read
let problem = null;             // { text, action?: 'refetch' | 'reload' }
let committed = null;           // { commit, pushed, pushError, period } of the last commit made here
let startedAt = 0;
let ticker = 0;
const folded = new Map();       // drift section id -> open?, when the person has toggled it
let goActions = [];             // index -> () => void, for the drift rows drawn last

let rerender = () => {};
let reload = async () => {};

export function initSnapshot(hooks) {
  rerender = hooks.rerender;
  reload = hooks.reload;
}

// ====================================================================== render
const period = p => (p ? `${esc(p.serviceFrom)} → ${esc(p.serviceTo)}` : 'none');
const when = iso => (iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '');

export function snapshotHtml() {
  goActions = [];
  const snaps = Object.values(store.snapshots || {});
  const current = snaps.map(s => `
    <div class="snap-cur">
      <b>${esc(s.meta.operator)}</b> service ${period(s.meta)}
      <span>fetched ${esc(when(s.meta.fetchedAt))}</span>
    </div>`).join('') || '<div class="empty">No snapshot committed yet.</div>';

  return `
  <div class="rv-pad">
    <div class="micro" style="margin-bottom:6px">Committed snapshot</div>
    ${current}
    ${committedHtml()}
    ${controlsHtml()}
    ${problem ? problemHtml() : ''}
  </div>
  ${result ? reportHtml(result) : ''}`;
}

function committedHtml() {
  if (!committed || result) return '';
  return `<div class="snap-ok">${committed.commit
    ? `Committed <code>${esc(String(committed.commit).slice(0, 7))}</code>, service ${committed.period}${committed.pushed ? ', pushed' : ', <b>not pushed</b>'}.`
    : 'Nothing to commit: sf-transit already had this snapshot.'}
    ${committed.pushError ? `<div class="rv-err">Push failed: ${esc(committed.pushError)}</div>` : ''}</div>`;
}

function controlsHtml() {
  if (phase === 'confirm') {
    return `
    <div class="snap-confirm">
      <p>Fetching uses <b>${CALLS_PER_REFRESH} of the 511 key's calls this hour</b> (the GTFS feed and
        the line list), from the same budget as the live arrivals and vehicles. It downloads about
        8&nbsp;MB and builds a snapshot, which takes a while. Nothing is committed until you choose to.</p>
      <div class="rv-actions">
        <button class="btn small primary" data-snap="fetch">Fetch from 511</button>
        <button class="btn small ghost" data-snap="cancel">Cancel</button>
      </div>
    </div>`;
  }
  if (phase === 'fetching') {
    return `
    <div class="snap-progress">
      <div class="snap-bar"><i></i></div>
      <div>Downloading 511's feed and building the snapshot… <span class="mono" id="snap-elapsed">${elapsed()}</span></div>
      <button class="btn small" disabled>Fetching…</button>
    </div>`;
  }
  return `
  <div class="rv-actions" style="margin-top:12px">
    <button class="btn small" data-snap="ask" ${phase === 'committing' ? 'disabled' : ''}
      title="Download 511's current feed and show how it differs. Uses ${CALLS_PER_REFRESH} of the key's hourly calls.">
      ${result ? 'Fetch again from 511…' : 'Refresh snapshot from 511…'}</button>
  </div>`;
}

function problemHtml() {
  const action = problem.action === 'refetch' ? '<button class="btn small" data-snap="ask">Fetch again…</button>'
    : problem.action === 'reload' ? '<button class="btn small" data-snap="reload">Reload</button>'
    : '';
  return `<div class="rv-error">${esc(problem.text)}${action ? `<div class="rv-actions">${action}</div>` : ''}</div>`;
}

const elapsed = () => `${Math.max(0, Math.round((Date.now() - startedAt) / 1000))} s`;

function reportHtml(r) {
  const d = r.drift;
  const dirty = isDirty();
  const n = dirty ? changes().length : 0;
  const busy = phase === 'committing';
  const commitBox = `
  <div class="snap-commit">
    ${dirty ? `<div class="rv-warn-box">You have ${n} unsaved curation edit${n === 1 ? '' : 's'}. Save or undo
      ${n === 1 ? 'it' : 'them'} first: committing the snapshot reloads the editor's state, which would drop
      ${n === 1 ? 'it' : 'them'}.</div>` : ''}
    ${r.unchanged ? '<div class="rv-note">511\'s data is the same as the committed snapshot, so committing makes no commit.</div>' : ''}
    <div class="rv-actions">
      <button class="btn small primary" data-snap="commit" ${dirty || busy ? 'disabled' : ''}>${busy ? 'Committing…' : 'Commit snapshot'}</button>
      <button class="btn small ghost" data-snap="discard" ${busy ? 'disabled' : ''} title="Close this report; nothing was committed">Discard</button>
    </div>
  </div>`;

  return `
  <div class="drift">
    <div class="drift-head">
      <div class="micro">${esc(d.operator)} service period</div>
      <div class="drift-period">${period(d.old)} <span class="arrow">⟶</span> <b>${period(d.new)}</b></div>
      <div class="drift-tags">
        ${r.unchanged ? '<span class="vpill">unchanged</span>' : ''}
        ${d.sameZip ? '<span class="vpill off">same GTFS zip</span>' : ''}
        <span>fetched ${esc(when(r.fetchedAt))} · against <code>${esc(String(r.baseVersion).slice(0, 7))}</code></span>
      </div>
    </div>
    ${commitBox}
    ${group('Stops', [
      sect('stops-added', 'Added', d.stopsAdded, stopAdded),
      sect('stops-removed', 'Removed', d.stopsRemoved, stopRemoved),
      sect('stops-moved', 'Moved', d.stopsMoved, stopMoved),
      sect('stops-renamed', 'Renamed', d.stopsRenamed, stopRenamed),
    ])}
    ${group('Lines', [
      sect('lines-added', 'Added', d.linesAdded, lineAddedOrRemoved),
      sect('lines-removed', 'Removed', d.linesRemoved, lineAddedOrRemoved),
      sect('lines-changed', 'Changed', d.linesChanged, lineChanged),
      sect('patterns-changed', 'Patterns changed', d.patternsChanged, patternChanged),
    ])}
    ${group('What it does to the curation', [
      sect('dead-platforms', 'Platforms that would go dead', d.deadPlatforms, deadPlatform, true),
      sect('dead-stations', 'Stations that would go dead', d.deadStations, deadStation, true),
      sect('new-unassigned', 'New unassigned stops', d.newUnassigned, newUnassigned, true),
    ])}
  </div>`;
}

const group = (title, sects) => `<div class="drift-group"><div class="micro drift-gt">${title}</div>${sects.join('')}</div>`;

/** A collapsible section with its count. Consequences for the curation start
 *  open, since they are what needs doing afterwards; the rest start folded. */
function sect(id, title, items, row, openByDefault = false) {
  const n = items.length;
  const isOpen = folded.has(id) ? folded.get(id) : openByDefault && n > 0;
  const rows = items.slice(0, MAX_ITEMS).map(row).join('')
    + (n > MAX_ITEMS ? `<div class="empty">…and ${n - MAX_ITEMS} more</div>` : '');
  return `
  <details class="drift-sect ${n ? '' : 'none'}" data-dsect="${id}" ${isOpen ? 'open' : ''}>
    <summary>${title}<em>${n}</em></summary>
    ${n ? rows : '<div class="empty">None.</div>'}
  </details>`;
}

/** A drift row; with `go`, clicking it shows the thing on the map. */
function item(html, go) {
  if (!go) return `<div class="drift-item">${html}</div>`;
  goActions.push(go);
  return `<div class="drift-item go" data-go="${goActions.length - 1}">${html}</div>`;
}

const code = pid => `<code title="${esc(pid)}">${esc(upstream(pid))}</code>`;

function bullet(id, short) {
  const l = lineById(id);
  return `<span class="mini-bullet" style="background:${esc(l?.color || '#7c8598')}${l?.textColor ? `;color:${esc(l.textColor)}` : ''}">${esc(short || l?.shortName || upstream(id))}</span>`;
}

/** Fly to a coordinate the current snapshot may not have, and ring it there. */
const ringAt = (points, zoom = 17) => () => {
  select(null, null);
  flyTo(points[points.length - 1].at, zoom);
  spotlightAt(points);
};

function where(pid) {
  const sid = ownerOf(pid);
  if (sid) return `in ${esc(stationById(sid)?.name || sid)}`;
  if (store.curation?.ignored?.[pid]) return 'ignored';
  return 'unassigned';
}

const stopAdded = s => item(`${code(s.platform)} ${esc(s.name)}`,
  ringAt([{ at: [s.lon, s.lat], code: upstream(s.platform) }]));

const stopRemoved = s => item(`${code(s.platform)} ${esc(s.name)} <span class="dim">· ${where(s.platform)}</span>`,
  ringAt([{ at: [s.lon, s.lat], code: upstream(s.platform) }]));

const stopMoved = s => item(`${code(s.platform)} ${esc(s.name)} <span class="dim">· ${Math.round(s.metres)} m · ${where(s.platform)}</span>`,
  ringAt([
    { at: [s.oldLon, s.oldLat], code: `${upstream(s.platform)} was` },
    { at: [s.lon, s.lat], code: upstream(s.platform) },
  ]));

const stopRenamed = s => item(`${code(s.platform)} ${esc(s.oldName)} → <b>${esc(s.name)}</b>`,
  platformPos(s.platform) ? () => { select(null, null); flyTo(platformPos(s.platform), 17); spotlight(s.platform); } : null);

/** Focus a line the editor knows; a line only the new snapshot has cannot be. */
const focusLine = (id, direction = null) => (lineById(id) ? () => {
  setActiveLine(id);
  if (direction !== null) {
    const i = (lineById(id).directions || []).findIndex(x => x.direction === direction);
    if (i >= 0) store.activeDir = i;
  }
  fitLine(id);
} : null);

const lineAddedOrRemoved = l => item(`${bullet(l.line, l.shortName)} ${esc(l.longName)} <span class="dim">· ${esc(l.mode)}</span>`,
  focusLine(l.line));

function fieldValue(field, v) {
  if (v === null || v === undefined) return '<span class="dim">none</span>';
  if (field === 'color' || field === 'textColor') return `<span class="swatch-dot" style="background:${esc(v)}"></span><code>${esc(v)}</code>`;
  return `<code>${esc(v)}</code>`;
}

const lineChanged = c => item(`${bullet(c.line)} ${esc(lineById(c.line)?.name || upstream(c.line))}
  ${c.changes.map(f => `<div class="drift-sub">${esc(f.field)}: ${fieldValue(f.field, f.old)} → ${fieldValue(f.field, f.new)}</div>`).join('')}`,
  focusLine(c.line));

function codes(list, max = 8) {
  return list.slice(0, max).map(code).join(' ') + (list.length > max ? ` <span class="dim">+${list.length - max}</span>` : '');
}

function patternChanged(p) {
  const head = !p.old ? `<b>new</b> → ${esc(p.new.headsign)}`
    : !p.new ? `${esc(p.old.headsign)} → <b>no trips</b>`
    : p.old.headsign !== p.new.headsign ? `${esc(p.old.headsign)} → <b>${esc(p.new.headsign)}</b>`
    : esc(p.new.headsign);
  const lines = [];
  if (p.stopsAdded.length) lines.push(`<div class="drift-sub">+${p.stopsAdded.length} stop${p.stopsAdded.length === 1 ? '' : 's'}: ${codes(p.stopsAdded)}</div>`);
  if (p.stopsRemoved.length) lines.push(`<div class="drift-sub">−${p.stopsRemoved.length} stop${p.stopsRemoved.length === 1 ? '' : 's'}: ${codes(p.stopsRemoved)}</div>`);
  if (p.old && p.new && !p.stopsAdded.length && !p.stopsRemoved.length && p.old.headsign === p.new.headsign) {
    lines.push('<div class="drift-sub">same stops, in a different order</div>');
  }
  return item(`${bullet(p.line)} <span class="dim">direction ${p.direction}</span> ${head}${lines.join('')}`,
    focusLine(p.line, p.direction));
}

const deadPlatform = d => item(`${code(d.platform)} ${esc(d.stationName)} <span class="dim">· ${esc(d.heading)}</span>`,
  stationById(d.station) ? () => { select(d.station, d.platform); flyToStation(d.station); } : null);

const deadStation = d => item(`${esc(d.name)} <code>${esc(d.station)}</code> <span class="dim">· ${d.platforms.map(p => esc(upstream(p))).join(' ')}</span>`,
  stationById(d.station) ? () => { select(d.station, null); flyToStation(d.station); } : null);

function newUnassigned(u) {
  const p = u.proposal;
  const to = p.station ? `join ${esc(stationById(p.station)?.name || p.station)}`
    : `new station ${esc(p.newStation.name)}`;
  return item(`${code(u.platform)} ${esc(u.stopName)}
    <div class="drift-sub">→ ${to}${p.heading ? ` · ${esc(p.heading)}` : ''} <span class="dim">· ${esc(p.reason)}</span></div>`,
  ringAt([{ at: [u.lon, u.lat], code: upstream(u.platform) }]));
}

// ======================================================================= wiring
export function wireSnapshot(body) {
  body.querySelectorAll('[data-snap]').forEach(b => {
    b.onclick = () => run(b.dataset.snap);
  });
  body.querySelectorAll('[data-go]').forEach(el => {
    el.onclick = () => goActions[Number(el.dataset.go)]?.();
  });
  body.querySelectorAll('details[data-dsect]').forEach(el => {
    el.addEventListener('toggle', () => folded.set(el.dataset.dsect, el.open));
  });
}

function run(what) {
  if (what === 'ask') { phase = 'confirm'; problem = null; rerender(); return; }
  if (what === 'cancel') { phase = result ? 'report' : 'idle'; rerender(); return; }
  if (what === 'fetch') { fetchNow(); return; }
  if (what === 'discard') { result = null; phase = 'idle'; problem = null; rerender(); return; }
  if (what === 'commit') { commitNow(); return; }
  if (what === 'reload') { reloadNow(); }
}

async function fetchNow() {
  if (phase === 'fetching') return;
  phase = 'fetching';
  problem = null;
  committed = null;
  // A successful fetch replaces the server's pending snapshot, so the report on
  // screen would no longer be the one a commit would write.
  result = null;
  startedAt = Date.now();
  clearInterval(ticker);
  ticker = setInterval(() => {
    const el = document.getElementById('snap-elapsed');
    if (el) el.textContent = elapsed();
  }, 1000);
  rerender();
  try {
    result = await api.snapshotFetch();
    phase = 'report';
    folded.clear();
    // It can take long enough for the panel to have been closed meanwhile.
    if (document.getElementById('review')?.classList.contains('closed')) {
      toast('The 511 snapshot fetch finished: open the review panel (Q) to read the drift', 'info');
    }
  } catch (err) {
    phase = 'idle';
    problem = {
      text: err.status === 409
        ? `${err.message} Wait for it to finish (perhaps in another tab), then fetch again.`
        : err.status === 503 ? err.message
        : `The fetch failed: ${err.message}`,
    };
  } finally {
    clearInterval(ticker);
    rerender();
  }
}

async function commitNow() {
  if (!result || phase === 'committing') return;
  if (isDirty()) {
    // The button is disabled then; this is for an edit made while the page was
    // deciding. The commit reloads the state, and the edit would be lost.
    toast('Save or undo your curation edits before committing the snapshot', 'err');
    rerender();
    return;
  }
  phase = 'committing';
  problem = null;
  rerender();
  const sent = result;
  try {
    const res = await api.snapshotCommit(sent.pending, sent.baseVersion);
    result = null;
    phase = 'idle';
    committed = { commit: res.commit, pushed: res.pushed, pushError: res.pushError, period: period(sent.drift.new) };
    // The snapshot is under every derived value and the review, so the state is
    // loaded whole rather than patched.
    try { await reload(); } catch (err) { toast(`Committed, but reloading failed: ${esc(err.message)}. Reload the page.`, 'err', null, 12000); }
    if (res.commit) {
      toast(`Committed snapshot ${esc(String(res.commit).slice(0, 7))}${res.pushed ? ' and pushed' : ', not pushed'}`, res.pushed ? 'ok' : 'info');
    } else {
      toast('Nothing to commit: sf-transit already had this snapshot', 'info');
    }
    if (res.pushError) toast(`Push failed: ${esc(res.pushError)}`, 'err', null, 9000);
  } catch (err) {
    phase = 'report';
    if (err.status === 404) {
      result = null;
      phase = 'idle';
      problem = { text: err.message, action: 'refetch' };
    } else if (err.status === 409) {
      problem = {
        text: 'Someone else refreshed the snapshot after this fetch, so this drift no longer describes what a commit would change. Reload, then fetch again.',
        action: 'reload',
      };
    } else {
      problem = { text: `The commit failed: ${err.message}` };
    }
  } finally {
    rerender();
  }
}

async function reloadNow() {
  if (isDirty()) {
    toast('Save or undo your curation edits first: reloading would drop them', 'err');
    return;
  }
  try {
    await reload();
    result = null;
    phase = 'idle';
    problem = null;
    hint('Reloaded. Fetch again to see the drift against the latest snapshot', 3600);
  } catch (err) {
    problem = { text: `Reload failed: ${err.message}`, action: 'reload' };
  }
  rerender();
}
