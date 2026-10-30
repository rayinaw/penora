"""mitmproxy addon: normalize flows into the pentera exchange format.

Writes one JSON record per completed flow to $DATA_DIR/exchanges.jsonl and
stores request/response bodies as files in $DATA_DIR/blobs/<ref>.

The sidecar's CDP capture (no_mitm mode) writes the exact same schema, so
downstream consumers never care which mode produced the data.
"""
import json
import os
import threading
import time
import uuid

from mitmproxy import http

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BLOB_DIR = os.path.join(DATA_DIR, "blobs")
EXCHANGE_FILE = os.path.join(DATA_DIR, "exchanges.jsonl")
MAX_BLOB = 20 * 1024 * 1024  # 20MB per body

_lock = threading.Lock()
_blob_dir_ready = False


def _store_blob(content: bytes | None) -> dict:
    global _blob_dir_ready
    if not _blob_dir_ready:
        os.makedirs(BLOB_DIR, exist_ok=True)
        _blob_dir_ready = True
    if not content:
        return {"size": 0, "ref": None, "truncated": False}
    ref = uuid.uuid4().hex
    truncated = len(content) > MAX_BLOB
    with open(os.path.join(BLOB_DIR, ref), "wb") as f:
        f.write(content[:MAX_BLOB])
    return {"size": len(content), "ref": ref, "truncated": truncated}


def _record(flow: http.HTTPFlow, error: str | None) -> dict:
    req = flow.request
    resp = flow.response
    rec = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "source": "mitm",
        "lease_id": os.environ.get("LEASE_ID"),
        "profile_id": os.environ.get("PROFILE_ID"),
        "page_url": None,
        "request": {
            "method": req.method,
            "url": req.pretty_url,
            "host": req.host,
            "path": req.path,
            "http_version": req.http_version,
            "headers": [[k, v] for k, v in req.headers.items()],
            "body": _store_blob(req.raw_content),
        },
        "response": None,
        "error": error,
        "client_conn": {
            "peer": list(flow.client_conn.peername) if flow.client_conn.peername else None
        },
    }
    if resp is not None:
        rec["response"] = {
            "status": resp.status_code,
            "reason": resp.reason,
            "http_version": resp.http_version,
            "headers": [[k, v] for k, v in resp.headers.items()],
            "body": _store_blob(resp.raw_content),
        }
    if flow.websocket is not None:
        rec["websocket"] = {
            "messages": len(flow.websocket.messages),
            "closed": flow.websocket.closed,
        }
    return rec


def _append(rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        with open(EXCHANGE_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class ExchangeLogger:
    def response(self, flow: http.HTTPFlow) -> None:
        try:
            _append(_record(flow, error=None))
        except Exception as e:  # never kill the proxy over logging
            print(f"[exchange-logger] response error: {e}")

    def error(self, flow: http.HTTPFlow) -> None:
        try:
            msg = flow.error.msg if flow.error else "flow error"
            _append(_record(flow, error=msg))
        except Exception as e:
            print(f"[exchange-logger] error handler failed: {e}")


addons = [ExchangeLogger()]
