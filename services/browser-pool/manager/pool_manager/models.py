from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Mode(str, Enum):
    mitm = "mitm"
    no_mitm = "no_mitm"


class ProfileIn(BaseModel):
    id: str
    proxy_url: str
    geo: Optional[str] = None
    slot: Optional[int] = None
    notes: Optional[str] = None


class ProfileStateIn(BaseModel):
    state: str  # free | quarantine | error


class LeaseRequest(BaseModel):
    engagement_id: str
    target: str
    role: str = "mapper"
    geo_pref: Optional[str] = None
    profile_pref: Optional[str] = None
    mode: Mode = Mode.mitm
    ttl_s: Optional[int] = None


class LeaseOut(BaseModel):
    lease_id: str
    profile_id: str
    mode: str
    geo: Optional[str]
    timezone: str
    exit_ip: Optional[str]
    sidecar_url: str
    sidecar_token: str
    novnc_url: str
    vnc_port: int
    expires_at: float
    audit: dict
    endpoints: dict = {}  # internal_url + proxy_url for in-network callers


class ReleaseIn(BaseModel):
    quarantine_profile: bool = False
    reason: Optional[str] = None
