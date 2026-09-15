// Right-hand inspector: everything about the selected station, and a card per
// platform. Edits are committed on change/blur so each field is one undo step.

import {
  store, edit, select, stationById, linesOf, esc, metresBetween,
} from './store.js';
import { flyToStation, refresh, hint, map } from './map.js';

const HEADINGS = ['northbound', 'southbound', 'eastbound', 'westbound'];
const AGENCIES = ['bart', 'caltrain'];
const ARROW = { northbound: 0, eastbound: 90, southbound: 180, westbound: 270 };

let pickStationFor = null;     // callback when the palette is used to pick a station

export function initInspector({ onNeedStationPicker }) {
  pickStationFor = onNeedStationPicker;
  document.getElementById('insp-close').onclick = () => { select(null, null); renderInspector(); };
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
  document.getElementById('insp-id').textContent = st.id;

  // line toggles
  const lineBox = document.getElementById('insp-lines');
  lineBox.innerHTML = '';
  for (const ln of store.doc.lines) {
    const on = (st.lines || []).includes(ln.id);
    const b = document.createElement('button');
    b.className = 'mini-bullet' + (on ? '' : ' off');
    b.style.background = ln.color;
    b.textContent = ln.shortName || ln.id;
    b.title = on ? `On ${ln.name} — click to remove` : `Add to ${ln.name}`;
    b.onclick = () => toggleLine(st.id, ln.id);
    lineBox.appendChild(b);
  }

  document.getElementById('insp-body').innerHTML = `
    ${sectionStation(st)}
    ${sectionPlatforms(st)}
    ${sectionTransfers(st)}
    ${sectionDanger(st)}
  `;
  wire(st);
}

// ------------------------------------------------------------------ sections
function sectionStation(st) {
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
      <label class="micro">Kind</label>
      <div class="seg" id="f-kind">
        <button data-v="streetLevel" class="${st.kind === 'streetLevel' ? 'on' : ''}">Street level</button>
        <button data-v="underground" class="${st.kind === 'underground' ? 'on' : ''}">Underground</button>
      </div>
    </div>
    <div class="field">
      <label class="micro">Centre coordinate</label>
      <div class="pair">
        <input class="inp mono" id="f-lat" value="${st.latitude}" inputmode="decimal">
        <input class="inp mono" id="f-lon" value="${st.longitude}" inputmode="decimal">
      </div>
    </div>
    <div class="field">
      <button class="btn" id="f-centre" style="width:100%;justify-content:center">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="2" fill="currentColor"/><circle cx="8" cy="8" r="5.5" stroke="currentColor" stroke-width="1.5"/><path d="M8 .8v2M8 13.2v2M.8 8h2M13.2 8h2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        Centre on its platforms
      </button>
    </div>
  </div>`;
}

function sectionPlatforms(st) {
  const cards = st.platforms.map((p, i) => {
    const d = store.drift?.get(String(p.id));
    const sel = store.selPlatform === String(p.id);
    let badge = '';
    if (d?.kind === 'missing') {
      badge = `<div class="drift-badge missing">Not in the current SFMTA feed</div>`;
    } else if (d?.kind === 'moved') {
      badge = `<div class="drift-badge moved">
        Feed places this pole ${d.metres} m away
        <button data-act="snap" data-i="${i}">snap to feed</button></div>`;
    } else if (d?.kind === 'ok') {
      badge = `<div class="drift-badge ok">Matches the feed${d.metres ? ` (${d.metres} m)` : ''}</div>`;
    }

    return `
    <div class="plat ${sel ? 'sel' : ''}" data-i="${i}" data-code="${esc(p.id)}">
      <div class="plat-head">
        <div class="compass" title="${esc(p.heading)}">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" style="transform:rotate(${ARROW[p.heading] ?? 0}deg)">
            <path d="M8 2.2l3.6 10-3.6-2.6-3.6 2.6L8 2.2z" fill="currentColor"/>
          </svg>
        </div>
        <div style="min-width:0">
          <div class="plat-code">${esc(p.id)}</div>
          <div class="plat-sub">${esc(p.stopName || '—')}</div>
        </div>
        <div class="plat-actions">
          <button class="icon-btn" data-act="locate" data-i="${i}" title="Show on the map">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 14.5S13 10 13 6.4A5 5 0 003 6.4C3 10 8 14.5 8 14.5z" stroke="currentColor" stroke-width="1.5"/><circle cx="8" cy="6.3" r="1.8" fill="currentColor"/></svg>
          </button>
          <button class="icon-btn danger" data-act="del-plat" data-i="${i}" title="Remove this platform">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3.5 4.5h9M6.5 4.5V3h3v1.5M5 4.5l.6 8h4.8l.6-8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
        </div>
      </div>

      <div class="field">
        <label class="micro">Stop name</label>
        <input class="inp" data-f="stopName" data-i="${i}" value="${esc(p.stopName || '')}">
      </div>
      <div class="field">
        <label class="micro">Direction label <span style="color:var(--ink-faint);text-transform:none;letter-spacing:0">— shown only when set</span></label>
        <input class="inp" data-f="name" data-i="${i}" value="${esc(p.name ?? '')}" placeholder="e.g. To Castro">
      </div>
      <div class="field">
        <label class="micro">Heading</label>
        <select class="inp" data-f="heading" data-i="${i}">
          ${HEADINGS.map(h => `<option value="${h}" ${p.heading === h ? 'selected' : ''}>${h}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label class="micro">Coordinate — drag the pole on the map</label>
        <div class="pair">
          <input class="inp mono" data-f="latitude" data-i="${i}" value="${p.latitude}" inputmode="decimal">
          <input class="inp mono" data-f="longitude" data-i="${i}" value="${p.longitude}" inputmode="decimal">
        </div>
      </div>
      ${badge}
    </div>`;
  }).join('');

  return `
  <div class="sect">
    <div class="sect-head">
      <div class="micro">Platforms · ${st.platforms.length}</div>
      <button class="icon-btn" id="add-plat" title="Add a platform">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3.5v9M3.5 8h9" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${cards || '<div class="empty">No platforms. A station needs at least one.</div>'}
  </div>`;
}

function sectionTransfers(st) {
  const t = (st.transferStations || []).map(id => {
    const to = stationById(id);
    return `<span class="tchip" title="${esc(id)}">${esc(to ? to.name : id)}
      <button class="x" data-act="untransfer" data-id="${esc(id)}">
        <svg width="9" height="9" viewBox="0 0 10 10" fill="none"><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      </button></span>`;
  }).join('');

  const a = AGENCIES.map(ag => {
    const on = (st.transferAgencies || []).includes(ag);
    return `<button class="tchip ${on ? '' : 'add'}" data-act="agency" data-ag="${ag}"
      style="${on ? 'border-style:solid;color:var(--ink)' : ''}">${ag.toUpperCase()}</button>`;
  }).join('');

  return `
  <div class="sect">
    <div class="sect-head"><div class="micro">Transfers</div></div>
    <div class="field">
      <label class="micro">To other stations <span style="text-transform:none;letter-spacing:0;color:var(--ink-faint)">— one-way by design</span></label>
      <div class="chips">${t}
        <button class="tchip add" data-act="add-transfer">
          <svg width="10" height="10" viewBox="0 0 12 12" fill="none"><path d="M6 2.5v7M2.5 6h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          Link a station
        </button>
      </div>
    </div>
    <div class="field">
      <label class="micro">Other agencies</label>
      <div class="chips">${a}</div>
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

// --------------------------------------------------------------------- wiring
function wire(st) {
  const body = document.getElementById('insp-body');
  const sid = st.id;

  const commit = (label, fn) => edit(label, d => fn(d.stations.find(s => s.id === sid), d));

  // --- station fields
  const name = body.querySelector('#f-name');
  name.onchange = () => {
    const v = name.value.trim();
    if (!v || v === st.name) { name.value = st.name; return; }
    commit(`Rename ${st.id}`, s => { s.name = v; });
  };

  const idf = body.querySelector('#f-id');
  idf.onchange = () => {
    const v = idf.value.trim();
    if (!v || v === sid) { idf.value = sid; return; }
    if (store.doc.stations.some(s => s.id === v)) {
      idf.classList.add('bad');
      hint(`A station with id "${v}" already exists`);
      return;
    }
    edit(`Rename id ${sid} → ${v}`, d => {
      d.stations.find(s => s.id === sid).id = v;
      for (const l of d.lines) l.stationIds = l.stationIds.map(x => x === sid ? v : x);
      for (const s of d.subways || []) s.stationIds = s.stationIds.map(x => x === sid ? v : x);
      for (const s of d.stations) {
        s.transferStations = (s.transferStations || []).map(x => x === sid ? v : x);
      }
    });
    select(v, store.selPlatform);
  };

  body.querySelectorAll('#f-kind button').forEach(b => {
    b.onclick = () => {
      if (b.dataset.v === st.kind) return;
      commit(`${st.id} → ${b.dataset.v}`, s => { s.kind = b.dataset.v; });
    };
  });

  for (const [id, key] of [['#f-lat', 'latitude'], ['#f-lon', 'longitude']]) {
    const el = body.querySelector(id);
    el.onchange = () => {
      const v = Number(el.value);
      if (!Number.isFinite(v)) { el.value = st[key]; return; }
      commit(`Move ${st.id}`, s => { s[key] = v; });
    };
  }

  body.querySelector('#f-centre').onclick = () => {
    if (!st.platforms.length) return;
    const lat = st.platforms.reduce((n, p) => n + p.latitude, 0) / st.platforms.length;
    const lon = st.platforms.reduce((n, p) => n + p.longitude, 0) / st.platforms.length;
    const moved = metresBetween(st, { latitude: lat, longitude: lon });
    commit(`Centre ${st.id}`, s => {
      s.latitude = Math.round(lat * 1e6) / 1e6;
      s.longitude = Math.round(lon * 1e6) / 1e6;
    });
    hint(`Centred on ${st.platforms.length} platforms — moved ${moved} m`);
  };

  // --- platform fields
  body.querySelectorAll('[data-f]').forEach(el => {
    el.onchange = () => {
      const i = Number(el.dataset.i), f = el.dataset.f;
      let v = el.value;
      if (f === 'latitude' || f === 'longitude') {
        v = Number(v);
        if (!Number.isFinite(v)) { renderInspector(); return; }
      }
      if (f === 'name') v = v.trim() === '' ? null : v.trim();
      if (f === 'stopName') v = v.trim();
      commit(`Edit ${st.platforms[i].id}`, s => { s.platforms[i][f] = v; });
    };
  });

  body.querySelectorAll('.plat').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]') || ev.target.closest('input,select')) return;
      select(sid, card.dataset.code);
      renderInspector();
      refresh('platforms');
    });
  });

  body.querySelectorAll('[data-act]').forEach(b => {
    b.onclick = ev => {
      ev.stopPropagation();
      const act = b.dataset.act, i = Number(b.dataset.i);

      if (act === 'locate') {
        const p = st.platforms[i];
        select(sid, String(p.id));
        map.easeTo({ center: [p.longitude, p.latitude], zoom: Math.max(map.getZoom(), 17.4), duration: 800 });
        renderInspector(); refresh('platforms');
      }

      if (act === 'snap') {
        const p = st.platforms[i];
        const d = store.drift?.get(String(p.id));
        if (!d) return;
        commit(`Snap ${p.id} to the feed`, s => {
          s.platforms[i].latitude = d.feedLat;
          s.platforms[i].longitude = d.feedLon;
        });
        hint(`Snapped ${p.id} to the SFMTA coordinate`);
      }

      if (act === 'del-plat') {
        if (st.platforms.length === 1) { hint('A station must keep at least one platform'); return; }
        const code = st.platforms[i].id;
        commit(`Remove platform ${code}`, s => { s.platforms.splice(i, 1); });
      }

      if (act === 'untransfer') {
        commit(`Unlink ${b.dataset.id}`, s => {
          s.transferStations = (s.transferStations || []).filter(x => x !== b.dataset.id);
        });
      }

      if (act === 'agency') {
        const ag = b.dataset.ag;
        commit(`Toggle ${ag} at ${st.id}`, s => {
          const list = s.transferAgencies || (s.transferAgencies = []);
          const k = list.indexOf(ag);
          if (k >= 0) list.splice(k, 1); else list.push(ag);
        });
      }

      if (act === 'add-transfer') {
        pickStationFor?.(target => {
          if (!target || target === sid) return;
          commit(`Link ${sid} → ${target}`, s => {
            const list = s.transferStations || (s.transferStations = []);
            if (!list.includes(target)) list.push(target);
          });
        });
      }
    };
  });

  body.querySelector('#add-plat').onclick = () => {
    const code = nextFreeCode();
    commit('Add platform', s => {
      s.platforms.push({
        id: code,
        heading: 'northbound',
        name: null,
        stopName: s.name,
        latitude: s.latitude,
        longitude: s.longitude,
      });
    });
    select(sid, code);
    hint(`Added platform ${code} — set its real stop code, then drag the pole into place`);
  };

  body.querySelector('#del-station').onclick = () => {
    if (!confirm(`Delete "${st.name}"?\n\nIt will also be removed from every line, subway and transfer list that references it.`)) return;
    edit(`Delete ${sid}`, d => {
      d.stations = d.stations.filter(s => s.id !== sid);
      for (const l of d.lines) l.stationIds = l.stationIds.filter(x => x !== sid);
      for (const s of d.subways || []) s.stationIds = s.stationIds.filter(x => x !== sid);
      for (const s of d.stations) {
        s.transferStations = (s.transferStations || []).filter(x => x !== sid);
      }
    });
    select(null, null);
  };
}

function toggleLine(sid, lineId) {
  const st = stationById(sid);
  const on = (st.lines || []).includes(lineId);
  edit(`${on ? 'Remove' : 'Add'} ${sid} ${on ? 'from' : 'to'} ${lineId}`, d => {
    const s = d.stations.find(x => x.id === sid);
    const l = d.lines.find(x => x.id === lineId);
    if (on) {
      s.lines = (s.lines || []).filter(x => x !== lineId);
      l.stationIds = l.stationIds.filter(x => x !== sid);
    } else {
      (s.lines || (s.lines = [])).push(lineId);
      if (!l.stationIds.includes(sid)) l.stationIds.push(sid);
    }
  });
}

/** A placeholder code that cannot collide with a real one already in the file. */
function nextFreeCode() {
  const used = new Set();
  for (const s of store.doc.stations) for (const p of s.platforms) used.add(String(p.id));
  for (let n = 90000; n < 99999; n++) if (!used.has(String(n))) return String(n);
  return '99999';
}
