# Self-hosted GitHub Actions runner for the homelab pipeline.
# One image for both machines: the Jetson (arm64, label dev) and the HP (amd64, label prod).
# Jobs drive the HOST's Docker through the mounted socket, so stacks deployed from here are
# ordinary host containers; deploy folders are bind-mounted at the same path as on the host.
FROM ubuntu:24.04

ARG TARGETARCH
ARG RUNNER_VERSION=2.338.0
ARG RUNNER_SHA256_AMD64=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
ARG RUNNER_SHA256_ARM64=628b4a7258487b80c1d3c221095a7ded349f5ae0175fd3dfab708d07428f041b
ARG BWS_VERSION=2.1.0
ARG BWS_SHA256_ARM64=18253757286e119d450133a87eb463bf8c1ce418ce24c834f4f250d60cba6f9e
ARG BWS_SHA256_AMD64=ba8233c3a4aee5d43e3c73bbd04d99e9bc5aba13bbbfd06d89b073abe732b860

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq rsync python3 sqlite3 gnupg tini unzip \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

# Actions runner, pinned + checked against the SHA-256 GitHub publishes in the release notes.
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in amd64) a=x64; s="$RUNNER_SHA256_AMD64";; arm64) a=arm64; s="$RUNNER_SHA256_ARM64";; esac; \
    mkdir -p /opt/runner && cd /opt/runner \
 && curl -fsSL -o r.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${a}-${RUNNER_VERSION}.tar.gz" \
 && echo "$s  r.tgz" | sha256sum -c - \
 && tar -xzf r.tgz && rm r.tgz \
 && ./bin/installdependencies.sh >/dev/null \
 && rm -rf /var/lib/apt/lists/*

# bws (Bitwarden Secrets Manager CLI) for envvault.py -- same pinned install as media-bridge.
COPY install_bws.py /tmp/install_bws.py
RUN python3 /tmp/install_bws.py && rm /tmp/install_bws.py

COPY entrypoint.sh /entrypoint.sh
# Ctrl-C is the runner's clean exit (it deletes its GitHub session); tini passes it on.
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
