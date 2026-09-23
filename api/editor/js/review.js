// The review panel: where 511 and the curation disagree, as `GET api/review`
// lists it (app/editor/review.py), and the snapshot refresh (snapshot.js).
//
// The review is computed from the *saved* curation, so it only changes with a
// save. Everything done here is an ordinary edit() on the unsaved curation:
// undoable, listed in the change list, validated by the server, and kept only
// once the person saves. So each row also looks at the unsaved curation, and a
// stop assigned or ignored in this edit shows as done until the save that
// makes the server drop it.
//
// Editor only: /map and a read-only editor never show it.

import {
  store, edit, select, esc, upstream, lineById, stationById, platformsOf, stopsOf, ownerOf,
  stationIdProblem, inboundTransfers, subwaysWith, deleteStationIn,
} from './store.js';
import { api } from './api.js';
import { flyTo, flyToStation, spotlight, hint } from './map.js';
import { openPalette } from './ui.js';
import { HEADINGS } from './inspector.js';
import { initSnapshot, snapshotHtml, wireSnapshot } from './snapshot.js';

const $ = id => document.getElementById(id);

/** Rows drawn per section. The seed has a dozen unassigned stops; a first run
 *  against a new operator could have thousands, and the panel must stay usable. */
const MAX_ROWS = 150;
const MAX_LINE_CHIPS = 8;

let review = null;          // the last ReviewResponse
let loading = false;
let loadError = null;
let open = false;
let tab = 'queue';
/** One inline form at a time: `{ key, kind }`, kind 'new' | 'ignore' | 'delete'. */
let expanded = null;
/** What has been typed or chosen per row, so the re-render after every validate
 *  answer keeps it: platform id -> { heading, id, name, note }. */
const drafts = new Map();
/** Proposed new-station id -> the id actually created for it in this edit. Stops
 *  at one corner share a proposed station; once the first has created it (maybe
 *  under an id the person changed), the rest join it instead of minting another. */
const minted = new Map();
/** Sections the person has folded, so a re-render does not unfold them. */
const folded = new Set();

let reloadState = async () => {};

export function initReview({ reload }) {
  reloadState = reload;
  initSnapshot({ rerender: renderReview, reload: () => reloadState() });
  $('review-chip').onclick = () => toggleReview();
  $('review-close').onclick = () => toggleReview(false);
  $('review-reload').onclick = () => refreshReview();
  document.querySelectorAll('#review .rv-tab').forEach(b => {
    b.onclick = () => { tab = b.dataset.tab; renderReview(); };
  });
}

const editable = () => !store.readOnly && !store.publicMap;

export const isReviewOpen = () => open;

export function toggleReview(force = !open) {
  open = !!force && editable();
  if (open && !review && !loading) refreshReview();
  renderReview();
}

/** Ask the server again. After a save the proposals are recomputed against the
 *  new curation, so anything remembered about the old ones goes. */
export async function refreshReview() {
  if (!editable()) return;
  loading = true;
  renderReview();
  try {
    const next = await api.review();
    if (review?.version !== next.version) { minted.clear(); drafts.clear(); expanded = null; }
    review = next;
    loadError = null;
  } catch (err) {
    loadError = err.message;
  } finally {
    loading = false;
    renderReview();
  }
}

// ======================================================================= state
// Each row's standing in the unsaved curation: null while it still needs a
// decision, else what was decided.

function stopDone(u) {
  const sid = ownerOf(u.stop);
  if (sid) return `Assigned to ${esc(stationById(sid)?.name || sid)}`;
  if (store.curation?.ignored?.[u.stop]) return 'Ignored';
  return null;
}

function deadStopDone(d) {
  const st = stationById(d.station);
  if (!st) return 'Station deleted';
  if (!platformsOf(st).some(p => stopsOf(p).includes(d.stop))) return 'Removed';
  return null;
}

const stationDone = d => (stationById(d.station) ? null : 'Deleted');

/** A dead station's stops are also dead stops (the contract lists them in
 *  both). They are dealt with by deleting the station, so they are shown there
 *  and not twice. */
function looseStops() {
  const dead = new Set((review?.deadStations || []).map(d => d.station));
  return (review?.deadStops || []).filter(d => !dead.has(d.station));
}

export function pendingCount() {
  if (!review) return null;
  return review.unassigned.filter(u => !stopDone(u)).length
    + looseStops().filter(d => !deadStopDone(d)).length
    + review.deadStations.filter(d => !stationDone(d)).length;
}

/** Where a proposal points now: an existing station, the station an earlier
 *  accept created for this stop's corner, or a station still to be created. */
function target(u) {
  const p = u.proposal;
  if (p.station) return { sid: p.station, missing: !stationById(p.station) };
  const made = minted.get(p.newStation.id);
  if (made && stationById(made)) return { sid: made, made: true };
  return { create: p.newStation };
}

const draftOf = pid => {
  if (!drafts.has(pid)) drafts.set(pid, {});
  return drafts.get(pid);
};
const headingOf = u => draftOf(u.stop).heading ?? u.proposal.heading ?? '';

// ====================================================================== render
export function renderReview() {
  const panel = $('review');
  if (!panel) return;
  if (!editable()) {
    open = false;
    panel.classList.add('closed');
    return;
  }

  const n = pendingCount();
  $('review-count').textContent = n === null ? (loadError ? '!' : '…') : n;
  $('review-dot').className = 'dot ' + (loadError ? 'bad' : n ? 'warn' : n === 0 ? 'live' : '') + (loading ? ' pending' : '');
  $('review-chip').title = loadError ? `The review could not be loaded: ${loadError}`
    : `${n ?? '…'} to review: unassigned 511 stops, and stops and stations 511 dropped (Q)`;
  $('review-chip').classList.toggle('on', open);
  $('rv-tab-count').textContent = n ?? '';

  panel.classList.toggle('closed', !open);
  if (!open) return;

  document.querySelectorAll('#review .rv-tab').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $('review-reload').hidden = tab !== 'queue';
  $('review-reload').classList.toggle('spin', loading);

  const body = $('review-body');
  const keep = focusState(body);
  if (tab === 'queue') {
    body.innerHTML = queueHtml();
    wireQueue(body);
  } else {
    body.innerHTML = snapshotHtml();
    wireSnapshot(body);
  }
  restoreFocus(body, keep);
}

function focusState(host) {
  const a = document.activeElement;
  const key = host.contains(a) && a.dataset?.key;
  if (!key) return null;
  return { key, start: a.selectionStart, end: a.selectionEnd };
}

function restoreFocus(host, keep) {
  if (!keep) return;
  const el = host.querySelector(`[data-key="${CSS.escape(keep.key)}"]`);
  if (!el) return;
  el.focus();
  try { if (keep.start != null) el.setSelectionRange(keep.start, keep.end); } catch { /* selects */ }
}

function section(id, title, count, sub, rows) {
  const isOpen = !folded.has(id);
  return `
  <details class="rv-sect" data-sect="${id}" ${isOpen ? 'open' : ''}>
    <summary><span class="micro">${title}</span><em>${count}</em></summary>
    ${sub ? `<div class="rv-sub">${sub}</div>` : ''}
    ${rows || '<div class="empty">Nothing here.</div>'}
  </details>`;
}

function capped(list, row) {
  const shown = list.slice(0, MAX_ROWS).map(row).join('');
  return shown + (list.length > MAX_ROWS ? `<div class="empty">…and ${list.length - MAX_ROWS} more</div>` : '');
}

function queueHtml() {
  if (!review) {
    return loadError
      ? `<div class="rv-error">The review could not be loaded: ${esc(loadError)}</div>`
      : '<div class="empty rv-pad">Loading the review…</div>';
  }
  const loose = looseStops();
  const stale = review.version !== store.version
    ? `<div class="rv-note">Computed for ${esc(String(review.version).slice(0, 7))}; this edit started from ${esc(String(store.version).slice(0, 7))}. Proposals may be out of date until the next save.</div>`
    : '';
  const err = loadError ? `<div class="rv-error">Could not refresh: ${esc(loadError)}</div>` : '';
  return `${err}${stale}
    ${section('unassigned', 'Unassigned stops', review.unassigned.length,
      '511 lists these, but no station has them, so the API leaves them out.',
      capped(review.unassigned, stopRow))}
    ${section('dead-stops', 'Stops 511 dropped', loose.length,
      'Still in a station, but 511 no longer lists them.',
      capped(loose, deadStopRow))}
    ${section('dead-stations', 'Stations with no live platforms', review.deadStations.length,
      '511 lists none of their platforms, so the API leaves them out.',
      capped(review.deadStations, stationRow))}
    <div class="rv-foot">Every action here is an edit: undo it with ⌘Z, and nothing is kept until you save.</div>`;
}

function lineChips(ids) {
  const ls = ids.map(lineById).filter(Boolean);
  if (!ls.length) return '<span class="rv-none">no line stops here</span>';
  return ls.slice(0, MAX_LINE_CHIPS).map(l =>
    `<span class="mini-bullet" style="background:${esc(l.color || '#7c8598')}${l.textColor ? `;color:${esc(l.textColor)}` : ''}" title="${esc(l.name)}">${esc(l.shortName || upstream(l.id))}</span>`).join('')
    + (ls.length > MAX_LINE_CHIPS ? `<span class="mini-more">+${ls.length - MAX_LINE_CHIPS}</span>` : '');
}

const stationLink = (sid, label) =>
  `<button class="rv-link" data-act="goto-station" data-sid="${esc(sid)}" title="Select ${esc(sid)}">${esc(label)}</button>`;

function proposalHtml(t) {
  if (t.create) {
    return `<b>New station</b> ${esc(t.create.name)} <code>${esc(t.create.id)}</code>`;
  }
  const st = stationById(t.sid);
  if (t.missing) return `<b>Join</b> <code>${esc(t.sid)}</code> <span class="rv-warn">deleted in this edit</span>`;
  return `<b>Join</b> ${stationLink(t.sid, st.name)} <code>${esc(t.sid)}</code>`
    + (t.made ? ' <span class="rv-new">new in this edit</span>' : '');
}

function stopRow(u) {
  const pid = u.stop;
  const done = stopDone(u);
  const t = target(u);
  const d = draftOf(pid);
  const heading = headingOf(u);
  const exp = expanded?.key === pid ? expanded.kind : null;

  const headingSelect = `
    <select class="inp rv-heading ${heading ? '' : 'need'}" data-f="heading" data-pid="${esc(pid)}" data-key="h-${esc(pid)}"
      title="${u.proposal.heading ? 'The proposed heading; change it if it is wrong' : 'No line stops here, so there is no direction of travel to read a heading from: choose one'}">
      ${heading ? '' : '<option value="" selected>Heading?</option>'}
      ${HEADINGS.map(h => `<option value="${h}" ${heading === h ? 'selected' : ''}>${h}</option>`).join('')}
    </select>`;

  let form = '';
  if (exp === 'new') {
    const idProblem = stationIdProblem((d.id ?? '').trim());
    const nameBlank = !(d.name ?? '').trim();
    form = `
    <div class="rv-form">
      <div class="micro">New station, with ${esc(upstream(pid))} ${esc(heading)} <span class="sub-label">— the id is permanent: the app keeps it in favourites</span></div>
      <label class="rv-lab">Id<input class="inp mono ${idProblem ? 'bad' : ''}" data-f="id" data-pid="${esc(pid)}" data-key="i-${esc(pid)}" value="${esc(d.id ?? '')}" spellcheck="false" autocomplete="off"></label>
      ${idProblem ? `<div class="rv-err">${esc(idProblem)}</div>` : ''}
      <label class="rv-lab">Name<input class="inp ${nameBlank ? 'bad' : ''}" data-f="name" data-pid="${esc(pid)}" data-key="n-${esc(pid)}" value="${esc(d.name ?? '')}"></label>
      ${nameBlank ? '<div class="rv-err">A name is required</div>' : ''}
      <div class="rv-actions">
        <button class="btn small primary" data-act="create" ${idProblem || nameBlank ? 'disabled' : ''}>Create station</button>
        <button class="btn small ghost" data-act="collapse">Cancel</button>
      </div>
    </div>`;
  } else if (exp === 'ignore') {
    const blank = !(d.note ?? '').trim();
    form = `
    <div class="rv-form">
      <label class="rv-lab">Why is it left out?<input class="inp" data-f="note" data-pid="${esc(pid)}" data-key="o-${esc(pid)}" value="${esc(d.note ?? '')}" placeholder="e.g. a school-trip stop no rider uses"></label>
      <div class="rv-err quiet">A note is required: an ignored stop with no reason looks like a mistake.</div>
      <div class="rv-actions">
        <button class="btn small primary" data-act="ignore-confirm" ${blank ? 'disabled' : ''}>Ignore stop</button>
        <button class="btn small ghost" data-act="collapse">Cancel</button>
      </div>
    </div>`;
  }

  return `
  <div class="rv-item ${done ? 'done' : ''} ${exp ? 'open' : ''}" data-row="stop" data-pid="${esc(pid)}">
    <div class="rv-row1">
      <span class="rv-code mono" title="${esc(pid)}">${esc(upstream(pid))}</span>
      <span class="rv-name">${esc(u.name)}</span>
    </div>
    <div class="rv-lines">${lineChips(u.lines)}</div>
    ${done
      ? `<div class="rv-done">✓ ${done} <span>· unsaved</span></div>`
      : `<div class="rv-prop">→ ${proposalHtml(t)}${u.proposal.heading ? ` · ${esc(u.proposal.heading)}` : ' · <span class="rv-warn">no heading</span>'}</div>
         <div class="rv-reason">${esc(u.proposal.reason)}</div>
         ${exp ? '' : `<div class="rv-actions">
           ${headingSelect}
           <button class="btn small primary" data-act="accept" ${t.missing ? 'disabled' : ''}
             title="${t.create ? 'Create the proposed station (you can change its id and name first)' : 'Add this stop to the proposed station'}">${t.create ? 'Accept…' : 'Accept'}</button>
           <button class="btn small" data-act="other" title="Pick another station, nearest first">Other…</button>
           <button class="btn small ghost" data-act="ignore" title="Leave it out, with a note saying why">Ignore…</button>
         </div>`}
         ${form}`}
  </div>`;
}

function deadStopRow(d) {
  const done = deadStopDone(d);
  const plan = done ? null : removal(d);
  return `
  <div class="rv-item ${done ? 'done' : ''}" data-row="dead-stop" data-pid="${esc(d.stop)}" data-platform="${esc(d.platform)}" data-sid="${esc(d.station)}">
    <div class="rv-row1">
      <span class="rv-code mono" title="${esc(d.stop)}">${esc(upstream(d.stop))}</span>
      <span class="rv-name">${stationLink(d.station, d.stationName)}</span>
      <span class="rv-meta">${esc(d.heading)}</span>
    </div>
    ${d.platform !== d.stop ? `<div class="rv-reason">An extra stop of platform <code>${esc(upstream(d.platform))}</code>, which stays</div>` : ''}
    ${done
      ? `<div class="rv-done">✓ ${done} <span>· unsaved</span></div>`
      : `<div class="rv-actions">
          <button class="btn small" data-act="remove-stop" ${plan.blocked ? `disabled title="${esc(plan.blocked)}"` : `title="${esc(plan.says)}"`}>Remove from station</button>
        </div>`}
  </div>`;
}

/** What removing a dead stop does to its platform. A platform is its primary stop
 *  plus any extras (two 511 ids on one shelter), so there are three cases: an extra
 *  just leaves the list; a primary with no extras takes the platform with it; and
 *  a primary with extras hands the platform to the next stop, which becomes its id,
 *  so the heading, signage and note it carries are not lost with the dead id. */
function removal(d) {
  const st = stationById(d.station);
  const p = st && platformsOf(st).find(x => x.id === d.platform);
  if (!p) return { blocked: 'Its platform is not in this edit' };
  if (d.stop !== p.id) return { kind: 'extra', says: `Drop it from platform ${upstream(p.id)}` };
  if ((p.stops || []).length) return { kind: 'promote', next: p.stops[0], says: `${upstream(p.stops[0])} becomes the platform's id` };
  if (platformsOf(st).length === 1) return { blocked: 'Its station has no other platform: delete the station instead' };
  return { kind: 'platform', says: 'Remove the platform' };
}

function stationRow(d) {
  const done = stationDone(d);
  const inbound = done ? [] : inboundTransfers(d.station);
  const subways = done ? [] : subwaysWith(d.station);
  const exp = expanded?.key === d.station && expanded.kind === 'delete';
  const names = ids => ids.map(id => esc(stationById(id)?.name || id)).join(', ');
  return `
  <div class="rv-item ${done ? 'done' : ''} ${exp ? 'open' : ''}" data-row="station" data-sid="${esc(d.station)}">
    <div class="rv-row1">
      <span class="rv-name">${done ? esc(d.name) : stationLink(d.station, d.name)}</span>
      <code class="rv-meta">${esc(d.station)}</code>
    </div>
    <div class="rv-reason">Stops ${d.stops.map(p => `<code>${esc(upstream(p))}</code>`).join(' ')}, none in 511</div>
    ${done ? `<div class="rv-done">✓ ${done} <span>· unsaved</span></div>` : `
      ${inbound.length ? `<div class="rv-warn-box">${inbound.length} station${inbound.length === 1 ? ' transfers' : 's transfer'} here: ${names(inbound)}</div>` : ''}
      ${exp ? `<div class="rv-form">
          <div>Deleting <b>${esc(d.name)}</b> also removes
            ${[inbound.length ? `the transfer${inbound.length === 1 ? '' : 's'} from ${names(inbound)}` : '',
               subways.length ? `its place in subway${subways.length === 1 ? '' : 's'} ${subways.map(s => `<code>${esc(s)}</code>`).join(', ')}` : '']
              .filter(Boolean).join(', and ')}.
            Its id <code>${esc(d.station)}</code> is retired with it.</div>
          <div class="rv-actions">
            <button class="btn small danger" data-act="delete-confirm">Delete station</button>
            <button class="btn small ghost" data-act="collapse">Cancel</button>
          </div>
        </div>` : `<div class="rv-actions">
          <button class="btn small" data-act="delete-station">Delete station</button>
        </div>`}`}
  </div>`;
}

// ======================================================================= wiring
function wireQueue(body) {
  body.querySelectorAll('details.rv-sect').forEach(el => {
    el.addEventListener('toggle', () => {
      if (el.open) folded.delete(el.dataset.sect); else folded.add(el.dataset.sect);
    });
  });

  // Inputs keep their text in the drafts on every keystroke, but only re-render
  // (to update the form's own checks) when a check's answer would change.
  body.querySelectorAll('[data-f]').forEach(el => {
    const pid = el.dataset.pid, f = el.dataset.f;
    const update = () => {
      const d = draftOf(pid);
      const before = formValid(pid);
      d[f] = el.value;
      if (f === 'heading' || formValid(pid) !== before || f === 'id') renderReview();
    };
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', update);
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); body.querySelector(`.rv-item[data-pid="${CSS.escape(pid)}"] .rv-form .btn.primary:not(:disabled)`)?.click(); }
      if (e.key === 'Escape') { e.stopPropagation(); expanded = null; renderReview(); }
    });
  });

  body.querySelectorAll('.rv-item').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.closest('button,input,select,textarea,a')) return;
      goTo(row);
    });
  });

  body.querySelectorAll('[data-act]').forEach(b => {
    b.addEventListener('click', e => {
      e.stopPropagation();
      const row = b.closest('.rv-item');
      act(b.dataset.act, row, b);
    });
  });
}

function formValid(pid) {
  const d = draftOf(pid);
  if (expanded?.kind === 'new') return !stationIdProblem((d.id ?? '').trim()) && !!(d.name ?? '').trim();
  if (expanded?.kind === 'ignore') return !!(d.note ?? '').trim();
  return true;
}

const stopOf = pid => review?.unassigned.find(u => u.stop === pid);

function goTo(row) {
  const kind = row.dataset.row;
  if (kind === 'stop') {
    const u = stopOf(row.dataset.pid);
    if (!u) return;
    // A stop no station owns has no pole to select, so it is ringed instead:
    // the same ring an `unassigned-stop` issue gets.
    select(null, null);
    flyTo([u.lon, u.lat], 17);
    spotlight(u.stop);
    return;
  }
  const sid = row.dataset.sid;
  if (!stationById(sid)) return;
  select(sid, kind === 'dead-stop' ? row.dataset.platform : null);
  flyToStation(sid);
  if (kind === 'station') hint(`${stationById(sid).name} has no live platforms, so no position on the map`);
}

function act(what, row, button) {
  if (what === 'goto-station') {
    const sid = button.dataset.sid;
    if (!stationById(sid)) { hint(`${sid} is not in this edit`); return; }
    select(sid, null);
    flyToStation(sid);
    return;
  }
  if (what === 'collapse') { expanded = null; renderReview(); return; }

  if (row.dataset.row === 'stop') {
    const u = stopOf(row.dataset.pid);
    if (u) stopAction(what, u, row);
    return;
  }
  if (what === 'remove-stop') removeDeadStop(looseStops().find(d => d.stop === row.dataset.pid));
  if (what === 'delete-station') {
    const sid = row.dataset.sid;
    // Only worth a second step when the deletion reaches past the station itself.
    if (inboundTransfers(sid).length || subwaysWith(sid).length) {
      expanded = { key: sid, kind: 'delete' };
      renderReview();
    } else {
      deleteStation(sid);
    }
  }
  if (what === 'delete-confirm') deleteStation(row.dataset.sid);
}

function needHeading(row) {
  const sel = row.querySelector('.rv-heading');
  sel?.classList.add('bad');
  sel?.focus();
  hint('Choose a heading first: no line stops here, so there is no direction of travel to read one from', 3600);
}

function stopAction(what, u, row) {
  const pid = u.stop;
  const heading = headingOf(u);

  if (what === 'accept') {
    if (!heading) { needHeading(row); return; }
    const t = target(u);
    if (t.create) {
      const d = draftOf(pid);
      d.id ??= t.create.id;
      d.name ??= t.create.name;
      expanded = { key: pid, kind: 'new' };
      renderReview();
      $('review-body').querySelector(`[data-key="i-${CSS.escape(pid)}"]`)?.focus();
      return;
    }
    if (t.missing) return;
    assign(u, t.sid, heading);
  }

  if (what === 'create') createStation(u, heading);

  if (what === 'other') {
    if (!heading) { needHeading(row); return; }
    openPalette('station', sid => { if (sid) assign(u, sid, heading); }, {
      near: [u.lon, u.lat],
      placeholder: `Station for ${upstream(pid)} ${u.name}, nearest first…`,
    });
  }

  if (what === 'ignore') {
    expanded = { key: pid, kind: 'ignore' };
    renderReview();
    $('review-body').querySelector(`[data-key="o-${CSS.escape(pid)}"]`)?.focus();
  }

  if (what === 'ignore-confirm') {
    const note = (draftOf(pid).note ?? '').trim();
    if (!note) return;
    const ok = edit(`Ignore ${upstream(pid)}`, c => {
      (c.ignored || (c.ignored = {}))[pid] = { note };
    });
    if (ok) { expanded = null; hint(`${upstream(pid)} ignored`); }
  }
}

/** Append the stop to a station, which clears that station's `verified` (store.js). */
function assign(u, sid, heading) {
  const st = stationById(sid);
  if (!st) return;
  const ok = edit(`Assign ${upstream(u.stop)} to ${st.name}`, c => {
    c.stations.stations[sid].platforms.push({ id: u.stop, heading });
  });
  if (ok) { expanded = null; hint(`${upstream(u.stop)} → ${st.name}`); }
}

function createStation(u, heading) {
  const pid = u.stop;
  const d = draftOf(pid);
  const id = (d.id ?? '').trim(), name = (d.name ?? '').trim();
  const problem = stationIdProblem(id);
  if (problem || !name || !heading) { renderReview(); return; }
  const ok = edit(`New station ${id} for ${upstream(pid)}`, c => {
    c.stations.stations[id] = { name, platforms: [{ id: pid, heading }] };
  });
  if (!ok) return;
  minted.set(u.proposal.newStation.id, id);
  expanded = null;
  hint(`${name} created with ${upstream(pid)}`);
}

function removeDeadStop(d) {
  if (!d) return;
  const plan = removal(d);
  if (plan.blocked) { hint(plan.blocked); return; }
  const st = stationById(d.station);
  const ok = edit(`Remove ${upstream(d.stop)} from ${st.name}`, c => {
    const s = c.stations.stations[d.station];
    const i = s.platforms.findIndex(p => p.id === d.platform);
    const p = s.platforms[i];
    if (plan.kind === 'extra') {
      p.stops = p.stops.filter(x => x !== d.stop);
      if (!p.stops.length) delete p.stops;
    } else if (plan.kind === 'promote') {
      const [next, ...rest] = p.stops;
      p.id = next;
      if (rest.length) p.stops = rest; else delete p.stops;
    } else {
      s.platforms.splice(i, 1);
    }
  });
  if (!ok) return;
  // A promoted platform is the same platform under its next stop's id.
  if (store.selPlatform === d.platform) select(d.station, plan.kind === 'promote' ? plan.next : null);
  hint(`${upstream(d.stop)} removed · ${plan.says}`);
}

function deleteStation(sid) {
  const st = stationById(sid);
  if (!st) return;
  const ok = edit(`Delete ${sid}`, c => deleteStationIn(c, sid));
  if (!ok) return;
  expanded = null;
  if (store.selStation === sid) select(null, null);
  hint(`${st.name} deleted`);
}
