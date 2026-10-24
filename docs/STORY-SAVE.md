# /story — Telegram Story Downloads + the /save One-Active Rule

## /story

Stories are downloaded with the requesting user's OWN connected Telegram
account (MTProto). The Bot API has no story download at all, so — exactly
like restricted posts — this cannot work through the bot account.

### Forms (only /story accepts these)

| Command | Meaning |
|---|---|
| `/story https://t.me/<username>/s/<story_id>` | Download that one story |
| `/story https://t.me/<username>` | Download all currently active stories of that peer |

The bare-username form is unambiguous: a bare username carries no
message id, so it can never be a post link. Active stories include
pinned/highlight stories, which persist past the usual 24h life;
duplicates between the active and pinned lists are removed by story id.

### Flow

`/story` mirrors `/save` exactly: connected account + active
subscription gates, one "⏳ Queued…" status message edited in place
(Queued → Working → Done/Failed), and a `LinkRequest` row drained by
the worker with the owner's own Telethon client. The saved copy lands
in that account's Saved Messages with the story caption verbatim
(standing caption policy: original caption only, nothing added).

Stories are downloads, so they lease temp-disk bytes through the same
size-aware disk admission as restricted downloads (unknown sizes count
at the big-file threshold). All-stories requests are capped at
`RESTRICTED_MAX_BATCH` (default 20) and paced like post ranges.

### Honest limits

- **Privacy is enforced by Telegram.** A story is downloadable only if
  the connected account is allowed to VIEW it: public (Everyone)
  stories work for anyone; Contacts / Close Friends stories only work
  if the account is included. There is no way around this.
- **Expired stories are gone.** A story past its 24h life that is not
  pinned as a highlight cannot be downloaded by anyone —
  `GetStoriesByID` simply returns nothing for it, and the request
  fails with "no longer available" instead of silently dying.
- **Peer can be a user or a channel.** Story links carry usernames
  only; a peer without a username has no public story link.
- **Media-less stories are skipped.** Text/poll-only stories have
  nothing to download; if a request contains no downloadable media at
  all, it fails with a clear message.
- **No media_records dedup for stories.** Stories have no message id
  to key the `telegram_account_id + source_chat_id + source_message_id`
  uniqueness on, so story saves upload directly to Saved Messages
  instead of going through the message-based capture path. This matches
  the explicit-save policy (explicit saves always fetch fresh), but it
  means re-requesting the same story saves a second copy.
- **Bot API limitation.** Story download is MTProto-only; nothing
  about this feature is available to plain Bot API bots.

## /save one-active-request rule

While an account has a `/save` request still PENDING or PROCESSING, a
new `/save` (or bare-link message, which routes through the same
handler) is refused with:

> ⏳ You still have a save in progress — please wait until it finishes
> before sending another link.

No queue row and no "Queued" status message are created for the
refused request.

Scope notes:

- The gate covers bot `/save` + bare-link messages only. It does NOT
  gate the worker-side `.get` Saved-Messages trigger or auto-capture —
  those are separate paths.
- The gate does NOT apply to `/story`: story rows are a separate
  command, and the worker's per-account lock still guarantees only one
  download actually runs per account at a time.
- The check is best-effort: two near-simultaneous `/save` commands
  can both pass before either row is committed. The worker's
  per-account lock remains the correctness guarantee (one download per
  account); the gate is a UX/anti-spam guard, not a lock.
