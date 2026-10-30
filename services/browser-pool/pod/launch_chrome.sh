#!/usr/bin/env bash
# Launch the persistent headed Chrome for this pod.
# Env: MODE (mitm|no_mitm), UPSTREAM_PROXY_URL, TZ, LOCALE, PROFILE_ID
while [ ! -f /run/prepare.done ]; do sleep 0.3; done

export DISPLAY=:99
export TZ="${TZ:-UTC}"
export HOME=/home/browser

UPSTREAM="${UPSTREAM_PROXY_URL:-}"
MODE="${MODE:-mitm}"

if [ "$MODE" = "mitm" ]; then
  PROXY_SERVER="http://127.0.0.1:8080"   # always via local mitm; it chains
                                         # upstream (or goes direct) itself
elif [ -n "$UPSTREAM" ]; then
  PROXY_SERVER="$UPSTREAM"                # no_mitm: native TLS → upstream
else
  PROXY_SERVER="direct://"
fi

# A previous pod lifecycle may have died leaving the profile's singleton
# lock behind (keyed to the old container hostname) — always clear it.
rm -f "$HOME/profile/SingletonLock" "$HOME/profile/SingletonCookie" "$HOME/profile/SingletonSocket"

EXT_ARGS=""
if [ -d /opt/extensions ] && ls /opt/extensions/*/ >/dev/null 2>&1; then
  EXT_LIST="$(ls -d /opt/extensions/*/ | sed 's:/$::' | paste -sd, -)"
  EXT_ARGS="--load-extension=${EXT_LIST}"
  # Chrome >=137 ignores --load-extension on branded builds — also load each
  # extension over CDP once DevTools is up (Extensions.loadUnpacked).
  ( python3 /opt/pentera/load_extensions.py ) &
fi

echo "[chrome] mode=$MODE proxy=$PROXY_SERVER tz=$TZ lang=${LOCALE:-en-US}"
exec google-chrome-stable \
  --user-data-dir="$HOME/profile" \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins='*' \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --lang="${LOCALE:-en-US}" \
  --window-size=1920,1080 \
  --window-position=0,0 \
  --start-maximized \
  --disable-session-crashed-bubble \
  --hide-crash-restore-bubble \
  --disable-features=TranslateUI,ChromeWhatsNewUI,DisableLoadExtensionCommandLineSwitch \
  --disable-component-update \
  --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
  --enable-unsafe-swiftshader \
  --use-gl=angle --use-angle=swiftshader \
  --proxy-server="$PROXY_SERVER" \
  --no-sandbox \
  --disable-dev-shm-usage \
  $EXT_ARGS \
  about:blank
