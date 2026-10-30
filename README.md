# penora

ARTEX, extended — the browser-pool integration layer. penora keeps ARTEX as the
platform and adds what it was missing: persistent headed-Chrome pods for
JS-heavy/authenticated/anti-bot targets, residential proxy egress, an operator
console, and CAPTCHA solving — with captured traffic flowing back into ARTEX's
traffic evidence store.

```
ARTEX ──(MCP, Streamable HTTP)──► bp-mcp ──(lease API)──► pool-manager ──(docker)──► Chrome pod
      │                            │                        │                        │ /data
      │                            │                        │                     mitm/CDP
      ◄──(/api/traffic/ingest)── traffic-bridge ◄── exchanges.jsonl ◄─────────────────┘
      │
      ├─ tool HTTP (exec/curl/fetch) ─► mitm :8788 ─► global_proxy ─► residential IP
      └─ browser traffic ─► profile proxy_url ─► residential IP (per profile)
```

## Layout

- `services/browser-pool/` — vendored from pentera (kept diffable): `manager/`
  (lease API), `pod/` (Chrome+mitm+CDP+sidecar image), `pool.config.example.yaml`
- `services/bp-mcp/` — MCP adapter: lease lifecycle + all sidecar browser tools
  as `mcp__browserpool__*` tools; also hosts the **operator console** + noVNC
  reverse proxy on :9100
- `services/traffic-bridge/` — tails every lease's `exchanges.jsonl`, converts
  to `ExternalExchange`, POSTs to ARTEX `/api/traffic/ingest` (resumable offsets)
- `artex/skills/browser-pool/SKILL.md` — worker skill; gates the MCP tools
  behind skill unlock (`mcps: browserpool`)
- `docs/` — browser-pool design doc + integration guide

## Residential proxy layer (deployed 2026-10-26)

25 Webshare **static dedicated residential IPs**, wired as one pool profile per
IP — `us-res-01..10` (US), `uk-res-01..05`, `sg-res-01..05`, `de-res-01..05`
(+ `vps-direct-01` fallback, direct datacenter exit). `pool.config.yaml` is
gitignored (real creds).

- **Browser egress** — each pod gets the profile's `proxy_url`: one profile =
  one identity = one fixed exit IP. Leases are exclusive (state machine
  free→leased; 409 on exhaustion) so two pods can never share an IP.
- **ARTEX tool egress** — `settings.global_proxy` in Postgres =
  `http://<creds>@<us-res-01 IP>:<port>`. With `traffic_capture=true`, the
  mitm on :8788 uses it as upstream: every exec/curl/WebFetch exits residential
  US instead of the datacenter IP. Verified: `api.ipify.org` → us-res-01 IP,
  `t-mobile.com` → HTTP 200 (was Akamai 403 on Contabo AS51167/FR).
- **`profile_pref`** — `bp_lease_acquire(profile_pref="us-res-01")` pins an
  exact profile (added: manager `models.py`/`registry.py`/`main.py` +
  bp-mcp tool schema). Use when browser + tool traffic must share one egress IP
  (cookie/session consistency). `geo_pref` remains for country-level selection.
- **Not covered**: raw TCP/UDP (nmap, DNS, raw TLS) still exits the datacenter
  IP. Keep HTTP-only paths for geo-sensitive targets; session-critical work
  stays in-browser.

Egress consistency rule: `us-res-01` doubles as `global_proxy`, so pinning it
gives browser and tools the same IP. Other engagements' tool traffic also exits
us-res-01 — fine for unauthenticated probes; keep session work in the pod.

## Operator console + noVNC proxy (bp-mcp, :9100)

Single SSH tunnel covers everything:

```bash
ssh -L 8787:127.0.0.1:8787 -L 9100:127.0.0.1:9100 hunter@<vps>
# ARTEX UI → localhost:8787 · Operator console → localhost:9100
```

- `GET /?k=<VNC_TOKEN>` — dashboard: paused ARTEX intents ("waiting for
  operator", with question + task link) + active pods with viewer links.
  Auto-refresh; released leases filtered out.
- `/v/<lease_id>/` — HTTP+WS reverse proxy to the pod's internal noVNC
  (`<pod>:6080` on pentera-net). Verified 200 + `101 Switching Protocols`.
- `bp_lease_acquire` returns a rewritten `novnc_url` pointing at the console —
  the link in a worker message is click-to-open, no host/port fixes.
- Auth: `?k=` token (default = `MCP_TOKEN`); 401 without it, 404 after release.

## CAPTCHA solving (deployed 2026-10-26)

- **NoPECHA** `chromium_automation` v0.6.1 in
  `$PENTERA_DATA/extensions/nopecha/`; API key lives in the extension's
  `manifest.json` `nopecha.key` (the automation build's official config path).
- **Chrome ≥137 kills `--load-extension`** (branded build, flag silently
  ignored) — pods load extensions via CDP instead: `pod/load_extensions.py`
  runs at Chrome start, waits for DevTools, calls `Extensions.loadUnpacked`
  per `/opt/extensions/*/`. Launched from `launch_chrome.sh`.
- Pool-manager only mounts `extensions_root` when `isdir()` is true **inside
  the manager container** — the host data root is bind-mounted at the same
  path (`PENTERA_ROOT`) so the check passes.
- **`browser_captcha_status`** MCP tool: detects reCAPTCHA/hCaptcha/Turnstile
  + `g-recaptcha-response` token presence → `clear|present|solving|solved`.
- Worker policy (SKILL.md): poll the tool; on `present` keep trying legitimate
  alternates (reload once, another section/path); only `ask_operator` after
  ~10 min unsolved AND alternates exhausted — 90s was too short.
- Verified end-to-end: reCAPTCHA demo solved, token len 2702.

## Operator notifications

- **ARTEX-native** (preferred): `server/notify_operator.go` fires on
  `ask_operator` → pushes to all enabled notification channels (Telegram/
  DingTalk/WeCom/Feishu/email/webhook — configure in UI System → Notifications).
  Includes task link via `notify_public_base_url` + env `ARTEX_OPERATOR_HINT`
  (console URL + tunnel command). Bypasses the vuln notification pipeline by
  design — operator asks are one-shot and immediate.
- bp-mcp side-band watcher remains as fallback (env `TG_BOT_TOKEN`/`TG_CHAT_ID`,
  currently unset → no-op).

## Verify / debug cheatsheet

```bash
# egress through ARTEX mitm (needs -k; workers trust _ca already)
curl -sk -x http://127.0.0.1:8788 https://api.ipify.org
curl -sk -x http://127.0.0.1:8788 -o /dev/null -w '%{http_code}\n' https://TARGET/

# pool state
curl -s http://127.0.0.1:8000/profiles | jq '.[] | {id,state,geo}'
curl -s http://127.0.0.1:8000/leases   | jq '.[] | {profile_id,state,target}'

# rebuild after code changes (restart alone reuses the old image!)
cd ~/penora && docker compose build pool-manager bp-mcp && \
  docker compose up -d pool-manager bp-mcp
# pod changes (launch_chrome.sh, load_extensions.py, Dockerfile):
docker compose build browser-pod-image    # new leases get it automatically

# global_proxy without UI login (JWT): write Postgres + restart
docker exec artex-postgres-1 psql -U artex -d artex -c \
  "INSERT INTO settings(key,value) VALUES('global_proxy','http://u:p@host:port') \
   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;"
docker restart artex-artex-1
```

## Bandwidth / ops notes

- Measured: ~1.7GB per active task-day (klaviyo run, ~20 leases). 2-3
  programs/day ≈ 200-250GB/mo → 250GB Webshare tier is the floor; monitor the
  dashboard, upgrade mid-cycle (prorated) if >70%.
- Webshare IP auth slot: VPS IP authorized; creds in `pool.config.yaml` anyway.
- ARTEX JWT auth: UI password required for `/api/*`; `/api/auth/*` + `/api/health`
  exempt. `jwt.key` lives in the container next to the binary.
- Operator reply to a paused intent:
  `POST /api/tasks/{id}/intents/{iid}/messages {"message":..., "request_id":...}`

## Quick start (fresh VPS)

```bash
docker network create pentera-net 2>/dev/null || true
cp services/browser-pool/pool.config.example.yaml \
   services/browser-pool/pool.config.yaml   # edit: proxies, profiles, data_root
docker compose build browser-pod-image     # heavy once, ~1.5GB
MCP_TOKEN=... ARTEX_URL=http://<artex>:8787 ARTEX_PASSWORD=... \
   docker compose up -d pool-manager bp-mcp traffic-bridge
```

Wire into ARTEX (UI): MCP → new server → name `browserpool`, transport `http`,
URL `http://<vps>:9100/mcp`, env `{"Authorization":"Bearer <MCP_TOKEN>"}`; then
visibility → bind to `worker`; copy the skill into ARTEX's skill dir.
See `docs/integration.md`.
