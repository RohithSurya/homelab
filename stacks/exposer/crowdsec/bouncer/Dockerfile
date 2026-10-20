# CrowdSec publishes the firewall bouncer as a release binary only — there is no
# official image — so this wraps the pinned, checksum-verified arm64 binary.
# To upgrade: bump VERSION and SHA256 together, then `docker compose build crowdsec-bouncer`.
FROM alpine:3.24

ARG VERSION=v0.0.36
ARG SHA256=ce184d3b1ae5888189d237bb0ff1d2414be2ef12729750a7321a4abd2d253de6

# The host's rules (and Docker's) live in iptables-LEGACY, so `iptables` must
# resolve to the legacy backend here, not Alpine's default nf_tables one.
RUN apk add --no-cache iptables iptables-legacy ipset ca-certificates \
 && ln -sf /usr/sbin/xtables-legacy-multi /usr/local/bin/iptables \
 && ln -sf /usr/sbin/xtables-legacy-multi /usr/local/bin/ip6tables \
 && iptables -V | grep -q legacy \
 && wget -q -O /tmp/fwb.tgz \
      "https://github.com/crowdsecurity/cs-firewall-bouncer/releases/download/${VERSION}/crowdsec-firewall-bouncer-linux-arm64.tgz" \
 && echo "${SHA256}  /tmp/fwb.tgz" | sha256sum -c - \
 && tar -xzf /tmp/fwb.tgz -C /tmp \
 && install -m 0755 "/tmp/crowdsec-firewall-bouncer-${VERSION}/crowdsec-firewall-bouncer" /usr/local/bin/ \
 && rm -rf /tmp/fwb.tgz /tmp/crowdsec-firewall-bouncer-*

ENTRYPOINT ["crowdsec-firewall-bouncer", "-c", "/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml"]
