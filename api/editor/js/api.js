// The editor's server calls. Paths are relative, so the same frontend works at
// /editor/ (behind the login) and at /map/ (public, read-only).

const j = async (res) => {
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) {
    // Problem bodies are {error, message}; FastAPI's own 422 is {detail}.
    const msg = body.message || body.error || `HTTP ${res.status}`;
    throw Object.assign(new Error(msg), { body, status: res.status });
  }
  return body;
};

const post = (path, payload, opts = {}) => fetch(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
  ...opts,
}).then(j);

export const api = {
  /** EditorState (app/models/editor.py). */
  state: () => fetch('api/state', { cache: 'no-store' }).then(j),
  /** ValidateRequest -> ValidateResponse: the server's validation and derived
   *  values for curation that has not been saved. */
  validate: (curation, signal) => post('api/validate', { curation }, { signal }),
  /** SaveRequest -> SaveResponse. 409 when sf-transit moved on and the rebase
   *  did not apply; 422 when validation fails. */
  save: (curation, message, baseVersion) => post('api/save', { curation, message, baseVersion }),
  /** Recent sf-transit commits touching curation/. */
  history: () => fetch('api/history', { cache: 'no-store' }).then(j),
};
