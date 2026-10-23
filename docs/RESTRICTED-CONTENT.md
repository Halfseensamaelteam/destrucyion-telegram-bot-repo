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
| `LINK_REQUEST_DISK_RESERVE_MB` | `3072` | Temp disk that must always stay free; `0` disables size-aware disk admission |
| `LINK_REQUEST_BIG_FILE_MB` | `100` | A request whose total media size exceeds this is big class |
| `LINK_REQUEST_BIG_POOL_PERCENT` | `30` | Big class may lease at most this % of usable disk (while the small class is present) |
| `LINK_REQUEST_BIG_GUARANTEE_PERCENT` | `15` | % of usable fenced off from small leases for the big class (while the big class is present) |
| `LINK_REQUEST_CLASS_IDLE_GRACE_SECONDS` | `30` | A class counts as present this long after its last lease grant; while a class is absent, the other may borrow the whole usable disk |

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

### Adaptive concurrency (memory budget)

A fixed slot count is a blunt instrument: it cannot tell small files
from huge ones. Setting `LINK_REQUEST_MEMORY_BUDGET_MB` (e.g. `3072` on
an 8 GB VPS) together with a high `LINK_REQUEST_MAX_CONCURRENCY`
ceiling switches the processor to adaptive mode. The processor runs a
continuous admission supervisor: slots refill the moment a transfer
finishes, and every tick (≤ the poll interval, and instantly on any
completion) it measures its own RAM (RSS, `app/core/resources.py`) and
adjusts the admission limit AIMD-style — the limit starts AT the cap,
halves within one tick when RAM goes over budget, and regrows by a
quarter per tick when comfortably under 80%. The budget is a brake,
not a ramp: a workload that never threatens RAM runs at exactly
fixed-cap speed (an earlier batch-based design re-evaluated only
between batches and measured 75% slower than the fixed cap on a
600-file run — a batch lives as long as its slowest transfer). The
budget covers the whole worker process, connected clients included.
Default `0` keeps the fixed-cap behaviour. Note the budget guards RAM
only: temp-disk headroom still scales with concurrency × largest file,
so size the ceiling with that in mind.

Setting `LINK_REQUEST_MAX_CONCURRENCY=0` **together with** a budget
selects budget-only mode: no human-set slot cap at all — the AIMD
controller above is the only RAM limiter, and an automatic disk guard
caps each batch at `free temp MB // 300` (a 200 MB-class file × 1.5
reserve per slot, recomputed every cycle), because RAM alone cannot
see the disk filling up. A cap of `0` without a budget falls back to
the fixed default of 10 rather than running unguarded.

### Size-aware disk admission (two-class byte leases)

The slot cap and the RAM budget count REQUESTS and MEMORY — neither
can see the disk. One hundred 10 MB saves and one hundred 900 MB
saves are the same "100 slots", but 1 GB vs 90 GB of temp files (each
transfer holds exactly 1× its file size: the downloader preallocates
the temp file). So before a request downloads anything, it leases its
total size in BYTES (`app/core/disk_lease.py`):

1. The processor fetches the request's messages first (the same
   per-id RPCs, all up front). A message's download size is known
   before any byte moves — `message.file.size` — and only posts that
   will actually download count: timed or protected (noforwards)
   posts. Everything else is a server-side forward and leases 0.
   (A download-path post whose size Telegram does not report is
   accounted at the big-file threshold and treated as big class.)
2. The lease is granted only while the invariant holds:
   `free disk − all active leases − this size ≥ LINK_REQUEST_DISK_RESERVE_MB`
   (default 3072), with free space re-measured at every decision.
   Otherwise the request waits asynchronously; it is never dropped.
3. The lease is released when the request finishes, fails, or is
   cancelled. The slot cap and the RAM brake above still apply —
   leases compose with them, they do not replace them.

Requests split into two classes by TOTAL size
(`LINK_REQUEST_BIG_FILE_MB`, default 100 MB):

- **Small** requests lease against nearly the whole usable disk
  (free − reserve), so a flood of small files drains at full speed
  and finishes first.
- **Big** requests lease against a pool capped at
  `LINK_REQUEST_BIG_POOL_PERCENT` (default 30%) of usable — a flood
  of huge videos can only ever hold that share. They keep moving,
  just fewer at a time, and cannot crowd the small class off the
  disk. One exception: a single big file bigger than the whole pool
  is still admitted on an otherwise idle disk — the cap never drops
  below one file's own size.
- Symmetrically, `LINK_REQUEST_BIG_GUARANTEE_PERCENT` (default 15%)
  of usable is fenced off from small leases, so the big class always
  has room to start even mid-flood. Nobody starves in either
  direction. When space frees, waiting small leases are served before
  waiting big ones; a waiter that still does not fit never blocks the
  other class behind it.

The split is **work-conserving**: both protections bind only while
the class they protect against is *present* — holding leases,
waiting for one, or granted one within
`LINK_REQUEST_CLASS_IDLE_GRACE_SECONDS` (default 30 s; the grace
covers the gaps between claim batches, so a momentary lull mid-flood
does not hand the whole disk over). Once a class is absent, the
other may borrow the *entire* usable disk: when the small flood has
drained, big transfers expand past the pool into the freed space
instead of crawling at the pool rate next to gigabytes of idle disk,
and while no big request is anywhere in sight, small leases ignore
the guarantee fence. Existing leases are never preempted, so a small
request that arrives while big transfers hold borrowed space waits
for natural big completions — leases release per file, and the
first release usually frees enough for one small file.

Edge cases, deliberately: a file larger than the whole usable budget
may run SOLO once nothing else is leased, provided it fits the disk
with 1 GB to spare; a file that cannot fit even an empty disk FAILS
fast with the reason recorded on the row (operator frees space)
instead of waiting forever. Note the corollary: disk admission
assumes the host keeps at least the reserve plus ~1 GB free — on a
disk already fuller than that, download saves fail fast rather than
becoming the thing that fills the disk. Set
`LINK_REQUEST_DISK_RESERVE_MB=0` to disable the whole mechanism
(behaviour is then identical to before it existed).

Worked example — the production shape this was built for: 400 users
saving 10–80 MB videos plus 100 users saving 300–900 MB videos, on a
disk with 22 GB free and the default 3 GB reserve (19 GB usable).
Small leases can use 19 GB minus the 2.9 GB guarantee ≈ 16 GB, so the
small class runs at the full 200 slot cap (≈200 × ≤80 MB) and drains
first. The big pool is 30% × 19 GB ≈ 5.7 GB, so roughly 6–19 big
videos (at 900 MB / 300 MB) transfer at any moment — continuously,
never zero, because the 2.9 GB guarantee is fenced off from the small
flood — while the remaining big requests wait their turn. And once
the small flood has drained (plus the 30 s grace), the pool cap
lifts: the remaining big videos expand into the full 19 GB usable,
so the tail of the big queue runs at full disk instead of staying
parked at ~5.7 GB next to idle space. Free disk
never drops below the 3 GB reserve no matter how the two floods mix.
`scripts/load_test_link_queue.py` reproduces this exact scenario
against a simulated disk (see its `--small-accounts` / `--big-accounts`
flags) and asserts all of the invariants above.

One behavioural note: because the lease needs the request's TOTAL
size, the processor now fetches all of a range's messages before the
first download. A fetch error mid-range therefore aborts the request
before any item is saved (previously the earlier items were already
saved when the error hit; the request was marked FAILED either way,
and fetch errors are normally chat-level, hitting on the first id).

## Implementation map

- `app/telegram/link_parser.py` — pure parser (public / `/c/` / topic /
  `/b/` / range / invite; converts `/c/<id>` to the `-100<id>` peer id).
- `app/services/restricted.py` — fetch-by-id on the owner's client, invite
  join, and `LinkRequestProcessor` (worker-side queue drainer).
- `app/core/disk_lease.py` — size-aware two-class byte leases over the
  temp disk (reserve + big pool + big guarantee), held per request
  between fetch and download.
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
