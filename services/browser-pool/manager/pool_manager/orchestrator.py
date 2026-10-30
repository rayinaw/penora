"""Docker orchestration for browser pods.

Each active lease = one container of pentera/browser-pod with:
  - the profile's persistent dir mounted at /home/browser (aged identity)
  - a per-lease data dir mounted at /data (exchange bundle)
  - host ports = port_base + profile.slot, bound to 127.0.0.1 only
"""
import asyncio
import os
import time

import docker
import httpx
from docker.errors import ImageNotFound, NotFound

GEO_TZ_FALLBACK = {
    "US": "America/New_York",
    "GB": "Europe/London",
    "UK": "Europe/London",
    "IE": "Europe/Dublin",
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam",
    "SE": "Europe/Stockholm",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "CA": "America/Toronto",
    "JP": "Asia/Tokyo",
    "SG": "Asia/Singapore",
    "VN": "Asia/Ho_Chi_Minh",
    "IN": "Asia/Kolkata",
    "BR": "America/Sao_Paulo",
    "HK": "Asia/Hong_Kong",
}


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = docker.from_env()

    def ensure_image(self) -> None:
        try:
            self.client.images.get(self.cfg.image)
        except ImageNotFound:
            raise RuntimeError(
                f"pod image '{self.cfg.image}' not built — "
                "run: docker compose build browser-pod-image"
            )

    def ensure_network(self) -> None:
        if not self.cfg.docker_network:
            return
        try:
            self.client.networks.get(self.cfg.docker_network)
        except NotFound:
            self.client.networks.create(self.cfg.docker_network)

    def pod_name(self, profile_id: str) -> str:
        return f"pentera-pod-{profile_id}"

    def host_port(self, kind: str, slot: int) -> int:
        return self.cfg.port_base[kind] + slot

    def start_pod(
        self,
        profile: dict,
        lease_id: str,
        mode: str,
        tz: str,
        token: str,
        target: str,
        engagement_id: str,
        role: str,
        vnc_password: str | None = None,
    ) -> dict:
        name = self.pod_name(profile["id"])
        self.stop_pod(profile["id"], missing_ok=True)

        slot = profile["slot"]
        sidecar_port = self.host_port("sidecar", slot)
        novnc_port = self.host_port("novnc", slot)
        vnc_port = self.host_port("vnc", slot)

        env = {
            "PROFILE_ID": profile["id"],
            "LEASE_ID": lease_id,
            "ENGAGEMENT_ID": engagement_id,
            "TARGET": target,
            "ROLE": role,
            "MODE": mode,
            "TZ": tz,
            "LOCALE": self.cfg.locale,
            "GEO": profile.get("actual_geo") or profile.get("geo") or "",
            "SIDECAR_TOKEN": token,
            "UPSTREAM_PROXY_URL": profile["proxy_url"],
            "DATA_DIR": "/data",
            "HOME": "/home/browser",
            "CDP_URL": "http://127.0.0.1:9222",
        }
        if vnc_password:
            env["VNC_PASSWORD"] = vnc_password

        profile_dir = os.path.join(self.cfg.profiles_root, profile["id"])
        lease_dir = os.path.join(self.cfg.data_root, lease_id)
        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(lease_dir, exist_ok=True)

        volumes = {
            profile_dir: {"bind": "/home/browser", "mode": "rw"},
            lease_dir: {"bind": "/data", "mode": "rw"},
        }
        if os.path.isdir(self.cfg.extensions_root):
            volumes[self.cfg.extensions_root] = {"bind": "/opt/extensions", "mode": "ro"}

        container = self.client.containers.run(
            self.cfg.image,
            name=name,
            detach=True,
            environment=env,
            volumes=volumes,
            network=self.cfg.docker_network or None,
            ports={
                "9000/tcp": ("127.0.0.1", sidecar_port),
                "6080/tcp": ("127.0.0.1", novnc_port),
                "5900/tcp": ("127.0.0.1", vnc_port),
            },
            shm_size="2g",
            labels={
                "pentera": "browser-pod",
                "pentera.profile": profile["id"],
                "pentera.lease": lease_id,
            },
        )
        return {
            "container_id": container.id,
            "sidecar_url": f"http://127.0.0.1:{sidecar_port}",
            # reachable only when docker_network is set (manager/agents in-net)
            "internal_url": (
                f"http://{name}:9000" if self.cfg.docker_network else None
            ),
            "proxy_url": profile["proxy_url"],
            "novnc_url": (
                f"http://{self.cfg.public_host}:{novnc_port}"
                "/vnc.html?autoconnect=true&resize=scale"
            ),
            "vnc_port": vnc_port,
            "sidecar_port": sidecar_port,
            "novnc_port": novnc_port,
        }

    def stop_pod(self, profile_id: str, missing_ok: bool = False) -> None:
        name = self.pod_name(profile_id)
        try:
            c = self.client.containers.get(name)
            c.remove(force=True)
        except NotFound:
            if not missing_ok:
                raise

    def pod_status(self, profile_id: str) -> str:
        try:
            return self.client.containers.get(self.pod_name(profile_id)).status
        except NotFound:
            return "absent"

    def list_pods(self) -> list[dict]:
        out = []
        for c in self.client.containers.list(
            all=True, filters={"label": "pentera=browser-pod"}
        ):
            out.append(
                {
                    "name": c.name,
                    "status": c.status,
                    "profile": c.labels.get("pentera.profile"),
                    "lease": c.labels.get("pentera.lease"),
                }
            )
        return out


async def wait_healthy(sidecar_url: str, timeout_s: int = 120) -> bool:
    """Wait for the pod's sidecar to come up AND attach to Chrome."""
    deadline = time.time() + timeout_s
    async with httpx.AsyncClient(timeout=5) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"{sidecar_url}/health")
                if r.status_code == 200 and r.json().get("chrome"):
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False
