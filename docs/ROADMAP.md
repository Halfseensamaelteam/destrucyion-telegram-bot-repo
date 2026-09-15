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

## PHASE 11 — Telegram Bot

Implement bot commands in order:
1. `/start /help /status`
2. `/accounts /connect /disconnect /subscription`
3. `/save`
4. Admin commands.

Test authorization for normal users and admins.

STOP if any user can access another user's account.

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