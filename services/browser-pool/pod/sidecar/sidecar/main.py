"""pentera browser-pod sidecar — FastAPI tool surface for agents.

Runs inside the pod on :9000. Chrome CDP stays on 127.0.0.1:9222; agents only
ever talk to this API (bearer token issued per lease by pool-manager).
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import models as M
from .cdp import BrowserController
from .exchange_store import ExchangeStore

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TOKEN = os.environ.get("SIDECAR_TOKEN")

store = ExchangeStore(DATA_DIR)
ctl = BrowserController(store=store)

_bearer = HTTPBearer(auto_error=False)


def auth(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    if TOKEN and (creds is None or creds.credentials != TOKEN):
        raise HTTPException(401, "invalid sidecar token")


async def _connect_loop():
    while True:
        try:
            await ctl.connect()
        except Exception:
            pass
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_connect_loop())
    yield
    task.cancel()


app = FastAPI(title="pentera-sidecar", version="0.1.0", lifespan=lifespan)


async def _wrap(coro):
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


# ---------- meta ----------

@app.get("/health")
async def health():
    return {
        "ok": True,
        "chrome": ctl.connected,
        "mode": ctl.mode,
        "profile_id": os.environ.get("PROFILE_ID"),
        "lease_id": os.environ.get("LEASE_ID"),
    }


@app.get("/info", dependencies=[Depends(auth)])
async def info():
    return {
        "profile_id": os.environ.get("PROFILE_ID"),
        "lease_id": os.environ.get("LEASE_ID"),
        "engagement_id": os.environ.get("ENGAGEMENT_ID"),
        "role": os.environ.get("ROLE"),
        "target": os.environ.get("TARGET"),
        "mode": ctl.mode,
        "geo": os.environ.get("GEO"),
        "tz": os.environ.get("TZ"),
        "locale": os.environ.get("LOCALE"),
    }


# ---------- browser tools ----------

@app.post("/tools/navigate", dependencies=[Depends(auth)])
async def navigate(inp: M.NavigateIn):
    return await _wrap(ctl.navigate(inp.url, inp.wait_until, inp.timeout_ms))


@app.get("/tools/snapshot", dependencies=[Depends(auth)])
async def snapshot():
    return await _wrap(ctl.snapshot())


@app.post("/tools/click", dependencies=[Depends(auth)])
async def click(inp: M.ClickIn):
    return await _wrap(ctl.click(inp.ref, inp.button, inp.click_count, inp.timeout_ms))


@app.post("/tools/hover", dependencies=[Depends(auth)])
async def hover(inp: M.HoverIn):
    return await _wrap(ctl.hover(inp.ref, inp.timeout_ms))


@app.post("/tools/fill", dependencies=[Depends(auth)])
async def fill(inp: M.FillIn):
    return await _wrap(ctl.fill(inp.ref, inp.text, inp.clear, inp.timeout_ms))


@app.post("/tools/select", dependencies=[Depends(auth)])
async def select(inp: M.SelectIn):
    return await _wrap(ctl.select(inp.ref, inp.values, inp.timeout_ms))


@app.post("/tools/press", dependencies=[Depends(auth)])
async def press(inp: M.PressIn):
    return await _wrap(ctl.press(inp.key))


@app.post("/tools/type", dependencies=[Depends(auth)])
async def type_text(inp: M.TypeIn):
    return await _wrap(ctl.type_text(inp.text, inp.delay_ms))


@app.post("/tools/eval", dependencies=[Depends(auth)])
async def eval_js(inp: M.EvalIn):
    return await _wrap(ctl.eval_js(inp.expression, inp.arg))


@app.post("/tools/fetch", dependencies=[Depends(auth)])
async def fetch_in_page(inp: M.FetchIn):
    return await _wrap(
        ctl.fetch_in_page(inp.url, inp.method, inp.headers, inp.body, inp.timeout_ms)
    )


@app.get("/tools/cookies", dependencies=[Depends(auth)])
async def cookies(urls: str | None = Query(default=None)):
    return await _wrap(ctl.cookies(urls))


@app.post("/tools/screenshot", dependencies=[Depends(auth)])
async def screenshot(inp: M.ScreenshotIn):
    return await _wrap(ctl.screenshot(store, inp.full_page, inp.return_b64))


@app.get("/tools/console", dependencies=[Depends(auth)])
async def console(level: str | None = None, limit: int = 200):
    items = list(ctl.console)
    if level:
        items = [i for i in items if i["type"] == level]
    return items[-limit:]


@app.get("/tools/tabs", dependencies=[Depends(auth)])
async def tabs():
    return await _wrap(ctl.tabs())


@app.post("/tools/tabs/new", dependencies=[Depends(auth)])
async def new_tab(url: str | None = None):
    return await _wrap(ctl.new_tab(url))


@app.post("/tools/tabs/switch", dependencies=[Depends(auth)])
async def switch_tab(inp: M.SwitchTabIn):
    return await _wrap(ctl.switch_tab(inp.index))


@app.post("/tools/tabs/close", dependencies=[Depends(auth)])
async def close_tab(inp: M.SwitchTabIn):
    return await _wrap(ctl.close_tab(inp.index))


# ---------- captured exchanges ----------

@app.get("/exchanges", dependencies=[Depends(auth)])
async def exchanges(limit: int = 200, host: str | None = None):
    return store.tail(limit=limit, host=host)


@app.get("/exchanges/{exchange_id}", dependencies=[Depends(auth)])
async def exchange_detail(exchange_id: str):
    rec = store.get(exchange_id)
    if rec is None:
        raise HTTPException(404, "exchange not found")
    return rec


@app.get("/blobs/{ref}", dependencies=[Depends(auth)])
async def blob(ref: str):
    raw = store.blob_bytes(ref)
    if raw is None:
        raise HTTPException(404, "blob not found")
    from fastapi.responses import Response

    return Response(content=raw, media_type="application/octet-stream")
