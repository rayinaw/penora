# ARTEX ↔ penora integration guide

Two integration surfaces, both one-way doors into ARTEX's existing machinery —
no ARTEX code changes.

## 1. bp-mcp → ARTEX MCP (Phase 1)

bp-mcp serves Streamable HTTP JSON-RPC at `POST /mcp` — the exact protocol
ARTEX's `mcphttp` client speaks (plain JSON responses accepted; no SSE).

In the ARTEX web UI:

1. **MCP → New server**
   - Name: **`browserpool`** (exactly this — the skill's `mcps:` front-matter
     gates tools by server name)
   - Transport: `http`
   - URL: `http://<vps>:9100/mcp`
   - Env / headers: `{"Authorization": "Bearer <MCP_TOKEN>"}` — required if
     bp-mcp runs with `MCP_TOKEN` set (recommended on a public VPS).
2. **Visibility** — bind the server to the agents that may use it:
   `worker` (execution) and optionally `planner`/`mainagent` for steering.
3. **Skill** — copy `artex/skills/browser-pool/` into ARTEX's skill dir
   (`$ARTEX_HOME/skills/` or upload via the Skills page). Because the skill
   declares `mcps: browserpool`, the `mcp__browserpool__*` tools stay
   deferred until a worker invokes the Skill — no prompt-bloat, no misfires.
   To have them globally available instead, drop the `mcps:` line or bind
   the server in visibility without skill-gating.

Tools exposed: `bp_lease_acquire`, `bp_lease_release`, `bp_lease_status`,
`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill`,
`browser_press`, `browser_type`, `browser_hover`, `browser_select`,
`browser_eval`, `browser_fetch`, `browser_cookies`, `browser_screenshot`,
`browser_tabs`, `browser_tab_new`, `browser_tab_switch`, `browser_tab_close`,
`browser_console`, `browser_exchanges`, `browser_exchange_detail`.

## 2. traffic-bridge → ARTEX ingest (Phase 2)

The bridge watches the pool `data_root` and POSTs every captured exchange to
`POST /api/traffic/ingest`. It authenticates like the ARTEX browser extension:
`Authorization: Bearer <jwt>` from `POST /api/auth/login`
(`username: "ARTEX"`, the admin password). Env:

- `ARTEX_URL` — reachable base, e.g. `http://artex:8787` (same net) or the
  public URL.
- `ARTEX_PASSWORD` — preferred; the bridge refreshes its 7-day JWT itself.
- `ARTEX_TOKEN` — static JWT alternative (will need periodic rotation).
- `PENTERA_DATA_ROOT` (compose) must equal pool.config.yaml's `data_root`.

Once bridged, pod traffic behaves like proxy/extension traffic: it shows on
the Traffic page, `traffic_search`/`traffic_get`/`traffic_blob` read it, and
`bind_finding_traffic` can attach it to findings as evidence.

## Identity & ops notes

- One `session_key` = one pod = one profile/IP/TZ. Agents MUST reuse the key
  for a workflow and release when done; parallel intents use distinct keys.
- noVNC takeover: `bp_lease_acquire` returns `novnc_url` — open it (host ports
  `16080+slot`, published on `public_host` — set to the VPS's reachable IP) to
  solve captchas/logins by hand; trust persists in the profile across leases.
- Security: keep `9100` and the pod ports bound to localhost or firewalled to
  the ARTEX host; `MCP_TOKEN` on; `public_host` should be the Tailscale/private
  IP, not the public one, if you don't want noVNC internet-facing.
