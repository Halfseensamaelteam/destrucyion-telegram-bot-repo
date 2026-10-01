# Development Guide — destrucyion-telegram-bot

This document is filled in progressively as phases complete (see
`docs/ROADMAP.md`). It currently contains the target setup; commands should be
verified and corrected during Phase 1 and Phase 2.

## Prerequisites

- Python 3.12
- `uv` (preferred) or `pip` + `venv`
- PostgreSQL (or a documented Termux-friendly fallback — see `docs/TERMUX.md`)
- Redis
- A Telegram API ID/Hash from https://my.telegram.org
- A Telegram Bot token from @BotFather

## Local Setup (Docker)

```bash
cp .env.example .env
# fill in .env with real values
docker compose up
```

Services started: `api`, `worker`, `postgres`, `redis`.

## Local Setup (Termux / no Docker)

```bash
pkg update && pkg upgrade
pkg install python git
pip install uv
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt   # or: uv sync, once pyproject is populated
cp .env.example .env
```

Run API and worker as separate processes:

```bash
uvicorn app.main:app --reload
python -m worker.main
```

Full Termux-specific notes (PostgreSQL availability, fallback database, known
issues) belong in `docs/TERMUX.md` and must be produced during Phase 2.

## Environment Variables

See `.env.example` for the full list. Never commit a filled-in `.env`.

## Database Migrations

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```

Every model change requires a migration. Never edit production schema by hand.

## Running Tests

```bash
pytest tests/unit
pytest tests/integration
pytest tests/telegram   # requires a dedicated test Telegram account; not run in CI
```

## Connecting a Telegram Account

Handled through the Bot's `/connect` command or the API's
`/api/accounts/auth/*` endpoints (phone → code → optional 2FA, or QR login).
Sessions are encrypted before being stored — see `CLAUDE.md` §13.

## Subscription Management

Subscriptions are admin-controlled in the initial milestone (no payments yet).
Use the admin bot commands (`/grant`, `/revoke`) or the admin API endpoints.

## Running the Worker

```bash
python -m worker.main
```

The worker loads all active Telegram accounts, decrypts their sessions,
starts one isolated Telethon client per account, and begins processing events.

## Deployment

- **Vercel**: FastAPI app + Telegram Bot webhook (request-driven only).
- **Persistent worker**: VPS / Docker host / Railway / Render / Fly.io —
  anywhere that supports a long-running process with stable outbound network
  access to PostgreSQL, Redis, and Telegram's MTProto servers.

## Keeping This Document Updated

Update this file whenever:
- environment variables change
- deployment steps change
- CLI commands change
- authentication flow changes
- database setup changes


## Payment Regression Checks

The payment core has a dedicated regression set:

```bash
python -m pytest tests/unit/services/test_midtrans.py tests/unit/services/test_payment.py tests/unit/api/test_midtrans.py -q
```

The broader unit suite should also be run before a development checkpoint.

A real Sandbox smoke test should exercise the Telegram purchase flow and verify
QRIS creation, Midtrans settlement, webhook processing, subscription
activation/extension, and Telegram notification.

Never put payment or Telegram credentials in source, documentation, commits,
logs, screenshots, or test output.
