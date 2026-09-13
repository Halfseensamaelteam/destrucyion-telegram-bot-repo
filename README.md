# destrucyion-telegram-bot

A multi-tenant Telegram service that preserves media — especially
timed/self-destructing media — into each connected Telegram account's own
Saved Messages, with per-user subscriptions and isolated Telethon clients per
account.

> This project is being rebuilt from the original single-account
> [`Saveit`](https://github.com/DevURANIUM/Saveit) script into a
> production-ready, multi-tenant service. It is being developed in strict,
> gated phases — see `docs/ROADMAP.md`.

## Start Here

If you are an AI coding agent (Antigravity, Claude Code, etc.), **read
`CLAUDE.md` first**, then `docs/ROADMAP.md`. Do not implement anything before
reading both.

| Doc | Purpose |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | Full project specification and non-negotiable rules |
| [`AGENTS.md`](./AGENTS.md) | Generic agent entry point (AGENTS.md convention) |
| [`.agents/rules/project.md`](./.agents/rules/project.md) | Condensed rules for Antigravity's workspace-rules system |
| [`docs/ROADMAP.md`](./docs/ROADMAP.md) | Phase-by-phase implementation plan (Phase 0–18) |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | System architecture and diagrams |
| [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) | Local setup — Docker and Termux |
| [`docs/INITIAL-AUDIT.md`](./docs/INITIAL-AUDIT.md) | Phase 0 deliverable (template) |
| [`docs/TERMUX.md`](./docs/TERMUX.md) | Phase 2 deliverable (template) |

## Status

| Phase | Status | Deliverable |
|-------|--------|-------------|
| Phase 0 — Repository Audit | ✅ PASS | `docs/INITIAL-AUDIT.md` |
| Phase 1 — Project Foundation | ✅ PASS | `app/`, `worker/`, `tests/`, `pyproject.toml` |
| Phase 2 — Local Environment Verification | ✅ PASS | `docs/TERMUX.md` |
| Phase 3 — Database Schema & ORM | ✅ PASS | Alembic, ORM models |
| Phase 4 — Tenant Management | ✅ PASS | `AccountService` |
| Phase 5 — Telegram Client Infrastructure | ✅ PASS | `TelegramClientManager` |
| Phase 6 — Authentication Flow | ✅ PASS | Telethon auth state machine |
| Phase 7 — Subscriptions & Tiers | ✅ PASS | `SubscriptionService` |
| Phase 8 — Media Capture Foundation | ✅ PASS | `MediaRecordRepository`, Constraints |
| Phase 9 — Media Forwarding | ✅ PASS | `SavedMessagesService`, Metadata |
| Phase 10 — Idempotency & Recovery | ✅ PASS | `RecoveryService`, DB state |
| Phase 11 — Telegram Bot | ✅ PASS | Command Handlers (`ptb`) |
| Phase 12 — FastAPI | ✅ PASS | REST API, API Keys, Isolation |
| Phase 13 — Redis & Distributed Coordination | ✅ PASS | Distributed Locking |
| Phase 14 — Docker | ✅ PASS | `Dockerfile`, `docker-compose.yml` |
| Phase 15 — Vercel | ✅ PASS | `vercel.json`, `api/index.py` |
| Phase 16–18 | ⏳ Pending | — |

## Tech Stack (planned)

Python 3.12 · FastAPI · Telethon · SQLAlchemy 2 · Alembic · PostgreSQL ·
Redis · Pydantic v2 · pytest · uv · Docker

## Important Constraints

- Never run a persistent Telethon listener as a Vercel Function.
- One isolated Telethon client/session per connected Telegram account.
- Telegram sessions are credentials — encrypted at rest, never logged or
  exposed.
- Subscriptions are database-authoritative, not timer-based.
- The service cannot promise every self-destructing item will always be
  captured — see `CLAUDE.md` §29.

## License

_Not yet decided._
