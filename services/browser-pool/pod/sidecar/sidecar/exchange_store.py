"""Shared reader/writer for the lease's exchange store.

Layout inside the lease data dir (mounted at /data in the pod):
  exchanges.jsonl   one normalized exchange record per line
  blobs/<ref>       raw request/response bodies
  screenshots/      png captures
  flows.mitm        raw mitmproxy stream (mitm mode only)

The mitm addon (mitm mode) and NetworkCapture (no_mitm mode) both append
records with the same schema; the API endpoints only read.
"""
import base64
import json
import os
import re
import threading
import time
import uuid
from collections import deque

MAX_BLOB = 20 * 1024 * 1024
TAIL_WINDOW = 8 * 1024 * 1024

_ref_re = re.compile(r"^[a-f0-9]{32}$")


class ExchangeStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.blobs_dir = os.path.join(data_dir, "blobs")
        self.shots_dir = os.path.join(data_dir, "screenshots")
        self.path = os.path.join(data_dir, "exchanges.jsonl")
        self._lock = threading.Lock()
        for d in (self.blobs_dir, self.shots_dir):
            os.makedirs(d, exist_ok=True)

    # ---------- write path (cdp capture) ----------

    def store_blob(self, content: bytes | None) -> dict:
        if not content:
            return {"size": 0, "ref": None, "truncated": False}
        ref = uuid.uuid4().hex
        truncated = len(content) > MAX_BLOB
        with open(os.path.join(self.blobs_dir, ref), "wb") as f:
            f.write(content[:MAX_BLOB])
        return {"size": len(content), "ref": ref, "truncated": truncated}

    def append(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ---------- read path (api) ----------

    def tail(self, limit: int = 200, host: str | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: deque = deque(maxlen=limit)
        with open(self.path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_WINDOW))
            data = f.read().decode("utf-8", "replace")
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            req = rec.get("request") or {}
            if host and host not in (req.get("host") or ""):
                continue
            out.append(rec)
        return list(out)

    def get(self, exchange_id: str, inline_max: int = 262_144) -> dict | None:
        if not os.path.exists(self.path):
            return None
        needle = f'"id":"{exchange_id}"'
        needle_sp = f'"id": "{exchange_id}"'
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if needle not in line and needle_sp not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") != exchange_id:
                    continue
                return self._with_bodies(rec, inline_max)
        return None

    def _with_bodies(self, rec: dict, inline_max: int) -> dict:
        for part in ("request", "response"):
            sub = rec.get(part)
            if not sub or not sub.get("body"):
                continue
            body = sub["body"]
            if not body.get("ref"):
                continue
            raw = self.blob_bytes(body["ref"])
            if raw is None:
                continue
            if len(raw) <= inline_max:
                try:
                    body["text"] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    body["b64"] = base64.b64encode(raw[:inline_max]).decode()
        return rec

    def blob_bytes(self, ref: str) -> bytes | None:
        if not _ref_re.match(ref):
            return None
        p = os.path.join(self.blobs_dir, ref)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def new_screenshot_path(self) -> str:
        return os.path.join(self.shots_dir, f"{int(time.time()*1000)}.png")
