// The editor's server calls. Paths are relative, so the same frontend works at
// / (the editor, behind the login) and at /map/ (public, read-only).

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
  /** EditorState (transit_editor/models/editor.py). */
  state: () => fetch('api/state', { cache: 'no-store' }).then(j),
  /** ValidateRequest -> ValidateResponse: the server's validation and derived
   *  values for curation that has not been saved. */
  validate: (curation, signal) => post('api/validate', { curation }, { signal }),
  /** SaveRequest -> SaveResponse. 409 when the data branch moved on and the rebase
   *  did not apply; 422 when validation fails. */
  save: (curation, message, baseVersion) => post('api/save', { curation, message, baseVersion }),
  /** Recent data branch commits touching curation/. */
  history: () => fetch('api/history', { cache: 'no-store' }).then(j),
  /** ReviewResponse (transit_editor/editor/models.py): unassigned stops with proposals, dead
   *  platforms, dead stations, all as of the saved curation at `version`. */
  review: () => fetch('api/review', { cache: 'no-store' }).then(j),
  /** SnapshotFetchResponse: 511's two files, through the relay, and the drift
   *  against the committed snapshot. Commits nothing. 503 when the relay has no
   *  copy, 409 when a fetch is already running. */
  snapshotFetch: () => post('api/snapshot/fetch', {}),
  /** SaveResponse. 404 when `pending` has been replaced or committed, 409 when
   *  another refresh was committed since the fetch. */
  snapshotCommit: (pending, baseVersion) => post('api/snapshot/commit', { pending, baseVersion }),
  /** ShapesResponse: every line direction's path, `{shapes: {id: [[lon, lat]...]}}`.
   *  The browser's own cache is left on: the server's ETag is a hash of the
   *  shapes, so a reload costs a 304 until 511 redraws. */
  shapes: () => fetch('api/shapes').then(j),
};
