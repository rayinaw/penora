"""pentera pool-manager — the browser pool control API.

Agents/control plane POST /leases to exclusively borrow a profile-bound pod.
Profile state transitions: free → leased → (free|quarantine|error).
"""
import asyncio
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import auditor
from .config import load
from .models import LeaseOut, LeaseRequest, Mode, ProfileIn, ProfileStateIn, ReleaseIn
from .orchestrator import GEO_TZ_FALLBACK, Orchestrator, wait_healthy
from .registry import Registry

cfg = load()
registry = Registry(cfg.db_path)
orch = Orchestrator(cfg)


async def _sweeper():
    while True:
        try:
            for lease in registry.expired_leases(cfg.heartbeat_stale_s):
                await _teardown(lease["id"], reason="expired")
        except Exception as e:
            print(f"[sweeper] error: {e}")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for p in cfg.profiles:
        registry.upsert_profile(
            {"id": p.id, "proxy_url": p.proxy_url, "geo": p.geo, "slot": p.slot, "notes": p.notes}
        )
    orch.ensure_image()
    orch.ensure_network()
    task = asyncio.create_task(_sweeper())
    yield
    task.cancel()


app = FastAPI(title="pentera-pool-manager", version="0.1.0", lifespan=lifespan)


# ---------- profiles ----------

@app.post("/profiles")
async def register_profile(p: ProfileIn):
    return registry.upsert_profile(p.model_dump())


@app.get("/profiles")
async def list_profiles():
    pods = {p["profile"]: p["status"] for p in orch.list_pods()}
    out = []
    for prof in registry.list_profiles():
        prof = dict(prof)
        prof.pop("proxy_url", None)  # don't leak upstream creds
        prof["pod_status"] = pods.get(prof["id"], "absent")
        out.append(prof)
    return out


@app.get("/profiles/{profile_id}")
async def get_profile(profile_id: str):
    p = registry.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    p.pop("proxy_url", None)
    p["pod_status"] = orch.pod_status(profile_id)
    p["lease_history"] = [
        l for l in registry.list_leases() if l["profile_id"] == profile_id
    ][:50]
    return p


@app.post("/profiles/{profile_id}/state")
async def set_profile_state(profile_id: str, inp: ProfileStateIn):
    if inp.state not in ("free", "quarantine", "error"):
        raise HTTPException(400, "state must be free|quarantine|error")
    if not registry.get_profile(profile_id):
        raise HTTPException(404, "profile not found")
    registry.set_profile_state(profile_id, inp.state)
    return {"ok": True}


@app.post("/profiles/{profile_id}/audit")
async def reaudit_profile(profile_id: str):
    p = registry.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    audit = await auditor.audit_proxy(
        p["proxy_url"], cfg.audit.get("proxycheck_key"), cfg.audit.get("timeout_s", 20)
    )
    if audit.get("ok"):
        registry.update_profile_audit(profile_id, audit)
    return audit


# ---------- leases ----------

@app.post("/leases", response_model=LeaseOut)
async def acquire_lease(req: LeaseRequest):
    profile = registry.acquire_free_profile(req.geo_pref, req.profile_pref)
    if profile is None:
        raise HTTPException(409, "no free profile matching constraints")

    lease_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(24)
    ttl = req.ttl_s or cfg.default_ttl_s

    try:
        # 1. measure the proxy's real exit before touching Chrome
        audit = await auditor.audit_proxy(
            profile["proxy_url"],
            cfg.audit.get("proxycheck_key"),
            cfg.audit.get("timeout_s", 20),
        )
        if not audit.get("ok"):
            raise HTTPException(502, f"proxy audit failed: {audit.get('error')}")
        registry.update_profile_audit(profile["id"], audit)

        tz = audit.get("timezone") or GEO_TZ_FALLBACK.get(
            (audit.get("country_code") or "").upper(), "UTC"
        )

        # 2. spawn the pod (docker calls are blocking → thread)
        endpoints = await asyncio.to_thread(
            orch.start_pod,
            profile,
            lease_id,
            req.mode.value,
            tz,
            token,
            req.target,
            req.engagement_id,
            req.role,
            os.environ.get("VNC_PASSWORD") or None,
        )

        # 3. wait for sidecar + chrome attach (via shared net when available)
        health_url = endpoints.get("internal_url") or endpoints["sidecar_url"]
        healthy = await wait_healthy(health_url, timeout_s=120)
        if not healthy:
            raise HTTPException(504, "pod did not become healthy in 120s")

        lease = registry.create_lease(
            {
                "id": lease_id,
                "profile_id": profile["id"],
                "engagement_id": req.engagement_id,
                "target": req.target,
                "role": req.role,
                "mode": req.mode.value,
                "state": "active",
                "sidecar_token": token,
                "endpoints": endpoints,
                "audit": audit,
                "expires_at": time.time() + ttl,
            }
        )
        ep = lease["endpoints"]
        return LeaseOut(
            lease_id=lease_id,
            profile_id=profile["id"],
            mode=req.mode.value,
            geo=audit.get("country_code"),
            timezone=tz,
            exit_ip=audit.get("ip"),
            sidecar_url=ep["sidecar_url"],
            sidecar_token=token,
            novnc_url=ep["novnc_url"],
            vnc_port=ep["vnc_port"],
            expires_at=lease["expires_at"],
            audit=audit,
            endpoints=ep,
        )
    except HTTPException:
        await asyncio.to_thread(orch.stop_pod, profile["id"], True)
        registry.set_profile_state(profile["id"], "error")
        raise
    except Exception as e:
        await asyncio.to_thread(orch.stop_pod, profile["id"], True)
        registry.set_profile_state(profile["id"], "error")
        raise HTTPException(500, f"lease failed: {e}")


@app.get("/leases")
async def list_leases(active_only: bool = Query(default=False)):
    leases = registry.list_leases(active_only=active_only)
    for l in leases:
        l.pop("sidecar_token", None)
    return leases


@app.get("/leases/{lease_id}")
async def get_lease(lease_id: str):
    lease = registry.get_lease(lease_id)
    if not lease:
        raise HTTPException(404, "lease not found")
    lease.pop("sidecar_token", None)
    lease["pod_status"] = orch.pod_status(lease["profile_id"])
    return lease


@app.post("/leases/{lease_id}/heartbeat")
async def heartbeat(lease_id: str):
    lease = registry.get_lease(lease_id)
    if not lease:
        raise HTTPException(404, "lease not found")
    if lease["state"] != "active":
        raise HTTPException(409, f"lease is {lease['state']}")
    registry.heartbeat(lease_id)
    return {"ok": True, "heartbeat_at": time.time()}


@app.post("/leases/{lease_id}/release")
async def release_lease(lease_id: str, inp: ReleaseIn | None = None):
    inp = inp or ReleaseIn()
    lease = registry.get_lease(lease_id)
    if not lease:
        raise HTTPException(404, "lease not found")
    await _teardown(
        lease_id,
        reason=inp.reason or "released",
        quarantine=inp.quarantine_profile,
    )
    return {"ok": True}


async def _teardown(lease_id: str, reason: str, quarantine: bool = False):
    lease = registry.get_lease(lease_id)
    if not lease or lease["state"] not in ("starting", "active"):
        return
    profile_id = lease["profile_id"]
    try:
        await asyncio.to_thread(orch.stop_pod, profile_id, True)
    except Exception as e:
        print(f"[teardown] stop pod {profile_id}: {e}")
    registry.update_lease(
        lease_id, state="released", released_at=time.time()
    )
    registry.set_profile_state(profile_id, "quarantine" if quarantine else "free")
    print(f"[teardown] lease={lease_id} profile={profile_id} reason={reason}")


# ---------- ops ----------

@app.get("/pods")
async def pods():
    return orch.list_pods()


@app.get("/health")
async def health():
    return {"ok": True, "profiles": len(registry.list_profiles())}
