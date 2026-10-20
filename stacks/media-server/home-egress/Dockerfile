FROM alpine:3.22
RUN apk add --no-cache tinyproxy
# config + filter are bind-mounted from ./home-egress (edit, then `docker restart home-egress`)
USER nobody
EXPOSE 8888
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
