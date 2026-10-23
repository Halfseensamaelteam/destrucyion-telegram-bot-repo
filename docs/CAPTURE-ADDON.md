# Auto-Capture Add-on (`/capture`)

Auto-Capture is a **paid add-on**, separate from the base subscription.
It governs exactly one thing: automatic capture of **protected**
(`Message.noforwards` / "Restrict saving content") **non-timed** media.

- Timed / self-destructing media: captured everywhere, unchanged — the
  add-on does not affect it.
- Explicit saves (`/save`, bare t.me links, the `.get` trigger,
  `.saveit`): unchanged.
- Protected non-timed media: captured **only** from the ONE channel/group
  the user registered with `/capture`, and only while their add-on is
  active. Everything else is skipped silently.

## User flow

All `/capture` copy is English.

```
/capture                        status (entitled) or the paywall (not entitled)
/capture add -1001234567890     register a channel/group
/capture remove -1001234567890  remove the registration
```

Rules:

- **ID only.** Links, usernames, and non-numeric arguments are rejected;
  positive IDs (user IDs) are rejected — channel/group IDs are negative.
- **Gates, in order:** connected account → active base subscription →
  active capture add-on. Without a base subscription the user is pointed
  to `/subscription` (the add-on is never sold there — the paywall lives
  only inside `/capture`). `/capture remove` is intentionally *not*
  gated on entitlement, so capture can always be stopped.
- **Limit 1 channel per account.** Registering a second one is rejected
  with the currently registered ID; the DB enforces the same rule with a
  UNIQUE constraint.
- **Membership is verified at add time** through the user's *own*
  connected account: a short-lived Telethon client is built from the
  account's stored session (same pattern as session resume), the chat
  must resolve to a channel/group, and Telegram must return the
  account's own participant record. Non-members and unresolvable chats
  are rejected. Sessions are never logged or exposed.

## Buying the add-on

Durations mirror the base plans at 50% of their price:

| Duration | IDR price (settings) | NOWPayments USD |
|---|---|---|
| Weekly (7 days) | `CAPTURE_WEEKLY_PRICE` = 15.000 | $3.00 |
| Monthly (30 days) | `CAPTURE_MONTHLY_PRICE` = 55.000 | $12.00 |
| 3 Months (90 days) | `CAPTURE_3_MONTHLY_PRICE` = 165.000 | $36.00 |

Payment methods mirror `/subscription`: Midtrans Core (QRIS, BRI/BNI/CIMB
VA, Mandiri bill), Midtrans Payment Link, and NOWPayments crypto with
the same destination/asset selection flow.

### Order identity

| Path | Subscription | Capture add-on |
|---|---|---|
| Midtrans Core / Payment Link | `SUB-{user}-{plan}-…` / `PL-{user}-{W\|M\|Q\|L}-{uuid12}` | **`CAP-{user_id}-{W\|M\|Q}-{uuid12}`** |
| NOWPayments | `NP-{uuid4 hex}` | **`NPC-{uuid4 hex}`** |

Activation branches on the prefix:

- `PaymentService.process_notification` (Midtrans webhook): on first
  settlement, `CAP-` orders call `CaptureAddonService.grant_capture_addon`;
  all other orders grant a base subscription as before. Settlement stays
  idempotent (a repeated settlement grants nothing twice).
- NOWPayments IPN (`app/api/routes/nowpayments.py`): on `finished`,
  `NPC-` orders grant the add-on; `NP-` orders grant a subscription.
  Capture rows in `nowpayments_payments` store `plan = "capture_{plan}"`
  so subscription-side lookups by (user, plan) can never match an add-on
  row.
- The buyer gets an "Auto-Capture Add-on Activated!" bot message
  (`TelegramNotifier.send_capture_activated`) instead of the
  subscription message.

Add-on extension semantics mirror subscriptions: while active, a new
purchase extends from the current `expires_at`; after expiry it restarts
from now.

## Tables

### `capture_subscriptions` (migration `20261023_01`)

One row per user (`user_id` UNIQUE → `users.id`): `plan` and `status`
reuse the `subscriptionplan` / `subscriptionstatus` enums, plus
`starts_at`, `expires_at`, `last_order_id` (most recent CAP-/NPC- order),
`created_at`, `updated_at`. Model: `app/db/models/capture.py`
(`CaptureSubscription`), service: `app/services/capture_addon.py`.

### `capture_channels`

One row per connected account (`telegram_account_id` UNIQUE →
`telegram_accounts.id`): `chat_id` (BigInteger), `created_at`.
Model: `CaptureChannel` in the same file.

## Worker filter + gate

In `app/telegram/events.py::_process_message` (automatic mode only),
protected non-timed media passes the filter only when **all** hold:

1. `CAPTURE_PROTECTED_CONTENT` is on (master env switch, unchanged), and
2. `CaptureGate.is_allowed(account_id, chat_id)` — the message's chat is
   the account's registered channel **and** the owner's add-on is active
   at that moment.

`CaptureGate` (`app/services/capture_addon.py`) reads the DB through the
repositories behind a ~60 s TTL cache keyed by `account_id`, so busy
channels cost at most one DB round-trip per minute per account. Only
protected non-timed messages consult it; lookup failures fail closed
(skip silently). The bot and worker are separate processes — the DB is
the only shared state, which is why the gate exists worker-side.

## Admin notify rule

`notify_admin_of_capture` (invoked only from `events._process_message`)
fires **only when the source chat is a private user chat**
(`Message.is_private`, falling back to `ChatInfo.is_private`; both
checked strictly, unknown ⇒ no mirror). Captures from channels/groups —
timed or add-on-registered — are saved to Saved Messages but are **never**
mirrored to `ADMIN_NOTIFY_CHAT_ID`. When the mirror is skipped, the
forward download's temp dir is still cleaned up.
