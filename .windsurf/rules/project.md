---
trigger: always_on
---

# Destrucyion Telegram Bot — Agent Rule

## Mandatory

Before doing any work, read:

```
@CLAUDE.md
```

`CLAUDE.md` is the primary project specification.

---

## Critical Rule: Work in Phases

NEVER implement the whole project at once.

Always work:

```
Phase
 ↓
Implement
 ↓
Run
 ↓
Test
 ↓
Trial
 ↓
Fix
 ↓
Retest
 ↓
Verify
 ↓
Checkpoint
 ↓
Next Phase
```

If the current phase fails:

```
STOP
```

Fix it before proceeding. Do not skip a failed test.

---

## Project Name

Use `destrucyion-telegram-bot`. Do not introduce the old project name
`Saveit` into new code.

---

## Development Targets

The project must work in:

1. Termux
2. Linux/Docker
3. Vercel for request-driven services
4. Persistent worker environment for Telethon

Do not make Docker mandatory for Termux.

Do not run a permanent Telethon listener in a Vercel Function.

---

## Technology

Preferred:

```
Python 3.12
FastAPI
Telethon
SQLAlchemy 2
Alembic
PostgreSQL
Redis
Pydantic v2
pytest
uv
Docker
```

Do not introduce another backend language without a concrete reason.

---

## Telegram Architecture

Telegram Bot: control interface.

Telethon: user account connection, media monitoring, media saving.

These are different identities. Each Telegram user account must have its own
Telethon client and session. Never use a global client for all users.

---

## Multi-Tenant Security

Always scope data by application user and Telegram account.

Never allow User A to reach User B's account/session/media.

Telegram numeric IDs are the identity source. Do not use usernames as primary
identity.

---

## Session Security

Telegram sessions are secrets. Never log, expose, commit, return through
APIs, or store in public files. Production sessions must be encrypted. Never
store login codes or 2FA passwords.

---

## Subscription

PostgreSQL is authoritative.

Active: `status == ACTIVE AND starts_at <= now AND expires_at > now`.

Expired subscriptions must stop new media processing. Do not use an
in-memory timer as the source of truth.

---

## Media Capture

Timed/self-destructing media is time-sensitive. Prioritize immediate capture.
Do not put capture behind a slow queue.

Initial media types: photo, video, document, voice, video note.

Never claim that all self-destructing media can always be captured.

---

## Saved Messages

Save media to `"me"` using the same Telethon user account that received it.
Never cross accounts. Every saved media item should preserve available
sender/source metadata.

---

## Idempotency

Use database-level idempotency:
`telegram_account_id + source_chat_id + source_message_id`.

Do not rely on Python sets or process memory. Assume duplicate updates and
concurrent workers.

---

## Worker

Accounts must be isolated:

```
worker
├── account A
├── account B
├── account C
└── account D
```

Failure of B must not terminate A, C, or D. Handle Telegram rate limits,
FloodWait, network errors, session revocation, and RPC errors.

---

## Bot

Keep handlers thin. Business logic belongs in services.

Suggested commands: `/start /help /status /accounts /connect /disconnect
/subscription /save`.

Admin commands may include: `/grant /revoke /users /accounts`.
