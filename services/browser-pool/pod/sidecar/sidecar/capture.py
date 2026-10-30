"""CDP Network-domain capture — used when MODE=no_mitm.

In no_mitm mode there is no local TLS terminator, so exchanges are harvested
straight from the browser's own Network events (request/response metadata +
getResponseBody). Records written are the same schema as the mitm addon's.

Playwright invokes CDP event handlers as sync callbacks on the running loop,
so body retrieval is always scheduled as an async task.
"""
import asyncio
import base64
import os
import time
import uuid
from urllib.parse import urlparse


class NetworkCapture:
    def __init__(self, page, store):
        self.page = page
        self.store = store
        self.session = None
        self._pending = {}  # requestId -> partial exchange state

    async def attach(self):
        try:
            self.session = await self.page.context.new_cdp_session(self.page)
            self.session.on("Network.requestWillBeSent", self._on_request)
            self.session.on("Network.responseReceived", self._on_response)
            self.session.on("Network.loadingFinished", self._on_finished)
            self.session.on("Network.loadingFailed", self._on_failed)
            await self.session.send(
                "Network.enable",
                {
                    "maxTotalBufferSize": 50 * 1024 * 1024,
                    "maxResourceBufferSize": 20 * 1024 * 1024,
                },
            )
        except Exception as e:
            print(f"[capture] attach failed on {self._url()}: {e}")

    # ---- event handlers (sync callbacks fired inside the loop) ----

    def _on_request(self, params):
        rid = params.get("requestId")
        req = params.get("request") or {}
        if not rid:
            return
        # A redirect reuses requestId; the carried redirectResponse finalizes
        # the previous hop — redirect hops carry no meaningful body.
        prev = self._pending.pop(rid, None)
        if prev is not None and params.get("redirectResponse"):
            self._write(prev, params["redirectResponse"], b"", None)
        self._pending[rid] = {
            "request": req,
            "response": None,
            "ts": params.get("wallTime") or time.time(),
            "type": params.get("type"),
            "frame_url": (params.get("frame") or {}).get("url"),
        }

    def _on_response(self, params):
        st = self._pending.get(params.get("requestId"))
        if st is not None:
            st["response"] = params.get("response")

    def _on_finished(self, params):
        st = self._pending.pop(params.get("requestId"), None)
        if st is not None:
            self._schedule(self._finish(params["requestId"], st, error=None))

    def _on_failed(self, params):
        st = self._pending.pop(params.get("requestId"), None)
        if st is not None:
            self._schedule(
                self._finish(params["requestId"], st, error=params.get("errorText"))
            )

    def _schedule(self, coro):
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    async def _finish(self, rid, st, error):
        body_raw = b""
        if st.get("response") is not None and self.session is not None:
            try:
                res = await self.session.send(
                    "Network.getResponseBody", {"requestId": rid}
                )
                raw = res.get("body") or ""
                body_raw = (
                    base64.b64decode(raw)
                    if res.get("base64Encoded")
                    else raw.encode("utf-8", "replace")
                )
            except Exception:
                pass
        self._write(st, st.get("response"), body_raw, error)

    # ---- record assembly ----

    def _write(self, st, response, resp_body_raw, error):
        req = st.get("request") or {}
        url = req.get("url") or ""
        parsed = urlparse(url)

        req_body_raw = b""
        if isinstance(req.get("postData"), str):
            req_body_raw = req["postData"].encode("utf-8", "replace")

        rec = {
            "id": uuid.uuid4().hex,
            "ts": st.get("ts") or time.time(),
            "source": "cdp",
            "lease_id": os.environ.get("LEASE_ID"),
            "profile_id": os.environ.get("PROFILE_ID"),
            "page_url": self._url() or st.get("frame_url"),
            "request": {
                "method": req.get("method"),
                "url": url,
                "host": parsed.netloc,
                "path": parsed.path + (("?" + parsed.query) if parsed.query else ""),
                "http_version": (response or {}).get("protocol"),
                "headers": [[k, v] for k, v in (req.get("headers") or {}).items()],
                "body": self.store.store_blob(req_body_raw),
            },
            "response": self._resp_dict(response, resp_body_raw),
            "error": error,
        }
        self.store.append(rec)

    def _resp_dict(self, response, body_raw):
        if response is None:
            return None
        return {
            "status": response.get("status"),
            "reason": response.get("statusText"),
            "http_version": response.get("protocol"),
            "remote_ip": response.get("remoteIPAddress"),
            "headers": [[k, v] for k, v in (response.get("headers") or {}).items()],
            "body": self.store.store_blob(body_raw),
        }

    def _url(self):
        try:
            return self.page.url
        except Exception:
            return None
