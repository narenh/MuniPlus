// Right-hand inspector. Two shapes: a multilevel station is levels and exits, a
// street station is a flat list of platforms. Station coordinates are shown but
// never editable - derived from exits at a multilevel station, from platforms at
// a street one.

import {
  store, edit, select, stationById, linesOf, esc, metresBetween,
  platformsOf, levelOf, hasCoord, anchorOf, isMultilevel,
} from './store.js';
import { flyToStation, refresh, hint, map, highlightLink } from './map.js';

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
const AGENCIES = ['bart', 'caltrain'];
const LEVEL_AGENCIES = ['muni', 'bart'];
const ARROW = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };

let pickStationFor = null;

export function initInspector({ onNeedStationPicker }) {
  pickStationFor = onNeedStationPicker;
  document.getElementById('insp-close').onclick = () => { select(null, null, null); renderInspector(); };
}

export function renderInspector() {
  const el = document.getElementById('inspector');
  const st = stationById(store.selStation);

  if (!st) {
    el.classList.add('closed');
    document.body.classList.add('no-inspector');
    document.body.classList.remove('inspector-open');
    return;
  }
  el.classList.remove('closed');
  document.body.classList.remove('no-inspector');
  document.body.classList.add('inspector-open');

  const ls = linesOf(st.id);
  el.style.setProperty('--accent', ls[0]?.color || '#6ea8fe');

  document.getElementById('insp-name').textContent = st.name;
  document.getElementById('insp-id').textContent = isMultilevel(st)
    ? `${st.id} · ${st.levels.length} level${st.levels.length === 1 ? '' : 's'} · ${(st.exits || []).length} exit${(st.exits || []).length === 1 ? '' : 's'}`
    : `${st.id} · ${st.platforms.length} platform${st.platforms.length === 1 ? '' : 's'}`;

  const box = document.getElementById('insp-lines');
  box.innerHTML = '';
  for (const ln of store.doc.lines) {
    const on = (st.lines || []).includes(ln.id);
    const b = document.createElement('button');
    b.className = 'mini-bullet' + (on ? '' : ' off');
    b.style.background = ln.color;
    b.textContent = ln.shortName || ln.id;
    b.title = on ? `${ln.name} — derived from the platforms below` : ln.name;
    b.disabled = true;                       // station.lines is derived
    box.appendChild(b);
  }

  document.getElementById('insp-body').innerHTML =
    sectionStation(st) +
    (isMultilevel(st) ? sectionLevels(st) + sectionExits(st) : sectionPlatforms(st, null)) +
    sectionTransfers(st) +
    sectionDanger(st);
  wire(st);
}

// ------------------------------------------------------------------ station
function sectionStation(st) {
  const src = isMultilevel(st) ? 'exits' : 'platforms';
  const coord = hasCoord(st)
    ? `<code>${st.latitude}, ${st.longitude}</code>`
    : `<span style="color:var(--warn)">no ${src} with coordinates yet</span>`;
  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Station</div></div>
    <div class="field">
      <label class="micro">Display name</label>
      <input class="inp" id="f-name" value="${esc(st.name)}">
    </div>
    <div class="field">
      <label class="micro">Identifier</label>
      <input class="inp mono" id="f-id" value="${esc(st.id)}">
    </div>
    <div class="field">
      <label class="micro">Structure <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— not stored; it is whichever shape the record has</span></label>
      <div class="seg" id="f-kind">
        <button data-v="flat"   class="${isMultilevel(st) ? '' : 'on'}">Street platforms</button>
        <button data-v="levels" class="${isMultilevel(st) ? 'on' : ''}">Multilevel</button>
      </div>
    </div>
    <div class="field">
      <label class="micro">Coordinate <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— derived from ${src}, not editable</span></label>
      <div class="derived mono">${coord}</div>
    </div>
  </div>`;
}

// ------------------------------------------------------------------- levels
function sectionLevels(st) {
  const levels = [...(st.levels || [])].sort((a, b) => b.id - a.id);
  const cards = levels.map((lv, i) => {
    const island = lv.isIsland === true ? 'island' : lv.isIsland === false ? 'sep' : 'unset';
    return `
    <div class="level" data-level="${lv.id}">
      <div class="level-head">
        <div class="depth mono ${lv.id > 0 ? 'above' : lv.id === 0 ? 'street' : ''}"
           title="${lv.id > 0 ? 'above street' : lv.id === 0 ? 'street level' : 'below street'}">${lv.id > 0 ? '+' : ''}${lv.id}</div>
        <input class="inp level-name" data-lf="name" data-level="${lv.id}" value="${esc(lv.name)}">
        <div class="level-move">
          <button class="icon-btn" data-act="lv-up" data-level="${lv.id}" title="Move up the stack"
                  ${i === 0 ? 'disabled' : ''}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 10l4-4 4 4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button class="icon-btn" data-act="lv-down" data-level="${lv.id}" title="Move down the stack"
                  ${i === levels.length - 1 ? 'disabled' : ''}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
        <button class="icon-btn danger" data-act="del-level" data-level="${lv.id}" title="Remove this level">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M4 8h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="pair" style="margin-bottom:9px">
        <div>
          <label class="micro">Depth</label>
          <input class="inp mono" type="number" data-lf="id" data-level="${lv.id}" value="${lv.id}" step="1">
        </div>
        <div>
          <label class="micro">Agency</label>
          <select class="inp" data-lf="agency" data-level="${lv.id}">
            <option value="">none</option>
            ${LEVEL_AGENCIES.map(a => `<option value="${a}" ${lv.agency === a ? 'selected' : ''}>${a}</option>`).join('')}
          </select>
        </div>
      </div>
      <div class="field">
        <label class="micro">Platforms on this level</label>
        <div class="seg" data-island="${lv.id}">
          <button data-v="unset" class="${island === 'unset' ? 'on' : ''}">Unset</button>
          <button data-v="island" class="${island === 'island' ? 'on' : ''}">Island</button>
          <button data-v="sep"    class="${island === 'sep' ? 'on' : ''}">Separated</button>
        </div>
      </div>
      ${platformCards(st, lv)}
      <button class="tchip add" data-act="add-plat" data-level="${lv.id}"
              style="width:100%;justify-content:center;height:27px;margin-top:2px">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        Add platform here
      </button>
    </div>`;
  }).join('');

  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Levels · ${levels.length}</div>
      <button class="icon-btn" id="add-level" title="Add a level">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${cards || '<div class="empty">No levels.</div>'}
  </div>`;
}

// -------------------------------------------------------------------- exits
function sectionExits(st) {
  const levels = [...(st.levels || [])].sort((a, b) => b.id - a.id);
  const cards = (st.exits || []).map(ex => {
    const sel = store.selExit === ex.id;
    const placed = Number.isFinite(ex.latitude);
    return `
    <div class="plat exit ${sel ? 'sel' : ''} ${ex.closed ? 'closed' : ''}" data-exit="${esc(ex.id)}">
      <div class="plat-head">
        <div class="compass" title="${ex.closed ? 'closed' : 'open'}">
          <svg width="13" height="13" viewBox="0 0 18 18" fill="none">
            <path d="M11 3H5v12h6" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M8 9h7M12 5.6L15.4 9 12 12.4" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
        <div style="min-width:0;flex:1">
          <input class="inp exit-name" data-xf="name" data-exit="${esc(ex.id)}" value="${esc(ex.name)}">
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate-exit" data-exit="${esc(ex.id)}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
          </button>
          <button class="icon-btn danger" data-act="del-exit" data-exit="${esc(ex.id)}" title="Delete this exit">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8h4.8l.6-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>

      <div class="pair" style="margin-bottom:9px">
        <div>
          <label class="micro">Lands on</label>
          <select class="inp" data-xf="level" data-exit="${esc(ex.id)}">
            ${levels.map(l => `<option value="${l.id}" ${l.id === ex.level ? 'selected' : ''}>${l.id} · ${esc(l.name)}</option>`).join('')}
          </select>
        </div>
        <div>
          <label class="micro">Status</label>
          <div class="seg">
            <button data-xtog="closed" data-exit="${esc(ex.id)}" data-v="0" class="${ex.closed ? '' : 'on'}">Open</button>
            <button data-xtog="closed" data-exit="${esc(ex.id)}" data-v="1" class="${ex.closed ? 'on' : ''}">Closed</button>
          </div>
        </div>
      </div>

      <div class="field">
        <label class="micro">Access</label>
        <div class="chips">
          ${['stairs', 'escalator', 'elevator'].map(f => `
            <button class="tchip ${ex[f] ? 'agency-on' : 'add'}" data-xtog="${f}" data-exit="${esc(ex.id)}">${f}</button>`).join('')}
        </div>
      </div>

      <div class="field" style="margin-bottom:0">
        <label class="micro">Coordinate — drag the door on the map</label>
        ${placed ? `<div class="pair">
          <input class="inp mono" data-xf="latitude"  data-exit="${esc(ex.id)}" value="${ex.latitude}" inputmode="decimal">
          <input class="inp mono" data-xf="longitude" data-exit="${esc(ex.id)}" value="${ex.longitude}" inputmode="decimal">
        </div>`
        : `<button class="btn" data-act="place-exit" data-exit="${esc(ex.id)}" style="width:100%;justify-content:center">
             Place this exit on the map
           </button>`}
      </div>
    </div>`;
  }).join('');

  const open = (st.exits || []).filter(e => !e.closed).length;
  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Exits · ${(st.exits || []).length}${open !== (st.exits || []).length ? ` · ${open} open` : ''}</div>
      <button class="icon-btn" id="add-exit" title="Add an exit">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${cards || '<div class="empty">No exits yet. Until one has coordinates this station has no position.</div>'}
  </div>`;
}

// ---------------------------------------------------------------- platforms
function sectionPlatforms(st, level) {
  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Platforms · ${st.platforms.length}</div>
      <button class="icon-btn" data-act="add-plat" title="Add a platform">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${platformCards(st, null) || '<div class="empty">No platforms.</div>'}
  </div>`;
}

function platformCards(st, level) {
  const list = level ? (level.platforms || []) : (st.platforms || []);
  return list.map(p => {
    const d = store.drift?.get(String(p.id));
    const sel = store.selPlatform === String(p.id);
    let badge = '';
    if (d?.kind === 'missing') badge = `<div class="drift-badge missing">Not in the current SFMTA feed</div>`;
    else if (d?.kind === 'moved') badge = `<div class="drift-badge moved">Feed places this ${d.metres} m away
      <button data-act="snap" data-code="${esc(p.id)}">snap to feed</button></div>`;
    else if (d?.kind === 'ok') badge = `<div class="drift-badge ok">Matches the feed${d.metres ? ` (${d.metres} m)` : ''}</div>`;

    const lines = store.doc.lines.map(ln => {
      const on = (p.lines || []).includes(ln.id);
      return `<button class="mini-bullet ${on ? '' : 'off'}" style="${on ? `background:${ln.color}` : ''}"
        data-act="tog-line" data-code="${esc(p.id)}" data-line="${ln.id}"
        title="${on ? `${esc(ln.name)} — click to remove` : `Add ${esc(ln.name)}`}">${esc(ln.shortName || ln.id)}</button>`;
    }).join('');

    const term = (p.lines || []).map(l => {
      const on = (p.terminates || []).includes(l);
      return `<button class="tchip ${on ? 'agency-on' : 'add'}" data-act="tog-term"
        data-code="${esc(p.id)}" data-line="${l}">${l}</button>`;
    }).join('') || '<span class="empty">no lines yet</span>';

    return `
    <div class="plat ${sel ? 'sel' : ''}" data-code="${esc(p.id)}">
      <div class="plat-head">
        <div class="compass" title="${esc(p.heading)}">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style="transform:rotate(${ARROW[p.heading] ?? 0}deg)">
            <path d="M8 2.2l3.6 10-3.6-2.6-3.6 2.6L8 2.2z" fill="currentColor"/></svg>
        </div>
        <div style="min-width:0">
          <div class="plat-code">${esc(p.id)}</div>
          <div class="plat-sub">${esc(p.stopName || p.name || st.name)}</div>
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate" data-code="${esc(p.id)}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
          </button>
          <button class="icon-btn danger" data-act="del-plat" data-code="${esc(p.id)}" title="Remove this platform">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8h4.8l.6-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>

      <div class="field">
        <label class="micro">Stop code</label>
        <input class="inp mono" data-pf="id" data-code="${esc(p.id)}" value="${esc(p.id)}">
      </div>
      ${isMultilevel(st) ? '' : `
      <div class="field">
        <label class="micro">Stop name</label>
        <input class="inp" data-pf="stopName" data-code="${esc(p.id)}" value="${esc(p.stopName || '')}">
      </div>`}
      <div class="field">
        <label class="micro">Label <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— signage, e.g. Platform 1</span></label>
        <input class="inp" data-pf="name" data-code="${esc(p.id)}" value="${esc(p.name ?? '')}" placeholder="none">
      </div>
      <div class="field">
        <label class="micro">Heading</label>
        <select class="inp" data-pf="heading" data-code="${esc(p.id)}">
          ${HEADINGS.map(h => `<option value="${h}" ${p.heading === h ? 'selected' : ''}>${h}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label class="micro">Lines</label>
        <div class="insp-lines">${lines}</div>
      </div>
      <div class="field">
        <label class="micro">Terminates here</label>
        <div class="chips">${term}</div>
      </div>
      <div class="field" style="margin-bottom:0">
        <label class="micro">Coordinate — drag the pole on the map</label>
        <div class="pair">
          <input class="inp mono" data-pf="latitude"  data-code="${esc(p.id)}" value="${p.latitude}"  inputmode="decimal">
          <input class="inp mono" data-pf="longitude" data-code="${esc(p.id)}" value="${p.longitude}" inputmode="decimal">
        </div>
      </div>
      ${badge}
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- transfers
const TMODE = {
  street: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-dasharray="0.1 2.6"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  indoor: '<svg width="13" height="9" viewBox="0 0 16 10" fill="none"><path d="M1 5h12" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M10.5 1.8L14 5l-3.5 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function sectionTransfers(st) {
  const inbound = store.doc.stations
    .filter(s => s.id !== st.id && (s.transfers || []).some(t => t.to === st.id))
    .map(s => s.id);

  const rows = (st.transfers || []).map(t => {
    const to = stationById(t.to);
    const mutual = inbound.includes(t.to);
    const away = to && hasCoord(to) && hasCoord(st) ? `${metresBetween(st, to)} m` : '—';
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

  const agencies = AGENCIES.map(ag => {
    const on = (st.transferAgencies || []).includes(ag);
    return `<button class="tchip ${on ? 'agency-on' : 'add'}" data-act="agency" data-ag="${ag}">${ag.toUpperCase()}</button>`;
  }).join('');

  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Transfers</div></div>
    ${rows ? `<div class="field"><div class="chips">${rows}</div></div>` : ''}
    ${inOnly ? `<div class="field"><label class="micro">One way in <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— they link here; this station does not link back</span></label><div class="chips">${inOnly}</div></div>` : ''}
    ${!rows && !inOnly ? '<div class="empty" style="margin-bottom:10px">No transfer links.</div>' : ''}
    <div class="field">
      <button class="tchip add" data-act="add-transfer" style="width:100%;justify-content:center;height:29px">
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        Link a station
      </button>
    </div>
    <div class="field" style="margin-top:12px">
      <label class="micro">Other agencies</label>
      <div class="chips">${agencies}</div>
    </div>
  </div>`;
}

function sectionDanger(st) {
  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Danger zone</div></div>
    <button class="btn" id="del-station" style="width:100%;justify-content:center;color:var(--bad);border-color:rgba(251,113,133,.3)">
      Delete “${esc(st.name)}”
    </button>
  </div>`;
}


// ==========================================================================
// Wiring. Every handler commits through edit(), so one gesture is one undo and
// derived fields are recomputed by the store.
// ==========================================================================
function wire(st) {
  const body = document.getElementById('insp-body');
  const sid = st.id;
  const commit = (label, fn) => edit(label, d => fn(d.stations.find(s => s.id === sid), d));

  const plat = (s2, code) => platformsOf(s2).find(x => String(x.id) === String(code));
  const exitOf = (s2, eid) => (s2.exits || []).find(x => x.id === eid);

  // ---------------------------------------------------------------- station
  const name = body.querySelector('#f-name');
  name.onchange = () => {
    const v = name.value.trim();
    if (!v || v === st.name) { name.value = st.name; return; }
    commit(`Rename ${sid}`, s => { s.name = v; });
  };

  const idf = body.querySelector('#f-id');
  idf.onchange = () => {
    const v = idf.value.trim();
    if (!v || v === sid) { idf.value = sid; return; }
    if (store.doc.stations.some(s => s.id === v)) { idf.classList.add('bad'); hint(`"${v}" already exists`); return; }
    edit(`Rename id ${sid} → ${v}`, d => {
      d.stations.find(s => s.id === sid).id = v;
      for (const l of d.lines) l.stationIds = l.stationIds.map(x => x === sid ? v : x);
      for (const s of d.subways || []) s.stationIds = s.stationIds.map(x => x === sid ? v : x);
      for (const s of d.stations) for (const t of s.transfers || []) if (t.to === sid) t.to = v;
    });
    select(v, store.selPlatform, store.selExit);
  };

  // Changing kind restructures the record, so it is deliberate and confirmed.
  body.querySelectorAll('#f-kind button').forEach(b => {
    b.onclick = () => {
      const toLevels = b.dataset.v === 'levels';
      if (toLevels === isMultilevel(st)) return;
      const msg = toLevels
        ? `Give "${st.name}" levels and exits?\n\nIts ${platformsOf(st).length} platform(s) move onto a new level at depth -1, and it gains an empty exits list. Until an exit has coordinates the station has no position.`
        : `Flatten "${st.name}" to street platforms?\n\nIts levels and exits are removed and every platform moves to a flat list. Level names, agencies, isIsland and all exits are lost.`;
      if (!confirm(msg)) return;
      commit(toLevels ? `${sid} → levels` : `${sid} → flat`, s => {
        const ps = platformsOf(s);
        if (toLevels) {
          delete s.platforms;
          s.levels = [{ id: -1, name: 'Platforms', agency: 'muni', isIsland: null, platforms: ps }];
          s.exits = [];
          for (const p of ps) delete p.stopName;
        } else {
          delete s.levels; delete s.exits;
          s.platforms = ps;
          for (const p of ps) if (p.stopName === undefined) p.stopName = s.name;
        }
      });
    };
  });

  // ----------------------------------------------------------------- levels
  body.querySelector('#add-level')?.addEventListener('click', () => {
    const used = new Set((st.levels || []).map(l => l.id));
    let depth = -1;
    while (used.has(depth)) depth--;
    commit(`Add level ${depth}`, s => {
      (s.levels || (s.levels = [])).push({ id: depth, name: 'New level', agency: null, isIsland: null, platforms: [] });
    });
    hint(`Level ${depth} added — rename it and set its depth`);
  });

  body.querySelectorAll('[data-lf]').forEach(el => {
    el.onchange = () => {
      const id = Number(el.dataset.level), f = el.dataset.lf;
      if (f === 'id') {
        const to = Math.trunc(Number(el.value));
        if (!Number.isInteger(to)) { hint('A level depth must be a whole number'); renderInspector(); return; }
        if (to !== id && (st.levels || []).some(l => l.id === to)) { hint(`Depth ${to} is already used here`); renderInspector(); return; }
        // exits reference levels by depth, so they move with it
        commit(`Level ${id} → ${to}`, s => {
          s.levels.find(l => l.id === id).id = to;
          for (const e of s.exits || []) if (e.level === id) e.level = to;
        });
        return;
      }
      const v = f === 'agency' ? (el.value || null) : el.value.trim();
      commit(`Edit level ${id}`, s => { s.levels.find(l => l.id === id)[f] = v; });
    };
  });

  body.querySelectorAll('[data-island]').forEach(seg => {
    seg.querySelectorAll('button').forEach(b => {
      b.onclick = () => {
        const id = Number(seg.dataset.island);
        const v = b.dataset.v === 'unset' ? null : b.dataset.v === 'island';
        commit(`Level ${id} isIsland → ${v}`, s => { s.levels.find(l => l.id === id).isIsland = v; });
      };
    });
  });

  // ------------------------------------------------------------------ exits
  body.querySelector('#add-exit')?.addEventListener('click', () => {
    const levels = st.levels || [];
    if (!levels.length) { hint('Add a level first — an exit has to land somewhere'); return; }
    const level = Math.max(...levels.map(l => l.id));   // the shallowest
    const eid = freshExitId(st);
    commit('Add exit', s => {
      (s.exits || (s.exits = [])).push({
        id: eid, name: 'New exit', level,
        stairs: true, escalator: false, elevator: false, closed: false,
        latitude: null, longitude: null,
      });
    });
    select(sid, null, eid);
    hint('Exit added — name it, then place it on the map');
  });

  body.querySelectorAll('[data-xf]').forEach(el => {
    el.onchange = () => {
      const eid = el.dataset.exit, f = el.dataset.xf;
      let v = el.value;
      if (f === 'level') v = Number(v);
      else if (f === 'latitude' || f === 'longitude') {
        v = Number(v);
        if (!Number.isFinite(v)) { renderInspector(); return; }
      } else v = v.trim();
      if (f === 'name' && !v) { renderInspector(); return; }
      commit(`Edit exit ${eid}`, s => { exitOf(s, eid)[f] = v; });
    };
  });

  body.querySelectorAll('[data-xtog]').forEach(b => {
    b.onclick = ev => {
      ev.stopPropagation();
      const eid = b.dataset.exit, f = b.dataset.xtog;
      const v = b.dataset.v !== undefined ? b.dataset.v === '1' : !exitOf(st, eid)[f];
      commit(`Exit ${eid} ${f} → ${v}`, s => { exitOf(s, eid)[f] = v; });
    };
  });

  // -------------------------------------------------------------- platforms
  body.querySelectorAll('[data-pf]').forEach(el => {
    el.onchange = () => {
      const code = el.dataset.code, f = el.dataset.pf;
      let v = el.value;
      if (f === 'latitude' || f === 'longitude') {
        v = Number(v);
        if (!Number.isFinite(v)) { renderInspector(); return; }
      } else v = v.trim();

      if (f === 'id') {
        if (!v || v === code) { renderInspector(); return; }
        const clash = store.doc.stations.some(s2 =>
          platformsOf(s2).some(p => String(p.id) === v && !(s2.id === sid && String(p.id) === code)));
        if (clash) { el.classList.add('bad'); hint(`Stop code ${v} is already used`); return; }
        commit(`Stop code ${code} → ${v}`, s => { plat(s, code).id = v; });
        select(sid, v, null);
        return;
      }
      if (f === 'name') v = v === '' ? null : v;
      commit(`Edit ${code}`, s => { plat(s, code)[f] = v; });
    };
  });

  body.querySelectorAll('[data-act="tog-line"]').forEach(b => {
    b.onclick = ev => {
      ev.stopPropagation();
      const code = b.dataset.code, line = b.dataset.line;
      const on = (plat(st, code).lines || []).includes(line);
      commit(`${on ? 'Remove' : 'Add'} ${line} at ${code}`, s => {
        const p = plat(s, code);
        p.lines = on ? (p.lines || []).filter(x => x !== line)
                     : [...(p.lines || []), line].sort();
        if (on && p.terminates) {
          p.terminates = p.terminates.filter(x => x !== line);
          if (!p.terminates.length) delete p.terminates;
        }
      });
    };
  });

  body.querySelectorAll('[data-act="tog-term"]').forEach(b => {
    b.onclick = ev => {
      ev.stopPropagation();
      const code = b.dataset.code, line = b.dataset.line;
      const on = (plat(st, code).terminates || []).includes(line);
      commit(`${line} ${on ? 'passes through' : 'terminates'} at ${code}`, s => {
        const p = plat(s, code);
        const next = on ? (p.terminates || []).filter(x => x !== line)
                        : [...(p.terminates || []), line].sort();
        if (next.length) p.terminates = next; else delete p.terminates;
      });
    };
  });

  body.querySelectorAll('[data-act="add-plat"]').forEach(b => {
    b.onclick = () => {
      const code = freshCode();
      const anchor = platformsOf(st)[0];
      const base = {
        id: code, heading: 'northbound', name: null,
        lines: [...(st.lines || [])].slice(0, 1),
        latitude: anchor?.latitude ?? st.latitude ?? 37.7749,
        longitude: anchor?.longitude ?? st.longitude ?? -122.4194,
      };
      if (!isMultilevel(st)) base.stopName = st.name;
      const levelId = b.dataset.level !== undefined ? Number(b.dataset.level) : null;
      commit('Add platform', s => {
        if (levelId !== null) s.levels.find(l => l.id === levelId).platforms.push(base);
        else s.platforms.push(base);
      });
      select(sid, code, null);
      hint(`Platform ${code} added — set its real stop code, then drag it into place`);
    };
  });

  // ------------------------------------------------------------ row actions
  body.querySelectorAll('.plat[data-code]').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]') || ev.target.closest('input,select,button')) return;
      select(sid, card.dataset.code, null);
      renderInspector(); refresh('platforms');
    });
  });
  body.querySelectorAll('.plat[data-exit]').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]') || ev.target.closest('input,select,button')) return;
      select(sid, null, card.dataset.exit);
      renderInspector(); refresh('exits');
    });
  });

  body.querySelectorAll('[data-act="hover-link"]').forEach(chip => {
    chip.onmouseenter = () => highlightLink(sid, chip.dataset.id);
    chip.onmouseleave = () => highlightLink(null, null);
  });

  body.querySelectorAll('[data-act]').forEach(b => {
    const act = b.dataset.act;
    if (['tog-line', 'tog-term', 'add-plat', 'hover-link'].includes(act)) return;
    b.onclick = ev => {
      ev.stopPropagation();
      const code = b.dataset.code, eid = b.dataset.exit;

      if (act === 'locate') {
        const p = plat(st, code);
        select(sid, code, null);
        map.easeTo({ center: [p.longitude, p.latitude], zoom: Math.max(map.getZoom(), 17.4), duration: 800 });
        renderInspector(); refresh('platforms');
      }

      if (act === 'locate-exit' || act === 'place-exit') {
        const e0 = exitOf(st, eid);
        select(sid, null, eid);
        if (Number.isFinite(e0.latitude)) {
          map.easeTo({ center: [e0.longitude, e0.latitude], zoom: Math.max(map.getZoom(), 18), duration: 800 });
        } else {
          // No coordinate yet. Drop it on the station itself - its platforms if
          // it has no position yet - and fly there, rather than wherever the
          // camera happens to be pointing.
          const at = anchorOf(st);
          if (!at) { hint('Nothing to anchor this exit to yet'); return; }
          commit(`Place exit ${e0.name}`, s2 => {
            const x = exitOf(s2, eid);
            x.latitude = Math.round(at[1] * 1e6) / 1e6;
            x.longitude = Math.round(at[0] * 1e6) / 1e6;
          });
          map.easeTo({ center: at, zoom: Math.max(map.getZoom(), 18), duration: 700 });
          hint('Dropped on the station — drag it onto the real door');
        }
        renderInspector(); refresh('exits');
      }

      if (act === 'del-exit') {
        const e0 = exitOf(st, eid);
        if (!confirm(`Delete exit "${e0.name}"?`)) return;
        commit(`Delete exit ${e0.name}`, s => { s.exits = s.exits.filter(x => x.id !== eid); });
        select(sid, null, null);
      }

      if (act === 'lv-up' || act === 'lv-down') {
        const id = Number(b.dataset.level);
        const order = [...st.levels].sort((x, y) => y.id - x.id);   // top first
        const i = order.findIndex(l => l.id === id);
        const j = act === 'lv-up' ? i - 1 : i + 1;
        if (j < 0 || j >= order.length) return;
        const other = order[j].id;
        // A level's depth is its identity, so a move is a swap of the two
        // depths - and every exit that lands on either one has to follow the
        // level it belongs to, not the number it used to carry.
        commit(`Swap levels ${id} and ${other}`, s2 => {
          const A = s2.levels.find(l => l.id === id);
          const B = s2.levels.find(l => l.id === other);
          A.id = other; B.id = id;
          for (const e of s2.exits || []) {
            if (e.level === id) e.level = other;
            else if (e.level === other) e.level = id;
          }
        });
        return;
      }

      if (act === 'del-level') {
        const id = Number(b.dataset.level);
        const lv = st.levels.find(l => l.id === id);
        const n = (lv.platforms || []).length;
        const refs = (st.exits || []).filter(e => e.level === id).length;
        if (!confirm(`Delete level ${id} "${lv.name}"?` +
          (n ? `\n\n${n} platform(s) on it will be deleted too.` : '') +
          (refs ? `\n${refs} exit(s) land here and will need a new level.` : ''))) return;
        commit(`Delete level ${id}`, s => { s.levels = s.levels.filter(l => l.id !== id); });
      }

      if (act === 'snap') {
        const d = store.drift?.get(String(code));
        if (!d) return;
        commit(`Snap ${code} to the feed`, s => {
          const p = plat(s, code);
          p.latitude = d.feedLat; p.longitude = d.feedLon;
        });
        hint(`Snapped ${code} to the SFMTA coordinate`);
      }

      if (act === 'del-plat') {
        if (platformsOf(st).length === 1) { hint('A station must keep at least one platform'); return; }
        commit(`Remove platform ${code}`, s => {
          if (isMultilevel(s)) {
            for (const l of s.levels) l.platforms = l.platforms.filter(p => String(p.id) !== String(code));
          } else s.platforms = s.platforms.filter(p => String(p.id) !== String(code));
        });
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
        commit(`${other} transfer → ${to}`, (s, d) => {
          const t = s.transfers.find(x => x.to === other);
          t.mode = to;
          if (to === 'indoor') {
            const os = d.stations.find(x => x.id === other);
            const back = (os.transfers || (os.transfers = [])).find(x => x.to === s.id);
            if (back) back.mode = 'indoor';
            else os.transfers.push({ to: s.id, mode: 'indoor' });
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

  body.querySelector('#del-station').onclick = () => {
    if (!confirm(`Delete "${st.name}"?\n\nIt is also removed from every line, subway and transfer that references it.`)) return;
    edit(`Delete ${sid}`, d => {
      d.stations = d.stations.filter(s => s.id !== sid);
      for (const l of d.lines) l.stationIds = l.stationIds.filter(x => x !== sid);
      for (const s of d.subways || []) s.stationIds = s.stationIds.filter(x => x !== sid);
      for (const s of d.stations) s.transfers = (s.transfers || []).filter(t => t.to !== sid);
    });
    select(null, null, null);
  };
}

function freshExitId(st) {
  const used = new Set((st.exits || []).map(e => e.id));
  for (let n = 1; ; n++) if (!used.has(`exit${n}`)) return `exit${n}`;
}

/** A placeholder code that cannot collide with a real one already in the file. */
function freshCode() {
  const used = new Set();
  for (const s of store.doc.stations) for (const p of platformsOf(s)) used.add(String(p.id));
  for (let n = 90000; n < 99999; n++) if (!used.has(String(n))) return String(n);
  return '99999';
}
