# syntax=docker/dockerfile:1.7
#
# The real slife2 distribution in a container. The build context is staged
# from the slife2 checkout (build.py), and the project's own installer runs
# inside the image. The shipped slife2.yaml is used unchanged: this is not a
# reduced bench configuration.
FROM python:3.13-slim

ARG DEBIAN_FRONTEND=noninteractive

# mosquitto is installed as a system package so the installer does not have
# to unpack Debian packages into ~/.local as a non-root user. ssh is the
# localhost.run tunnel's client.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client xz-utils unzip dpkg \
      mosquitto mosquitto-clients ripgrep \
 && ln -sf /usr/sbin/mosquitto /usr/local/bin/mosquitto \
 && rm -rf /var/lib/apt/lists/*

# The same uv the installer would fetch, installed from PyPI instead of the
# ghcr.io image so a flaky registry TLS handshake does not block the build.
RUN pip install --no-cache-dir uv==0.12.17

RUN useradd --create-home --uid 1000 --shell /bin/bash slife2

WORKDIR /opt/slife2
COPY --chown=slife2:slife2 . /opt/slife2
# COPY --chown sets the copied entries; the directory WORKDIR created is still
# root's, and the installer needs to create .venv beside the sources.
RUN chown -R slife2:slife2 /opt/slife2

USER slife2
ENV HOME=/home/slife2 \
    PATH=/home/slife2/.local/bin:/opt/slife2/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    SLIFE2_RUNTIME_DIR=/home/slife2/.slife2/runtime \
    PYTHONPATH=/opt/slife2-bench/py

# Docker Desktop's kernel looks like WSL to credstore; this shim makes it use
# the native Linux backend instead of trying powershell.exe. See the file.
COPY --chown=slife2:slife2 docker/sitecustomize.py /opt/slife2-bench/py/sitecustomize.py

# The project's installer: Python 3.13, the venv, the external programs the
# shipped configuration names, and the credential hints. Running it is the
# point; a hand-built venv would be a different distribution.
RUN bash scripts/install.sh

# Warm the exact npx resolution path the shipped config names. Without this, a
# fresh container's first start can lose the initial MCP handshake while npx
# downloads the package, and the shell is absent for that run. The config is
# unchanged; only the package cache is warmed.
RUN for i in 1 2 3; do \
        npx -y @wonderwhy-er/desktop-commander@latest < /dev/null && exit 0; \
        sleep 2; \
    done; exit 1

COPY --chown=slife2:slife2 docker/entrypoint.sh /usr/local/bin/slife2-entrypoint
COPY --chown=slife2:slife2 docker/ask.py /opt/slife2-bench/
RUN chmod +x /usr/local/bin/slife2-entrypoint

EXPOSE 24000
ENTRYPOINT ["/usr/local/bin/slife2-entrypoint"]
