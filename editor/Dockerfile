# Muni+ Atlas - the data.json editor.
#
# The image carries git and a clone of the repo, because Save commits to the
# working tree. Nothing is installed from npm: the server is plain Node.

FROM node:22-alpine

RUN apk add --no-cache git openssh-client ca-certificates tini

WORKDIR /srv

# The repo itself is provided at runtime (a Coolify volume, or cloned by the
# entrypoint), so only the editor is baked into the image.
COPY . /srv/editor

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
CMD ["sh", "/srv/editor/entrypoint.sh"]
