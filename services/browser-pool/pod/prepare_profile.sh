#!/usr/bin/env bash
# One-shot prepare step (supervisord priority 5).
# - Ensures mitmproxy CA exists on the persistent volume
# - Trusts it in the NSS db that this profile's Chrome uses
# Other launch scripts gate on /run/prepare.done.
set -euo pipefail
export HOME=/home/browser
mkdir -p "$HOME/.pki/nssdb" "$DATA_DIR/blobs"

if [ "${MODE:-mitm}" = "mitm" ]; then
  # mitmproxy generates its CA under confdir on first run
  if [ ! -f "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" ]; then
    timeout 15 mitmdump --set confdir="$HOME/.mitmproxy" -p 18099 >/dev/null 2>&1 || true
  fi
  if [ -f "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" ]; then
    # certutil reads stdin for entropy noise — feed /dev/null so it can't
    # block forever inside a container, and cap with timeout either way.
    timeout 10 certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password \
      </dev/null 2>/dev/null || true
    timeout 10 certutil -d "sql:$HOME/.pki/nssdb" -A -t "CT,C,C" -n mitmproxy \
      -i "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" </dev/null 2>/dev/null || true
  else
    echo "[prepare] WARN: mitmproxy CA not generated; TLS capture will fail" >&2
  fi
fi

touch /run/prepare.done
echo "[prepare] done MODE=${MODE:-mitm} TZ=${TZ:-unset} PROFILE=${PROFILE_ID:-?}"
