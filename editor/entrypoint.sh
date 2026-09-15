#!/bin/sh
# Make sure REPO_ROOT holds a checkout the editor can commit to, then serve.
set -e

: "${REPO_ROOT:=/srv/repo}"
: "${GIT_BRANCH:=}"

if [ -n "$GIT_REPO_URL" ] && [ ! -d "$REPO_ROOT/.git" ]; then
  echo "atlas: cloning $GIT_REPO_URL into $REPO_ROOT"
  # GIT_REPO_URL may embed a token, e.g. https://x-access-token:TOKEN@github.com/owner/repo.git
  git clone --depth 50 ${GIT_BRANCH:+--branch "$GIT_BRANCH"} "$GIT_REPO_URL" "$REPO_ROOT"
fi

if [ -d "$REPO_ROOT/.git" ]; then
  git config --global --add safe.directory "$REPO_ROOT"
  git -C "$REPO_ROOT" config user.name  "${GIT_AUTHOR_NAME:-Muni+ Atlas}"
  git -C "$REPO_ROOT" config user.email "${GIT_AUTHOR_EMAIL:-atlas@muniplus.local}"
  [ -n "$GIT_BRANCH" ] && git -C "$REPO_ROOT" checkout "$GIT_BRANCH" 2>/dev/null || true
else
  echo "atlas: WARNING - $REPO_ROOT is not a git repo; commits will fail."
  echo "atlas: mount the repo there, or set GIT_REPO_URL."
fi

exec node /srv/editor/server.js
