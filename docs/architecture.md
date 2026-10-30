# Pentera — Autonomous Pentest Agent System

Hệ thống multi-agent cho autonomous bug bounty hunting, mô phỏng workflow của một
bug bounty hunter thật: đặt giả định trên từng chức năng/API, recon tối đa, đi qua
từng tính năng bằng browser thật, hunt theo hypothesis chứ không quét mù.

## Tổng quan

```
                        ┌─────────────────────────────────────────────┐
                        │              CONTROL PLANE                  │
                        │  engagement DB │ task queue │ coverage model │
                        │  scope guard   │ event bus  │ dashboard API  │
                        └───────┬───────────────────┬─────────────────┘
                                │ tasks             │ artifacts/events
        ┌───────────────────────┼───────────────────┼───────────────────────┐
        ▼                       ▼                   ▼                       ▼
  ┌──────────┐          ┌──────────────┐    ┌─────────────┐        ┌─────────────┐
  │Coordinator│          │ Agent Runtime │    │Browser Pool │        │  Identity   │
  │ (LLM loop)│          │(LLM loops,    │   │  Service    │        │  Service    │
  │           │          │ role prompts) │   │             │        │             │
  └──────────┘          └──────┬───────┘    └──────┬──────┘        └─────────────┘
                               │                 │ lease → sidecar tools
                               ▼                 ▼
                        recon / mapper / research / hunter agents
```

## Nguyên tắc thiết kế

1. **Deterministic vs LLM boundary.** Coverage tracking, dedup, scope enforcement,
   task queue = code. Hypothesis generation, prioritization, triage judgment = LLM.
   Không bao giờ để LLM tự nhớ "đã test gì" — nó query coverage model.

2. **Artifacts, không phải chat.** Agents giao tiếp qua structured artifacts
   (exchange records, asset inventory, findings) ghi vào engagement DB — không
   truyền text blob cho nhau.

3. **Event-driven, không phase-gated.** Coordinator không đợi recon xong mới cho
   hunter chạy. Mỗi artifact mới → audit tick → hunt task nếu có hypothesis đủ mạnh.

4. **Coverage = (endpoint, param, vuln_class, auth_role).** Đây là cái làm cho
   hunting "thông minh": biết chính xác còn trống chỗ nào.

5. **Scope guard là middleware code**, nằm giữa mọi agent tool và network egress.
   Agent hallucinate URL ra ngoài scope phải bị chặn cứng, không phải prompt-nhắc.

## Agents

| Agent         | Vai trò |
|---------------|---------|
| Coordinator   | Nhận target từ user, audit artifact stream, sinh hunt tasks, quản lý budget/priority |
| Recon         | Phân loại target (company assets / subdomain / domain / api / url), find-max recon, săn cả bug giai đoạn recon (leakage, exposed assets) |
| Mapper/Auth   | Crawl unauth + post-auth (tools + browser thật), register/login qua từng tính năng, capture mọi exchange |
| Researcher    | Đọc docs/spec/blog/vuln history của target → target-knowledge cho business-logic testing |
| Hunter (n×)   | Nhận hunt task trên endpoint cụ thể, test theo vuln class + hypothesis |
| Verifier      | Replay PoC, dedup vs known findings, chặn false positive trước khi lên dashboard |

## Services

- **browser-pool** (repo này build trước): persistent headed Chrome profiles,
  1 profile = 1 fixed proxy IP = 1 OS timezone, exclusive leasing, MITM/no-MITM,
  noVNC human takeover. Xem `docs/browser-pool.md`.
- **identity**: email catch-all → webhook → poll API; SMS fallback; account
  registry (target, role, creds, session state, profile binding).
- **control-plane**: engagement DB (Postgres), task queue, coverage model,
  scope guard, dashboard, human-in-the-loop approvals.

## Build order

1. ✅ browser-pool service (component này)
2. control plane MVP: engagement DB + task queue + agent loop runtime + artifact ingest
3. event-driven coordinator + coverage model
4. identity service + dashboard + verifier
