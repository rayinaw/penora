#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HOME" "$DATA_DIR" "$DATA_DIR/blobs" "$HOME/profile" "$HOME/.pki/nssdb"
rm -f /run/prepare.done 2>/dev/null || true

exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
