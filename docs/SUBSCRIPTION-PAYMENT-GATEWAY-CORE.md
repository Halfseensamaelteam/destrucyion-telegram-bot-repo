# Subscription & Payment Gateway Core

## Purpose

This document is a protection contract for human developers and AI coding agents working on the subscription and payment system.

The payment flow has been validated in the Midtrans Sandbox environment. Future changes MUST preserve the validated payment, webhook, and subscription behavior unless an explicit change to the core is requested.

> **Rule:** Do not modify validated payment/subscription core logic merely to change prices, add plans, change UI, or add another payment method.

---

## 1. Protected Core

The following areas are considered **PROTECTED**.

### Midtrans configuration

File: `app/services/midtrans.py`

Do not change these validated behaviors without explicit instruction:

- Sandbox uses `is_production=False`.
- The Midtrans Server Key is loaded from application settings/secrets.
- QRIS uses:
  ```python
  "qris": {
      "acquirer": "gopay",
  }
  ```
- QRIS payment expiry is controlled by `custom_expiry`.
- `Payment.expiry_time` represents the **payment/QRIS expiry**, not subscription expiry.
- Order IDs must remain unique.
- Payment amounts must come from the application's subscription-plan pricing configuration.

### Payment persistence

Files:

- `app/services/payment.py`
- `app/db/models/payment.py`
- `app/db/repositories/payment_repo.py`

Preserve:

- Payment creation and persistence.
- `order_id` uniqueness.
- Transaction ID tracking.
- Payment status tracking.
- QR string and payment expiry storage.
- The distinction between payment expiry and subscription expiry.

Do not repurpose payment fields to represent subscription state.

### Midtrans webhook

File: `app/api/routes/midtrans.py`

This is a **highly protected area**.

Do not weaken, remove, or bypass:

- Midtrans signature verification.
- Server-key based signature validation.
- `hmac.compare_digest` verification.
- `order_id` validation.
- Payment existence checks.
- `gross_amount` validation.
- Midtrans transaction-status mapping.
- Payment status updates.
- Settlement handling.
- Duplicate-settlement protection.
- Subscription activation after successful settlement.
- Telegram notification after a newly settled payment.

A webhook must never grant subscription access merely because a client says that payment succeeded.

### Subscription activation and extension

Files:

- `app/services/subscription.py`
- `app/db/models/subscription.py`
- `app/db/repositories/subscription_repo.py`

Preserve these rules:

1. A successful settlement can activate the user's subscription.
2. Weekly = 7 days.
3. Monthly = 30 days.
4. Lifetime = no expiration.
5. If a finite subscription is still active, purchasing another finite plan extends from the existing `expires_at`, not from the current time.
6. If the existing subscription is expired/inactive, a new finite plan starts from the new purchase time.
7. Lifetime remains active without an expiration date.
8. Subscription status and expiration must remain internally consistent.
9. A repeated settlement notification must not grant the same payment twice.

### Time handling

The database may store timezone-aware UTC datetimes.

User-facing subscription expiry is displayed in **WIB (UTC+7)**.

Do not confuse:

- `Payment.expiry_time` → QRIS/payment expiration.
- `Subscription.expires_at` → premium subscription expiration.

---

## 2. Safe Changes

The following changes are normally safe when implemented without changing the protected core.

### Prices

Price changes may modify the subscription pricing configuration, for example:

- Weekly price.
- Monthly price.
- Lifetime price.

Do not modify webhook verification or payment settlement logic just to change a price.

After changing prices, verify that the displayed amount and Midtrans transaction amount remain consistent.

### Adding subscription plans

When adding a new plan:

- Update the plan enum/model.
- Add its price to the pricing configuration.
- Add its duration/expiration rule.
- Add the corresponding UI/button.
- Ensure payment creation supports the plan.
- Ensure subscription display supports the plan.

Do not rewrite existing Weekly, Monthly, or Lifetime behavior unnecessarily.

### Subscription UI

It is safe to modify:

- Button labels.
- Menu layout.
- Subscription descriptions.
- Payment instructions.
- Telegram messages.
- Presentation of plan information.

UI changes must not bypass server-side payment validation.

### Adding payment methods

Additional payment methods may be added around the existing payment architecture.

When adding one:

- Keep the existing QRIS flow working.
- Do not remove QRIS-specific requirements.
- Keep payment records consistent.
- Keep webhook verification and settlement handling secure.
- Reuse the existing payment/subscription service boundaries where possible.

Adding a payment method is **not** a reason to weaken the existing webhook.

---

## 3. Database Rules

Any database model/schema change requires an appropriate Alembic migration.

Never modify production database structure only by changing the SQLAlchemy model.

Before changing payment/subscription fields, check whether the field is already used by:

- payment creation,
- webhook processing,
- subscription activation,
- subscription display,
- notification messages.

---

## 4. AI Coding Agent Rules

AI coding agents MUST follow these rules:

### Rule A — Preserve validated behavior

If a requested feature can be implemented without changing protected core logic, do not change the core.

### Rule B — Minimal changes

Prefer the smallest change that satisfies the requested feature.

Do not refactor unrelated payment/subscription code while implementing a price, plan, UI, or payment-method change.

### Rule C — Do not infer permission

An instruction such as:

- "change the price"
- "add a plan"
- "change the subscription menu"
- "add another payment method"

does **not** imply permission to rewrite:

- webhook verification,
- settlement logic,
- subscription extension logic,
- Midtrans authentication,
- payment persistence.

### Rule D — Explicit core changes

Changes to protected core logic require an explicit request identifying what core behavior should change and why.

Before such a change, inspect the current implementation and its callers.

### Rule E — Test after payment changes

At minimum, validate syntax/imports and the affected behavior.

For payment/webhook changes, test the relevant flow before considering the change complete.

### Rule F — Never expose secrets

Never place the following in source code, documentation, commits, logs, screenshots, or test output:

- Midtrans Server Key.
- Midtrans Client Key when unnecessary.
- Telegram Bot Token.
- Database credentials.
- Redis credentials.
- Webhook secrets.
- Other application credentials.

Use environment variables/application settings.

---

## 5. Change Matrix

| Area | Status | Typical changes |
|---|---|---|
| Midtrans authentication | 🔒 PROTECTED | Only explicit core changes |
| QRIS charge structure | 🔒 PROTECTED | Only explicit core changes |
| Webhook signature verification | 🔒 PROTECTED | Only explicit core changes |
| Settlement/idempotency | 🔒 PROTECTED | Only explicit core changes |
| Payment persistence | 🔒 PROTECTED | Only explicit core changes |
| Subscription extension | 🔒 PROTECTED | Only explicit core changes |
| Payment expiry handling | 🔒 PROTECTED | Only explicit core changes |
| Price configuration | ✅ SAFE | Change prices |
| New subscription plan | ✅ SAFE* | Add plan while preserving existing behavior |
| Subscription UI | ✅ SAFE | Labels/layout/instructions |
| Telegram payment message | ✅ SAFE | Wording/presentation |
| Additional payment method | ⚠️ CONTROLLED | Add without breaking existing payment flow |
| Database schema | ⚠️ CONTROLLED | Requires migration |
| Security/authentication | 🔒 PROTECTED | Explicit review required |

\* Adding a plan may require touching protected interfaces, but existing plan behavior must remain unchanged.

---

## 6. Completion Checklist

Before finishing a subscription/payment change, confirm:

- [ ] Existing payment flow still works.
- [ ] Existing QRIS flow still works.
- [ ] Sandbox/production configuration was not accidentally changed.
- [ ] Webhook signature verification remains intact.
- [ ] Settlement cannot be replayed to grant access twice.
- [ ] Payment amount matches the selected plan price.
- [ ] Payment expiry is not confused with subscription expiry.
- [ ] Active finite subscriptions extend from the existing expiry.
- [ ] Lifetime remains non-expiring.
- [ ] Database changes have migrations.
- [ ] No secrets were added to source/docs/logs.
- [ ] Relevant Python files compile/import successfully.
- [ ] Unrelated protected code was not refactored.

---

## 7. Important Context

The current implementation has been tested through the Midtrans Sandbox flow, including:

- QRIS transaction creation.
- QRIS payment expiry.
- Midtrans settlement notification.
- Webhook signature validation.
- Payment status persistence.
- Subscription activation.
- Subscription extension.
- WIB expiry display.
- Telegram activation notification.

Treat these behaviors as **validated contracts**.

Future feature work should build around these contracts rather than silently changing them.
