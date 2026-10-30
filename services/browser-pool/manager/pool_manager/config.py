import os
from dataclasses import dataclass, field

import yaml


@dataclass
class ProfileSeed:
    id: str
    proxy_url: str
    geo: str | None = None
    slot: int | None = None
    notes: str | None = None


@dataclass
class Config:
    db_path: str = "./manager-data/pool.db"
    image: str = "pentera/browser-pod:dev"
    profiles_root: str = "/opt/pentera/profiles"
    data_root: str = "/opt/pentera/lease-data"
    extensions_root: str = "/opt/pentera/extensions"
    port_base: dict = field(
        default_factory=lambda: {"sidecar": 19000, "novnc": 16080, "vnc": 15900}
    )
    public_host: str = "127.0.0.1"
    docker_network: str = ""  # shared net for manager↔pod comms; empty = manager on host
    locale: str = "en-US"
    default_ttl_s: int = 3600
    heartbeat_stale_s: int = 120
    audit: dict = field(default_factory=dict)
    profiles: list[ProfileSeed] = field(default_factory=list)


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("POOL_CONFIG", "./pool.config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config(
        db_path=raw.get("db_path", Config.db_path),
        image=raw.get("image", Config.image),
        profiles_root=raw.get("profiles_root", Config.profiles_root),
        data_root=raw.get("data_root", Config.data_root),
        extensions_root=raw.get("extensions_root", Config.extensions_root),
        port_base={**Config().port_base, **(raw.get("port_base") or {})},
        public_host=raw.get("public_host", Config.public_host),
        docker_network=raw.get("docker_network", Config.docker_network),
        locale=raw.get("locale", Config.locale),
        default_ttl_s=raw.get("default_ttl_s", Config.default_ttl_s),
        heartbeat_stale_s=raw.get("heartbeat_stale_s", Config.heartbeat_stale_s),
        audit=raw.get("audit") or {},
        profiles=[
            ProfileSeed(
                id=p["id"],
                proxy_url=p["proxy_url"],
                geo=p.get("geo"),
                slot=p.get("slot"),
                notes=p.get("notes"),
            )
            for p in (raw.get("profiles") or [])
        ],
    )
    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)), exist_ok=True)
    return cfg
