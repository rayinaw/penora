"""bp-mcp — MCP (Streamable HTTP) adapter for the pentera browser pool.

Exposes pool-manager lease control + per-lease sidecar browser tools as MCP
tools that ARTEX (or any MCP client) can call over plain HTTP JSON-RPC.

Protocol surface (what ARTEX's mcphttp client speaks):
    POST /mcp   {"jsonrpc":"2.0","id":N,"method":..., "params":...}
        initialize            -> {protocolVersion, capabilities, serverInfo}
        tools/list            -> {tools:[{name,description,inputSchema}]}
        tools/call            -> {content:[{type:text,text}], isError}
        ping                  -> {}
        notifications/*       -> 202 (no body)
    DELETE /mcp -> 200 (session cleanup ack; sessions live in sessions.json)
    GET  /health -> liveness

Env:
    POOL_MANAGER_URL   default http://pool-manager:8000
    MCP_TOKEN          optional bearer for this endpoint itself
    SESSIONS_FILE      default /data/sessions.json
    USE_INTERNAL_URL   "1" (default) = reach sidecars via docker-network URLs
"""
import asyncio
import json
import os
import time
import urllib.parse
from contextlib import asynccontextmanager

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

MGR = os.environ.get("POOL_MANAGER_URL", "http://pool-manager:8000").rstrip("/")
TOKEN = os.environ.get("MCP_TOKEN") or ""
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "/data/sessions.json")
USE_INTERNAL = os.environ.get("USE_INTERNAL_URL", "1") != "0"

# Operator console + noVNC proxy (browser-based takeover without SSH port juggling).
# The console pages are token-gated by VNC_TOKEN (falls back to MCP_TOKEN).
VNC_TOKEN = os.environ.get("VNC_TOKEN") or TOKEN
VNC_PUBLIC_BASE = os.environ.get("VNC_PUBLIC_BASE", "http://127.0.0.1:9100").rstrip("/")
# ARTEX API — polled for intents paused on ask_operator ("waiting for operator").
ARTEX_URL = os.environ.get("ARTEX_URL", "").rstrip("/")
ARTEX_PASSWORD = os.environ.get("ARTEX_PASSWORD", "")
# Telegram push when a worker pauses waiting for the operator.
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
# Shown on the console page and appended to Telegram alerts — the exact tunnel
# command that makes both URLs reachable from the operator's machine.
SSH_HINT = os.environ.get("SSH_HINT", "ssh -L 8787:127.0.0.1:8787 -L 9100:127.0.0.1:9100 hunter@173.249.6.22")

HEARTBEAT_EVERY = 30  # pool-manager reaps when heartbeat_at is stale (>120s default)
WATCH_EVERY = 8       # paused-intent poll interval
MAX_SNAPSHOT_CHARS = 30000
MAX_EXCHANGES = 200
MAX_BLOB = 64 * 1024
MCP_VERSION = "2025-06-18"

# ---------------------------------------------------------------- sessions --

sessions: dict[str, dict] = {}  # session_key -> lease record


def _load_sessions():
    global sessions
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            sessions = json.load(f)
    except Exception:
        sessions = {}


def _save_sessions():
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sessions, f)
        os.replace(tmp, SESSIONS_FILE)
    except Exception as e:
        print(f"[bp-mcp] sessions save failed: {e}")


def sidecar_url(lease: dict) -> str:
    """Prefer the in-network pod URL (bp-mcp shares pentera-net); fall back to
    the host-mapped sidecar port for non-docker deployments."""
    if USE_INTERNAL:
        internal = (lease.get("endpoints") or {}).get("internal_url")
        if internal:
            return internal
    return lease["sidecar_url"]


def sess(key: str) -> dict | None:
    return sessions.get(key or "default")


async def _heartbeat_loop():
    """Keep every session's lease alive; drop sessions whose lease is gone."""
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY)
        for key, lease in list(sessions.items()):
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(f"{MGR}/leases/{lease['lease_id']}/heartbeat")
                    if r.status_code in (404, 409):
                        print(f"[bp-mcp] session {key}: lease {lease['lease_id']} gone ({r.status_code})")
                        sessions.pop(key, None)
                        _save_sessions()
            except Exception as e:
                print(f"[bp-mcp] heartbeat {key}: {e}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load_sessions()
    task = asyncio.create_task(_heartbeat_loop())
    watcher = asyncio.create_task(_watcher_loop())
    yield
    task.cancel()
    watcher.cancel()


app = FastAPI(title="bp-mcp", version="0.1.0", lifespan=lifespan)


def _check_auth(request: Request) -> bool:
    if not TOKEN:
        return True
    return request.headers.get("authorization") == f"Bearer {TOKEN}"


# ------------------------------------------------------------------ tools ---

def _s(props=None, required=(), extra=None):
    out = {"type": "object", "properties": props or {}, "required": list(required)}
    if extra:
        out.update(extra)
    return out


SK = ("session_key", {"type": "string",
                     "description": "Lease/session handle returned by bp_lease_acquire. Reuse the same value for every browser_* call in this intent; default 'default'."})
SKP = [SK]


def _props(*items):
    return {k: v for k, v in items}


TOOLS = [
    {
        "name": "bp_lease_acquire",
        "description": "Acquire an exclusive browser pod from the pool: persistent headed Chrome bound to one profile, one fixed proxy exit IP and one timezone. Returns a session_key to pass to every browser_* call, plus novnc_url (give it to the operator — they can solve captchas/logins by hand; trust persists in the profile). mode=mitm records all page traffic to the lease exchange store (default); mode=no_mitm captures via CDP only, for targets that reject the mitm cert.",
        "inputSchema": _s(_props(
            ("session_key", {"type": "string", "description": "Name for this browser session (e.g. the task/intent id). Required to run several pods in parallel; default 'default'."}),
            ("mode", {"type": "string", "enum": ["mitm", "no_mitm"], "default": "mitm"}),
            ("geo_pref", {"type": "string", "description": "Preferred profile geo (e.g. 'US'); optional."}),
            ("profile_pref", {"type": "string", "description": "Pin a specific profile id (e.g. 'us-res-01'); use when the browser must share the tool-side egress IP (global_proxy). Fails with 409 if that profile is leased."}),
            ("ttl_s", {"type": "integer", "description": "Hard lease cap seconds (default pool-side 3600)."}),
            ("target", {"type": "string", "description": "Target label recorded on the lease (auditing)."}),
            ("engagement_id", {"type": "string", "description": "Engagement/task id recorded on the lease."}),
        ), []),
    },
    {
        "name": "bp_lease_release",
        "description": "Release the lease for a session: destroys the pod, frees the profile. Call when done with a session; unreleased leases still die at ttl but block the profile until then.",
        "inputSchema": _s(_props(*SKP), []),
    },
    {
        "name": "bp_lease_status",
        "description": "Show active browser-pool sessions this adapter holds (and their novnc_url/exit_ip). Pass session_key for one session.",
        "inputSchema": _s(_props(*SKP), []),
    },
    {
        "name": "browser_navigate",
        "description": "Navigate the leased browser to a URL. Returns final url/title/http status. Requires an acquired session.",
        "inputSchema": _s(_props(*SKP, ("url", {"type": "string"}), ("wait_until", {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "load"})), ["url"]),
    },
    {
        "name": "browser_snapshot",
        "description": "Accessibility snapshot of the current page: numbered element refs (eN) plus text — the primary way to read a page before clicking/filling. Large pages are truncated.",
        "inputSchema": _s(_props(*SKP), []),
    },
    {
        "name": "browser_click",
        "description": "Click an element by snapshot ref (e.g. 'e15').",
        "inputSchema": _s(_props(*SKP, ("ref", {"type": "string"}), ("button", {"type": "string", "enum": ["left", "right", "middle"], "default": "left"}), ("click_count", {"type": "integer", "default": 1})), ["ref"]),
    },
    {
        "name": "browser_fill",
        "description": "Fill an input by snapshot ref.",
        "inputSchema": _s(_props(*SKP, ("ref", {"type": "string"}), ("text", {"type": "string"}), ("clear", {"type": "boolean", "default": True})), ["ref", "text"]),
    },
    {
        "name": "browser_press",
        "description": "Press a key (e.g. 'Enter', 'Tab', 'Control+a').",
        "inputSchema": _s(_props(*SKP, ("key", {"type": "string"})), ["key"]),
    },
    {
        "name": "browser_type",
        "description": "Type text as keystrokes (for fields that need real key events).",
        "inputSchema": _s(_props(*SKP, ("text", {"type": "string"}), ("delay_ms", {"type": "integer", "default": 0})), ["text"]),
    },
    {
        "name": "browser_hover",
        "description": "Hover an element by snapshot ref (reveals menus/tooltips).",
        "inputSchema": _s(_props(*SKP, ("ref", {"type": "string"})), ["ref"]),
    },
    {
        "name": "browser_select",
        "description": "Select option(s) in a <select> by snapshot ref.",
        "inputSchema": _s(_props(*SKP, ("ref", {"type": "string"}), ("values", {"type": "array", "items": {"type": "string"}})), ["ref", "values"]),
    },
    {
        "name": "browser_eval",
        "description": "Run JavaScript in the page and return the result. Use for DOM queries the snapshot misses, storage reads, or triggering app internals.",
        "inputSchema": _s(_props(*SKP, ("expression", {"type": "string"}), ("arg", {"description": "optional JSON arg passed to the expression"})), ["expression"]),
    },
    {
        "name": "browser_fetch",
        "description": "fetch() inside the page context — request that inherits session cookies, service-worker signing, and SPA middleware state. The right way to replay an API call exactly as the app makes it.",
        "inputSchema": _s(_props(*SKP, ("url", {"type": "string"}), ("method", {"type": "string", "default": "GET"}), ("headers", {"type": "object"}), ("body", {"type": "string"}), ("timeout_ms", {"type": "integer", "default": 30000})), ["url"]),
    },
    {
        "name": "browser_cookies",
        "description": "Read browser cookies (optionally filtered to comma-separated urls).",
        "inputSchema": _s(_props(*SKP, ("urls", {"type": "string", "description": "comma-separated URL list to scope cookies"})), []),
    },
    {
        "name": "browser_screenshot",
        "description": "PNG screenshot. By default returns the artifact path inside the lease dir (host: <data_root>/<lease_id>/screenshots/). Set return_b64=true only when a small image is actually needed inline.",
        "inputSchema": _s(_props(*SKP, ("full_page", {"type": "boolean", "default": False}), ("return_b64", {"type": "boolean", "default": False})), []),
    },
    {
        "name": "browser_tabs",
        "description": "List open tabs (index, url, title, active).",
        "inputSchema": _s(_props(*SKP), []),
    },
    {
        "name": "browser_tab_new",
        "description": "Open a new tab (optionally at a URL) and switch to it.",
        "inputSchema": _s(_props(*SKP, ("url", {"type": "string"})), []),
    },
    {
        "name": "browser_tab_switch",
        "description": "Switch the active tab by index from browser_tabs.",
        "inputSchema": _s(_props(*SKP, ("index", {"type": "integer"})), ["index"]),
    },
    {
        "name": "browser_tab_close",
        "description": "Close a tab by index.",
        "inputSchema": _s(_props(*SKP, ("index", {"type": "integer"})), ["index"]),
    },
    {
        "name": "browser_console",
        "description": "Recent console messages from the page (level filter, limit). Useful for JS errors that explain a broken page.",
        "inputSchema": _s(_props(*SKP, ("level", {"type": "string"}), ("limit", {"type": "integer", "default": 100})), []),
    },
    {
        "name": "browser_captcha_status",
        "description": "Detect captcha/anti-bot widgets on the current page (reCAPTCHA, hCaptcha, Cloudflare Turnstile/interstitial) and whether a solved token exists. CALL THIS when a page shows a challenge: never click/type into a captcha — if solver_active or a widget is present, wait ~10s and poll again; while polling, try legitimate alternates (reload once, a different page section, a non-captcha path). Only ask_operator with novnc_url after ~10 minutes of unsolved polling and exhausted alternates.",
        "inputSchema": _s(_props(*SKP), []),
    },
    {
        "name": "browser_exchanges",
        "description": "Light index of HTTP exchanges the pod captured for this lease (id/method/url/status/sizes, newest first). Use browser_exchange_detail to read one fully.",
        "inputSchema": _s(_props(*SKP, ("host", {"type": "string", "description": "substring filter on request host"}), ("limit", {"type": "integer", "default": 50})), []),
    },
    {
        "name": "browser_exchange_detail",
        "description": "Full record of one captured exchange (headers + inlined body text, or body blob ref). Pass an id from browser_exchanges.",
        "inputSchema": _s(_props(*SKP, ("exchange_id", {"type": "string"})), ["exchange_id"]),
    },
]

_TOOL_MAP = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------- upstream ---

async def _mgr(method: str, path: str, body=None, timeout=120):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, f"{MGR}{path}", json=body)
        if r.status_code >= 400:
            raise UpstreamErr(f"pool-manager {r.status_code}: {r.text[:500]}")
        return r.json()


async def _sc(lease: dict, method: str, path: str, body=None, timeout=90, params=None):
    url = sidecar_url(lease).rstrip("/") + path
    headers = {"Authorization": f"Bearer {lease['sidecar_token']}"}
    async with httpx.AsyncClient(timeout=timeout) as c:
        if method == "GET":
            r = await c.get(url, headers=headers, params=params if params is not None else (body or {}))
        else:
            r = await c.request(method, url, headers=headers, json=body, params=params)
        if r.status_code >= 400:
            raise UpstreamErr(f"sidecar {r.status_code}: {r.text[:500]}")
        return r.json()


class UpstreamErr(Exception):
    pass


def _need(args: dict, *names):
    for n in names:
        v = args.get(n)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise UpstreamErr(f"missing required argument: {n}")
        args[n] = v


def _lite_exchange(rec: dict) -> dict:
    req = rec.get("request") or {}
    resp = rec.get("response") or {}
    return {
        "id": rec.get("id"),
        "ts": rec.get("ts"),
        "method": req.get("method"),
        "url": req.get("url"),
        "status": resp.get("status"),
        "req_size": (req.get("body") or {}).get("size", 0),
        "resp_size": (resp.get("body") or {}).get("size", 0),
        "page_url": rec.get("page_url"),
        "error": rec.get("error"),
    }


# ---------------------------------------------------------------- handlers --

async def handle_tool(name: str, args: dict):
    args = args or {}
    key = args.get("session_key") or "default"

    if name == "bp_lease_acquire":
        if sess(key):
            return {"ok": True, "already": True, "session_key": key,
                    "note": "session already holds a lease — reuse it or bp_lease_release first",
                    **_lease_public(sess(key))}
        body = {
            "mode": args.get("mode") or "mitm",
            "target": args.get("target") or "(unspecified)",
            "engagement_id": args.get("engagement_id") or key,
            "role": args.get("role") or "mapper",
        }
        for opt in ("geo_pref", "profile_pref", "ttl_s"):
            if args.get(opt) is not None:
                body[opt] = args[opt]
        lease = await _mgr("POST", "/leases", body, timeout=180)
        sessions[key] = lease
        _save_sessions()
        return {"ok": True, "session_key": key, **_lease_public(lease),
                "next": "drive the browser with browser_navigate/browser_snapshot/... using this session_key"}

    if name == "bp_lease_release":
        lease = sess(key)
        if not lease:
            return {"ok": True, "note": f"no lease held for session '{key}'"}
        try:
            await _mgr("POST", f"/leases/{lease['lease_id']}/release",
                       {"reason": "agent released"}, timeout=60)
        finally:
            sessions.pop(key, None)
            _save_sessions()
        return {"ok": True, "released": lease["lease_id"], "session_key": key}

    if name == "bp_lease_status":
        if args.get("session_key"):
            lease = sess(key)
            if not lease:
                return {"ok": False, "error": f"no lease for session '{key}'"}
            return {"ok": True, "session_key": key, **_lease_public(lease)}
        return {"ok": True,
                "sessions": {k: _lease_public(v) for k, v in sessions.items()} or "none"}

    # everything below needs a live session
    lease = sess(key)
    if not lease:
        raise UpstreamErr(
            f"no browser lease for session '{key}' — call bp_lease_acquire(session_key='{key}') first")

    if name == "browser_navigate":
        _need(args, "url")
        return await _sc(lease, "POST", "/tools/navigate",
                         {"url": args["url"], "wait_until": args.get("wait_until") or "load",
                          "timeout_ms": args.get("timeout_ms") or 30000}, timeout=60)

    if name == "browser_snapshot":
        snap = await _sc(lease, "GET", "/tools/snapshot")
        if isinstance(snap, str) and len(snap) > MAX_SNAPSHOT_CHARS:
            return snap[:MAX_SNAPSHOT_CHARS] + f"\n... [snapshot truncated at {MAX_SNAPSHOT_CHARS} chars — use browser_eval for targeted DOM queries]"
        return snap

    if name == "browser_click":
        _need(args, "ref")
        return await _sc(lease, "POST", "/tools/click",
                         {"ref": args["ref"], "button": args.get("button") or "left",
                          "click_count": args.get("click_count") or 1})

    if name == "browser_fill":
        _need(args, "ref", "text")
        return await _sc(lease, "POST", "/tools/fill",
                         {"ref": args["ref"], "text": args["text"],
                          "clear": args.get("clear", True)})

    if name == "browser_press":
        _need(args, "key")
        return await _sc(lease, "POST", "/tools/press", {"key": args["key"]})

    if name == "browser_type":
        _need(args, "text")
        return await _sc(lease, "POST", "/tools/type",
                         {"text": args["text"], "delay_ms": args.get("delay_ms") or 0})

    if name == "browser_hover":
        _need(args, "ref")
        return await _sc(lease, "POST", "/tools/hover", {"ref": args["ref"]})

    if name == "browser_select":
        _need(args, "ref", "values")
        return await _sc(lease, "POST", "/tools/select",
                         {"ref": args["ref"], "values": args["values"]})

    if name == "browser_eval":
        _need(args, "expression")
        return await _sc(lease, "POST", "/tools/eval",
                         {"expression": args["expression"], "arg": args.get("arg")})

    if name == "browser_fetch":
        _need(args, "url")
        return await _sc(lease, "POST", "/tools/fetch",
                         {"url": args["url"], "method": args.get("method") or "GET",
                          "headers": args.get("headers") or {}, "body": args.get("body"),
                          "timeout_ms": args.get("timeout_ms") or 30000}, timeout=60)

    if name == "browser_cookies":
        return await _sc(lease, "GET", "/tools/cookies",
                         {"urls": args.get("urls")} if args.get("urls") else {})

    if name == "browser_screenshot":
        return await _sc(lease, "POST", "/tools/screenshot",
                         {"full_page": bool(args.get("full_page")),
                          "return_b64": bool(args.get("return_b64"))})

    if name == "browser_tabs":
        return await _sc(lease, "GET", "/tools/tabs")

    if name == "browser_tab_new":
        params = {"url": args["url"]} if args.get("url") else None
        return await _sc(lease, "POST", "/tools/tabs/new", None, params=params, timeout=30)

    if name == "browser_tab_switch":
        _need(args, "index")
        return await _sc(lease, "POST", "/tools/tabs/switch", {"index": args["index"]})

    if name == "browser_tab_close":
        _need(args, "index")
        return await _sc(lease, "POST", "/tools/tabs/close", {"index": args["index"]})

    if name == "browser_console":
        params = {}
        if args.get("level"):
            params["level"] = args["level"]
        params["limit"] = args.get("limit") or 100
        return await _sc(lease, "GET", "/tools/console", params)

    if name == "browser_captcha_status":
        js = """(() => {
  const q = (s) => !!document.querySelector(s);
  const tok = (n) => { const e = document.querySelector(`textarea[name="${n}"],[name="${n}"]`); return e && e.value ? true : false; };
  const widgets = {
    recaptcha: q('iframe[src*="recaptcha"],.g-recaptcha,[data-sitekey]'),
    hcaptcha: q('iframe[src*="hcaptcha"],.h-captcha'),
    turnstile: q('iframe[src*="challenges.cloudflare"],.cf-turnstile,[data-cfasync]'),
    cloudflare_interstitial: /just a moment|checking your browser|verify you are human|đang kiểm tra/i.test(document.title + ' ' + (document.body ? document.body.innerText.slice(0, 400) : '')),
  };
  const tokens = { g: tok('g-recaptcha-response'), h: tok('h-captcha-response'), cf: tok('cf-turnstile-response') };
  const any_widget = widgets.recaptcha || widgets.hcaptcha || widgets.turnstile || widgets.cloudflare_interstitial;
  const solved = tokens.g || tokens.h || tokens.cf;
  const solver_active = !!document.querySelector('[id*="nopecha"],[class*="nopecha"],.nopecha-script,[data-nopecha]') || !!window.__nopecha;
  let status, advice;
  if (solved || !any_widget) { status = solved ? 'solved' : 'clear'; advice = 'no captcha blocking — continue working'; }
  else if (solver_active) { status = 'solving'; advice = 'solver extension appears active — wait ~10s and re-poll browser_captcha_status; do NOT click/type into the challenge'; }
  else { status = 'present'; advice = 'challenge present, no solver signal — keep polling ~10s and try legit alternates (reload once, another section, a different path); only ask_operator with the novnc_url after ~10 min unsolved and alternates exhausted'; }
  return {status, widgets, tokens, solver_active, url: location.href, title: document.title};
})()"""
        return await _sc(lease, "POST", "/tools/eval", {"expression": js})

    if name == "browser_exchanges":
        limit = min(int(args.get("limit") or 50), MAX_EXCHANGES)
        params = {"limit": limit}
        if args.get("host"):
            params["host"] = args["host"]
        recs = await _sc(lease, "GET", "/exchanges", params)
        return [_lite_exchange(r) for r in recs]

    if name == "browser_exchange_detail":
        _need(args, "exchange_id")
        return await _sc(lease, "GET", f"/exchanges/{args['exchange_id']}")

    raise UpstreamErr(f"unknown tool {name}")


def _novnc_url(lease: dict) -> str | None:
    """The click-and-watch URL through this service's console proxy — works over
    the operator's single SSH tunnel (or a public base if VNC_PUBLIC_BASE is
    pointed there). Embed the console token so the pasted link is one-click."""
    lid = lease.get("lease_id")
    if not lid:
        return lease.get("novnc_url")
    # path must be bare "websockify" — noVNC resolves it relative to the page
    # dir (/v/<lid>/), so v/<lid>/websockify would double-prefix the ws URL.
    u = (f"{VNC_PUBLIC_BASE}/v/{lid}/vnc.html?autoconnect=true&resize=scale"
         f"&path=websockify")
    return u + (f"&k={VNC_TOKEN}" if VNC_TOKEN else "")


def _lease_public(lease: dict) -> dict:
    return {
        "lease_id": lease.get("lease_id"),
        "profile_id": lease.get("profile_id"),
        "mode": lease.get("mode"),
        "geo": lease.get("geo"),
        "timezone": lease.get("timezone"),
        "exit_ip": lease.get("exit_ip"),
        "novnc_url": _novnc_url(lease),
        "expires_at": lease.get("expires_at"),
        "sidecar": sidecar_url(lease),
    }


# ------------------------------------------------------------ JSON-RPC layer --

def _rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def mcp(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        frame = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)

    rid = frame.get("id")
    method = frame.get("method") or ""
    params = frame.get("params") or {}

    # notifications (no id) -> 202, no body
    if rid is None:
        return Response(status_code=202)

    if method == "initialize":
        return _rpc_result(rid, {
            "protocolVersion": params.get("protocolVersion") or MCP_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "bp-mcp", "version": "0.1.0"},
        })
    if method == "ping":
        return _rpc_result(rid, {})
    if method == "tools/list":
        return _rpc_result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name") or ""
        if name not in _TOOL_MAP:
            return _rpc_error(rid, -32602, f"unknown tool: {name}")
        try:
            out = await handle_tool(name, params.get("arguments") or {})
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
            return _rpc_result(rid, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except UpstreamErr as e:
            return _rpc_result(rid, {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "isError": True,
            })
        except Exception as e:  # never leak a traceback frame
            return _rpc_result(rid, {
                "content": [{"type": "text", "text": f"Error: internal: {e}"}],
                "isError": True,
            })

    return _rpc_error(rid, -32601, f"method not found: {method}")


@app.delete("/mcp")
async def mcp_delete(request: Request):
    # Session-end hint from Streamable HTTP clients. Our sessions are named and
    # long-lived (survive client reconnects), so just acknowledge.
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"ok": True, "sessions": len(sessions), "pool_manager": MGR}


# ============================================================ vnc console ====
#
# Operator console + noVNC proxy. Reach everything through ONE tunnel:
#   ssh -L 9100:127.0.0.1:9100 hunter@<vps>   →   http://localhost:9100/
#
# GET /            dashboard: workers waiting on ask_operator + live pods
# GET /api/console/state   same data as JSON (dashboard polls it)
# ANY /v/<lease>/<path>    proxy to the pod's noVNC (pod_name:6080 on pentera-net)
# WS  /v/<lease>/websockify websocket leg of noVNC
#
# Auth: VNC_TOKEN via ?k= (sets a cookie) — the novnc_url handed to workers
# already embeds the token, so pasted links are one-click.


def _vnc_ok(request: Request) -> bool:
    if not VNC_TOKEN:
        return True
    return (request.query_params.get("k") == VNC_TOKEN
            or request.cookies.get("vnc_k") == VNC_TOKEN)


def _vnc_gate(request: Request):
    if _vnc_ok(request):
        return None
    return HTMLResponse(
        "<h3>operator console</h3><p>unauthorized — open the console with the "
        "novnc/console link (it carries <code>?k=</code>), or append your token.</p>",
        status_code=401)


def _pod_novnc_base(lease: dict) -> str | None:
    """In-network noVNC base for a lease's pod (pod runs websockify on :6080)."""
    internal = (lease.get("endpoints") or {}).get("internal_url")
    if internal:
        host = urllib.parse.urlparse(internal).hostname
        if host:
            return f"http://{host}:6080"
    # fall back: strip the public host-port URL down to its container name guess
    pod = lease.get("pod") or lease.get("pod_name") or lease.get("container")
    if pod:
        return f"http://{pod}:6080"
    return None


def _find_lease(lease_id: str) -> dict | None:
    for lease in sessions.values():
        if lease.get("lease_id") == lease_id:
            return lease
    return None


async def _leases_from_mgr() -> list:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{MGR}/leases")
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, list) else d.get("leases", [])
    except Exception as e:
        print(f"[console] leases fetch: {e}")
    return []


# --- ARTEX paused-intent polling (ask_operator waits) -------------------------

_artex_jwt: dict = {"token": None, "exp": 0.0}


async def _artex_token() -> str | None:
    if not ARTEX_URL or not ARTEX_PASSWORD:
        return None
    if _artex_jwt["token"] and time.time() < _artex_jwt["exp"] - 3600:
        return _artex_jwt["token"]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{ARTEX_URL}/api/auth/login",
                             json={"username": "ARTEX", "password": ARTEX_PASSWORD})
            if r.status_code == 200:
                _artex_jwt["token"] = r.json()["token"]
                _artex_jwt["exp"] = time.time() + 6 * 86400
                return _artex_jwt["token"]
    except Exception as e:
        print(f"[console] artex login: {e}")
    return None


_TERMINAL = {"done", "completed", "archived", "deleted", "stopped", "failed", "settled"}


async def _paused_intents() -> list:
    """Intent nodes in state=paused across live tasks = workers blocked on the
    operator (ask_operator pauses the intent; the question is in its chat)."""
    tok = await _artex_token()
    if not tok:
        return []
    out = []
    try:
        async with httpx.AsyncClient(timeout=15,
                                     headers={"Authorization": f"Bearer {tok}"}) as c:
            r = await c.get(f"{ARTEX_URL}/api/tasks")
            if r.status_code != 200:
                return []
            d = r.json()
            tasks = d if isinstance(d, list) else (d.get("tasks") or [])
            live = [t for t in tasks
                    if str(t.get("status") or "").lower() not in _TERMINAL][:20]
            for t in live:
                tid = str(t.get("id") or t.get("task_id") or "")
                if not tid:
                    continue
                ri = await c.get(f"{ARTEX_URL}/api/exploration/intents",
                                 params={"task": tid, "limit": 300})
                if ri.status_code != 200:
                    continue
                intents = ri.json()
                items = intents.get("items", intents) if isinstance(intents, dict) else intents
                for n in items or []:
                    if str(n.get("state")) != "paused":
                        continue
                    payload = n.get("payload") or ""
                    try:
                        pj = json.loads(payload)
                        payload = pj.get("text") or pj.get("description") or payload
                    except Exception:
                        pass
                    out.append({
                        "task_id": tid,
                        "task": t.get("name") or t.get("description") or f"task {tid}",
                        "intent_id": str(n.get("id")),
                        "intent": payload[:300],
                        "since": n.get("ts"),
                        "task_url": f"http://localhost:8787/function/tasks/detail?id={tid}",
                    })
    except Exception as e:
        print(f"[console] paused-intent poll: {e}")
    return out


waiting: dict = {}          # (task_id,intent_id) -> waiting record (seen state)


async def _tg_send(text: str):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                         json={"chat_id": TG_CHAT_ID, "text": text,
                               "disable_web_page_preview": False})
    except Exception as e:
        print(f"[console] telegram send failed: {e}")


async def _watcher_loop():
    """Detect newly-paused intents → Telegram alert with the console link."""
    while True:
        await asyncio.sleep(WATCH_EVERY)
        try:
            paused = await _paused_intents()
            cur = {(p["task_id"], p["intent_id"]) for p in paused}
            for w in paused:
                wkey = f"{w['task_id']}:{w['intent_id']}"
                if wkey in waiting:
                    continue
                waiting[wkey] = w
                links = "\n".join(
                    f"  • session {k}: {_novnc_url(v) or '-'}"
                    for k, v in list(sessions.items())[:5])
                msg = (f"⏸ Worker waiting for operator\n"
                       f"task: {w['task']}\nintent #{w['intent_id']}: {w['intent']}\n\n"
                       f"console: {VNC_PUBLIC_BASE}/" +
                       (f"?k={VNC_TOKEN}" if VNC_TOKEN else "") + "\n" +
                       (f"browser pods:\n{links}\n\n" if links else "\n") +
                       f"task: {w['task_url']}\n\n"
                       f"open the console locally:\n{SSH_HINT}")
                await _tg_send(msg)
            for k, w in list(waiting.items()):
                if (w["task_id"], w["intent_id"]) not in cur:
                    waiting.pop(k, None)
        except Exception as e:
            print(f"[console] watcher: {e}")


# --- console page + api --------------------------------------------------------

_PAGE = """<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=6>
<title>operator console</title>
<style>
 body{font:14px/1.45 ui-monospace,Menlo,Consolas,monospace;background:#0d1117;color:#c9d1d9;max-width:860px;margin:24px auto;padding:0 16px}
 h1{font-size:17px} h2{font-size:14px;color:#8b949e;border-bottom:1px solid #30363d;padding-bottom:4px;margin-top:28px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px;margin:10px 0}
 .wait{border-color:#d29922} .none{color:#6e7681}
 a{color:#58a6ff;text-decoration:none} a:hover{text-decoration:underline}
 code{background:#21262d;padding:2px 6px;border-radius:4px;font-size:12px}
 .k{color:#7ee787}.dim{color:#6e7681;font-size:12px}
</style>
<h1>operator console</h1>
<div class=dim>tunnel: <code id=tun></code></div>
<h2>⏸ waiting for operator</h2><div id=wait>…</div>
<h2>🖥 active browser pods</h2><div id=pods>…</div>
<script>
const S = await (await fetch('api/console/state'+location.search)).json();
tun.textContent = S.ssh_hint;
wait.innerHTML = S.waiting.length ? S.waiting.map(w=>`
 <div class="card wait"><b>${esc(w.task)}</b> · intent #${w.intent_id}
 <div class=dim>${esc(w.intent)}</div>
 <div style="margin-top:6px"><a href="${w.task_url}">open task in ARTEX →</a></div></div>`).join('')
 : '<div class="card none">no worker waiting</div>';
pods.innerHTML = S.pods.length ? S.pods.map(p=>`
 <div class=card><b>${esc(p.session||'')}</b> · lease <code>${p.lease_id.slice(0,8)}</code>
 · ${esc(p.profile||'')} · ${esc(p.mode||'')} · ${esc(p.exit_ip||'')}
 <div style="margin-top:6px"><a href="${p.url}">open noVNC viewer →</a></div></div>`).join('')
 : '<div class="card none">no active pods</div>';
function esc(s){return (s||'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]))}
</script>"""


@app.get("/", response_class=HTMLResponse)
async def console_home(request: Request):
    gate = _vnc_gate(request)
    if gate:
        return gate
    r = HTMLResponse(_PAGE)
    if request.query_params.get("k") == VNC_TOKEN and VNC_TOKEN:
        r.set_cookie("vnc_k", VNC_TOKEN, httponly=True, samesite="lax")
    return r


@app.get("/api/console/state")
async def console_state(request: Request):
    gate = _vnc_gate(request)
    if gate:
        return gate
    leases = await _leases_from_mgr()
    pods = []
    for l in leases:
        if str(l.get("state") or "active") != "active":
            continue
        lid = l.get("lease_id") or l.get("id") or ""
        sess_key = next((k for k, v in sessions.items()
                         if v.get("lease_id") == lid), "")
        pods.append({
            "lease_id": lid, "session": sess_key,
            "profile": l.get("profile_id"), "mode": l.get("mode"),
            "exit_ip": l.get("exit_ip"),
            "url": (f"/v/{lid}/vnc.html?autoconnect=true&resize=scale"
                    f"&path=websockify&k={VNC_TOKEN}") if lid else "#",
        })
    paused = await _paused_intents()
    return {"waiting": paused, "pods": pods, "ssh_hint": SSH_HINT,
            "ts": int(time.time())}


# --- noVNC proxy ----------------------------------------------------------------

@app.api_route("/v/{lease_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def vnc_http(lease_id: str, path: str, request: Request):
    gate = _vnc_gate(request)
    if gate:
        return gate
    lease = _find_lease(lease_id)
    if not lease:
        return HTMLResponse("lease not found (released?)", status_code=404)
    base = _pod_novnc_base(lease)
    if not base:
        return HTMLResponse("pod has no in-network noVNC endpoint", status_code=502)
    if not path:
        return Response(status_code=302,
                        headers={"location": f"/v/{lease_id}/vnc.html?autoconnect=true&resize=scale&path=websockify&k={VNC_TOKEN}"})
    url = f"{base}/{path}"
    if request.query_params:
        url += "?" + request.url.query
    body = await request.body()
    hdrs = {k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "connection", "content-length", "cookie")}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
            r = await c.request(request.method, url, content=body or None, headers=hdrs)
    except Exception as e:
        return HTMLResponse(f"pod unreachable: {e}", status_code=502)
    out = {k: v for k, v in r.headers.items()
           if k.lower() not in ("connection", "transfer-encoding", "content-length")}
    resp = Response(content=r.content, status_code=r.status_code, headers=out,
                    media_type=r.headers.get("content-type"))
    if request.query_params.get("k") == VNC_TOKEN and VNC_TOKEN:
        resp.set_cookie("vnc_k", VNC_TOKEN, httponly=True, samesite="lax")
    return resp


@app.websocket("/v/{lease_id}/websockify")
async def vnc_ws(ws: WebSocket, lease_id: str):
    if VNC_TOKEN and ws.query_params.get("k") != VNC_TOKEN and ws.cookies.get("vnc_k") != VNC_TOKEN:
        await ws.close(code=4401)
        return
    lease = _find_lease(lease_id)
    if not lease:
        await ws.close(code=4404)
        return
    base = _pod_novnc_base(lease)
    if not base:
        await ws.close(code=4502)
        return
    upstream = base.replace("http://", "ws://") + "/websockify"
    if ws.scope.get("query_string"):
        upstream += "?" + ws.scope["query_string"].decode()
    await ws.accept()
    sub_hdr = ws.headers.get("sec-websocket-protocol")
    subs = [s.strip() for s in sub_hdr.split(",")] if sub_hdr else None
    try:
        async with websockets.connect(upstream, max_size=None, subprotocols=subs) as up:
            async def c2u():
                while True:
                    m = await ws.receive()
                    if m.get("type") == "websocket.disconnect":
                        break
                    if m.get("bytes") is not None:
                        await up.send(m["bytes"])
                    elif m.get("text") is not None:
                        await up.send(m["text"])

            async def u2c():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            t1, t2 = asyncio.create_task(c2u()), asyncio.create_task(u2c())
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass
