"""traffic-bridge — ship pentera browser-pool exchanges into ARTEX's traffic store.

Watches <DATA_ROOT>/*/exchanges.jsonl (the dirs pool-manager bind-mounts into
pods at /data), converts every new record to ARTEX's ExternalExchange shape and
POSTs it to /api/traffic/ingest in batches. Auth = ARTEX JWT from /api/auth/login,
auto-refreshed on 401.

Record mapping (pentera exchange -> ExternalExchange):
    request.method            -> method
    request.url               -> url            (skipped unless http(s))
    response.status           -> status         (records without a response are skipped)
    request.headers [[k,v]]   -> requestHeaders (last value wins; see also multi-join)
    response.headers [[k,v]]  -> responseHeaders
    request/response body     -> text body resolved via blobs/<ref>; binary skipped

State: byte offsets per file in <DATA_ROOT>/.bridge-state.json, so restarts
resume instead of re-shipping (ARTEX has no ingest-side dedupe — resending a
line would create a duplicate row).

Env:
    DATA_ROOT         default /lease-data   (pool data_root mounted read-only)
    ARTEX_URL         required, e.g. http://artex:8787
    ARTEX_PASSWORD    ARTEX admin password (login -> JWT)
    ARTEX_TOKEN       alternatively a static JWT (expires ~7d; password is preferred)
    POLL_S            scan interval, default 3
    BATCH             records per ingest POST, default 50 (server cap)
    MAX_BODY          cap per body shipped, default 256KiB (server also caps)
    SKIP_HOSTS        comma-separated host substrings to drop (e.g. your own infra)
"""
import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

DATA_ROOT = os.environ.get("DATA_ROOT", "/lease-data").rstrip("/")
ARTEX_URL = os.environ.get("ARTEX_URL", "").rstrip("/")
ARTEX_PASSWORD = os.environ.get("ARTEX_PASSWORD", "")
ARTEX_TOKEN = os.environ.get("ARTEX_TOKEN", "")
POLL_S = float(os.environ.get("POLL_S", "3"))
BATCH = int(os.environ.get("BATCH", "50"))
MAX_BODY = int(os.environ.get("MAX_BODY", str(256 * 1024)))
SKIP_HOSTS = [s.strip().lower() for s in os.environ.get("SKIP_HOSTS", "").split(",") if s.strip()]
STATE_PATH = os.environ.get("BRIDGE_STATE_PATH", "/state/bridge-state.json")
REF_RE = re.compile(r"^[a-f0-9]{32}$")

# MIME classes ARTEX's own extension also skips — binary payloads never decode
# as text and would only pollute the FTS index.
SKIP_CT_PREFIX = ("image/", "font/", "audio/", "video/")
SKIP_CT_EXACT = {"application/octet-stream", "application/pdf",
                 "application/zip", "application/x-protobuf"}

_token = None
_token_at = 0.0
TOKEN_TTL = 6 * 24 * 3600  # server JWTs live 7d; refresh a day early


def log(msg):
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def get_token(force=False):
    global _token, _token_at
    if ARTEX_TOKEN and not force:
        return ARTEX_TOKEN
    if _token and not force and time.time() - _token_at < TOKEN_TTL:
        return _token
    if not ARTEX_PASSWORD:
        raise RuntimeError("set ARTEX_PASSWORD (preferred) or ARTEX_TOKEN")
    st, res = _http("POST", f"{ARTEX_URL}/api/auth/login",
                    {"username": "ARTEX", "password": ARTEX_PASSWORD})
    if st != 200 or not res.get("token"):
        raise RuntimeError(f"artex login failed: {st} {res}")
    _token, _token_at = res["token"], time.time()
    log("artex login ok (new JWT)")
    return _token


# --------------------------------------------------------------- conversion --

def pairs_to_map(pairs):
    out = {}
    for kv in pairs or []:
        if isinstance(kv, (list, tuple)) and len(kv) == 2:
            k, v = str(kv[0]).lower(), str(kv[1])
            out[k] = (out[k] + ", " + v) if k in out else v
    return out


def blob_text(lease_dir, body_obj):
    """Resolve a {size, ref, truncated} body to text; None when absent/binary."""
    if not body_obj or not body_obj.get("ref"):
        return ""
    ref = body_obj["ref"]
    if not REF_RE.match(ref):
        return ""
    path = os.path.join(lease_dir, "blobs", ref)
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BODY + 1)
    except OSError:
        return ""
    try:
        text = raw[:MAX_BODY].decode("utf-8")
    except UnicodeDecodeError:
        return ""  # binary — skip, matches extension behavior
    if len(raw) > MAX_BODY:
        text += "\n... [truncated by bridge]"
    return text


def is_binaryish(headers_map):
    ct = headers_map.get("content-type", "").split(";")[0].strip().lower()
    return ct in SKIP_CT_EXACT or any(ct.startswith(p) for p in SKIP_CT_PREFIX)


def convert(lease_dir, rec):
    """pentera exchange dict -> ExternalExchange dict, or None to skip."""
    req = rec.get("request") or {}
    url = req.get("url") or ""
    if not url.startswith(("http://", "https://")):
        return None
    host_l = (req.get("host") or "").lower()
    if any(s in host_l for s in SKIP_HOSTS):
        return None
    resp = rec.get("response")
    if not resp or resp.get("status") is None:
        return None  # aborted/failed request — keep the index honest
    req_h = pairs_to_map(req.get("headers"))
    resp_h = pairs_to_map(resp.get("headers"))
    req_body = "" if is_binaryish(req_h) else blob_text(lease_dir, req.get("body"))
    resp_body = "" if is_binaryish(resp_h) else blob_text(lease_dir, resp.get("body"))
    return {
        "method": req.get("method") or "GET",
        "url": url,
        "status": resp.get("status") or 0,
        "requestHeaders": req_h,
        "requestBody": req_body,
        "responseHeaders": resp_h,
        "responseBody": resp_body,
    }


# -------------------------------------------------------------------- loop --

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log(f"state save failed: {e}")


def find_exchange_files():
    out = []
    try:
        for name in os.listdir(DATA_ROOT):
            p = os.path.join(DATA_ROOT, name, "exchanges.jsonl")
            if os.path.isfile(p):
                out.append((name, p))
    except OSError as e:
        log(f"scan failed: {e}")
    return out


def read_new_lines(path, offset):
    """Read complete jsonl lines from offset onward. Returns (lines, new_offset).
    A partial trailing line (writer mid-append) is left for the next poll."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:  # file rotated/truncated
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], offset
        data = data[:cut + 1]
    new_offset = offset + len(data)
    return data.decode("utf-8", "replace").splitlines(), new_offset


def main():
    if not ARTEX_URL:
        log("FATAL: ARTEX_URL not set")
        sys.exit(1)
    log(f"watching {DATA_ROOT} -> {ARTEX_URL}/api/traffic/ingest (batch={BATCH}, poll={POLL_S}s)")
    state = load_state()
    seen_ids = set()  # exchange ids already shipped this run (extra safety)
    backoff = 1.0
    pending = []  # (lease_dir, file_key, converted) not yet acked
    pending_keys = []

    while True:
        try:
            # 1) collect
            for lease_id, path in find_exchange_files():
                key = os.path.basename(path) + ":" + lease_id
                # state keyed by lease_id (one file per lease)
                offset = state.get(lease_id, 0)
                lines, new_off = read_new_lines(path, offset)
                if not lines:
                    continue
                n = 0
                for line in lines:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = rec.get("id")
                    if not rid or rid in seen_ids:
                        continue
                    conv = convert(os.path.dirname(path), rec)
                    if conv is None:
                        seen_ids.add(rid)
                        continue
                    pending.append(conv)
                    pending_keys.append(rid)
                    n += 1
                state[lease_id] = new_off  # consumed regardless; pending flush decides shipping
                save_state(state)
                if n:
                    log(f"{lease_id[:8]}: +{n} exchanges queued")

            # 2) flush in batches; on failure keep pending and back off
            while pending:
                batch, keys = pending[:BATCH], pending_keys[:BATCH]
                tok = get_token()
                st, res = _http("POST", f"{ARTEX_URL}/api/traffic/ingest",
                                {"exchanges": batch},
                                {"Authorization": f"Bearer {tok}"}, timeout=60)
                if st == 401 and not ARTEX_TOKEN:
                    _token_force = get_token(force=True)
                    st, res = _http("POST", f"{ARTEX_URL}/api/traffic/ingest",
                                    {"exchanges": batch},
                                    {"Authorization": f"Bearer {_token_force}"}, timeout=60)
                if st == 200:
                    # per-entry failures are counted but the batch was accepted —
                    # resending them would only duplicate the good rows.
                    seen_ids.update(keys)
                    del pending[:BATCH], pending_keys[:BATCH]
                    if res.get("failed"):
                        log(f"ingest: {res.get('ingested')} ok, {res.get('failed')} dropped (malformed)")
                    backoff = 1.0
                    continue
                log(f"ingest http {st}: {res} — retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                break

            time.sleep(POLL_S)
        except KeyboardInterrupt:
            save_state(state)
            return
        except Exception as e:
            log(f"loop error: {e} — continuing")
            time.sleep(max(POLL_S, backoff))


if __name__ == "__main__":
    main()
