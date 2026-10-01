# Phase Roadmap — destrucyion-telegram-bot

The project must be implemented in this order. Do not skip phases. Do not begin
a phase until the previous one has reported `PASS` (see `CLAUDE.md` §2–3, §25).

---

## PHASE 0 — Repository Audit

**Goal:** Understand the existing Saveit repository.

**Tasks:**
- Inspect every source file.
- Inspect dependencies.
- Inspect existing Telegram logic.
- Inspect session handling.
- Inspect media handling.
- Inspect configuration.
- Identify obsolete global state.
- Document current behavior.

Do NOT rewrite the application in this phase.

**Deliver:** `docs/INITIAL-AUDIT.md`

**Verification:**

- Repository runs in its original form.
- Existing behavior documented.
- Known limitations documented.

STOP after this phase.

---

## PHASE 1 — Project Rename and Foundation

**Goal:** Convert the project identity to `destrucyion-telegram-bot`.

**Tasks:**
- Rename package/project metadata.
- Create clean Python project structure.
- Create configuration system.
- Create `.env.example`.
- Create `.gitignore`.
- Add logging.
- Preserve original functionality where practical.

**Verification:**
```
python --version
python -m pytest
```
and run the application locally. Termux must be tested.

STOP if installation fails.

---

## PHASE 2 — Termux Compatibility

**Goal:** Make the core project run on Android Termux.

**Test:** Termux, Python, uv, FastAPI, Telethon, SQLite/dev database.

If PostgreSQL cannot reasonably run in the chosen Termux setup, document the
development fallback. Do not silently replace PostgreSQL in the production
architecture.

Verification must include an actual Termux run.

**Deliver:** `docs/TERMUX.md`

STOP until a clean Termux development run works.

---

## PHASE 3 — Database

**Implement:** `users`, `telegram_accounts`, `subscriptions`, `media_records`.

**Add:** SQLAlchemy, Alembic, migrations, repository/service layer.

**Test:** migrations, inserts, queries, constraints, idempotency, tenant isolation.

STOP if migrations or isolation tests fail.

---

## PHASE 4 — Subscription System

**Implement:** subscription service, weekly subscription, expiration, renewal,
active check, admin grant/revoke.

**Test:** active, expired, future, renewed, revoked.

STOP until all tests pass.

---

## PHASE 5 — Telegram Account Authentication

**Implement:** account creation, phone authentication, code verification, 2FA,
session serialization, session encryption, database storage.

**CRITICAL DESIGN CONSTRAINT — Telethon session continuity:**

`send_code_request()` and the subsequent `sign_in()` MUST be performed using
the exact same underlying session/connection (same auth key). Creating a new
`TelegramClient` with a fresh/empty `StringSession()` for `send_code_request`,
disconnecting it, and later creating ANOTHER new client with another fresh
`StringSession()` for `sign_in()` — even while correctly passing along
`phone_code_hash` — WILL reliably fail with `PhoneCodeExpiredError`, because
the login code is cryptographically tied to the specific auth key/connection
that requested it, not just the phone number.

This matters specifically because this project's authentication happens
across multiple separate bot updates (phone number message, then code
message, then optional password message) — each potentially handled by a
different function call, unlike a single long-running script (e.g. the
original Saveit.py) where one client stays connected throughout.

**Required pattern:**
1. In the "request code" step: create the client, connect, call
   `send_code_request()`, then — BEFORE disconnecting — capture
   `temp_session = client.session.save()`.
2. Return/store `temp_session` alongside `phone_code_hash` in the caller's
   temporary state (e.g. bot conversation `user_data`, or Redis with short
   TTL). This temp session is pre-authentication and low-sensitivity, but
   still must not be logged and should not persist beyond the auth flow.
3. In the "verify code" (and "verify 2FA password") step: reconstruct the
   client using that SAME `temp_session` string (`StringSession(temp_session)`)
   — never a fresh empty session — before calling `sign_in()`.

**Test with a dedicated Telegram test account, end-to-end, for real** —
Telethon mocks/unit tests will NOT catch this class of bug, since a mock
doesn't know the difference between "same auth key" and "different auth
key". A unit test can verify that the same session string is threaded
through the calls, but only a real trial with an actual Telegram account
verifies the fix actually works against Telegram's servers.

**Verify:** login (real trial, code accepted on first try, not "expired"),
restart, load session, disconnect, reconnect. Also verify the 2FA path
specifically, since it's a third step reusing the same temp session again.

STOP if session persistence is unreliable, or if code verification fails
with "expired" errors under normal (prompt) use.

---

## PHASE 6 — Telegram Client Manager
**Implement:** `TelegramClientManager` supporting multiple accounts, startup,
shutdown, reconnect, account isolation, subscription checks.

**Trial:** run Account A, B, C. Verify all three operate independently. Then
intentionally break one account.

**Expected:** `A = running, B = reconnecting/error, C = running`.

STOP if one account affects another.

---

## PHASE 7 — Media Capture

Extract the original media-saving behavior.

**Implement:** photo, video, document, voice, video note, timed/self-destructing
detection.

Test normal media first, then timed media, then duplicate updates.

STOP until capture reliability is verified.

---

## PHASE 8 — Sender Metadata

Implement metadata extraction.

**Test senders:** username exists, username absent, first/last name only,
anonymous/group context where available.

Verify the Saved Messages caption.

STOP if sender identity can be confused between users.

---

## PHASE 9 — Saved Messages

**Implement:** account → incoming media → same account → Saved Messages.

**Verify:** Account A → Account A's Saved Messages; Account B → Account B's
Saved Messages. Never cross accounts.

STOP if routing is incorrect.

---

## PHASE 10 — Idempotency and Recovery

**Test:** duplicate Telegram update, worker restart, database restart, Redis
restart, network failure, Telegram reconnect, process crash.

Verify that media is not unnecessarily duplicated.

STOP until recovery behavior is predictable.

---

## Phase 11: Auth Refactoring, Per-User Credentials & QR Login

### Objectives
Refactor the authentication flow to prevent Telegram's automated OTP detection/blocking, enforce isolated per-user API credentials, restrict access to authorized admins only, and enforce a strict 1-User-to-1-Account relationship.

---

### Tasks

#### 11.1 Database Schema & Migration Update
- [ ] Add `api_id` (Integer) and `api_hash` (String) columns to the `telegram_accounts` model.
- [ ] Add a `UNIQUE` constraint on `telegram_accounts.user_id` to enforce 1 account per user.
- [ ] Create an Alembic migration script (`alembic revision --autogenerate -m "add_api_credentials_and_unique_user_id"`) and test schema migration (`alembic upgrade head`).

#### 11.2 Admin Authorization Guard
- [ ] Implement a bot decorator/middleware in `python-telegram-bot` to check if incoming updates are from an authorized admin (`ADMIN_TELEGRAM_IDS` in `.env` or `users.is_admin == True`).
- [ ] Reject commands from non-admin users with an "Unauthorized access" message.

#### 11.3 Telethon QR Code Authentication Flow
- [ ] Deprecate phone number + OTP code input in Telegram chat to avoid OTP burn/block.
- [ ] Update `/connect` handler flow:
  1. Check if user already has an active or bound account in `telegram_accounts`. If yes, reject and ask them to `/disconnect` first.
  2. Prompt the admin user to provide their own `api_id` and `api_hash` (from https://my.telegram.org).
  3. Initialize Telethon `TelegramClient` with the provided `api_id` and `api_hash`.
  4. Generate a login QR Code using Telethon's `qr_login()` mechanism.
  5. Render the QR code image in-memory using `qrcode` + `io.BytesIO` and send it as a photo to the Telegram chat with instructions (Settings > Devices > Link Desktop Device).
  6. Listen for scan completion. Upon successful login, encrypt the session string, store `api_id`, `api_hash`, and `session_ciphertext` in `telegram_accounts`, and set status to `active`.

#### 11.4 Refactor Commands & Worker Manager
- [ ] Refactor `/accounts` command to display only the single bound account status instead of listing multiple accounts.
- [ ] Refactor `/disconnect` to clean up the user's single account, terminate worker session, and reset status.
- [ ] Update `worker/supervisor.py` to instantiate Telethon clients using each account's specific `api_id` and `api_hash` from the database, falling back to global `.env` credentials only if legacy records exist.

---

### Definition of Done (DoD)
1. Alembic migration runs cleanly without errors.
2. Non-admin users cannot interact with the bot.
3. User cannot connect more than 1 account simultaneously.
4. User can complete the login process by scanning a QR Code sent by the bot without entering any OTP in chat.
5. Telethon worker successfully starts and manages userbot sessions using per-user `api_id` and `api_hash`.

---

## PHASE 12 — FastAPI

Implement the REST API.

**Test:** authentication, authorization, tenant isolation, account management,
subscription, media history. Use automated API tests.

STOP until API tests pass.

---

## PHASE 13 — Redis and Distributed Coordination

Add Redis for locks, worker coordination, temporary auth state, rate limiting
where necessary.

**Test:** start two worker instances. Verify that one Telegram account does not
accidentally run twice.

STOP if duplicate clients can occur.

---

## PHASE 14 — Docker

**Create:** `Dockerfile`, `docker-compose.yml` with services `api`, `worker`,
`postgres`, `redis`.

**Verify:** `docker compose up`. Run integration tests inside the environment.

STOP if Docker behavior differs materially from local behavior.

---

## PHASE 15 — Vercel

Deploy request-driven components: FastAPI, Telegram Bot webhook, optional
frontend.

Do NOT deploy the persistent Telethon worker as a Vercel Function.

**Verify:** API health, bot webhook, database connectivity, authentication, CORS
if needed.

STOP if the production API is unstable.

---

## PHASE 16 — Persistent Worker

Deploy the Telethon worker to a persistent runtime.

**Verify:** startup, account loading, reconnect, subscription enforcement,
media processing, graceful shutdown.

Test worker restart. Test one account failure.

STOP if account isolation fails.

---

## PHASE 17 — Production Hardening

**Implement:** rate limiting, structured logs, audit logs, health checks,
graceful shutdown, retry policy, FloodWait handling, session revocation
handling, database connection recovery, Redis recovery, security review.

Run the complete test suite.

---

## PHASE 18 — Final Trial

Perform an end-to-end test:

```
User → Telegram Bot → Subscription → Connect Telegram Account →
Telethon session → Worker → Incoming media → Timed/self-destructing media →
Capture → Sender metadata → Saved Messages
```

- Test at least two independent users.
- Test at least two Telegram accounts.
- Test subscription expiration.
- Test worker restart.
- Test duplicate messages.

Only after this phase is the project considered production-ready.

---

## PHASE 19 — Post-Launch Fixes & Admin Notifications

> Added after real-world testing surfaced bugs the original test suite
> didn't cover (several bot handlers had zero test coverage — see
> "Verify" below), plus a new operator-requested feature.

**Bug fixes (all from real production use, not synthetic testing):**

1. `/subscription`, `/status`, and `/admin grant` all crashed with
   `AttributeError` on `lifetime` plans, because `expires_at` is `None`
   for lifetime subscriptions by design, and `.strftime()` was called on
   it unconditionally. Fixed to show "Never (Lifetime access)".
2. `/connect` conversation could get a user permanently stuck: if they
   started `/connect` and abandoned it mid-flow (no `/cancel`), every
   future `/connect` from them silently matched nothing (PTB treats them
   as "already in a conversation", and the persisted state survives bot
   restarts). Fixed with `allow_reentry=True` and a
   `conversation_timeout` on the ConversationHandler (requires the
   `python-telegram-bot[job-queue]` extra).
3. Media capture caption showed Telegram's `0x7FFFFFFF` (2147483647)
   "view once" sentinel as a literal, nonsensical second count. Fixed to
   display "View once".

**New feature — Admin Notifications (CLAUDE.md §16.3):**

`ADMIN_NOTIFY_CHAT_ID` — when configured, every captured media item also
sends a richer, separate notification to the operator's own chat/channel,
including the connected customer's full (decrypted) phone number and
subscription details, by explicit operator decision. Requires:

- New `phone_ciphertext` column (encrypted full phone number, captured
  from Telethon's `me.phone` after a successful QR login — this works
  even though the login itself never asks for a phone number, since it's
  a property of the authenticated account).
- `DISPLAY_TIMEZONE` setting for rendering timestamps (CLAUDE.md §16.2 —
  there is no automatic per-user timezone available from Telegram).
- `app/telegram/admin_notify.py` — sends via a standalone `telegram.Bot`
  API client from the worker process, independent of the bot's own
  long-polling `Application`.

**Verify:**
- `/subscription`, `/status`, `/admin grant ... lifetime` all reply
  correctly instead of crashing silently.
- Start `/connect`, abandon it (don't finish, don't `/cancel`), then send
  `/connect` again — must restart cleanly, not go silent.
- Trigger a real timed/view-once media capture; confirm the caption says
  "View once", not a raw second count.
- With `ADMIN_NOTIFY_CHAT_ID` set to a real chat the bot is a member of,
  capture a media item and confirm the notification arrives with correct
  sender info, customer info, full phone, and subscription detail.
- With `ADMIN_NOTIFY_CHAT_ID` unset, confirm capture still works
  end-to-end with zero notification attempts (no errors, no delay).

---

## PHASE 20 — Payment & Subscription Core Hardening

**Goal:** Lock the validated Midtrans QRIS and subscription behavior before
future feature work changes prices, plans, UI, or payment methods.

**Required reading:**
- `docs/SUBSCRIPTION-PAYMENT-GATEWAY-CORE.md`
- `CLAUDE.md` payment/subscription protection rules
- `AGENTS.md` change-control rules

**Protected behavior:**
- Midtrans Sandbox uses `is_production=False`.
- QRIS charge requests retain `"acquirer": "gopay"`.
- Payment expiry and subscription expiry remain separate concepts.
- Webhook signature verification must remain intact.
- Settlement must remain idempotent.
- Finite subscriptions extend from an existing active `expires_at`.
- Weekly = 7 days, Monthly = 30 days, Lifetime = no expiration.
- User-facing subscription expiry remains displayed in WIB where configured by
  the current application behavior.

**Regression verification:**
- [ ] QRIS transaction creation succeeds in Sandbox.
- [ ] Invalid webhook signature is rejected.
- [ ] Gross amount mismatch is rejected.
- [ ] Settlement activates the subscription.
- [ ] Repeated settlement does not grant the subscription twice.
- [ ] Active Weekly purchase extends the existing expiry by 7 days.
- [ ] Active Monthly purchase extends the existing expiry by 30 days.
- [ ] Lifetime remains non-expiring.
- [ ] Payment expiry is not used as subscription expiry.
- [ ] Telegram activation notification shows the finite expiry correctly.
- [ ] No protected payment code was refactored for unrelated feature work.

**Change-control rule:**

Price changes, new plans, UI changes, and additional payment methods should be
implemented around the validated core. A request for one of those changes does
not automatically authorize changes to authentication, webhook verification,
settlement, payment persistence, or subscription-extension logic.

**STOP:** Do not proceed with unrelated payment/subscription feature work until
this protection contract and its relevant regression checks are understood.
