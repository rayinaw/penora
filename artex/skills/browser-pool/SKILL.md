---
name: browser-pool
description: "Invoke when an intent needs a real persistent browser: JS-heavy SPAs, login/session flows, anti-bot targets, multi-step UI workflows, or replaying requests in page context. Leases an exclusive headed-Chrome pod (fixed proxy IP + timezone + persistent profile, noVNC human takeover) via the browser-pool MCP."
mcps: browser-pool
---

# browser-pool: persistent headed-Chrome pods

The `mcp__browser-pool__*` tools are available once this skill loads (fetch schemas
with SearchExtraTools). One `session_key` = one exclusive browser pod. Keep the
same `session_key` for every call in a workflow; use a distinct key per
parallel session.

## Session lifecycle

1. `bp_lease_acquire(session_key, mode, geo_pref?, profile_pref?, target?, ttl_s?)`
   - `geo_pref`: pick a free profile by country (e.g. `US`). `profile_pref`:
     pin an exact profile id (e.g. `us-res-01`) — use it when the browser must
     share the tool-side egress IP (ARTEX `global_proxy` = `us-res-01`), e.g.
     when replaying session cookies through exec/curl. 409 = that profile is
     leased; fall back to `geo_pref` and keep session work in-browser.
   - `mitm` (default): all page traffic recorded to the lease exchange store —
     it flows into the traffic system, and `browser_exchanges` can search it.
   - `no_mitm`: CDP capture only. Use when the target rejects the mitm CA
     (cert pinning, payment/SSO flows, TLS fingerprint checks).
   - The response includes `novnc_url` — if the flow hits a captcha, reCAPTCHA,
     MFA prompt, or complex login, hand that URL to the operator so they can
     solve it by hand; trust persists in the profile across leases.
   - `exit_ip`/`timezone` describe the pod's identity — cite them in findings.
2. Work: `browser_navigate` → `browser_snapshot` → act on element refs
   (`browser_click`/`browser_fill`/`browser_select`/`browser_hover`) → snapshot
   again. `browser_eval` for DOM/storage reads the snapshot misses;
   `browser_fetch` to replay an in-app request exactly as the page makes it
   (cookies + signing + SPA middleware preserved — the right tool for API
   calls that curl from Bash cannot reproduce).
3. `bp_lease_release(session_key)` when the session is done. Unreleased leases
   die at TTL anyway but hold the profile slot.

## Reading captured traffic

- `browser_exchanges(session_key, host?, limit?)` → light index
  (id/method/url/status/sizes).
- `browser_exchange_detail(session_key, exchange_id)` → full record with
  headers + inline bodies.
- The same exchanges are also bridged into the platform traffic store — for
  finding evidence, prefer `traffic_search`/`traffic_get` + `bind_finding_traffic`
  so the evidence is bound the standard way.

## Captcha / anti-bot challenges

When a page shows a challenge (reCAPTCHA, hCaptcha, Turnstile, "Just a moment"):

1. Call `browser_captcha_status(session_key)`.
   - `clear`/`solved` → continue working.
   - `solving` → the solver extension is working. Do NOT click, type, or
     navigate — that interferes with the solve. Wait ~10s and re-poll.
   - `present` → keep polling ~10s; the solver may still be working.
2. While polling, exhaust every automated option before involving the
   operator: reload the page once, retry the flow, try a different section or
   a non-captcha path to the same goal, wait for the solver between attempts.
3. Only after ~10 minutes unsolved AND alternates exhausted: `ask_operator`
   with the `novnc_url` — the operator solves it by hand in the real browser.
   Session, cookies, and IP do not change; resume when they reply.

## Rules

- A lease is exclusive and expensive (~15-30s pod start). Acquire once per
  session, reuse it; do not re-acquire per page.
- Every `browser_*` call needs the `session_key` from `bp_lease_acquire`.
- `browser_snapshot` output is truncated at ~30k chars — on huge pages prefer
  `browser_eval` with a targeted selector instead of paging the snapshot.
- `browser_screenshot` returns an artifact path by default (screenshots live in
  the lease dir on the pool host); only `return_b64` for small inline images.
- Do not pass the session to another workflow — profile state, cookies, and IP
  are identity; mixing targets in one session contaminates both.
