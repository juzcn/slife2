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
    SLIFE2_RUNTIME_DIR=/home/slife2/.slife2/runtime

# The project's installer: Python 3.13, the venv, the external programs the
# shipped configuration names, and the credential hints. Running it is the
# point; a hand-built venv would be a different distribution.
RUN bash scripts/install.sh

# Docker Desktop's kernel looks like WSL to credstore; this shim makes it use
# the native Linux backend instead of trying powershell.exe. See the file.
# **Inside the venv, not on `PYTHONPATH`.**  A task container is built from this
# image's filesystem, and a filesystem snapshot carries no `ENV` — so a shim
# reached only through an environment variable disappeared with it, and the
# first credential lookup in that container died on `powershell.exe`.  A
# `sitecustomize` in the venv's own site-packages is inherited by every
# interpreter that uses the venv, with nothing to export.
RUN site="$(/opt/slife2/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
 && cp /opt/slife2/docker/sitecustomize.py "$site/sitecustomize.py"

# Install **every** external source's package, and the browsers.  The bench's
# task containers unpack this image and its tool gate waits for each source to
# report ready; a cold `npx`/`uvx` install is exactly what a 180 s connect
# budget cannot cover, and a source that misses it fails the trial.  The list is
# the config's, not this file's — see docker/prewarm.py — and a failure fails
# the build rather than leaving an invisible hole.
COPY --chown=slife2:slife2 docker/prewarm.py /opt/slife2-bench/prewarm.py
RUN /opt/slife2/.venv/bin/python /opt/slife2-bench/prewarm.py

# **Both browser sources drive the installed Google Chrome.**  @playwright/mcp
# defaults to `channel: chrome` and chrome-devtools-mcp to `stable`; both look
# for /opt/google/chrome/chrome, and a tool call against a bundled Chromium
# fails with "Chromium distribution 'chrome' is not found".  The package
# install needs root; its own npm cache and HOME keep the slife2 user's
# `_npx` tree and `~/.cache` user-owned, which is what the tar ships.
USER root
RUN HOME=/root npm_config_cache=/tmp/root-npm \
      npx -y playwright@latest install chrome \
 && rm -rf /tmp/root-npm /root/.cache
USER slife2

COPY --chown=slife2:slife2 docker/entrypoint.sh /usr/local/bin/slife2-entrypoint
COPY --chown=slife2:slife2 docker/ask.py /opt/slife2-bench/
RUN chmod +x /usr/local/bin/slife2-entrypoint

EXPOSE 24000
ENTRYPOINT ["/usr/local/bin/slife2-entrypoint"]
