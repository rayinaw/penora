# Working with this repo

## What this is

penora — the ARTEX extension layer. ARTEX remains the platform; this repo holds
the browser-pool service (vendored from pentera, unchanged) plus two thin
adapters that plug it into ARTEX:

- `services/bp-mcp/` — FastAPI MCP server (Streamable HTTP JSON-RPC at
  `POST /mcp`). Tools: `bp_lease_acquire/release/status` (pool-manager) +
  `browser_*` (sidecar proxy). Sessions keyed by `session_key`, persisted to
  `/data/sessions.json`, heartbeated every 30s so pool-manager doesn't reap.
- `services/traffic-bridge/` — stdlib-only daemon that tails
  `$DATA_ROOT/*/exchanges.jsonl` and POSTs converted records to ARTEX
  `/api/traffic/ingest` (JWT via /api/auth/login, offset state in
  `.bridge-state.json`).

## Conventions

- `services/browser-pool/` is VENDORED — keep changes minimal and clearly
  marked; it should stay diffable against pentera.
- ARTEX's MCP client accepts plain JSON responses (no SSE needed). Don't add
  session-id or SSE machinery to bp-mcp unless a client requires it.
- `session_key` design: MCP clients are stateless per-call in ARTEX (a fresh
  connection per agent run), so leases hang off an explicit key, not the MCP
  session. Default key "default" = one shared browser.
- pool-manager reaps leases when `heartbeat_at` is stale (>120s) or past
  `expires_at` — bp-mcp's heartbeat loop is load-bearing, don't remove it.
- ARTEX ingest has no dedupe: the bridge must only advance a file offset after
  a 200 response, and per-entry failures are still "consumed" (resending a
  good row duplicates it).
- `pool.config.yaml` / real proxy creds never commit (see .gitignore).

## Deploy checklist (VPS)

1. `docker network create pentera-net`
2. `pool.config.yaml` from example — set `profiles_root`, `data_root`
   (host paths!), `port_base`, `public_host` (this host's reachable IP for
   noVNC), real `profiles:` proxy entries, `image: pentera/browser-pod:dev`.
3. `docker compose build browser-pod-image` then `up -d` the three services.
4. In ARTEX: MCP server name MUST be `browserpool` (the skill's `mcps:` gate
   references that name), transport `http`, URL `http://<host>:9100/mcp`.
