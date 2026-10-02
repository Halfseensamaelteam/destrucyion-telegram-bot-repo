# Destrucyion Telegram Bot — AI Development Instructions

## 1. Project Identity

Project name:

```
destrucyion-telegram-bot
```

Do not use `Saveit` as the application/project name in new code, documentation, package metadata, Docker services, or UI.

The project is a multi-user Telegram service for preserving media — especially timed/self-destructing media — into the corresponding user's own Telegram Saved Messages.

The system must support:

- Multiple application users.
- Multiple Telegram user accounts.
- Telegram Bot as the control interface.
- Per-user subscriptions.
- Subscription expiration.
- Automatic timed/self-destructing media processing.
- Manual media saving.
- Sender identification.
- Saved Messages delivery.
- Local development through Termux.
- Docker-based local development where possible.
- Vercel deployment for request-driven components.
- Persistent worker deployment for Telegram MTProto clients.

The original `Saveit` repository (single-process Telethon script, one session, `downloads/` folder, hardcoded owner ID) is a reference for behavior only — not the target architecture.

---

## 2. MOST IMPORTANT RULE: STEP-BY-STEP DEVELOPMENT

The project MUST be developed in sequential phases.

Do NOT implement the entire system in one pass.

The AI coding agent must complete one phase, test it, debug it, and verify it before starting the next phase.

The workflow is:

```
PLAN
  ↓
IMPLEMENT
  ↓
RUN
  ↓
TEST
  ↓
TRIAL / ERROR
  ↓
DEBUG
  ↓
RETEST
  ↓
VERIFY
  ↓
CHECKPOINT
  ↓
NEXT PHASE
```

Never skip the verification checkpoint.

If a phase fails, STOP.

Fix the current phase before continuing.

Do not start the next phase merely because the code compiles.

---

## 3. PHASE GATE SYSTEM

Every phase must have a clear:

- objective
- files affected
- implementation tasks
- test procedure
- expected result
- failure cases
- acceptance criteria

At the end of every phase, produce a short report:

```
PHASE:
STATUS:

Implemented:
- ...

Tests:
- ...

Trial:
- ...

Errors:
- ...

Fixes:
- ...

Verification:
- ...

Next phase:
- ...
```

Allowed statuses:

```
PASS
PASS WITH WARNINGS
BLOCKED
FAILED
```

Only `PASS` or explicitly approved `PASS WITH WARNINGS` may proceed to the next phase.

---

## 4. DO NOT MAKE ASSUMPTIONS

Before implementing Telegram-specific behavior, verify it against:

- current Telethon documentation
- Telegram API behavior
- actual test results

Do not invent APIs.

Do not assume that every self-destructing Telegram message can be captured.

Do not assume media properties are identical for photos, videos, documents, or other media types.

If behavior is uncertain:

1. Create a minimal experiment.
2. Run it.
3. Record the result.
4. Adapt the implementation.

---

## 5. DEVELOPMENT ENVIRONMENTS

The project must support two local development modes.

### Mode A — Termux

Primary goal:

```
Android
  ↓
Termux
  ↓
Python
  ↓
PostgreSQL-compatible development database
  ↓
Redis-compatible service if required
  ↓
API + Worker
```

The project must NOT depend exclusively on Docker.

The core API and worker should be runnable directly from Termux.

Avoid Linux features that are unavailable or unreliable on Android/Termux unless there is no practical alternative.

Document all Termux setup commands in `docs/TERMUX.md`.

The project should work with:

```
python
pip
venv
uv
```

where supported.

Prefer `uv`, but do not make the project impossible to run with standard Python tooling.

### Mode B — Docker

Provide `docker-compose.yml` for desktop/server Linux development.

Docker is useful but not mandatory for Termux.

---

## 6. PRODUCTION ARCHITECTURE

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

See `docs/ARCHITECTURE.md` for the full breakdown.

---

## 7. VERCEL RULE

Vercel may host:

- FastAPI / HTTP API
- Telegram Bot webhook
- web dashboard
- health endpoints

Do NOT use a Vercel Function as a permanent Telethon listener.

Do NOT depend on Vercel filesystem persistence.

Long-running Telegram user-account clients must run in a persistent worker (VPS, Docker host, Railway, Render, Fly.io, or another persistent container service). The exact provider is not hard-coded into the application.

---

## 8. RECOMMENDED TECHNOLOGY

```
Python 3.12
FastAPI
Telethon
SQLAlchemy 2.x
Alembic
PostgreSQL
Redis
Pydantic v2
pydantic-settings
pytest
pytest-asyncio
httpx
uv
Docker
```

Prefer asynchronous programming.

Do not introduce Node.js, Go, Rust, or another backend language unless there is a concrete reason.

---

## 9. MULTI-TENANT MODEL

An application user may own multiple Telegram accounts.

```
Application User A
├── Telegram Account 1
├── Telegram Account 2
└── Subscription

Application User B
├── Telegram Account 3
└── Subscription
```

Every Telegram account must have:

- isolated Telethon client
- isolated session
- isolated connection state
- isolated processing state

Never create one global `TelegramClient` for all users.

Never share sessions.

Never send User A's media to User B.

---

## 10. DATABASE

Minimum tables:

### users

```
id
telegram_user_id
username
first_name
last_name
is_admin
created_at
updated_at
```

### telegram_accounts

```
id
user_id
telegram_user_id
username
phone_masked
session_ciphertext
status
last_connected_at
last_error
created_at
updated_at
```

### subscriptions

```
id
user_id
plan
starts_at
expires_at
status
created_at
updated_at
```

### media_records

```
id
user_id
telegram_account_id
source_chat_id
source_chat_title
source_chat_username
source_message_id
sender_telegram_id
sender_username
sender_display_name
media_type
ttl_seconds
saved_message_id
status
error
created_at
saved_at
```

Add a unique constraint on `telegram_account_id + source_chat_id + source_message_id` to prevent duplicate processing.

Use Alembic for schema migrations. Never manually modify production schema.

---

## 11. SUBSCRIPTION

Subscription is database-authoritative. Do not use in-memory timers as the source of truth.

Active subscription:

```
status == ACTIVE
AND starts_at <= now
AND expires_at > now
```

Example:

```
plan: weekly
starts_at:  2026-09-09T00:00:00Z
expires_at: 2026-09-16T00:00:00Z
```

When expired:

- reject new media processing
- stop automatic processing for that user
- preserve historical data
- preserve connected account unless explicitly disconnected
- allow renewal

Do not implement payments in the initial milestone. Admin-controlled subscriptions come first.

---

## 12. TELEGRAM AUTHENTICATION

The Telegram Bot is NOT the Telegram user account being monitored. There are two separate identities:

```
Bot User
    │
    └── controls application

Telegram User Account
    │
    └── monitored through Telethon
```

Never assume `bot_user_id == telegram_account_id`.

Authentication may use: phone, login code, 2FA, QR authentication.

Never store login codes or 2FA passwords. Telegram sessions are sensitive credentials.

---

## 13. SESSION SECURITY

Production session storage:

```
Telethon StringSession
        ↓
     Encrypt
        ↓
   PostgreSQL
```

Worker:

```
PostgreSQL
        ↓
     Decrypt
        ↓
StringSession
        ↓
Telethon Client
```

Never:

- log session strings
- return sessions from APIs
- commit session strings
- put sessions into error messages
- store raw sessions in public files

Use an environment-provided encryption key (`SESSION_ENCRYPTION_KEY`).

Local development may use file sessions temporarily. Local session files must be in `.gitignore`.

---

## 14. AUTOMATIC MEDIA PROCESSING

Initial supported media: photo, video, document, voice, video note.

```
Telegram event
      ↓
Identify Telegram account
      ↓
Check subscription
      ↓
Check message/media
      ↓
Detect timed/self-destructing media
      ↓
Create idempotency record
      ↓
Capture media immediately
      ↓
Create metadata
      ↓
Send to Saved Messages
      ↓
Persist saved_message_id
      ↓
Mark complete
```

Self-destructing media is time-sensitive. Do not perform unnecessary slow operations before capture. Do not introduce a queue that can delay capture.

---

## 15. SAVED MESSAGES

Saved media must be sent to `"me"` using the Telethon client belonging to the same Telegram account that received the media.

Never use an administrator's account. Never use the Bot account. Never cross user boundaries.

---

## 16. SENDER INFORMATION

Every saved media item should preserve, when Telegram exposes it:

```
Sender name
Sender username
Sender Telegram numeric ID

Source chat ID
Source chat title
Source chat username

Source message ID
Received timestamp
TTL/self-destruct information
```

Example caption:

```
💾 Destrucyion

👤 Sender: John Doe
🔗 Username: @johndoe
🆔 Telegram ID: 123456789

💬 Chat: Example Group
🆔 Chat ID: -100123456789
📨 Message ID: 98765

⏱ TTL: 10 seconds
🕐 Received: 2026-09-09 00:30:21 UTC
```

Never fabricate missing information. If username does not exist, use `Username: unavailable`.

### 16.1 Automatic vs. manual capture (mirrors Saveit's HANDLER)

Automatic capture only saves TIMED/self-destructing media by default
(`CAPTURE_ONLY_TIMED=true`). The account owner can also reply to ANY media
message with a manual trigger command (`SAVE_TRIGGER`, default `.saveit`,
matching the original Saveit.py's `HANDLER`) to force-save it regardless of
whether it is timed — including retrying a message that previously
`FAILED`. The manual trigger only ever fires for the account owner's own
outgoing messages; it must never be reachable by anyone else in the chat.

### 16.2 Timestamp display timezone

Telegram exposes no per-user timezone anywhere in its API (Bot API or
MTProto) — there is no way to automatically detect what timezone a given
Telegram user is in. All rendered timestamps (captions, admin
notifications) use a single operator-configured `DISPLAY_TIMEZONE` (an
IANA name, default `UTC`) rather than a true per-viewer automatic one.
Do not claim or imply per-user automatic timezone detection anywhere in
the UI copy.

### 16.3 Admin notifications (operator's private channel)

If `ADMIN_NOTIFY_CHAT_ID` is configured, every successfully captured media
item ALSO sends a second, richer notification to that chat, separate from
the customer-facing caption in their own Saved Messages:

```
🔔 New destructing media captured

Sender (of the media):
🆔 555666
👤 John Doe (@greekwho)
💬 Chat: Some Group
📦 Type: photo | TTL: 10s

Your customer (account owner):
🆔 7231326415
👤 @gigabasechad
📱 +6281234567890          ← FULL number, see note below
👑 Lifetime (Active) — never expires

🕐 2026-09-28 16:33:00 UTC
```

**Explicit security decision — full phone number.** The connected
customer's real, full phone number is decrypted and shown here, by
deliberate operator choice, even though it remains masked
(`phone_masked`) everywhere customer-facing. This is a narrower exposure
than removing masking altogether: the full number is still encrypted at
rest (`phone_ciphertext`, same `SessionCipher` as session/api_hash — never
plaintext in the database) and is only ever decrypted to build this one
notification, sent to this one operator-configured chat. It is never
logged, never returned from any API endpoint, and never shown to the
customer themselves or to any other user.

There is no email field anywhere in this system — users authenticate only
via Telegram, and no email-collection flow exists. Do not invent one to
satisfy this notification; if email becomes a real requirement later, that
is new scope (a `/setemail` flow or similar) and should go through its own
phase, not be bolted onto this notification silently.

A failure to send the admin notification (e.g. bot lacks permission in
that chat) must never affect the actual capture/save outcome — it is
strictly best-effort, logged and swallowed, never raised.

---

## 17. IDEMPOTENCY

A message must not be saved twice.

Unique key: `telegram_account_id + source_chat_id + source_message_id`, enforced at the database level.

Never rely on `set()` or process memory.

The system must survive: duplicate updates, worker restart, multiple worker replicas, API retries, process crashes.

---

## 18. WORKER

Worker responsibilities:

- load active Telegram accounts
- decrypt sessions
- create Telethon clients
- register event handlers
- process media
- reconnect failed accounts
- enforce subscription state
- isolate account failures

```
worker
├── account A
├── account B
├── account C
└── account D
```

If account B fails: `A = running, B = reconnecting, C = running, D = running`.

Do not terminate all accounts because one account failed.

---

## 19. BOT

Suggested user commands:

```
/start
/help
/status
/account
/accounts
/connect
/disconnect
/subscription
/save
```

Admin: `/admin /grant /revoke /users /accounts`

Bot handlers must remain thin. Business logic belongs in services.

---

## 20. API

```
GET  /api/health
GET  /api/users/me
GET  /api/accounts
POST /api/accounts/auth/start
POST /api/accounts/auth/code
POST /api/accounts/auth/2fa
POST /api/accounts/auth/qr
POST /api/accounts/{id}/disconnect
GET  /api/subscription
GET  /api/media
POST /api/media/{id}/retry
```

Admin:

```
GET  /api/admin/users
POST /api/admin/users/{id}/subscription
POST /api/admin/users/{id}/disable
```

Never expose Telegram session credentials through any endpoint. Validate webhook secrets on Telegram Bot webhook endpoints.

---

## 21. REDIS

Redis may handle: distributed locks, temporary authentication state, rate limiting, worker coordination, queues, reconnect state.

Redis is NOT the source of truth for subscriptions. PostgreSQL is authoritative.

Media capture is time-sensitive — do not put self-destructing media capture behind a slow queue.

---

## 22. LOGGING

Use structured logging. Useful fields: `user_id, telegram_account_id, telegram_user_id, source_chat_id, source_message_id, operation, status`.

Never log: session strings, auth codes, 2FA passwords, encryption keys, private media, sensitive credentials.

---

## 23. TESTING PHILOSOPHY

Testing is mandatory before moving phases. Use three levels:

**Level 1 — Unit**: subscription calculation, metadata formatting, idempotency, authorization.

**Level 2 — Integration**: PostgreSQL, Redis, API, service layer.

**Level 3 — Telegram Trial**: real test Telegram account only when required — authentication, incoming media, timed media, Saved Messages, sender metadata.

Never put real credentials into automated CI.

---

## 24. PHASE ROADMAP

See `docs/ROADMAP.md` for the full phase-by-phase plan (Phase 0 through Phase 18). Do not skip phases and do not begin implementation before reading it.

---

## 25. PHASE DISCIPLINE

The AI agent must NEVER say "I'm moving to the next phase" until the current phase has been verified.

Instead, report:

```
Phase completed.
Tests passed.
Trial completed.
No blocking errors.
Checkpoint created.

Waiting for approval before Phase X.
```

If the user explicitly instructs the agent to continue automatically, it may continue only when the current phase has passed all acceptance criteria.

If a test fails: `STOP → diagnose → fix → rerun → verify`. Never hide failures.

---

## 26. CHANGE CONTROL

Avoid massive rewrites. For each phase: inspect → modify only necessary files → test → review diff → test again.

Do not refactor unrelated code. Do not delete working functionality without a migration reason.

---

## 27. DEFINITION OF DONE

A phase is DONE only when: code exists, code runs, tests pass, trial has been performed where required, errors are handled, security is checked, documentation is updated, acceptance criteria are satisfied.

"Code compiles" is NOT sufficient.

---

## 28. FINAL SECURITY RULE

Telegram session credentials are equivalent to account passwords. Treat them as highly sensitive.

Never expose them. Never log them. Never commit them. Never return them through API responses. Never send them to another service unless explicitly required and securely protected.

---

## 29. FINAL TELEGRAM LIMITATION

The service must never promise that every self-destructing Telegram media item can always be preserved.

The implementation should make the capture attempt as early as possible when the Telegram API exposes the media to the connected account.

Possible failure reasons include: Telegram restrictions, protected content, permissions, unavailable media, account restrictions, network failure, API behavior.

The application must report actual success/failure accurately.


---

## 30. PAYMENT & SUBSCRIPTION CORE PROTECTION

The validated Midtrans QRIS and subscription flow is a protected contract.

Before changing subscription prices, adding plans, changing subscription UI,
or adding another payment method, read:

`docs/SUBSCRIPTION-PAYMENT-GATEWAY-CORE.md`

### 30.1 Protected Midtrans behavior

The following behavior must remain unchanged unless the user explicitly
requests a core change:

- Sandbox uses `is_production=False`.
- The Server Key remains loaded from application settings/secrets.
- QRIS charge requests retain:
  ```python
  "qris": {
      "acquirer": "gopay",
  }
  ```
- QRIS `custom_expiry` remains payment expiry configuration.
- `Payment.expiry_time` represents QRIS/payment expiry only.
- Order IDs remain unique.
- Payment amounts are derived from subscription-plan pricing.

### 30.2 Protected webhook behavior

`app/api/routes/midtrans.py` is highly protected.

Do not weaken, remove, or bypass:

- Midtrans signature verification.
- Server-key based signature validation.
- `hmac.compare_digest`.
- `order_id` validation.
- Payment existence checks.
- `gross_amount` validation.
- Transaction-status mapping.
- Payment status persistence.
- Settlement handling.
- Duplicate-settlement protection.
- Subscription activation after successful settlement.
- Telegram notification after a newly settled payment.

A webhook must never grant subscription access merely because a client reports
that payment succeeded.

### 30.3 Protected subscription behavior

Preserve:

- Weekly = 7 days.
- Monthly = 30 days.
- Lifetime = no expiration.
- Active finite subscriptions extend from the existing active `expires_at`.
- Expired/inactive finite subscriptions start from the new purchase time.
- Lifetime remains active without an expiration date.
- Subscription status and expiration remain internally consistent.
- Repeated settlement notifications must not grant the same payment twice.

Do not confuse:

- `Payment.expiry_time` → QRIS/payment expiration.
- `Subscription.expires_at` → premium subscription expiration.

### 30.4 Safe feature changes

Requests to:

- change prices,
- add a subscription plan,
- change subscription UI/messages,
- add another payment method,

do not automatically authorize modification of the protected payment,
webhook, settlement, persistence, or subscription-extension core.

Implement the smallest change around the existing architecture. Do not refactor
unrelated protected code.

Additional payment methods must preserve the existing QRIS flow and webhook
security.

### 30.5 Regression requirement

After a payment/subscription change, verify the affected behavior and, where
relevant:

- QRIS Sandbox transaction creation.
- Invalid signature rejection.
- Amount mismatch rejection.
- Settlement activation.
- Duplicate settlement idempotency.
- Active Weekly extension.
- Active Monthly extension.
- Lifetime non-expiration.
- Correct WIB expiry display.
- Correct Telegram activation notification.

Code compilation alone is not sufficient for payment-core changes.

### 30.6 Secret handling

Never place Midtrans keys, Telegram bot tokens, database credentials, Redis
credentials, webhook secrets, encryption keys, or other credentials in source,
documentation, commits, logs, screenshots, or test output.

Use environment variables/application settings.

### 30.7 Production repository safety

The repository's `main` branch is used by the production deployment pipeline.
Do not use `main` for speculative experiments or temporary downloadable
artifacts.

Temporary downloadable artifacts must use a temporary branch. Documentation
or rule updates intended for the production repository may be committed to
`main` when explicitly requested.

