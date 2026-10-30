#!/usr/bin/env bash
while [ ! -f /run/prepare.done ]; do sleep 0.3; done

ARGS="-display :99 -forever -shared -rfbport 5900 -listen 0.0.0.0 -o /var/log/x11vnc.log"
if [ -n "${VNC_PASSWORD:-}" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /run/vncpass
  ARGS="$ARGS -rfbauth /run/vncpass"
else
  # Acceptable only because orchestrator binds noVNC/VNC to 127.0.0.1 and
  # access is expected via Tailscale/SSH tunnel.
  ARGS="$ARGS -nopw"
fi
exec x11vnc $ARGS
