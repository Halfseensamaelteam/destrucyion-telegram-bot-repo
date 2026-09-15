# Production Deployment Guide (Phase 16-18)

This guide explains how to deploy the destrucyion-telegram-bot service to production.

## Architecture Overview

The service consists of two main components:

1. **Vercel (Serverless):** FastAPI + Telegram Bot Webhook
   - HTTP API endpoints
   - Telegram Bot webhook endpoint
   - Request-driven, no persistent connections

2. **Persistent Worker (VPS/Docker):** Telethon Clients
   - Long-running Telegram MTProto connections
   - Media processing and forwarding
   - Must run on persistent infrastructure (VPS, Docker host, etc.)

**CRITICAL REMINDER:** Do NOT deploy the `worker` to Vercel or any serverless platform. Serverless environments will terminate the process prematurely, breaking the persistent Telegram websocket connections.

---

## Part 1: Deploy to Vercel (FastAPI + Bot Webhook)

### Prerequisites

1. Vercel account
2. GitHub repository connected to Vercel
3. PostgreSQL database (Neon, Supabase, or other cloud provider)
4. Redis instance (Upstash, Redis Cloud, or other cloud provider)

### Step 1: Configure Environment Variables in Vercel

In your Vercel project settings, add the following environment variables:

- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection string
- `BOT_TOKEN` - Telegram Bot token from BotFather
- `TELEGRAM_API_ID` - Telegram API ID
- `TELEGRAM_API_HASH` - Telegram API Hash
- `SESSION_ENCRYPTION_KEY` - 32-byte URL-safe base64 key for session encryption
- `APP_SECRET_KEY` - FastAPI secret key
- `APP_ENV` - Set to `production`

### Step 2: Deploy to Vercel

Push your code to GitHub and Vercel will automatically deploy:

```bash
git push origin main
```

### Step 3: Set Up Telegram Bot Webhook

After Vercel deployment, set the webhook URL:

```bash
# Replace YOUR_VERCEL_URL with your actual Vercel domain
curl -F "url=https://YOUR_VERCEL_URL.vercel.app/webhook/telegram" \
  https://api.telegram.org/bot$BOT_TOKEN/setWebhook
```

Verify the webhook is set:

```bash
curl https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo
```

### Step 4: Test the Bot

Send `/start` command to your bot in Telegram. You should receive a response.

---

## Part 2: Deploy Persistent Worker to VPS

### Prerequisites

1. A Linux VPS (e.g., DigitalOcean, Hetzner, AWS EC2).
2. Docker and Docker Compose installed on the VPS.
3. Git installed.

### Step 1: Clone the Repository

SSH into your VPS and clone the repository:

```bash
git clone https://github.com/cahsun147/destrucyion-telegram-bot.git
cd destrucyion-telegram-bot
```

### Step 2: Configure Environment Variables

Copy the example environment file and configure it with your production secrets:

```bash
cp .env.example .env
nano .env
```

Ensure the following variables are set correctly for production:
- `DATABASE_URL` - Same PostgreSQL database as Vercel
- `REDIS_URL` - Same Redis instance as Vercel
- `BOT_TOKEN` - Same bot token as Vercel
- `TELEGRAM_API_ID` - Same API ID as Vercel
- `TELEGRAM_API_HASH` - Same API Hash as Vercel
- `SESSION_ENCRYPTION_KEY` - Same encryption key as Vercel

### Step 3: Start the Persistent Services

We will use the provided `docker-compose.yml` to launch the required infrastructure (Postgres, Redis) and the persistent worker.

*(Note: Since FastAPI and Bot Webhook are on Vercel, you only need to run the worker on your VPS. If you want to run Postgres/Redis locally on the VPS instead of using cloud providers, include those services.)*

Start the services in detached mode:

```bash
# If using cloud Postgres/Redis (recommended)
docker compose up -d worker

# If running Postgres/Redis locally on VPS
docker compose up -d postgres redis worker
```

### Step 4: Verification

Verify that the worker has started successfully and acquired the necessary Redis locks.

1. **Check Worker Logs:**
   ```bash
   docker compose logs -f worker
   ```
   You should see logs indicating a successful connection to Redis and the database, followed by account synchronization. Look for logs like:
   - `account_lock_acquired`
   - `client_started`

2. **Verify Isolation:**
   Connect two Telegram accounts through the bot. Monitor the worker logs to ensure both maintain active sessions simultaneously without crossing state or dropping connections.

3. **Verify Graceful Shutdown:**
   Restart the worker container to test shutdown behavior:
   ```bash
   docker compose restart worker
   ```
   Watch the logs to confirm it cleanly releases the Redis locks (`account_lock_released`) before exiting.

---

## Maintenance

### Update Vercel Deployment

Push new code to GitHub, Vercel will automatically deploy:

```bash
git push origin main
```

### Update Worker

To update the worker with new code:

```bash
git pull origin main
docker compose build worker
docker compose up -d worker
```

### Database Migrations

Run Alembic migrations on your PostgreSQL database:

```bash
# From the VPS or local machine with DATABASE_URL set
alembic upgrade head
```

---

## Local Development

For local development, you can run the bot in long polling mode:

```bash
python -m app.bot.main
```

This is only for development - production uses webhook mode on Vercel.
