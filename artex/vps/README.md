# VPS deployment artifacts (live at /home/hunter/artex/)

This directory captures the deployment-only files that live on the VPS but
are not part of the ARTEX source repo (rayinaw/artex).

- `docker-compose.yml` — the live compose on the VPS. Differs from the
  upstream/ARTEX-repo compose on purpose:
  - `image: artex:local` (locally built image — public `autumn27/artex` was
    unavailable)
  - external `pentera-net` network (shared with the penora stack so
    traffic-bridge / bp-mcp can reach it)
  - `ARTEX_OPERATOR_HINT` (tunnel hint shown to workers)
  - `127.0.0.1` port bindings (reachable only via SSH tunnel)

Not captured here (runtime state, not source):
- `data/` — Postgres-adjacent state (jwt.key, SQLite), task artifacts,
  traffic captures, transcripts. Lives in `/home/hunter/artex/data` +
  `pgdata` docker volume. Back up with `docker exec artex-postgres-1
  pg_dump ...`, not via git.
- `dist/amd64/artex` — the deployed binary (build from rayinaw/artex source).
- `.env` — secrets (POSTGRES_PASSWORD etc.). Never commit.

Binary deploy (already documented in the main README): build
`dist/amd64/artex` with `-tags embedui` on Windows, copy to
`/home/hunter/artex/dist/amd64/`, `docker build -t artex:local .` on the VPS,
`docker compose up -d artex`.
