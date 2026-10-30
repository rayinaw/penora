"""SQLite-backed registry: profiles, leases, deterministic port slots.

A profile's `slot` fixes all its host ports (port_base + slot), matching the
"every profile has a fixed CDP/proxy/display identity" model.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id          TEXT PRIMARY KEY,
  slot        INTEGER UNIQUE,
  proxy_url   TEXT NOT NULL,
  geo         TEXT,
  state       TEXT NOT NULL DEFAULT 'free',
  actual_geo  TEXT,
  actual_ip   TEXT,
  timezone    TEXT,
  last_audit  TEXT,
  notes       TEXT,
  created_at  REAL,
  last_used   REAL
);
CREATE TABLE IF NOT EXISTS leases (
  id            TEXT PRIMARY KEY,
  profile_id    TEXT NOT NULL,
  engagement_id TEXT,
  target        TEXT,
  role          TEXT,
  mode          TEXT,
  state         TEXT,
  sidecar_token TEXT,
  endpoints     TEXT,
  audit         TEXT,
  created_at    REAL,
  heartbeat_at  REAL,
  expires_at    REAL,
  released_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_leases_state ON leases(state);
"""


class Registry:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ---------- profiles ----------

    def upsert_profile(self, p: dict) -> dict:
        with self._lock:
            if p.get("slot") is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(slot), 0) + 1 AS s FROM profiles"
                ).fetchone()
                p["slot"] = row["s"]
            self._conn.execute(
                """INSERT INTO profiles
                   (id, slot, proxy_url, geo, state, notes, created_at)
                   VALUES (?, ?, ?, ?, COALESCE(
                       (SELECT state FROM profiles WHERE id=?), 'free'), ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     proxy_url=excluded.proxy_url,
                     geo=excluded.geo,
                     slot=excluded.slot,
                     notes=excluded.notes""",
                (
                    p["id"],
                    p["slot"],
                    p["proxy_url"],
                    p.get("geo"),
                    p["id"],
                    p.get("notes"),
                    time.time(),
                ),
            )
            self._conn.commit()
            return self.get_profile(p["id"])

    def get_profile(self, profile_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM profiles WHERE id=?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_profiles(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM profiles ORDER BY slot").fetchall()
        return [dict(r) for r in rows]

    def set_profile_state(self, profile_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE profiles SET state=? WHERE id=?", (state, profile_id)
            )
            self._conn.commit()

    def update_profile_audit(self, profile_id: str, audit: dict) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE profiles SET actual_geo=?, actual_ip=?, timezone=?,
                   last_audit=? WHERE id=?""",
                (
                    audit.get("country_code"),
                    audit.get("ip"),
                    audit.get("timezone"),
                    json.dumps(audit),
                    profile_id,
                ),
            )
            self._conn.commit()

    def acquire_free_profile(
        self, geo_pref: str | None, profile_pref: str | None = None
    ) -> dict | None:
        """Atomically pick + lock a free profile. Returns None if pool empty."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM profiles WHERE state='free' ORDER BY last_used ASC"
            ).fetchall()
            if not rows:
                return None
            cand = list(rows)
            if profile_pref:
                cand = [r for r in cand if r["id"] == profile_pref]
            if geo_pref:
                exact = [r for r in cand if (r["geo"] or "").upper() == geo_pref.upper()]
                if exact:
                    cand = exact
            for r in cand:
                cur = self._conn.execute(
                    "UPDATE profiles SET state='leased', last_used=? "
                    "WHERE id=? AND state='free'",
                    (time.time(), r["id"]),
                )
                if cur.rowcount == 1:
                    self._conn.commit()
                    return self.get_profile(r["id"])
            return None

    # ---------- leases ----------

    def create_lease(self, lease: dict) -> dict:
        with self._lock:
            lid = lease.get("id") or uuid.uuid4().hex
            now = time.time()
            self._conn.execute(
                """INSERT INTO leases
                   (id, profile_id, engagement_id, target, role, mode, state,
                    sidecar_token, endpoints, audit, created_at, heartbeat_at,
                    expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lid,
                    lease["profile_id"],
                    lease.get("engagement_id"),
                    lease.get("target"),
                    lease.get("role"),
                    lease.get("mode"),
                    lease.get("state", "starting"),
                    lease.get("sidecar_token"),
                    json.dumps(lease.get("endpoints") or {}),
                    json.dumps(lease.get("audit") or {}),
                    now,
                    now,
                    lease.get("expires_at"),
                ),
            )
            self._conn.commit()
            return self.get_lease(lid)

    def get_lease(self, lease_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM leases WHERE id=?", (lease_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["endpoints"] = json.loads(d.get("endpoints") or "{}")
        d["audit"] = json.loads(d.get("audit") or "{}")
        return d

    def list_leases(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM leases ORDER BY created_at DESC"
        if active_only:
            q = (
                "SELECT * FROM leases WHERE state IN ('starting','active') "
                "ORDER BY created_at DESC"
            )
        out = []
        for r in self._conn.execute(q).fetchall():
            d = dict(r)
            d["endpoints"] = json.loads(d.get("endpoints") or "{}")
            out.append(d)
        return out

    def update_lease(self, lease_id: str, **fields) -> None:
        cols = []
        vals = []
        for k, v in fields.items():
            if k in ("endpoints", "audit") and not isinstance(v, str):
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(lease_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE leases SET {', '.join(cols)} WHERE id=?", vals
            )
            self._conn.commit()

    def heartbeat(self, lease_id: str, extra_ttl_s: int | None = None) -> None:
        with self._lock:
            now = time.time()
            self._conn.execute(
                "UPDATE leases SET heartbeat_at=? WHERE id=?", (now, lease_id)
            )
            if extra_ttl_s:
                self._conn.execute(
                    "UPDATE leases SET expires_at=? WHERE id=?",
                    (now + extra_ttl_s, lease_id),
                )
            self._conn.commit()

    def expired_leases(self, stale_s: int) -> list[dict]:
        now = time.time()
        rows = self._conn.execute(
            """SELECT * FROM leases
               WHERE state IN ('starting','active')
               AND ( (expires_at IS NOT NULL AND expires_at < ?)
                  OR (heartbeat_at IS NOT NULL AND heartbeat_at < ?) )""",
            (now, now - stale_s),
        ).fetchall()
        return [dict(r) for r in rows]
