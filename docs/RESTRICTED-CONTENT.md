# Restricted Content Saving (private / "Restrict saving content" channels)

Save photos, videos, documents and text posts from chats that have
**Restrict saving content** enabled — including private channels — into the
connected account's own Saved Messages.

## Why forwarding never works, and what this does instead

A protected chat marks its messages `noforwards`. Telegram's servers refuse
to forward or copy such messages for **every** client (Bot API and MTProto:
`ChatForwardsRestrictedError`). The restriction is on *forwarding*, not on
*downloading*: an account that is a **member** of the chat can download the
media bytes over MTProto like any other media. So saving is always:

```
get_messages(peer, id) → download_media (bytes) → send_file (new upload)
```

The new upload is a brand-new, unprotected file in Saved Messages. This is
the same path `SavedMessagesService` already used for timed media, and the
same principle the single-account reference bots (e.g. Save-Restricted-
Content) use — adapted here to this project's multi-tenant architecture:

| Aspect | Reference bots | destrucyion |
|---|---|---|
| Telegram access | One shared string session, or a Pyrogram login per user stored in MongoDB | The user's **own** Telethon account, already connected via `/connect` (encrypted session, per-account api_id/api_hash) |
| Delivery | Bot API `send_photo/send_video` back to the chat — Bot API upload limit applies (~50 MB) | MTProto upload by the user's own client into their **Saved Messages** — no Bot API upload limit |
| Where a link is processed | Inside the bot process (bot + userbot in one process) | Bot writes a `LinkRequest` row → worker drains it (`PostgreSQL is authoritative`, AGENTS.md rule 6) |
| Duplicates | None / in-memory flags | Automatic capture dedupes via the DB UNIQUE key `(account, chat, message)`; an explicit link request always delivers a fresh copy (owner's choice — no check stands in the way) |
| Captions | Original caption only | Original caption only — the copy carries the post's own description verbatim, no sender/chat/timestamp metadata block |
| Extra modes | Link only | Link + automatic capture of protected posts on arrival + reply `.saveit` (existing) |

## Three ways to use it

### 1. Send the link to the bot

```
/save https://t.me/c/1234567/890          private / restricted post
/save https://t.me/channel/123            public post
/save https://t.me/c/1234567/100-110      range (capped, see settings)
/save https://t.me/+AbCdEfGh             join a private chat first
```

Sending a bare `t.me` link as a plain message works the same. The bot
replies "Queued"; the worker fetches it (usually within ~3 s) and the copy
appears in **your Saved Messages**, with a confirmation back in the bot chat.

### 2. From your own Saved Messages (no bot round-trip)

Open your own Saved Messages in any Telegram client and send:

```
.get https://t.me/c/1234567/890
.get https://t.me/c/1234567/100-110
.get https://t.me/+AbCdEfGh
```

The command message deletes itself; progress and the summary are posted to
Saved Messages. The command is only honoured inside Saved Messages, never
in a chat with another person.

### 3. Automatically, on arrival

With `CAPTURE_PROTECTED_CONTENT=true` (default), any incoming protected
post is saved when it arrives even if `CAPTURE_ONLY_TIMED=true` — like
timed media, a protected post cannot be retrieved by forwarding later, so
arrival is the only reliable moment.

## Requirements & honest limits

- **Membership is mandatory for private chats.** A `/c/` link cannot even
  be resolved by a non-member. Join first (invite link above); there is no
  technical way around this and none is attempted.
- The connected account must be running in the worker (active subscription).
- Albums are saved per message id; a link to one item saves that item.
- Text-only protected posts are re-sent as a new text message in Saved
  Messages.
- Ranges are capped by `RESTRICTED_MAX_BATCH` (default 20) and paced by
  `RESTRICTED_BATCH_DELAY_SECONDS` (default 2 s); FloodWait is retried by
  the existing Saved Messages retry logic.
- Only ever save content you have the right to keep. This feature uses the
  account's own legitimate member access; it does not bypass membership,
  encryption, or any access control — only the forward/copy restriction.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `CAPTURE_PROTECTED_CONTENT` | `true` | Auto-save protected posts on arrival |
| `RESTRICTED_TRIGGER` | `.get` | Saved-Messages link command (empty disables) |
| `RESTRICTED_MAX_BATCH` | `20` | Max posts per link/range request |
| `RESTRICTED_BATCH_DELAY_SECONDS` | `2` | Pause between range items |
| `LINK_REQUEST_POLL_SECONDS` | `3` | Worker queue poll interval |
| `LINK_REQUEST_MAX_CONCURRENCY` | `10` | Link requests processed at once (max 1 per account) |
| `LINK_REQUEST_STALE_SECONDS` | `600` | A claimed request older than this is requeued as abandoned |

## Concurrency & multiple workers

The processor claims each batch atomically — one
`UPDATE … WHERE status = PENDING … RETURNING` flips rows to PROCESSING —
so several worker processes/containers can drain the same queue without
ever receiving the same request twice (verified on PostgreSQL with two
concurrent claimers). Within a batch, requests run concurrently up to
`LINK_REQUEST_MAX_CONCURRENCY`, with at most one in-flight request per
account. A worker that dies mid-request leaves its claims behind; once a
claim is older than `LINK_REQUEST_STALE_SECONDS` it is requeued
automatically (a live worker's in-flight requests are never stolen,
because only claims older than the threshold are touched).

## Implementation map

- `app/telegram/link_parser.py` — pure parser (public / `/c/` / topic /
  `/b/` / range / invite; converts `/c/<id>` to the `-100<id>` peer id).
- `app/services/restricted.py` — fetch-by-id on the owner's client, invite
  join, and `LinkRequestProcessor` (worker-side queue drainer).
- `app/telegram/events.py` — `.get` Saved-Messages handler + protected
  auto-capture; saving itself reuses `_process_message` (capture record,
  idempotency, admin notify).
- `app/services/saved_messages.py` — protected media now goes straight to
  download + re-upload instead of a doomed forward attempt first.
- `app/bot/handlers/restricted.py` — `/save` + bare-link intake (queue only).
- `app/db/models/link_request.py`, `app/db/repositories/link_request_repo.py`,
  migration `20261021_01` — the bot→worker queue table.

## Phase report

```
PHASE: Restricted content saving (post-Phase 18 feature)
STATUS: PASS WITH WARNINGS

Implemented:
- Link parser for public/private/topic/bot/range/invite links
- Download+re-upload for protected (noforwards) media, incl. auto-capture
- .get trigger in Saved Messages; invite join via the owner's client
- /save bot command + bare-link intake backed by link_requests queue
- Worker LinkRequestProcessor; DB migration 20261021_01

Tests:
- Unit: link parser (incl. malformed links), restricted service fetch /
  join error mapping, processor queue contract (SQLite), events handler
  registration + protected auto-capture filter

Warnings:
- Live verification against a real protected channel requires a deployed
  worker with a connected member account; it cannot be done in unit tests.
  Run one manual trial after `alembic upgrade head` on the VPS:
  /save a /c/ link from a restrict-enabled channel the account has joined,
  and confirm the copy lands in Saved Messages.
```
