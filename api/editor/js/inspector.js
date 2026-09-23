// Right-hand inspector. It shows the selected station (its flat list of
// platforms, transfers and verification) or, with no station selected, the
// focused line's overrides. Coordinates, stop names and the lines serving a
// platform are shown but never editable: they come from 511, not from a person.
//
// On the public /map/ it is a plain read-only view: no form fields, and none of
// the curators' own fields (notes, verification), which describe the editing
// rather than the station.

import {
  store, edit, select, stationById, stationIds, linesOf, lineById, allLines, esc,
  metresBetween, platformsOf, stopsOf, platformIdOf, derivedStation, derivedStop, stationPos,
  stopPos, upstream, unclaimedNear, today, lineOverride, snapshotLine,
  setLineOverride, knownModes, setActiveLine, transferPartners, deleteStationIn,
  transfersOf, findTransfer, addTransferIn, removeTransferIn,
  mergeStationsIn, movePlatformIn, foldPlatformIn, stationsNear, suggestStationId, stationIdProblem,
} from './store.js';
import { hint, highlightLink, flyTo, showCandidates, onCandidate, fitLine } from './map.js';

export const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
/** 511 operator codes, as `transferAgencies` stores them (Embarcadero has BA). */
const AGENCIES = [['BA', 'BART'], ['CT', 'Caltrain']];
const ARROW = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };
/** Line chips in the station header before the rest fold into "+N": a
 *  downtown stop served by twenty bus lines must not become a wall of chips. */
const MAX_HEADER_LINES = 12;
const MAX_PLATFORM_LINES = 10;
const CANDIDATES_SHOWN = 12;

let pickStationFor = null;
let pickLineFor = null;
let addingFor = null;        // station id whose add-platform picker is open
/** An open "merge with station" form: `{sid, other, keep, name}`. */
let merging = null;
/** An open "move platform" row: `{sid, pid, step: 'choose' | 'new', id, name}`. */
let moving = null;
/** How far "Merge into platform…" looks for other stations' platforms. Two ids on
 *  one shelter sit metres apart; 150 m is well past any shelter and short of the
 *  next block's stop. */
const FOLD_REACH_M = 150;
let addQuery = '';

export function initInspector({ onNeedStationPicker, onNeedLinePicker }) {
  pickStationFor = onNeedStationPicker;
  pickLineFor = onNeedLinePicker;
  document.getElementById('insp-close').onclick = () => {
    if (store.selStation) select(null, null);
    else { store.lineInspector = false; }
    closeAdd();
    renderInspector();
  };
  onCandidate(pid => { if (addingFor) addPlatform(addingFor, pid); });
}

/** Close the add-platform picker, if it is open. Returns whether it was. */
export function closeAdd() {
  if (!addingFor) return false;
  addingFor = null;
  addQuery = '';
  showCandidates(null);
  return true;
}

export function renderInspector() {
  const el = document.getElementById('inspector');
  const st = stationById(store.selStation);
  // The line form is overrides, all editorial, so /map/ never opens it.
  const ln = !st && store.lineInspector && !store.publicMap ? lineById(store.activeLine) : null;
  if (addingFor && addingFor !== store.selStation) closeAdd();

  if (!st && !ln) {
    el.classList.add('closed');
    document.body.classList.add('no-inspector');
    document.body.classList.remove('inspector-open');
    return;
  }
  el.classList.remove('closed');
  document.body.classList.remove('no-inspector');
  document.body.classList.add('inspector-open');

  // A validate answer redraws the inspector while someone may be typing in it,
  // so the focused field, its unsent text and the caret survive the redraw.
  const keep = focusState();
  if (st) renderStation(st, store.selStation);
  else renderLine(ln);
  restoreFocus(keep);
}

function focusState() {
  const a = document.activeElement;
  const key = a?.closest?.('#inspector') && a.dataset?.key;
  if (!key) return null;
  return { key, value: a.value, checked: a.checked, start: a.selectionStart, end: a.selectionEnd };
}

function restoreFocus(keep) {
  if (!keep) return;
  const el = document.querySelector(`#inspector [data-key="${CSS.escape(keep.key)}"]`);
  if (!el) return;
  if (el.type === 'checkbox') el.checked = keep.checked;
  else if (el.value !== keep.value) el.value = keep.value;
  el.focus();
  try { if (keep.start != null) el.setSelectionRange(keep.start, keep.end); } catch { /* selects */ }
}

const sub = text => `<span class="sub-label">— ${text}</span>`;

// ======================================================================= station
function renderStation(st, sid) {
  const el = document.getElementById('inspector');
  const ls = linesOf(sid);
  el.style.setProperty('--accent', ls[0]?.color || '#6ea8fe');

  document.getElementById('insp-name').textContent = st.name;
  const n = platformsOf(st).length;
  const plats = `${n} platform${n === 1 ? '' : 's'}`;
  document.getElementById('insp-id').innerHTML = store.publicMap ? plats
    : `${esc(sid)} · ${plats} · ${st.verified
      ? `<span class="vpill">verified ${esc(st.verified)}</span>`
      : '<span class="vpill off">unverified</span>'}`;

  // Which lines serve a station is 511's to say, so these are links, not toggles.
  const box = document.getElementById('insp-lines');
  box.innerHTML = '';
  for (const ln of ls.slice(0, MAX_HEADER_LINES)) box.appendChild(lineChip(ln));
  if (ls.length > MAX_HEADER_LINES) {
    const more = document.createElement('span');
    more.className = 'mini-more';
    const rest = ls.slice(MAX_HEADER_LINES);
    more.textContent = `+${rest.length}`;
    more.title = rest.map(l => l.name).join('\n');
    box.appendChild(more);
  }

  if (merging && merging.sid !== sid) merging = null;
  if (moving && moving.sid !== sid) moving = null;
  document.getElementById('insp-body').innerHTML =
    (merging ? sectionMerge(sid) : '') +
    sectionStation(st, sid) +
    sectionPlatforms(st, sid) +
    sectionTransfers(st, sid) +
    sectionDanger(st);
  wireStation(st, sid);
}

function lineChip(ln) {
  const b = document.createElement('button');
  b.className = 'mini-bullet';
  b.style.background = ln.color || '#7c8598';
  if (ln.textColor) b.style.color = ln.textColor;
  b.textContent = ln.shortName || upstream(ln.id);
  b.title = `${ln.name} — focus this line`;
  b.onclick = () => { setActiveLine(ln.id); fitLine(ln.id); };
  return b;
}

function sectionStation(st, sid) {
  const d = derivedStation(sid);
  const coord = d && d.lat != null
    ? `<code>${d.lat}, ${d.lon}</code>`
    : '<span style="color:var(--warn)">no live platforms, so no coordinate</span>';
  if (store.publicMap) {
    // The name is the header; the id, notes and verification are the curators'.
    return `
  <div class="sect">
    <div class="field">
      <label class="micro">Coordinate ${sub('centre of its platforms')}</label>
      <div class="derived mono">${coord}</div>
    </div>
  </div>`;
  }
  const former = (st.formerIds || []).length
    ? ` · formerly ${st.formerIds.map(f => `<code>${esc(f)}</code>`).join(' ')}` : '';
  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Station</div></div>
    <div class="field">
      <label class="micro">Display name</label>
      <input class="inp" data-key="f-name" id="f-name" value="${esc(st.name)}">
    </div>
    <div class="field">
      <label class="micro">Identifier ${sub('permanent: the app stores it in favourites')}</label>
      <div class="derived mono"><code>${esc(sid)}</code>${former}</div>
    </div>
    <div class="field">
      <label class="micro">Coordinate ${sub('centre of its live platforms')}</label>
      <div class="derived mono">${coord}</div>
    </div>
    <div class="field">
      <label class="micro">Verified ${sub('checked on the map; cleared when platforms change')}</label>
      <div class="verify-row">
        ${st.verified
          ? `<span class="vpill">${esc(st.verified)}</span>
             <button class="tchip x-verify edit-only" data-act="unverify" title="Clear the date">Clear</button>`
          : '<span class="vpill off">not verified</span>'}
        <span class="grow"></span>
        ${st.verified === today() ? '' : `<button class="btn small edit-only" data-act="verify" title="Mark verified today (V)">
          Mark verified <span class="kbd">V</span></button>`}
      </div>
    </div>
    <div class="field">
      <label class="check"><input type="checkbox" id="f-hub" data-key="f-hub" ${st.hub ? 'checked' : ''}>
        Hub ${sub('a major metro interchange, drawn large and white')}</label>
    </div>
    <div class="field">
      <label class="micro">Note ${sub('what was checked, and how')}</label>
      <textarea class="inp" data-key="f-note" id="f-note" rows="2" placeholder="none">${esc(st.note ?? '')}</textarea>
    </div>
  </div>`;
}

// ---------------------------------------------------------------- platforms
function sectionPlatforms(st, sid) {
  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Platforms · ${platformsOf(st).length}</div>
      <button class="icon-btn edit-only ${addingFor === sid ? 'on' : ''}" data-act="add-plat" title="Add a platform from an unclaimed 511 stop">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${addingFor === sid ? addPicker(sid) : ''}
    ${platformCards(st) || '<div class="empty">No platforms.</div>'}
  </div>`;
}

/** Unclaimed 511 stops, nearest first, filtered by id or stop name. */
function candidates(sid) {
  const at = stationPos(sid) || null;
  const q = addQuery.trim().toLowerCase();
  const all = unclaimedNear(at);
  return q
    ? all.filter(c => `${c.id} ${c.p.name || ''}`.toLowerCase().includes(q))
    : all;
}

function addPicker(sid) {
  const list = candidates(sid);
  const rows = list.slice(0, CANDIDATES_SHOWN).map(c => {
    const lines = c.p.lines.map(lineById).filter(Boolean);
    return `
    <button class="cand" data-act="pick-cand" data-pid="${esc(c.id)}">
      <span class="cand-code mono">${esc(upstream(c.id))}</span>
      <span class="cand-name">${esc(c.p.name || '')}</span>
      <span class="cand-lines">${lines.slice(0, 5).map(l =>
        `<i style="background:${esc(l.color || '#7c8598')}" title="${esc(l.name)}"></i>`).join('')}</span>
      <em class="mono">${stationPos(sid) ? `${c.metres} m` : ''}</em>
    </button>`;
  }).join('');
  return `
  <div class="add-picker">
    <input class="inp" data-key="add-q" id="add-q" placeholder="Stop id or 511 name…" value="${esc(addQuery)}" autocomplete="off" spellcheck="false">
    <div class="cand-list">${rows || '<div class="empty">No unclaimed stops match.</div>'}</div>
    <div class="cand-foot">${list.length} unclaimed stop${list.length === 1 ? '' : 's'}${
      list.length > CANDIDATES_SHOWN ? `, nearest ${CANDIDATES_SHOWN} shown` : ''} · also on the map
      <button class="tchip" data-act="cancel-add">Cancel</button></div>
  </div>`;
}

function platformCards(st) {
  const all = platformsOf(st);
  return all.map(p => {
    const pid = p.id;
    const d = derivedStop(pid);
    const live = !!d?.live;
    const sel = !!store.selPlatform && platformIdOf(store.selPlatform) === pid;
    const at = stopPos(pid);
    const stops = stopsOf(p);

    // A platform serves every line any of its stops does, in line order.
    const lineIds = new Set(stops.flatMap(x => derivedStop(x)?.lines || []));
    const served = allLines().filter(l => lineIds.has(l.id));
    const pub = store.publicMap;
    const lines = served.slice(0, MAX_PLATFORM_LINES).map(ln =>
      `<button class="mini-bullet" style="background:${esc(ln.color || '#7c8598')}" disabled
        title="${esc(ln.name)}">${esc(ln.shortName || upstream(ln.id))}</button>`).join('')
      + (served.length > MAX_PLATFORM_LINES ? `<span class="mini-more">+${served.length - MAX_PLATFORM_LINES}</span>` : '')
      || '<span class="empty">none</span>';

    return `
    <div class="plat ${sel ? 'sel' : ''} ${live ? '' : 'dead'}" data-pid="${esc(pid)}">
      <div class="plat-head">
        <div class="compass" title="${esc(p.heading)}">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style="transform:rotate(${ARROW[p.heading] ?? 0}deg)">
            <path d="M8 2.2l3.6 10-3.6-2.6-3.6 2.6L8 2.2z" fill="currentColor"/></svg>
        </div>
        <div style="min-width:0">
          <div class="plat-code" title="${esc(pid)}">${esc(upstream(pid))}${live ? ''
            : ' <span class="dead-tag" title="511 no longer lists this stop. The API leaves it out; it is not removed automatically.">not in 511</span>'}</div>
          <div class="plat-sub">${esc(d?.name || p.name || st.name)}</div>
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate" data-pid="${esc(pid)}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
          </button>
          <button class="icon-btn edit-only ${moving?.pid === pid ? 'on' : ''}" data-act="move-plat" data-pid="${esc(pid)}" title="Move this platform to another station, or to a new one">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3 8h9M9 4.5L12.5 8 9 11.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="icon-btn danger edit-only" data-act="del-plat" data-pid="${esc(pid)}" title="Remove this platform">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8h4.8l.6-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>

      ${pub ? `
      <div class="field">
        <label class="micro">Heading</label>
        <div class="derived">${esc(p.heading)}${p.name ? ` · signed <b>${esc(p.name)}</b>` : ''}</div>
      </div>` : `
      <div class="field">
        <label class="micro">Heading</label>
        <select class="inp" data-pf="heading" data-pid="${esc(pid)}" data-key="h-${esc(pid)}">
          ${HEADINGS.map(h => `<option value="${h}" ${p.heading === h ? 'selected' : ''}>${h}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label class="micro">Signage ${sub('e.g. Platform 1; not the 511 name')}</label>
        <input class="inp" data-pf="name" data-pid="${esc(pid)}" data-key="n-${esc(pid)}" value="${esc(p.name ?? '')}" placeholder="none">
      </div>
      <div class="field">
        <label class="micro">Note</label>
        <input class="inp" data-pf="note" data-pid="${esc(pid)}" data-key="o-${esc(pid)}" value="${esc(p.note ?? '')}" placeholder="none">
      </div>`}
      ${moveRow(store.selStation, pid)}
      ${stopsField(p, all)}
      <div class="field">
        <label class="micro">Lines ${sub('from 511, over all its stops')}</label>
        <div class="insp-lines">${lines}</div>
      </div>
      <div class="field" style="margin-bottom:0">
        <label class="micro">Coordinate ${sub('from 511')}</label>
        <div class="derived mono">${at
          ? `<code>${at[1]}, ${at[0]}</code>`
          : '<span style="color:var(--warn)">none: not in 511\'s current data</span>'}</div>
      </div>
    </div>`;
  }).join('');
}

/**
 * A platform's 511 stops: usually just its own id. Where 511 numbers one place
 * more than once (the N and its bus substitute on one Duboce shelter), the
 * others are listed here and can be split back out into platforms of their own.
 * "Merge into platform" does the opposite: this platform's stops join another
 * platform, of this station or a nearby one, which keeps its own heading,
 * signage and note.
 */
function stopsField(p, all) {
  const sid = store.selStation;
  const stops = stopsOf(p);
  const chips = stops.map((x, i) => {
    const d = derivedStop(x);
    const far = i && stopPos(p.id) && stopPos(x) ? metresBetween(stopPos(p.id), stopPos(x)) : null;
    return `<span class="tchip link" title="${esc(d?.name || '')}${far !== null ? ` · ${far} m from ${upstream(p.id)}` : ''}">
      <span class="mono">${esc(upstream(x))}</span>${i ? '' : ' <em>primary</em>'}${far !== null ? ` <em>${far} m</em>` : ''}${
      d?.live ? '' : ' <em style="color:var(--warn)">not in 511</em>'}${
      i ? `<button class="x edit-only" data-act="split-stop" data-pid="${esc(p.id)}" data-stop="${esc(x)}"
        title="Make ${esc(upstream(x))} a platform of its own again">split out</button>` : ''}</span>`;
  }).join('');

  const option = (osid, q) => `<option value="${esc(osid)}|${esc(q.id)}">${esc(upstream(q.id))} · ${esc(q.heading)}${
    q.name ? ` · ${esc(q.name)}` : ''}${osid === sid ? '' : ` · ${esc(stationById(osid).name)}`}</option>`;
  const here = all.filter(q => q.id !== p.id).map(q => option(sid, q)).join('');
  const nearby = stationsNear(stopPos(p.id), FOLD_REACH_M, sid)
    .map(({ id }) => platformsOf(stationById(id)).map(q => option(id, q)).join('')).join('');
  const merge = store.publicMap || (!here && !nearby) ? '' : `
    <select class="inp edit-only" data-act-change="merge-into" data-pid="${esc(p.id)}" data-key="m-${esc(p.id)}"
      title="511 numbers this place more than once: fold this platform's stops into another platform">
      <option value="">Merge into platform…</option>
      ${here ? `<optgroup label="This station">${here}</optgroup>` : ''}
      ${nearby ? `<optgroup label="Within ${FOLD_REACH_M} m">${nearby}</optgroup>` : ''}
    </select>`;
  return `
      <div class="field">
        <label class="micro">Stops ${sub(stops.length > 1 ? 'one place, several 511 ids' : '511 stop id')}</label>
        <div class="chips">${chips}</div>
        ${merge}
      </div>`;
}

// ---------------------------------------------------------------- transfers
// Both-ways by construction: a transfer is one pair, so there is no direction to
// show and nothing to reciprocate.
const TMODE = {
  street: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M3 5h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-dasharray="0.1 2.6"/><path d="M11.5 2L14.5 5l-3 3M4.5 2L1.5 5l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  indoor: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M3 5h10" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M11.5 2L14.5 5l-3 3M4.5 2L1.5 5l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function sectionTransfers(st, sid) {
  const here = stationPos(sid);

  const rows = transfersOf(sid).map(t => {
    const to = stationById(t.to);
    const there = stationPos(t.to);
    const away = here && there ? `${metresBetween(here, there)} m` : '—';
    return `
    <span class="tchip link both" data-act="hover-link" data-id="${esc(t.to)}">
      <i class="dir">${TMODE[t.mode] || TMODE.street}</i>
      <span class="nm">${esc(to ? to.name : t.to)}</span>
      <em>${t.mode === 'indoor' ? 'indoor' : away}</em>
      <button class="x" data-act="cycle-mode" data-id="${esc(t.to)}" title="Switch to ${t.mode === 'indoor' ? 'street' : 'indoor'}">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 4.5h6.5M6.5 2.5L8.8 4.5 6.5 6.5M10 7.5H3.5M5.5 5.5L3.2 7.5 5.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <button class="x" data-act="untransfer" data-id="${esc(t.to)}" title="Remove the transfer (both ways)">
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </button>
    </span>`;
  }).join('');

  const agencies = AGENCIES.map(([ag, label]) => {
    const on = (st.transferAgencies || []).includes(ag);
    return `<button class="tchip ${on ? 'agency-on' : 'add'}" data-act="agency" data-ag="${ag}" title="${label} (${ag})">${label}</button>`;
  }).join('');
  // Read-only hides the agencies not set, so with none set there is nothing to show.
  const anyAgency = AGENCIES.some(([ag]) => (st.transferAgencies || []).includes(ag));

  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Transfers</div></div>
    ${rows ? `<div class="field"><div class="chips">${rows}</div></div>` : '<div class="empty" style="margin-bottom:10px">No transfers.</div>'}
    <div class="field">
      <button class="tchip add" data-act="add-transfer" style="width:100%;justify-content:center;height:29px">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        Add a transfer
      </button>
    </div>
    ${store.readOnly && !anyAgency ? '' : `<div class="field" style="margin-top:12px">
      <label class="micro">Other agencies ${sub(store.publicMap ? 'transfer here' : 'no platforms in the data yet')}</label>
      <div class="chips agencies">${agencies}</div>
    </div>`}
  </div>`;
}

function sectionDanger(st) {
  return `
  <div class="sect edit-only">
    <div class="sect-head"><div class="micro">Station</div></div>
    <button class="btn" data-act="merge-start" style="width:100%;justify-content:center;margin-bottom:8px"
      title="Fold another station into this one, or this one into it">Merge with station…</button>
    <button class="btn" id="del-station" style="width:100%;justify-content:center;color:var(--bad);border-color:rgba(251,113,133,.3)">
      Delete “${esc(st.name)}”
    </button>
  </div>`;
}

/**
 * Merging two stations: which one stays, and its name. The one that goes keeps
 * working as an id (it becomes a former id of the one that stays), so this is
 * about which id is the real one from now on, not about losing anything.
 */
function sectionMerge(sid) {
  const ids = [merging.sid, merging.other];
  const keep = merging.keep, gone = ids.find(x => x !== keep);
  const K = stationById(keep), G = stationById(gone);
  const n = platformsOf(G).length;
  const choice = id => {
    const st = stationById(id);
    const np = platformsOf(st).length;
    return `<label class="merge-choice ${id === keep ? 'on' : ''}">
      <input type="radio" name="merge-keep" value="${esc(id)}" ${id === keep ? 'checked' : ''}>
      <span><b>${esc(st.name)}</b> <code>${esc(id)}</code><em>${np} platform${np === 1 ? '' : 's'}</em></span>
    </label>`;
  };
  return `
  <div class="sect edit-only merge-form">
    <div class="sect-head"><div class="micro">Merge stations</div></div>
    <div class="field"><label class="micro">Keep the id of</label>${ids.map(choice).join('')}</div>
    <div class="field"><label class="micro">Name</label>
      <input class="inp" id="merge-name" value="${esc(merging.name)}" data-key="merge-name"></div>
    <div class="merge-says">${esc(G.name)}'s ${n} platform${n === 1 ? '' : 's'} join ${esc(K.name)}.
      <code>${esc(gone)}</code> becomes a former id of <code>${esc(keep)}</code>, so the API redirects it and saved
      favourites follow. Its transfers move to ${esc(K.name)}${findTransfer(keep, gone) ? ', and the transfer between the two goes' : ''}.</div>
    <div class="rv-actions">
      <button class="btn small primary" data-act="merge-confirm">Merge</button>
      <button class="btn small ghost" data-act="merge-cancel">Cancel</button>
    </div>
  </div>`;
}

/** The inline row a platform card shows while it is being moved to another station. */
function moveRow(sid, pid) {
  if (!moving || moving.sid !== sid || moving.pid !== pid) return '';
  if (moving.step === 'new') {
    const problem = stationIdProblem((moving.id || '').trim());
    return `
    <div class="move-row edit-only">
      <div class="micro">A new station with ${esc(upstream(pid))} ${sub('the id is permanent: the app keeps it in favourites')}</div>
      <label class="rv-lab">Id<input class="inp mono ${problem ? 'bad' : ''}" id="move-id" value="${esc(moving.id || '')}" data-key="move-id" spellcheck="false" autocomplete="off"></label>
      <div class="rv-err" id="move-id-err">${problem ? esc(problem) : ''}</div>
      <label class="rv-lab">Name<input class="inp" id="move-name" value="${esc(moving.name || '')}" data-key="move-name"></label>
      <div class="rv-actions">
        <button class="btn small primary" data-act="move-new-confirm">Create station</button>
        <button class="btn small ghost" data-act="move-cancel">Cancel</button>
      </div>
    </div>`;
  }
  return `
    <div class="move-row edit-only">
      <div class="micro">Move ${esc(upstream(pid))} to…</div>
      <div class="rv-actions">
        <button class="btn small" data-act="move-existing" data-pid="${esc(pid)}">An existing station…</button>
        <button class="btn small" data-act="move-new" data-pid="${esc(pid)}">A new station…</button>
        <button class="btn small ghost" data-act="move-cancel">Cancel</button>
      </div>
    </div>`;
}

/** Add a snapshot stop to a station as a new platform. */
function addPlatform(sid, pid) {
  const st = stationById(sid);
  if (!st) return;
  // Only what a person decides. Its coordinate, stop name and lines are 511's;
  // the heading is required and cannot be derived, so it starts as a guess the
  // card highlights for fixing.
  const ok = edit(`Add platform ${upstream(pid)} to ${st.name}`, c => {
    c.stations.stations[sid].platforms.push({ id: pid, heading: 'northbound' });
  });
  if (!ok) return;
  closeAdd();
  select(sid, pid);
  hint(`Platform ${upstream(pid)} added — set its heading`);
}

/** Mark the selected station verified today. Used by the V key as well. */
export function markVerified(sid = store.selStation) {
  const st = stationById(sid);
  if (!st || store.readOnly) return false;
  const d = today();
  if (st.verified === d) { hint('Already verified today'); return false; }
  return edit(`Verify ${st.name}`, c => { c.stations.stations[sid].verified = d; });
}

// ==========================================================================
// Wiring. Every handler commits through edit(), so one gesture is one undo,
// and the server recomputes derived values and validation afterwards.
// ==========================================================================
function wireStation(st, sid) {
  const body = document.getElementById('insp-body');
  const commit = (label, fn) => edit(label, c => fn(c.stations.stations[sid], c));
  const plat = (s, pid) => platformsOf(s).find(x => x.id === pid);

  // ---------------------------------------------------------------- station
  const name = body.querySelector('#f-name');
  if (name) name.onchange = () => {
    const v = name.value.trim();
    // A blank name fails the model, so the server could not even validate it.
    if (!v || v === st.name) { name.value = st.name; return; }
    commit(`Rename ${sid}`, s => { s.name = v; });
  };

  const hub = body.querySelector('#f-hub');
  if (hub) hub.onchange = () => {
    if (hub.checked === !!st.hub) return;
    // Unset rather than false, so the file only says what someone decided.
    commit(`${hub.checked ? 'Hub' : 'Not a hub'}: ${st.name}`, s => { if (hub.checked) s.hub = true; else delete s.hub; });
  };

  const note = body.querySelector('#f-note');
  if (note) note.onchange = () => {
    const v = note.value.trim();
    if (v === (st.note ?? '')) return;
    commit(`Note on ${st.name}`, s => { if (v) s.note = v; else delete s.note; });
  };

  // -------------------------------------------------------------- platforms
  body.querySelectorAll('[data-pf]').forEach(el => {
    el.onchange = () => {
      const pid = el.dataset.pid, f = el.dataset.pf;
      const v = el.value.trim();
      const label = { heading: 'Heading', name: 'Signage', note: 'Note' }[f];
      commit(`${label} of ${upstream(pid)}`, s => {
        const p = plat(s, pid);
        if (v) p[f] = v; else delete p[f];
      });
    };
  });

  // Fold one platform into another: its stops become the other's extra stops,
  // and it goes. The other keeps its heading, signage and note.
  body.querySelectorAll('[data-act-change="merge-into"]').forEach(el => {
    el.onchange = () => {
      const from = el.dataset.pid;
      const [to, into] = el.value.split('|');
      if (!into) return;
      if (to !== sid && platformsOf(st).length === 1) {
        el.value = '';
        hint(`${upstream(from)} is ${st.name}'s only platform: merge the stations instead`, 4200);
        return;
      }
      edit(`${upstream(from)} merged into platform ${upstream(into)}`, c => foldPlatformIn(c, sid, from, to, into));
      select(to, into);
    };
  });

  // The merge form and the move row keep what is typed without re-rendering on
  // every key, which would drop the focus.
  body.querySelectorAll('input[name="merge-keep"]').forEach(r => {
    r.onchange = () => { merging.keep = r.value; merging.name = stationById(r.value).name; renderInspector(); };
  });
  const mname = body.querySelector('#merge-name');
  if (mname) mname.oninput = () => { merging.name = mname.value; };
  const mid = body.querySelector('#move-id');
  if (mid) mid.oninput = () => {
    moving.id = mid.value;
    const problem = stationIdProblem(mid.value.trim());
    mid.classList.toggle('bad', !!problem);
    body.querySelector('#move-id-err').textContent = problem || '';
  };
  const mnm = body.querySelector('#move-name');
  if (mnm) mnm.oninput = () => { moving.name = mnm.value; };

  const q = body.querySelector('#add-q');
  if (q) {
    q.oninput = () => { addQuery = q.value; renderInspector(); };
    q.onkeydown = e => {
      if (e.key === 'Enter') {
        const first = candidates(sid)[0];
        if (first) addPlatform(sid, first.id);
      } else if (e.key === 'Escape') {
        e.stopPropagation();
        closeAdd(); renderInspector();
      }
    };
  }

  // ------------------------------------------------------------ row actions
  body.querySelectorAll('.plat[data-pid]').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]') || ev.target.closest('input,select,button,textarea')) return;
      select(sid, card.dataset.pid);
    });
  });

  body.querySelectorAll('[data-act="hover-link"]').forEach(chip => {
    chip.onmouseenter = () => highlightLink(sid, chip.dataset.id);
    chip.onmouseleave = () => highlightLink(null, null);
  });

  body.querySelectorAll('[data-act]').forEach(b => {
    const act = b.dataset.act;
    if (act === 'hover-link') return;
    b.onclick = ev => {
      ev.stopPropagation();
      const pid = b.dataset.pid;

      if (act === 'locate') {
        select(sid, pid);
        const at = stopPos(pid);
        if (at) flyTo(at, 17.4); else hint(`${upstream(pid)} has no coordinate`);
      }

      if (act === 'add-plat') {
        if (store.readOnly) return;
        if (addingFor === sid) closeAdd();
        else { addingFor = sid; addQuery = ''; showCandidates(sid); }
        renderInspector();
        body.querySelector('#add-q')?.focus();
      }

      if (act === 'pick-cand') addPlatform(sid, b.dataset.pid);
      if (act === 'cancel-add') { closeAdd(); renderInspector(); }

      if (act === 'verify') markVerified(sid);
      if (act === 'unverify') commit(`Unverify ${st.name}`, s => { delete s.verified; });

      // An extra stop becomes its own platform again, just after this one, facing
      // the same way until someone says otherwise.
      if (act === 'split-stop') {
        const stop = b.dataset.stop;
        commit(`Split ${upstream(stop)} out of ${upstream(pid)}`, s => {
          const p = plat(s, pid);
          p.stops = (p.stops || []).filter(x => x !== stop);
          if (!p.stops.length) delete p.stops;
          s.platforms.splice(s.platforms.indexOf(p) + 1, 0, { id: stop, heading: p.heading });
        });
        select(sid, stop);
      }

      if (act === 'del-plat') {
        if (platformsOf(st).length === 1) { hint('A station must keep at least one platform'); return; }
        commit(`Remove platform ${upstream(pid)}`, s => {
          s.platforms = s.platforms.filter(p => p.id !== pid);
        });
        if (store.selPlatform && platformIdOf(store.selPlatform) === pid) select(sid, null);
      }

      if (act === 'merge-start') {
        pickStationFor?.(other => {
          if (!other || other === sid) return;
          merging = { sid, other, keep: sid, name: st.name };
          renderInspector();
          document.getElementById('insp-body')?.scrollTo?.(0, 0);
        }, { near: stationPos(sid), placeholder: `Merge ${st.name} with…` });
      }
      if (act === 'merge-cancel') { merging = null; renderInspector(); }
      if (act === 'merge-confirm') {
        const { keep } = merging;
        const gone = keep === merging.sid ? merging.other : merging.sid;
        const newName = (merging.name || '').trim();
        if (!newName) { hint('A name is required'); return; }
        const ok = edit(`Merge ${gone} into ${keep}`, c => mergeStationsIn(c, keep, gone, newName));
        if (ok) { merging = null; select(keep, null); hint(`${gone} merged into ${keep}`); }
      }

      if (act === 'move-plat') {
        moving = moving?.pid === pid ? null : { sid, pid, step: 'choose' };
        renderInspector();
      }
      if (act === 'move-cancel') { moving = null; renderInspector(); }
      const onlyOne = () => {
        if (platformsOf(st).length > 1) return false;
        hint(`That is ${st.name}'s only platform: merge the stations, or rename this one`, 4200);
        return true;
      };
      if (act === 'move-existing') {
        if (onlyOne()) return;
        pickStationFor?.(to => {
          if (!to || to === sid) return;
          const ok = edit(`Move ${upstream(pid)} to ${to}`, c => movePlatformIn(c, sid, pid, to));
          if (ok) { moving = null; select(to, pid); hint(`${upstream(pid)} moved to ${stationById(to)?.name || to}`); }
        }, { near: stopPos(pid), placeholder: `Move ${upstream(pid)} to…` });
      }
      if (act === 'move-new') {
        if (onlyOne()) return;
        const name = derivedStop(pid)?.name || '';
        moving = { sid, pid, step: 'new', name, id: suggestStationId(name) };
        renderInspector();
        document.getElementById('move-id')?.focus();
      }
      if (act === 'move-new-confirm') {
        const id = (moving.id || '').trim(), name = (moving.name || '').trim();
        const problem = stationIdProblem(id);
        if (problem) { hint(problem); return; }
        if (!name) { hint('A name is required'); return; }
        const from = moving.pid;
        const ok = edit(`New station ${id} from ${upstream(from)}`, c => {
          c.stations.stations[id] = { name, platforms: [] };
          movePlatformIn(c, sid, from, id);
        });
        if (ok) { moving = null; select(id, from); hint(`${name} created`); }
      }

      if (act === 'untransfer') {
        const other = b.dataset.id;
        commit(`Remove transfer ${sid} ↔ ${other}`, (s, c) => removeTransferIn(c, sid, other));
      }

      if (act === 'cycle-mode') {
        const other = b.dataset.id;
        const to = findTransfer(sid, other)?.mode === 'indoor' ? 'street' : 'indoor';
        commit(`Transfer ${sid} ↔ ${other} → ${to}`, (s, c) => { findTransfer(sid, other, c).mode = to; });
      }

      if (act === 'agency') {
        const ag = b.dataset.ag;
        commit(`Toggle ${ag} at ${sid}`, s => {
          const list = s.transferAgencies || (s.transferAgencies = []);
          const k = list.indexOf(ag);
          if (k >= 0) list.splice(k, 1); else list.push(ag);
        });
      }

      if (act === 'add-transfer') {
        pickStationFor?.(target => {
          if (!target || target === sid) return;
          if (findTransfer(sid, target)) { hint('Already a transfer'); return; }
          commit(`Transfer ${sid} ↔ ${target}`, (s, c) => addTransferIn(c, sid, target));
        });
      }
    };
  });

  const del = body.querySelector('#del-station');
  if (del) del.onclick = () => {
    const partners = transferPartners(sid).map(id => stationById(id)?.name || id);
    const links = partners.length
      ? `\n\nIts transfer${partners.length === 1 ? '' : 's'} with ${partners.join(', ')} go${partners.length === 1 ? 'es' : ''} too.`
      : '';
    if (!confirm(`Delete "${st.name}"?${links}\n\nIt is also removed from every subway and transfer that references it. Its id cannot be reused by another station without breaking saved favourites.`)) return;
    edit(`Delete ${sid}`, c => deleteStationIn(c, sid));
    select(null, null);
  };
}

// ========================================================================== line
function renderLine(ln) {
  const el = document.getElementById('inspector');
  el.style.setProperty('--accent', ln.color || '#6ea8fe');
  document.getElementById('insp-name').textContent = ln.name;
  const nst = new Set((ln.directions || []).flatMap(d => d.stations)).size;
  document.getElementById('insp-id').textContent = `${ln.id} · ${ln.mode} · ${nst} station${nst === 1 ? '' : 's'}`;
  document.getElementById('insp-lines').innerHTML = '';

  const o = lineOverride(ln.id) || {};
  const snap = snapshotLine(ln.id) || {};
  const modes = knownModes();
  const isSet = f => o[f] !== undefined && o[f] !== null;
  const from511 = v => v ? `511: <code>${esc(v)}</code>` : 'not set by 511';
  const cleared = f => isSet(f)
    ? `<button class="tchip clear edit-only" data-clear="${f}" title="Remove the override">Use 511's</button>` : '';

  const replaces = (o.replaces || []).map(id => {
    const r = lineById(id);
    return `<span class="tchip link">
      <span class="mini-bullet" style="background:${esc(r?.color || '#7c8598')}">${esc(r?.shortName || upstream(id))}</span>
      <span class="nm">${esc(r?.name || id)}</span>
      <button class="x" data-act="unreplace" data-id="${esc(id)}" title="Remove">
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </button></span>`;
  }).join('');
  const replacedBy = allLines().filter(l => (l.replaces || []).includes(ln.id));

  const colour = (f, label) => `
    <div class="field">
      <label class="micro">${label} ${sub(from511(snap[f]))}</label>
      <div class="row-inp">
        <input type="color" class="swatch" data-lf-swatch="${f}" value="${esc(ln[f] || snap[f] || '#000000')}">
        <input class="inp mono" data-lf="${f}" data-key="l-${f}" value="${esc(o[f] ?? '')}" placeholder="${esc(snap[f] || '#RRGGBB')}" spellcheck="false">
        ${cleared(f)}
      </div>
    </div>`;

  document.getElementById('insp-body').innerHTML = `
  <div class="sect">
    <div class="sect-head"><div class="micro">Line ${sub('overrides of what 511 says; blank uses 511\'s')}</div></div>
    <div class="field">
      <label class="micro">Name ${sub(`511: <code>${esc(`${snap.shortName || ''} ${snap.longName || ''}`.trim())}</code>`)}</label>
      <div class="row-inp">
        <input class="inp" data-lf="name" data-key="l-name" value="${esc(o.name ?? '')}" placeholder="${esc(isSet('name') ? '511 default' : ln.name)}">
        ${cleared('name')}
      </div>
    </div>
    ${colour('color', 'Colour')}
    ${colour('textColor', 'Text colour')}
    <div class="field">
      <label class="micro">Mode ${sub(from511(snap.mode))}</label>
      <div class="row-inp">
        <input class="inp" data-lf="mode" data-key="l-mode" list="mode-list" value="${esc(o.mode ?? '')}" placeholder="${esc(snap.mode || '')}" spellcheck="false">
        <datalist id="mode-list">${modes.map(m => `<option value="${esc(m)}">`).join('')}</datalist>
        ${cleared('mode')}
      </div>
    </div>
    <div class="field">
      <label class="check"><input type="checkbox" data-lf="hidden" data-key="l-hidden" ${o.hidden ? 'checked' : ''}>
        Hidden ${sub('left out of the app\'s line lists')}</label>
    </div>
    <div class="field">
      <label class="micro">Replaces ${sub('lines this one substitutes for')}</label>
      <div class="chips">${replaces}
        <button class="tchip add" data-act="add-replace">
          <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          Add a line</button>
      </div>
      ${replacedBy.length ? `<div class="empty" style="margin-top:6px">Replaced by ${replacedBy.map(l => esc(l.name)).join(', ')}</div>` : ''}
    </div>
    <div class="field">
      <label class="micro">Note</label>
      <textarea class="inp" data-lf="note" data-key="l-note" rows="2" placeholder="none">${esc(o.note ?? '')}</textarea>
    </div>
  </div>
  <div class="sect">
    <div class="sect-head"><div class="micro">Directions ${sub('511\'s most-run pattern each way')}</div></div>
    ${(ln.directions || []).map(d => `
      <div class="derived" style="margin-bottom:6px">→ <b>${esc(d.headsign)}</b>
        <span class="mono" style="color:var(--ink-faint)"> · ${d.stations.length} stations · ${d.stops.length} stops</span></div>`).join('')
      || '<div class="empty">None.</div>'}
  </div>`;
  wireLine(ln);
}

const HEX = /^#?[0-9a-fA-F]{6}$/;

function wireLine(ln) {
  const body = document.getElementById('insp-body');
  const id = ln.id;
  const set = (field, value, label) =>
    edit(label || `${field} of ${ln.shortName || id}`, c => setLineOverride(c, id, field, value));

  body.querySelectorAll('[data-lf]').forEach(el => {
    const f = el.dataset.lf;
    el.onchange = () => {
      if (f === 'hidden') { set('hidden', el.checked, `${el.checked ? 'Hide' : 'Show'} ${ln.shortName || id}`); return; }
      let v = el.value.trim();
      if ((f === 'color' || f === 'textColor') && v) {
        // Colours are `#RRGGBB`, upper case (app/models/ids.py); anything else
        // would fail the model before the validator could say why.
        if (!HEX.test(v)) { el.classList.add('bad'); hint('A colour is six hex digits, like #FAA633'); return; }
        v = '#' + v.replace('#', '').toUpperCase();
      }
      if (v === ((lineOverride(id) || {})[f] ?? '')) return;
      set(f, v || null);
    };
  });

  body.querySelectorAll('[data-lf-swatch]').forEach(el => {
    el.onchange = () => set(el.dataset.lfSwatch, el.value.toUpperCase());
  });

  body.querySelectorAll('[data-clear]').forEach(b => {
    b.onclick = () => set(b.dataset.clear, null, `Use 511's ${b.dataset.clear} for ${ln.shortName || id}`);
  });

  body.querySelectorAll('[data-act="unreplace"]').forEach(b => {
    b.onclick = () => {
      const next = ((lineOverride(id) || {}).replaces || []).filter(x => x !== b.dataset.id);
      set('replaces', next, `${ln.shortName || id} no longer replaces ${upstream(b.dataset.id)}`);
    };
  });

  const add = body.querySelector('[data-act="add-replace"]');
  if (add) add.onclick = () => {
    pickLineFor?.(other => {
      if (!other || other === id) return;
      const cur = (lineOverride(id) || {}).replaces || [];
      if (cur.includes(other)) { hint('Already listed'); return; }
      set('replaces', [...cur, other], `${ln.shortName || id} replaces ${upstream(other)}`);
    });
  };
}
