# AGENTS.md

This file follows the common `AGENTS.md` convention so that any AI coding
agent (Antigravity, Claude Code, Cursor, Copilot Workspace, etc.) knows how to
work in this repository.

## Read First

Before making any change, read `CLAUDE.md` in full. It is the primary
specification for this project and takes precedence over general assumptions.

Antigravity users: workspace rules also live in `.agents/rules/project.md`,
which summarizes the same rules for that tool's rules mechanism.

## Project

```
destrucyion-telegram-bot
```

A multi-tenant Telegram service that saves media — especially
timed/self-destructing media — to the *same* connected Telegram account's own
Saved Messages, with per-user subscriptions and an isolated Telethon client
per account.

## Non-Negotiable Rules (summary)

1. **Work in phases.** Follow `docs/ROADMAP.md` in order. Never implement the
   whole system in one pass. Stop and report at every phase gate.
2. **Never run a persistent Telethon listener on Vercel.** Vercel is for
   request-driven components only (FastAPI, bot webhook). Long-running
   Telethon clients belong in a persistent worker.
3. **One Telethon client per Telegram account.** Never share a client or
   session across accounts or users.
4. **Tenant isolation is mandatory.** No application user may ever access
   another user's Telegram session, subscription, media records, or account
   info.
5. **Sessions are credentials.** Never log, expose, commit, or return a
   Telegram session string through any interface. Encrypt at rest.
6. **PostgreSQL is authoritative for subscriptions.** Never use in-memory
   timers or Redis as the source of truth.
7. **Idempotency is database-enforced**, keyed on
   `telegram_account_id + source_chat_id + source_message_id`. Never use a
   Python `set()` for this.
8. **Don't over-promise.** Never claim every self-destructing media item can
   always be captured — report actual success/failure.

## Commands

_Fill in once Phase 1 establishes the real tooling:_

```bash
# install
uv sync

# run tests
pytest

# run API
uvicorn app.main:app --reload

# run worker
python -m worker.main

# migrations
alembic upgrade head
```

## Where Things Live

- `CLAUDE.md` — full specification (read this first).
- `docs/ROADMAP.md` — phase-by-phase implementation plan.
- `docs/ARCHITECTURE.md` — system architecture and diagrams.
- `docs/DEVELOPMENT.md` — local setup (Docker and Termux).
- `docs/INITIAL-AUDIT.md` — Phase 0 deliverable (audit of the original repo).
- `docs/TERMUX.md` — Phase 2 deliverable (verified Termux setup).
- `.agents/rules/project.md` — condensed rules for Antigravity's rules system.


## Payment & Subscription Core Protection

The validated payment/subscription implementation is a protected contract.

Before changing subscription prices, adding plans, changing subscription UI,
or adding another payment method, read:

`docs/SUBSCRIPTION-PAYMENT-GATEWAY-CORE.md`

### Protected behavior

- Midtrans Sandbox must use `is_production=False`.
- QRIS must retain `"acquirer": "gopay"`.
- `Payment.expiry_time` is payment/QRIS expiry, not subscription expiry.
- Webhook signature verification must not be weakened or bypassed.
- Settlement must remain idempotent.
- Payment amount and order ID validation must remain server-side.
- Weekly = 7 days.
- Monthly = 30 days.
- Lifetime = no expiration.
- Active finite subscriptions extend from the existing active `expires_at`,
  not from the current time.
- Existing validated behavior must remain intact when adding UI, prices,
  plans, or payment methods.

### Change control

1. Prefer the smallest possible change.
2. Do not refactor unrelated payment/subscription code.
3. Do not infer permission to modify protected core from a request to change a
   price, plan, UI, or payment method.
4. Protected-core changes require an explicit request identifying the intended
   behavior change.
5. Run the relevant regression tests after payment/subscription changes.
6. Never place credentials or secrets in source, documentation, commits, logs,
   screenshots, or test output.

### Production repository safety

The `main` branch of this repository is used as a production deployment
source. Avoid speculative or temporary runtime changes on `main`.
Documentation-only changes are acceptable when intentionally requested.
Temporary downloadable artifacts should use a temporary branch and must not
alter production behavior.



## Payment & Subscription Core Protection

Before changing subscription pricing, plans, UI, payment methods, payment
persistence, webhook behavior, or subscription logic, read
`docs/SUBSCRIPTION-PAYMENT-GATEWAY-CORE.md` and
`docs/PAYMENTS-MIDTRANS.MD`.

Protected behavior includes:

- Sandbox `is_production=False`.
- QRIS `acquirer=gopay`.
- Server-side webhook signature verification.
- Payment amount validation.
- Settlement idempotency.
- Weekly 7-day and Monthly 30-day durations.
- Lifetime without expiration.
- Active finite subscription extension from existing `expires_at`.
- Separate payment expiry and subscription expiry.

Do not refactor this core for unrelated feature requests.
