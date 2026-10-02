# Termux Development Guide

> **Root Cause of Slowness:** Python 3.14 (which Termux ships) has **no pre-built ARM64 wheels** on PyPI for packages like `pydantic-core`, `cryptography`, and `watchfiles`. So any tool that tries to install them will trigger a slow Rust/C compilation (30–60 min). The instructions below avoid this entirely.

---

## Prerequisites

Install [Termux](https://f-droid.org/en/packages/com.termux/) from **F-Droid** (not the Play Store version — it is outdated).

```bash
pkg update && pkg upgrade -y
```

---

## 1. Install System Dependencies via `pkg`

```bash
# Core tools
pkg install -y python git openssl libffi

# Pre-compiled Python packages (avoids Rust/C compilation entirely)
pkg install -y python-cryptography

# uv package manager
pkg install -y uv
```

> `pkg install python-pydantic` does not exist in Termux — that's OK, we'll handle pydantic differently below.

---

## 2. Clone Repository

```bash
git clone https://github.com/cahsun147/destrucyion-telegram-bot.git
cd destrucyion-telegram-bot
```

---

## 3. Setup Environment Variables

```bash
cp .env.example .env
```

Generate a session encryption key:
```bash
python scripts/generate_key.py
```

Open `.env` and fill in your values:
```bash
nano .env
```

Key fields to fill in:
- `SESSION_ENCRYPTION_KEY` — paste the key generated above
- `API_ID`, `API_HASH` — from https://my.telegram.org
- `BOT_TOKEN` — from @BotFather
- `DATABASE_URL` — use SQLite for local dev (see note below)

> **SQLite for local dev (recommended on Termux):**
> ```
> DATABASE_URL=sqlite+aiosqlite:///./dev.db
> ```
> No Postgres setup needed this way.

---

## 4. Create Virtual Environment & Install Dependencies

Use Python's built-in venv with `--system-site-packages` so the venv can see Termux's pre-compiled `cryptography`:

```bash
# Create venv
python -m venv --system-site-packages .venv
source .venv/bin/activate

# Install packages — --prefer-binary finds compatible manylinux wheels
# installs sqlite extra for aiosqlite (SQLite async driver)
pip install --prefer-binary \
    fastapi \
    uvicorn \
    telethon \
    pydantic \
    "pydantic-settings" \
    sqlalchemy \
    alembic \
    aiosqlite \
    python-dotenv \
    structlog \
    httpx \
    "python-telegram-bot"
```

> **Why not `uv sync`?** `uv` on Python 3.14/ARM64 builds everything from source. `pip install --prefer-binary` actively picks compatible pre-built manylinux wheels where available.

---

## 5. Verify Installation

```bash
python -c "import cryptography, pydantic, fastapi, telethon, sqlalchemy; print('All OK')"
```

---

## 6. Run the Application

You need two Termux sessions (or install `tmux`: `pkg install tmux`):

**Terminal 1 — FastAPI App:**
```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — Worker Supervisor:**
```bash
source .venv/bin/activate
python -m worker.main
```

---

## Tips

| Problem | Solution |
|---|---|
| Android kills background worker | Run `termux-wake-lock` before starting worker |
| `uv sync` compiling for ages | Use `pip install --prefer-binary` as above instead |
| `pkg install python-pydantic` not found | Normal — install via pip as shown above |
| Want multiple terminals without a second session | `pkg install tmux` then use `tmux new-session` |


## Payment Sandbox Regression

The payment core can be tested without an external payment call:

```bash
python -m pytest tests/unit/services/test_midtrans.py tests/unit/services/test_payment.py tests/unit/api/test_midtrans.py -q
```

For a real Sandbox trial, run the application and Telegram bot normally and
complete a subscription purchase. The public Midtrans notification endpoint
must be reachable for webhook delivery.

Keep all Midtrans, Telegram, database, Redis, and encryption credentials only
in the local environment configuration.
