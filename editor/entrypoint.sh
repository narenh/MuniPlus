#!/bin/sh
# Get REPO_ROOT into the best state we can, then serve. Never exit non-zero for
# a missing or unconfigured repo: Atlas falls back to read-only and says so.
set -e

: "${REPO_ROOT:=/srv/repo}"
: "${GIT_BRANCH:=}"

if [ -n "$GIT_REPO_URL" ] && [ ! -d "$REPO_ROOT/.git" ]; then
  echo "atlas: cloning into $REPO_ROOT"
  # GIT_REPO_URL may embed a token: https://x-access-token:TOKEN@github.com/owner/repo.git
  rm -rf "$REPO_ROOT.clone"
  if git clone --depth 50 ${GIT_BRANCH:+--branch "$GIT_BRANCH"} "$GIT_REPO_URL" "$REPO_ROOT.clone"; then
    rm -rf "$REPO_ROOT" && mv "$REPO_ROOT.clone" "$REPO_ROOT"
  else
    echo "atlas: clone failed - continuing read-only with the bundled copy"
    rm -rf "$REPO_ROOT.clone"
  fi
fi

if [ -d "$REPO_ROOT/.git" ]; then
  git config --global --add safe.directory "$REPO_ROOT"
  git -C "$REPO_ROOT" config user.name  "${GIT_AUTHOR_NAME:-Muni+ Atlas}"
  git -C "$REPO_ROOT" config user.email "${GIT_AUTHOR_EMAIL:-atlas@muniplus.local}"
  if [ -n "$GIT_BRANCH" ]; then
    git -C "$REPO_ROOT" checkout "$GIT_BRANCH" 2>/dev/null \
      || echo "atlas: branch $GIT_BRANCH not found; staying on $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
  fi
else
  echo "atlas: no git repo at $REPO_ROOT - starting read-only"
fi

exec node /srv/repo/editor/server.js
