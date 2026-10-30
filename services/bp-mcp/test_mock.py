"""End-to-end smoke test: mock pool-manager + mock sidecar -> real bp-mcp calls.

Run:  python test_mock.py  (bp-mcp must be running with POOL_MANAGER_URL pointing
at the mock manager, port 8001; mock sidecar on 8002)
"""
import asyncio
import json
import sys

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ---- mock pool-manager on :8001 ----
mgr = FastAPI()
LEASES = {}


@mgr.post("/leases")
async def acquire(body: dict):
    lease_id = "mocklease" + body.get("engagement_id", "x")
    lease = {
        "lease_id": lease_id, "profile_id": "mock-profile", "mode": body.get("mode", "mitm"),
        "geo": "US", "timezone": "America/New_York", "exit_ip": "203.0.113.9",
        "sidecar_url": "http://127.0.0.1:8002", "sidecar_token": "tok-" + lease_id,
        "novnc_url": "http://vps:16081/vnc.html", "vnc_port": 15901,
        "expires_at": 9999999999, "audit": {"ok": True},
        "endpoints": {"internal_url": "http://pod-mock:9000", "sidecar_port": 19001},
    }
    LEASES[lease_id] = lease
    return lease


@mgr.post("/leases/{lease_id}/heartbeat")
async def hb(lease_id: str):
    return {"ok": True}


@mgr.post("/leases/{lease_id}/release")
async def rel(lease_id: str, body: dict | None = None):
    LEASES.pop(lease_id, None)
    return {"ok": True}


# ---- mock sidecar on :8002 ----
sc = FastAPI()


@sc.post("/tools/navigate")
async def nav(body: dict):
    return {"url": body["url"], "title": "Mock Page", "status": 200, "timed_out": False}


@sc.get("/tools/snapshot")
async def snap():
    return JSONResponse("- button \"Login\" [e3]\n- textbox \"Email\" [e5]\n- link \"Help\" [e9]", media_type="application/json")


@sc.post("/tools/click")
async def click(body: dict):
    return {"ok": True, "url": "https://x.example/after"}


@sc.post("/tools/fill")
async def fill(body: dict):
    return {"ok": True}


@sc.get("/exchanges")
async def ex(limit: int = 200, host: str | None = None):
    return [{
        "id": "ex1", "ts": 1234.5, "source": "cdp", "lease_id": "mocklease", "profile_id": "mock-profile",
        "page_url": "https://x.example/",
        "request": {"method": "GET", "url": "https://x.example/api/me", "host": "x.example",
                    "path": "/api/me", "headers": [["accept", "application/json"]],
                    "body": {"size": 0, "ref": None}},
        "response": {"status": 200, "headers": [["content-type", "application/json"]],
                     "body": {"size": 42, "ref": "a" * 32}},
        "error": None,
    }]


@sc.get("/exchanges/{exchange_id}")
async def exd(exchange_id: str):
    return {"id": exchange_id, "request": {"method": "GET", "url": "https://x.example/api/me"},
            "response": {"status": 200, "body": {"text": "{\"user\":\"u\"}"}}}


# ---- drive the real bp-mcp ----

async def call(client, name, args, expect_ok=True):
    r = await client.post("http://127.0.0.1:9100/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}})
    frame = r.json()
    assert "result" in frame, f"no result for {name}: {frame}"
    res = frame["result"]
    text = res["content"][0]["text"] if res.get("content") else ""
    return res["isError"], text


async def main():
    import uvicorn
    for app_, port in ((mgr, 8001), (sc, 8002)):
        cfg = uvicorn.Config(app_, host="127.0.0.1", port=port, log_level="error")
        srv = uvicorn.Server(cfg)
        asyncio.create_task(srv.serve())
    await asyncio.sleep(1.5)

    async with httpx.AsyncClient(timeout=60) as c:
        # auth on the mock sidecar: our adapter must send the lease token.
        # We cheat: mock accepts any token. Fine — flow is what we test.
        errs, out = await call(c, "bp_lease_acquire",
                               {"session_key": "s1", "target": "x.example", "engagement_id": "e1"})
        print("acquire:", errs, out[:300]); assert not errs
        errs, out = await call(c, "browser_navigate", {"session_key": "s1", "url": "https://x.example"})
        print("navigate:", errs, out); assert not errs and "Mock Page" in out
        errs, out = await call(c, "browser_snapshot", {"session_key": "s1"})
        print("snapshot:", errs, out[:80]); assert not errs and "e3" in out
        errs, out = await call(c, "browser_click", {"session_key": "s1", "ref": "e3"})
        print("click:", errs, out); assert not errs
        errs, out = await call(c, "browser_exchanges", {"session_key": "s1"})
        print("exchanges:", errs, out); assert not errs and "api/me" in out
        errs, out = await call(c, "browser_exchange_detail", {"session_key": "s1", "exchange_id": "ex1"})
        print("detail:", errs, out[:150]); assert not errs
        errs, out = await call(c, "bp_lease_status", {"session_key": "s1"})
        print("status:", errs, out[:120]); assert not errs
        errs, out = await call(c, "bp_lease_release", {"session_key": "s1"})
        print("release:", errs, out); assert not errs
        # post-release: navigate must fail cleanly
        errs, out = await call(c, "browser_navigate", {"session_key": "s1", "url": "https://x"})
        print("post-release navigate:", errs, out); assert errs
    print("\nALL OK")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
