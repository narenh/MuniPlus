# Canopy Transit Editor. Coolify builds this with base directory /editor, so the
# build context is editor/ and everything the server needs (transit_editor/,
# frontend/) lives under it.
FROM python:3.13-slim

# git and ssh: the server clones and pushes the data branch with the deploy key.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*

# An unprivileged user that owns /data. A named volume mounted there on first
# use takes this ownership, so the checkout is writable without root.
RUN useradd --create-home --uid 10001 editor \
 && mkdir /data && chown editor:editor /data

WORKDIR /srv
COPY pyproject.toml ./
COPY transit_editor ./transit_editor
# Editable, so the imported package is /srv/transit_editor and paths relative to
# it (the frontend in /srv/frontend) resolve the same as in a checkout.
RUN pip install --no-cache-dir -e .
COPY frontend ./frontend

USER editor
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

# Liveness only: /health answers 200 whenever the process serves. Whether the
# data is loaded is in its body, and is not a reason to restart.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# One worker, deliberately: see transit_editor/main.py. --proxy-headers because
# Coolify's proxy terminates TLS in front of this.
CMD ["uvicorn", "transit_editor.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
