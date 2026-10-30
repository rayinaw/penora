#!/usr/bin/env python3
"""Load unpacked extensions into the running Chrome via CDP.

Chrome >=137 ignores --load-extension on branded builds, so launch_chrome.sh
runs this in the background after Chrome starts: it waits for the DevTools
endpoint, then calls Extensions.loadUnpacked once per /opt/extensions/*/ dir.
"""
import base64
import glob
import json
import os
import socket
import struct
import sys
import time
import urllib.request

CDP = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
EXT_ROOT = "/opt/extensions"


def browser_ws(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = json.load(urllib.request.urlopen(CDP + "/json/version", timeout=3))
            return d["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(1)
    raise SystemExit("[ext] CDP not ready in time")


def ws_call(ws_url, method, params):
    host, rest = ws_url.replace("ws://", "").split("/", 1)
    h, p = host.split(":")
    s = socket.create_connection((h, int(p)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    s.send(("GET /" + rest + " HTTP/1.1\r\nHost: " + host +
            "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = s.recv(4096).decode(errors="replace")
    if "101" not in resp.splitlines()[0]:
        raise RuntimeError("ws upgrade failed: " + resp.splitlines()[0])
    body = json.dumps({"id": 1, "method": method, "params": params}).encode()
    if len(body) < 126:
        hdr = b"\x81" + bytes([0x80 | len(body)])
    elif len(body) < 65536:
        hdr = b"\x81\xfe" + struct.pack(">H", len(body))
    else:
        hdr = b"\x81\xff" + struct.pack(">Q", len(body))
    s.send(hdr + b"\x00" * 4 + body)
    s.settimeout(15)
    data = s.recv(1 << 20)
    s.close()
    # strip 2-byte header (+ extended len if present); frames are unmasked from server
    i = 2
    ln = data[1] & 0x7F
    if ln == 126:
        i = 4
    elif ln == 127:
        i = 10
    return json.loads(data[i:])


def main():
    dirs = [d for d in sorted(glob.glob(EXT_ROOT + "/*/"))
            if os.path.isfile(d + "manifest.json")]
    if not dirs:
        print("[ext] none found under", EXT_ROOT)
        return
    ws = browser_ws()
    for d in dirs:
        try:
            r = ws_call(ws, "Extensions.loadUnpacked", {"path": d.rstrip("/")})
            print("[ext]", os.path.basename(d.rstrip("/")), "->", r)
        except Exception as e:
            print("[ext]", d, "load failed:", e)


if __name__ == "__main__":
    main()
