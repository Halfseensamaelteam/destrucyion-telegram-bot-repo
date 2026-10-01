# Architecture — destrucyion-telegram-bot

## Overview

`destrucyion-telegram-bot` is a multi-tenant service that preserves Telegram
media — especially timed/self-destructing media — into the same account's own
Saved Messages. It replaces the original single-process `Saveit` script
(one `TelegramClient`, one session, global state) with an isolated,
database-backed, multi-account service.

## Production Architecture

```
                        Telegram
                           │
                 ┌─────────┴─────────┐
                 │                   │
                 ▼                   ▼
          Telegram Bot        Telegram User
          Bot API             MTProto
                 │                   │
                 ▼                   ▼
             ┌──────────────────────────┐
             │     Application          │
             │                          │
             │ FastAPI                  │
             │ Bot handlers             │
             │ Business services        │
             └────────────┬─────────────┘
                          │
                ┌─────────┴─────────┐
                ▼                   ▼
          PostgreSQL              Redis
                │                   │
                └─────────┬─────────┘
                          │
                          ▼
                Persistent Worker
                          │
                ┌─────────┼─────────┐
                ▼         ▼         ▼
             Account A Account B Account C
                │         │         │
                ▼         ▼         ▼
             Telegram  Telegram  Telegram
             Saved Msg Saved Msg Saved Msg
```

## Deployment Split

Vercel hosts request-driven components only:

```
Vercel
  ├── FastAPI / API
  ├── Admin dashboard (optional)
  └── Telegram Bot webhook
```

A persistent worker (VPS / Docker host / Railway / Render / Fly.io / Termux for
dev) hosts long-running Telethon clients:

```
Worker/VPS/Container
  └── Telethon clients
       ├── account #1
       ├── account #2
       ├── account #3
       └── ...
```

**Rule:** Never run a permanent Telethon event listener inside a Vercel
Function. Vercel Functions are request-driven and instances scale to zero;
`run_until_disconnected()` cannot live there.

## Local Development

```
docker compose
 ├── app (FastAPI)
 ├── worker (Telethon)
 ├── postgres
 └── redis
```

or, on Termux, API + worker run directly via `uv`/`venv` without Docker (see
`docs/DEVELOPMENT.md` and `docs/TERMUX.md`).

## Multi-Account / Multi-Tenant Model

```
Application User A
├── Telegram Account 1
├── Telegram Account 2
└── Subscription

Application User B
├── Telegram Account 3
└── Subscription
```

Each Telegram account has its own isolated Telethon client, session, connection
state, and processing state. There is no global `TelegramClient`.

## Worker Isolation

```
worker
  ├── account task A
  ├── account task B  (fails independently)
  ├── account task C
  └── account task D
```

If account B fails: `A = running, B = reconnecting, C = running, D = running`.
The worker never restarts all accounts because one account failed.

## Data Flow — Automatic Media Capture

```
Telegram event
    ↓
identify account
    ↓
verify subscription
    ↓
verify media
    ↓
detect timed/self-destructing state
    ↓
create idempotency record
    ↓
download/obtain media
    ↓
create source metadata
    ↓
send to Saved Messages
    ↓
store saved_message_id
    ↓
mark completed
```

The capture path is kept short and free of unnecessary DB/network calls, since
self-destructing media is time-sensitive.

## Module Layout

```
app/
├── api/            FastAPI routes + dependencies
├── bot/            Telegram Bot handlers (thin; call services)
├── telegram/        Telethon client manager, auth, media, metadata, events
├── services/       Business logic (subscription, account, media, user)
├── db/             Models, repositories, session, migrations
├── core/           Config, security, logging, exceptions
└── main.py

worker/
├── main.py         Worker entrypoint (persistent process)
└── supervisor.py   Per-account task supervision / restart logic
```

See `CLAUDE.md` for full database schema, API surface, and security rules.


## Payment & Subscription Boundary

Payment and subscription are separate domain concerns:

```
Telegram subscription UI
        ↓
PaymentService
   ├── MidtransService → Midtrans
   └── PaymentRepository → PostgreSQL
                              ↑
                       Midtrans webhook
                              ↓
                    verified settlement
                              ↓
                    SubscriptionService
                              ↓
                    Telegram notification
```

`Payment.expiry_time` is the QRIS/payment deadline. It must never be
treated as `Subscription.expires_at`, which represents premium access
expiry.

Settlement is authoritative: a client-side claim of successful payment never
activates a subscription.
