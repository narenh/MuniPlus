'use strict';
// Thin wrapper over the git CLI. Every call is scoped to the repo root and
// every write is scoped to the one file the editor owns, so a stray change
// elsewhere in the working tree can never ride along in an editor commit.

const { execFile } = require('child_process');
const path = require('path');

function makeGit(repoRoot, { authorName, authorEmail, remote = 'origin' } = {}) {
  function run(args, opts = {}) {
    return new Promise((resolve, reject) => {
      execFile('git', ['-C', repoRoot, ...args], {
        maxBuffer: 32 * 1024 * 1024,
        env: { ...process.env, GIT_TERMINAL_PROMPT: '0' },
        ...opts,
      }, (err, stdout, stderr) => {
        if (err) { err.stderr = String(stderr || ''); return reject(err); }
        resolve(String(stdout));
      });
    });
  }

  const api = {
    run,

    async available() {
      try { await run(['rev-parse', '--git-dir']); return true; }
      catch { return false; }
    },

    async branch() {
      return (await run(['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
    },

    async head() {
      try {
        const line = (await run(['log', '-1', '--format=%H%x00%h%x00%an%x00%aI%x00%s'])).trim();
        const [sha, short, author, date, subject] = line.split('\0');
        return { sha, short, author, date, subject };
      } catch { return null; }
    },

    /** Is the one file we own modified in the working tree? */
    async isDirty(file) {
      const out = await run(['status', '--porcelain', '--', file]);
      return out.trim().length > 0;
    },

    /** Unified diff of our file only. */
    async diff(file) {
      return run(['diff', '--', file]);
    },

    async log(file, limit = 40) {
      const out = await run([
        'log', `-${limit}`, '--format=%H%x00%h%x00%an%x00%aI%x00%s', '--', file,
      ]);
      return out.split('\n').filter(Boolean).map(l => {
        const [sha, short, author, date, subject] = l.split('\0');
        return { sha, short, author, date, subject };
      });
    },

    /** The file's contents at a given revision. */
    async show(rev, file) {
      return run(['show', `${rev}:${file}`]);
    },

    async discard(file) {
      await run(['checkout', '--', file]);
    },

    /**
     * Stage ONLY our file and commit it. Returns null when there was nothing
     * to commit, so a no-op save does not create an empty commit.
     */
    async commit(file, message) {
      await run(['add', '--', file]);
      const staged = await run(['diff', '--cached', '--name-only', '--', file]);
      if (!staged.trim()) return null;

      const args = ['commit', '-m', message, '--only', '--', file];
      if (authorName && authorEmail) {
        args.unshift('-c', `user.name=${authorName}`, '-c', `user.email=${authorEmail}`);
      }
      await run(args);
      return api.head();
    },

    async push(branch) {
      return run(['push', remote, `HEAD:${branch}`]);
    },
  };

  return api;
}

module.exports = { makeGit };
