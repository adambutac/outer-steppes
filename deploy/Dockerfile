# Outer Steppes bridge (DESIGN.md, Deployment and rollout).
#
# Build from the repo root (the context must contain pyproject.toml and src/):
#   podman build --format docker -f deploy/Dockerfile -t outer-steppes .
# Podman is the supported runtime (DESIGN.md → Deployment and rollout → Runtime). --format docker keeps the
# HEALTHCHECK below, which Podman's default OCI format drops.
#
# Run it, then expose it with Tailscale Funnel on the host (ADR-0015):
#   podman run -d --name outer-steppes -p 127.0.0.1:8080:8080 \
#     -v outer-steppes-data:/var/lib/outer-steppes --env-file /etc/outer-steppes/env outer-steppes
#   tailscale funnel --bg 127.0.0.1:8080
#
# - Always publish as 127.0.0.1:8080. A bare `-p 8080:8080` binds 0.0.0.0 on
#   the host and exposes the bridge directly over plain HTTP, bypassing
#   Funnel's TLS. Don't count on a host firewall such as ufw to filter a
#   published port: whether it does depends on the runtime and its mode.
# - Keep the env file outside the repository. It holds the registration token.
#   (`osctl container` also accepts a .env at the repo root, which .gitignore
#   and .dockerignore both exclude.)
# - The volume is this container's own database: registrations and OAuth
#   clients made against another database do not carry over.
#
# Only the public listener is exposed. The admin listener (/v1, /metrics,
# /readyz) stays on the container's loopback; run the operator CLI inside.
# It inherits the container's registration token; -it is needed for the
# hidden prompts of `member add` and `webhook set`. On the host,
# `python scripts/osctl.py bridge status` does this itself:
#   podman exec -it outer-steppes osctl bridge status
# Only osctl.py is copied from scripts/ (a test checks it). osctl_command.py
# is deliberately not in this image: it is the only code that uses the bot
# token, and it runs from a separate environment. osctl_container.py drives
# podman on the host. Inside, `osctl container` and `osctl command` say they
# run on the host.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 outer-steppes \
 && useradd --system --uid 10001 --gid outer-steppes --home-dir /nonexistent --shell /usr/sbin/nologin outer-steppes \
 && install -d -o outer-steppes -g outer-steppes -m 0700 /var/lib/outer-steppes

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
 && rm -rf /app/src
COPY scripts/osctl.py /usr/local/bin/osctl
RUN chmod 0755 /usr/local/bin/osctl \
 && sed -i '1i #!/usr/local/bin/python' /usr/local/bin/osctl

# In a container the public listener binds all interfaces so the published
# port (127.0.0.1:8080 on the host, where Funnel points) reaches it; the admin
# listener must stay on loopback (the server enforces it).
ENV PUBLIC_BIND=0.0.0.0:8080 \
    ADMIN_BIND=127.0.0.1:8081 \
    DATABASE_PATH=/var/lib/outer-steppes/bridge.db \
    OSCTL_ADMIN_URL=http://127.0.0.1:8081

VOLUME ["/var/lib/outer-steppes"]
EXPOSE 8080
USER outer-steppes

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/readyz', timeout=4).status == 200 else 1)"]

# umask 077 here as well as in the process, so nothing the shell creates is group/world readable.
ENTRYPOINT ["/bin/sh", "-c", "umask 077 && exec outer-steppes"]
