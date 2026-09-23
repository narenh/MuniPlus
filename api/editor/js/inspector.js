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
  metresBetween, platformsOf, derivedStation, derivedPlatform, stationPos,
  platformPos, upstream, unclaimedNear, today, lineOverride, snapshotLine,
  setLineOverride, knownModes, setActiveLine,
} from './store.js';
import { hint, highlightLink, flyTo, showCandidates, onCandidate, fitLine } from './map.js';

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
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

  document.getElementById('insp-body').innerHTML =
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
    ? all.filter(c => `${c.id} ${c.p.stopName || ''}`.toLowerCase().includes(q))
    : all;
}

function addPicker(sid) {
  const list = candidates(sid);
  const rows = list.slice(0, CANDIDATES_SHOWN).map(c => {
    const lines = c.p.lines.map(lineById).filter(Boolean);
    return `
    <button class="cand" data-act="pick-cand" data-pid="${esc(c.id)}">
      <span class="cand-code mono">${esc(upstream(c.id))}</span>
      <span class="cand-name">${esc(c.p.stopName || '')}</span>
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
  return platformsOf(st).map(p => {
    const pid = p.id;
    const d = derivedPlatform(pid);
    const live = !!d?.live;
    const sel = store.selPlatform === pid;
    const at = platformPos(pid);

    const served = (d?.lines || []).map(lineById).filter(Boolean);
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
          <div class="plat-sub">${esc(d?.stopName || p.name || st.name)}</div>
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate" data-pid="${esc(pid)}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
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
      <div class="field">
        <label class="micro">Lines ${sub('from 511')}</label>
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

// ---------------------------------------------------------------- transfers
const TMODE = {
  street: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-dasharray="0.1 2.6"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  indoor: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function sectionTransfers(st, sid) {
  const inbound = stationIds()
    .filter(id => id !== sid && (stationById(id).transfers || []).some(t => t.to === sid));
  const here = stationPos(sid);

  const rows = (st.transfers || []).map(t => {
    const to = stationById(t.to);
    const mutual = inbound.includes(t.to);
    const there = stationPos(t.to);
    const away = here && there ? `${metresBetween(here, there)} m` : '—';
    return `
    <span class="tchip link ${mutual ? 'both' : 'out'}" data-act="hover-link" data-id="${esc(t.to)}">
      <i class="dir">${TMODE[t.mode] || TMODE.street}</i>
      <span class="nm">${esc(to ? to.name : t.to)}</span>
      <em>${t.mode === 'indoor' ? 'indoor' : away}</em>
      <button class="x" data-act="cycle-mode" data-id="${esc(t.to)}" title="Switch to ${t.mode === 'indoor' ? 'street' : 'indoor'}">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M2 4.5h6.5M6.5 2.5L8.8 4.5 6.5 6.5M10 7.5H3.5M5.5 5.5L3.2 7.5 5.5 9.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <button class="x" data-act="untransfer" data-id="${esc(t.to)}" title="Remove">
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </button>
    </span>`;
  }).join('');

  const inOnly = inbound.filter(id => !(st.transfers || []).some(t => t.to === id)).map(id => `
    <span class="tchip link in" data-act="hover-link" data-id="${esc(id)}">
      <i class="dir">${TMODE.street}</i>
      <span class="nm">${esc(stationById(id)?.name || id)}</span>
      <em>links here</em>
      <button class="x mk" data-act="reciprocate" data-id="${esc(id)}" title="Link back">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </span>`).join('');

  const agencies = AGENCIES.map(([ag, label]) => {
    const on = (st.transferAgencies || []).includes(ag);
    return `<button class="tchip ${on ? 'agency-on' : 'add'}" data-act="agency" data-ag="${ag}" title="${label} (${ag})">${label}</button>`;
  }).join('');
  // Read-only hides the agencies not set, so with none set there is nothing to show.
  const anyAgency = AGENCIES.some(([ag]) => (st.transferAgencies || []).includes(ag));

  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Transfers</div></div>
    ${rows ? `<div class="field"><div class="chips">${rows}</div></div>` : ''}
    ${inOnly ? `<div class="field"><label class="micro">One way in ${sub('they link here; this station does not link back')}</label><div class="chips">${inOnly}</div></div>` : ''}
    ${!rows && !inOnly ? '<div class="empty" style="margin-bottom:10px">No transfer links.</div>' : ''}
    <div class="field">
      <button class="tchip add" data-act="add-transfer" style="width:100%;justify-content:center;height:29px">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        Link a station
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
    <div class="sect-head"><div class="micro">Danger zone</div></div>
    <button class="btn" id="del-station" style="width:100%;justify-content:center;color:var(--bad);border-color:rgba(251,113,133,.3)">
      Delete “${esc(st.name)}”
    </button>
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
        const at = platformPos(pid);
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

      if (act === 'del-plat') {
        if (platformsOf(st).length === 1) { hint('A station must keep at least one platform'); return; }
        commit(`Remove platform ${upstream(pid)}`, s => {
          s.platforms = s.platforms.filter(p => p.id !== pid);
        });
        if (store.selPlatform === pid) select(sid, null);
      }

      if (act === 'untransfer') {
        commit(`Unlink ${b.dataset.id}`, s => {
          s.transfers = (s.transfers || []).filter(t => t.to !== b.dataset.id);
        });
      }

      // An indoor passage cannot be one-way, so switching to indoor sets both
      // sides; switching away only touches this one.
      if (act === 'cycle-mode') {
        const other = b.dataset.id;
        const cur = (st.transfers || []).find(t => t.to === other)?.mode;
        const to = cur === 'indoor' ? 'street' : 'indoor';
        commit(`${other} transfer → ${to}`, (s, c) => {
          const t = s.transfers.find(x => x.to === other);
          t.mode = to;
          if (to === 'indoor') {
            const os = c.stations.stations[other];
            const back = (os.transfers || (os.transfers = [])).find(x => x.to === sid);
            if (back) back.mode = 'indoor';
            else os.transfers.push({ to: sid, mode: 'indoor' });
          }
        });
        if (to === 'indoor') hint('Indoor links work both ways, so the reciprocal was added too');
      }

      if (act === 'reciprocate') {
        const other = b.dataset.id;
        const mode = (stationById(other)?.transfers || []).find(t => t.to === sid)?.mode || 'street';
        commit(`Link ${sid} back to ${other}`, s => {
          (s.transfers || (s.transfers = [])).push({ to: other, mode });
        });
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
          if ((st.transfers || []).some(t => t.to === target)) { hint('Already linked'); return; }
          commit(`Link ${sid} → ${target}`, s => {
            (s.transfers || (s.transfers = [])).push({ to: target, mode: 'street' });
          });
        });
      }
    };
  });

  const del = body.querySelector('#del-station');
  if (del) del.onclick = () => {
    if (!confirm(`Delete "${st.name}"?\n\nIt is also removed from every subway and transfer that references it. Its id cannot be reused by another station without breaking saved favourites.`)) return;
    edit(`Delete ${sid}`, c => {
      delete c.stations.stations[sid];
      for (const s of Object.values(c.stations.subways || {})) s.stations = s.stations.filter(x => x !== sid);
      for (const s of Object.values(c.stations.stations)) {
        if (s.transfers) s.transfers = s.transfers.filter(t => t.to !== sid);
      }
    });
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
        <span class="mono" style="color:var(--ink-faint)"> · ${d.stations.length} stations · ${d.platforms.length} stops</span></div>`).join('')
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
