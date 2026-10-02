# Midtrans Payment Integration

## Scope

This document describes the operational payment flow currently used by
`destrucyion-telegram-bot`. The validated flow uses Midtrans QRIS in Sandbox
during development.

## QRIS Configuration

The Sandbox client must be initialized with:

```python
midtransclient.CoreApi(
    is_production=False,
    server_key=server_key,
)
```

QRIS requests use:

```python
"payment_type": "qris",
"qris": {
    "acquirer": "gopay",
},
```

Current QRIS payment expiry is configured separately from subscription
expiry.

## Payment Record

A payment records the Midtrans transaction and the selected subscription plan.

Important fields include:

- `order_id`
- `transaction_id`
- `plan`
- `amount`
- `currency`
- `status`
- `qr_string`
- `expiry_time`

`Payment.expiry_time` means the QRIS/payment deadline only.

It is NOT the premium subscription expiration.

## Webhook

The Midtrans notification endpoint must verify the notification before changing
payment or subscription state.

Required protections:

1. Build the signature input from `order_id`, `status_code`,
   `gross_amount`, and the configured Server Key.
2. Hash with SHA-512.
3. Compare using `hmac.compare_digest`.
4. Validate the referenced payment exists.
5. Validate the notification amount against the persisted payment amount.
6. Map the Midtrans transaction status.
7. Persist the payment status.
8. On a newly observed settlement, grant the subscription.
9. Do not grant the same payment again on repeated settlement notifications.

A client-side success message is never an authorization to activate premium
access.

## Subscription Effects

After a verified settlement:

- Weekly grants 7 days.
- Monthly grants 30 days.
- Lifetime has no expiration.
- An active finite subscription extends from its existing
  `Subscription.expires_at`.
- An expired/inactive finite subscription starts from the new purchase time.
- Lifetime remains non-expiring.

The payment deadline and subscription deadline are intentionally separate.

## Telegram Flow

The bot currently:

1. Shows subscription plans.
2. Creates the QRIS payment.
3. Persists the payment.
4. Renders the QR code.
5. Shows the payment amount, order ID, status, and QRIS expiry.
6. Waits for the verified Midtrans settlement webhook.
7. Activates or extends the subscription.
8. Sends the activation notification.

## Testing

The current regression tests cover:

- Sandbox QRIS configuration.
- QRIS request structure.
- QRIS expiry parsing.
- Settlement activation.
- Duplicate settlement idempotency.
- Valid and invalid webhook signatures.
- Missing payment handling.
- Payment amount mismatch.

A real Midtrans Sandbox Telegram purchase has also been exercised successfully
through settlement and subscription notification.

## Change Control

Changing a price, plan, button, UI message, or adding another payment method
does not automatically authorize changes to:

- Midtrans authentication.
- QRIS request structure.
- webhook signature verification.
- payment persistence.
- settlement/idempotency.
- subscription extension rules.

For those core changes, explicitly request the core change and run a new
regression and Sandbox verification checkpoint.

## Secrets

Never place Server Keys, Client Keys when unnecessary, Bot Tokens, database
credentials, Redis credentials, webhook secrets, encryption keys, or Telegram
session strings in source code, documentation, commits, logs, screenshots, or
test output.
