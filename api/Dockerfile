# Muni+ API. Coolify builds this with base directory /api, so the build context is
# api/ and everything the server needs (app/, editor/) lives under it.
FROM python:3.13-slim

# git and ssh: the server clones and pushes sf-transit with the deploy key.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client \
 && rm -rf /var/lib/apt/lists/*

# An unprivileged user that owns /data. A named volume mounted there on first
# use takes this ownership, so the checkout and SQLite are writable without root.
RUN useradd --create-home --uid 10001 muni \
 && mkdir /data && chown muni:muni /data

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
# Editable, so the imported package is /srv/app and paths relative to it (the
# editor's static files in /srv/editor) resolve the same as in a checkout.
RUN pip install --no-cache-dir -e .
COPY editor ./editor

USER muni
ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

# Liveness only: /health answers 200 whenever the process serves. Whether the
# data and feeds are good is in its body, and is not a reason to restart.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# One worker, deliberately: see app/main.py and PLAN.md. --proxy-headers because
# Coolify's proxy terminates TLS in front of this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
