# Production Deployment Guide (Phase 16)

This guide explains how to deploy the persistent Telethon worker to a standard Linux VPS (Ubuntu/Debian) using Docker Compose.

**CRITICAL REMINDER:** Do NOT deploy the `worker` to Vercel or any serverless platform. Serverless environments will terminate the process prematurely, breaking the persistent Telegram websocket connections.

## Prerequisites

1. A Linux VPS (e.g., DigitalOcean, Hetzner, AWS EC2).
2. Docker and Docker Compose installed on the VPS.
3. Git installed.

## Step 1: Clone the Repository

SSH into your VPS and clone the repository:

```bash
git clone https://github.com/cahsun147/destrucyion-telegram-bot.git
cd destrucyion-telegram-bot
```

## Step 2: Configure Environment Variables

Copy the example environment file and configure it with your production secrets:

```bash
cp .env.example .env
nano .env
```

Ensure the following variables are set correctly for production:
- `DATABASE_URL`
- `REDIS_URL`
- `BOT_TOKEN`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `SESSION_ENCRYPTION_KEY`

## Step 3: Start the Persistent Services

We will use the provided `docker-compose.yml` to launch the required infrastructure (Postgres, Redis) and the persistent worker.

*(Note: If you have deployed the FastAPI app to Vercel, you do not need to run the `api` service on your VPS. You can choose to run just the database, redis, and worker.)*

Start the services in detached mode:

```bash
# Run everything (Database, Redis, Worker, and API)
docker compose up -d

# OR, if you only want to run the background services and worker (API is on Vercel)
docker compose up -d postgres redis worker
```

## Step 4: Verification

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

## Maintenance

To update the worker with new code:

```bash
git pull origin main
docker compose build worker
docker compose up -d worker
```
