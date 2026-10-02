# Phase 0 — Initial Audit Report

**Project:** destrucyion-telegram-bot  
**Audited reference:** `reference/saveit-original/`  
**Audit date:** 2026-09-10  
**Auditor:** Antigravity AI  

---

## 1. Files Audited

| File | Size | Purpose |
|------|------|---------|
| `Saveit.py` | 3 181 bytes | Main application — single Python script |
| `.env.example` | 59 bytes | Environment variable template |
| `run.bat` | 1 004 bytes | Windows launcher script |
| `run.sh` | 1 200 bytes | Linux/macOS/Termux launcher script |
| `README.md` | 3 295 bytes | User documentation |
| `LICENSE` | 1 085 bytes | MIT License |

---

## 2. Saveit.py — Detailed Analysis

### 2.1 Entry Point and Process Model

```
asyncio.run(main())
    └── TelegramClient("save", api_id, api_hash)
            └── client.run_until_disconnected()
```

- **Single process**, single file, single function scope.
- **Single Telethon client** named `"save"` (produces a `save.session` file).
- The process runs as a **blocking foreground process** until manually killed or disconnected.
- No background task management, no worker abstraction, no restart logic.

### 2.2 Configuration

Loaded via `python-dotenv` from a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `API_ID` | — (required) | Telegram MTProto App API ID |
| `API_HASH` | — (required) | Telegram MTProto App API hash |
| `HANDLER` | `.saveit` | Command trigger string for manual saves |
| `AUTO_SAVE_TIMED` | `true` | Toggle automatic timed-media capture |

> **Note:** `AUTO_SAVE_TIMED` appears in the README and is read in the code, but is **absent from `.env.example`** — a documentation inconsistency.

### 2.3 Session Handling

```python
client = TelegramClient("save", api_id, api_hash)
```

- Telethon creates a **local file session** named `save.session` in the working directory.
- **Single session = single Telegram user account.**
- The session file is a **plain, unencrypted SQLite database** on disk.
- No `.gitignore` — session file could be accidentally committed.
- No session encryption at rest.

### 2.4 Global State

```python
client = TelegramClient(...)       # global singleton
downloads_path = Path("downloads") # global path
saved_message_ids = set()          # global in-memory deduplication
save_lock = asyncio.Lock()         # global asyncio lock
your_user_id = None                # global, set in main()
```

- **`saved_message_ids`** is an in-memory `set` — lost on process restart.
- **`save_lock`** is in-memory — no cross-process protection.
- **`your_user_id`** is set once at startup by `client.get_me()`.
- No database, no persistence layer, no Redis.

### 2.5 Media Saving Pipeline

```
Incoming Telegram event
        │
        ├─── [AUTO] is_timed_media(message)?
        │         └── save_media(message, sender_id)
        │
        └─── [MANUAL] message text == HANDLER?
                  ├── sender_id == your_user_id? (owner-only guard)
                  ├── is reply? (get reply message)
                  └── save_media(reply_message, sender_id)
```

`save_media(message, sender_id)`:

1. Builds `message_key = (chat_id, message_id)`.
2. Acquires `save_lock`.
3. Checks `saved_message_ids` — returns early if already seen.
4. Adds key to `saved_message_ids`.
5. **Downloads media to local `downloads/` folder** via `client.download_media()`.
6. **Sends the local file** to `"me"` (Saved Messages) via `client.send_file()` with `force_document=True`.
7. On error: removes key from `saved_message_ids` and re-raises.

> **Critical:** The pipeline goes through disk. Media is first written to `downloads/` locally, then re-uploaded to Saved Messages.

### 2.6 Timed / Self-Destructing Media Detection

```python
def is_timed_media(message):
    return bool(
        message
        and message.media
        and getattr(message.media, "ttl_seconds", None)
    )
```

- Detection relies on `message.media.ttl_seconds` being non-None and truthy.
- Defensive `getattr(..., None)` — does not crash on missing attribute.
- No restriction by media type — any media with `ttl_seconds` set will be captured.
- **Whether Telegram always exposes `ttl_seconds` for every self-destructing message is not proven** (see §7 Limitations).

---

## 3. CRITICAL: Trigger Mode — Manual vs. Automatic

**There are TWO distinct operation modes. This is the most important design point for future phases.**

### Mode A — Automatic (timed/self-destructing media ONLY)

- Enabled by default (`AUTO_SAVE_TIMED=true`).
- Fires on every **incoming** `NewMessage` event automatically.
- **No user command required.**
- Captures media **only if** `message.media.ttl_seconds` is set.
- **Regular, non-timed media is NOT captured automatically.**

### Mode B — Manual (any media, owner-triggered)

- Triggered by the account owner typing the HANDLER string (default `.saveit`) **as a reply** to a target message.
- The command is only honored if `event.sender_id == your_user_id` (owner-only guard).
- Works for **any media** — timed or not — as long as the message has media.

> **⚠️ Design implication for all future phases:**  
> "Automatic" saving applies to timed/self-destructing media only.  
> Non-timed/regular media requires an explicit user action (e.g., `/save` bot command or reply trigger).  
> **Do not design a system that claims to save all incoming media automatically.**  
> This distinction must be preserved in Phase 7 (Media Capture) and Phase 11 (Telegram Bot).

---

## 4. .env.example — Analysis

```
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
HANDLER=.saveit
```

- Only three variables.
- **Missing `AUTO_SAVE_TIMED`** despite the code supporting it.
- No inline comments or documentation.

---

## 5. run.bat — Analysis

- Checks Python and pip are installed.
- Creates `.env` from `.env.example` interactively if absent.
- Installs `telethon` and `python-dotenv` **globally** via `pip install --upgrade`.
- Runs `python Saveit.py`.
- **No virtual environment** — pollutes system Python.
- **No version pinning** — always fetches latest, may break.
- Windows-only.

---

## 6. run.sh — Analysis

- Same flow as `run.bat` for Linux/macOS/Termux.
- Additionally runs `git stash / git pull / git stash pop` before install — **auto-updates from remote**.
- `auto-git-pull` is **unsafe in any managed deployment** — upstream changes could break a running instance without notice.
- Uses `python3` (Linux convention).
- **No virtual environment. No version pinning.**
- macOS/Linux/Termux compatible (handles `sed -i ''` vs `sed -i` difference).

---

## 7. Architecture Summary (Original)

```
┌────────────────────────────────────────────────────┐
│                  Saveit.py (single process)         │
│                                                    │
│  TelegramClient("save", api_id, api_hash)          │
│     │                                              │
│     ├── Event: NewMessage (incoming=True)          │
│     │       └── auto_save_timed_media()            │
│     │               └── if ttl_seconds → save      │
│     │                                              │
│     └── Event: NewMessage (pattern=HANDLER)        │
│             └── download_with_handler()            │
│                     └── if owner reply → save      │
│                                                    │
│  save_media():                                     │
│     download_media() → downloads/ (disk)           │
│     send_file("me", file_path, force_document)     │
│                                                    │
│  State: in-memory set, asyncio.Lock                │
│  Session: save.session (SQLite, unencrypted)       │
│  Output: downloads/ folder + Saved Messages        │
└────────────────────────────────────────────────────┘
```

**Single-user. Single-account. No database. No API. No subscriptions. No multi-tenancy.**

---

## 8. Known Limitations and Gaps

| # | Limitation | Impact on New System |
|---|-----------|---------------------|
| L1 | **Single Telegram account** | One isolated client per account required (CLAUDE.md §9) |
| L2 | **Single application user** — no user management | Multi-tenant user model required |
| L3 | **No subscriptions** — no access control | Subscription system required (Phase 4) |
| L4 | **In-memory deduplication (`set()`)** — lost on restart | DB unique constraint required (CLAUDE.md §17) |
| L5 | **No session encryption** — plain SQLite on disk | Encrypt sessions at rest (CLAUDE.md §13) |
| L6 | **Disk-based relay** — download → disk → re-upload | Consider streaming or in-memory approach in Phase 7 |
| L7 | **No structured logging** — `print()` only | Structured logging required (CLAUDE.md §22) |
| L8 | **Minimal caption** — only `sender_id` | Full metadata caption required (CLAUDE.md §16) |
| L9 | **No error persistence** — failures lost on restart | `media_records` DB table with status/error (Phase 3) |
| L10 | **No Telegram Bot** — user-account only | Bot API layer required (Phase 11) |
| L11 | **No REST API** | FastAPI layer required (Phase 12) |
| L12 | **No Docker / deployment abstraction** | Docker Compose required (Phase 14) |
| L13 | **`AUTO_SAVE_TIMED` missing from `.env.example`** | Fix in Phase 1 new `.env.example` |
| L14 | **`run.sh` auto-git-pulls** — unsafe in production | Explicit deployment strategy in Phase 14–16 |
| L15 | **No pinned dependency versions** | `pyproject.toml` with pinned deps required (Phase 1) |
| L16 | **`ttl_seconds` availability not guaranteed** | Document and handle TTL-missing cases (CLAUDE.md §29) |
| L17 | **No `.gitignore`** — session may be committed | Phase 1 must create `.gitignore` |

---

## 9. Behaviors to Preserve or Adapt

| Behavior | Decision |
|----------|----------|
| Auto-capture timed/self-destructing media on arrival | **Preserve** — core feature (Phase 7) |
| Manual save via reply trigger | **Adapt** → `/save` bot command (Phase 11) |
| Send to Saved Messages (`"me"`) via same account | **Preserve** — same account boundary rule (Phase 9) |
| `force_document=True` to avoid re-compression | **Preserve** — maintain original quality |
| `getattr(message.media, "ttl_seconds", None)` detection | **Preserve as starting point**, verify in Phase 7 |
| Single Telethon session per account | **Preserve** — extend to multiple isolated accounts |

---

## 10. Original Dependencies

| Package | Version Pinned? | Purpose |
|---------|----------------|---------|
| `telethon` | No (`--upgrade`) | Telegram MTProto client |
| `python-dotenv` | No (`--upgrade`) | `.env` file loading |

**Python version constraint:** 3.9+ (from run script message only; no formal constraint file)

---

## 11. Explicit Non-Features of the Original

The original repository does **not** have:

- ❌ Multi-user support  
- ❌ Multi-account support  
- ❌ Telegram Bot interface  
- ❌ Subscriptions or access control  
- ❌ REST API  
- ❌ Database  
- ❌ Docker support  
- ❌ Encrypted sessions  
- ❌ Vercel deployment  
- ❌ Persistent worker abstraction  
- ❌ Tests  
- ❌ Structured logging  
- ❌ Guaranteed capture of every self-destructing message  

---

## 12. Verification Notes

- The original script was not executed in this phase. Phase 0 is documentation-only per ROADMAP.md.
- First run requires interactive Telegram login (phone → code → optional 2FA). Subsequent runs reuse `save.session`.
- To run manually: `pip install telethon python-dotenv && python Saveit.py`

---

## 13. Summary for Phase 1

Phase 0 confirms:

1. The original `Saveit` is a working single-user proof-of-concept.
2. Core Telegram patterns (timed-media detection via `ttl_seconds`, `send_file("me", ..., force_document=True)`) are valid references.
3. **Critical trigger model:** timed/self-destructing media → automatic; regular media → explicit owner command. This distinction must be preserved exactly.
4. The entire application state, session management, user management, and deployment model must be redesigned from scratch for the multi-tenant `destrucyion-telegram-bot`.
5. No code from the original can be used as-is in production without addressing all limitations in §8.


## 14. Later Implementation Checkpoint

The report above remains the historical Phase 0 audit of the original Saveit
reference. The original application did not contain the current payment and
subscription architecture.

The rebuilt project now has a validated Midtrans Sandbox payment flow with
QRIS, persistent payment records, secure webhook validation, settlement-driven
subscription activation, subscription extension, and Telegram notification.

The payment/subscription protection contract is documented in
`docs/SUBSCRIPTION-PAYMENT-GATEWAY-CORE.md`.
