"""BrowserController — the agent-facing control surface for the pod's Chrome.

Connects to the in-pod Chrome over CDP (playwright transport). All tool
operations go through here; the network-capture piece lives in capture.py.
"""
import asyncio
import os
import time
from collections import deque
from pathlib import Path

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text()

FETCH_JS = """async (req) => {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), req.timeout_ms || 30000);
  try {
    const r = await fetch(req.url, {
      method: req.method,
      headers: req.headers || {},
      body: req.body ?? null,
      credentials: 'include',
      signal: ctrl.signal,
    });
    const buf = await r.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return {
      status: r.status, statusText: r.statusText, url: r.url,
      headers: [...r.headers.entries()], body_b64: btoa(bin),
    };
  } finally { clearTimeout(t); }
}"""


class BrowserController:
    def __init__(self, store=None):
        self.cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
        self.mode = os.environ.get("MODE", "mitm")
        self.store = store
        self._pw = None
        self._browser = None
        self._ctx = None
        self._active = None
        self._lock = asyncio.Lock()
        self.console: deque = deque(maxlen=2000)
        self._captures = {}

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def connect(self):
        async with self._lock:
            if self.connected:
                return
            if self._pw is None:
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
            self._ctx = (
                self._browser.contexts[0]
                if self._browser.contexts
                else await self._browser.new_context()
            )
            self._ctx.on("page", self._on_page)
            for p in self._ctx.pages:
                self._hook_page(p)

    def _on_page(self, page):
        self._hook_page(page)

    def _on_close(self, page):
        self._captures.pop(page, None)
        if self._active is page:
            self._active = None

    def _hook_page(self, page):
        try:
            page.on(
                "console",
                lambda m: self.console.append(
                    {
                        "ts": time.time(),
                        "type": m.type,
                        "text": m.text[:2000],
                        "page": self._safe_url(page),
                    }
                ),
            )
            page.on("close", self._on_close)
            if self.mode == "no_mitm" and self.store is not None and page not in self._captures:
                from .capture import NetworkCapture

                cap = NetworkCapture(page, self.store)
                self._captures[page] = cap
                try:
                    asyncio.get_running_loop().create_task(cap.attach())
                except RuntimeError:
                    pass
        except Exception as e:
            print(f"[ctl] hook_page failed: {e}")

    def _safe_url(self, page):
        try:
            return page.url
        except Exception:
            return None

    async def page(self):
        """Current active page; falls back to newest open tab, else a new one."""
        await self.connect()
        if self._active is not None and not self._active.is_closed():
            return self._active
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._active = pages[-1] if pages else await self._ctx.new_page()
        if self._active not in self._captures:
            self._hook_page(self._active)
        return self._active

    def _loc(self, page, ref):
        return page.locator(f'[data-pent-ref="{ref}"]').first

    # ---- tools ----

    async def navigate(self, url, wait_until="load", timeout_ms=30_000):
        page = await self.page()
        status = None
        timed_out = False
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            status = resp.status if resp else None
        except PWTimeout:
            timed_out = True
        return {
            "url": page.url,
            "title": await page.title(),
            "status": status,
            "timed_out": timed_out,
        }

    async def snapshot(self):
        page = await self.page()
        return await page.evaluate(SNAPSHOT_JS)

    async def click(self, ref, button="left", click_count=1, timeout_ms=10_000):
        page = await self.page()
        loc = self._loc(page, ref)
        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
        await loc.click(button=button, click_count=click_count, timeout=timeout_ms)
        return {"ok": True, "url": page.url}

    async def hover(self, ref, timeout_ms=10_000):
        page = await self.page()
        loc = self._loc(page, ref)
        await loc.scroll_into_view_if_needed(timeout=timeout_ms)
        await loc.hover(timeout=timeout_ms)
        return {"ok": True}

    async def fill(self, ref, text, clear=True, timeout_ms=10_000):
        page = await self.page()
        loc = self._loc(page, ref)
        if clear:
            await loc.fill(text, timeout=timeout_ms)
        else:
            await loc.type(text, timeout=timeout_ms)
        return {"ok": True}

    async def select(self, ref, values, timeout_ms=10_000):
        page = await self.page()
        selected = await self._loc(page, ref).select_option(values, timeout=timeout_ms)
        return {"ok": True, "selected": selected}

    async def press(self, key):
        page = await self.page()
        await page.keyboard.press(key)
        return {"ok": True}

    async def type_text(self, text, delay_ms=0):
        page = await self.page()
        await page.keyboard.type(text, delay=delay_ms)
        return {"ok": True}

    async def eval_js(self, expression, arg=None):
        page = await self.page()
        result = await page.evaluate(expression, arg)
        return {"result": result}

    async def fetch_in_page(self, url, method="GET", headers=None, body=None, timeout_ms=30_000):
        import base64

        page = await self.page()
        res = await page.evaluate(
            FETCH_JS,
            {
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body,
                "timeout_ms": timeout_ms,
            },
        )
        raw = base64.b64decode(res.pop("body_b64") or "")
        try:
            res["body_text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            res["body_b64"] = base64.b64encode(raw).decode()
        res["body_size"] = len(raw)
        return res

    async def cookies(self, urls=None):
        await self.connect()
        url_list = [u.strip() for u in urls.split(",") if u.strip()] if urls else None
        return await self._ctx.cookies(url_list) if url_list else await self._ctx.cookies()

    async def screenshot(self, store, full_page=False, return_b64=False):
        import base64

        page = await self.page()
        path = store.new_screenshot_path()
        data = await page.screenshot(path=path, full_page=full_page)
        out = {"path": path}
        if return_b64:
            out["b64"] = base64.b64encode(data).decode()
        return out

    async def tabs(self):
        await self.connect()
        out = []
        for i, p in enumerate(self._ctx.pages):
            out.append(
                {
                    "index": i,
                    "url": self._safe_url(p),
                    "title": await p.title() if not p.is_closed() else None,
                    "closed": p.is_closed(),
                    "active": p is self._active,
                }
            )
        return out

    async def switch_tab(self, index):
        await self.connect()
        pages = self._ctx.pages
        if index < 0 or index >= len(pages):
            raise IndexError(f"tab index {index} out of range ({len(pages)} tabs)")
        self._active = pages[index]
        await self._active.bring_to_front()
        return {"ok": True, "url": self._safe_url(self._active)}

    async def close_tab(self, index):
        await self.connect()
        pages = self._ctx.pages
        if index < 0 or index >= len(pages):
            raise IndexError(f"tab index {index} out of range ({len(pages)} tabs)")
        await pages[index].close()
        return {"ok": True}

    async def new_tab(self, url=None):
        await self.connect()
        page = await self._ctx.new_page()
        self._active = page
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return {"ok": True, "url": self._safe_url(page)}
