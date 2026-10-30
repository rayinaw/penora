#!/usr/bin/env bash
# mitmproxy in explicit-proxy mode on :8080, chaining UPSTREAM_PROXY_URL.
# Disabled entirely when MODE=no_mitm (target sees real Chrome TLS).
while [ ! -f /run/prepare.done ]; do sleep 0.3; done

if [ "${MODE:-mitm}" != "mitm" ]; then
  echo "[mitm] MODE=$MODE — disabled, staying idle"
  exec sleep infinity
fi

export HOME=/home/browser

# Parse UPSTREAM_PROXY_URL → mitm --mode + --upstream-auth
python3 - <<'PY' > /run/mitm_args.sh
import os, shlex, urllib.parse
u = os.environ.get("UPSTREAM_PROXY_URL", "")
if not u:
    print("MITM_MODE=regular")
else:
    p = urllib.parse.urlparse(u if "://" in u else "http://" + u)
    scheme = "https" if p.scheme == "https" else "http"
    port = p.port or (443 if scheme == "https" else 80)
    print(f"MITM_MODE=upstream:{scheme}://{p.hostname}:{port}")
    if p.username:
        creds = urllib.parse.unquote(p.username) + ":" + urllib.parse.unquote(p.password or "")
        print(f"MITM_AUTH={shlex.quote(creds)}")
PY
source /run/mitm_args.sh

AUTH_ARG=""
[ -n "${MITM_AUTH:-}" ] && AUTH_ARG="--upstream-auth ${MITM_AUTH}"

echo "[mitm] mode=${MITM_MODE} capture→$DATA_DIR/exchanges.jsonl"
exec mitmdump \
  --mode "${MITM_MODE:-regular}" \
  ${AUTH_ARG} \
  --listen-host 127.0.0.1 \
  --listen-port 8080 \
  --set confdir="$HOME/.mitmproxy" \
  --set flow_detail=3 \
  --set stream_large_bodies=10m \
  --set connection_strategy=lazy \
  -s /opt/pentera/mitm_addon.py \
  --save-stream-file "$DATA_DIR/flows.mitm"
