# Muni+ Atlas - the data.json editor.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not editor/:
#     docker build -f editor/Dockerfile -t atlas .
# In Coolify: Base Directory "/", Dockerfile Location "/editor/Dockerfile".
#
# With no environment variables at all this produces a working, read-only Atlas:
# the repo (including appdata/data.json) is copied in, and a second copy is kept
# as a seed so the editor still has something to show even if REPO_ROOT is later
# pointed at an empty volume. Set GIT_REPO_URL to make it editable.
#
# Nothing is installed from npm: the server is plain Node.

FROM node:22-alpine

RUN apk add --no-cache git openssh-client ca-certificates tini

WORKDIR /srv

# The whole repo, so REPO_ROOT's default (the parent of editor/) just works.
COPY . /srv/repo

# A fallback copy that survives REPO_ROOT being remounted.
RUN mkdir -p /srv/repo/editor/seed \
 && cp /srv/repo/appdata/data.json /srv/repo/editor/seed/data.json

ENV PORT=8787 \
    HOST=0.0.0.0 \
    REPO_ROOT=/srv/repo \
    DATA_FILE=appdata/data.json \
    GIT_AUTHOR_NAME="Muni+ Atlas" \
    GIT_AUTHOR_EMAIL="atlas@muniplus.local"

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
  CMD wget -qO- "http://127.0.0.1:${PORT}/api/state" >/dev/null || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["sh", "/srv/repo/editor/entrypoint.sh"]
