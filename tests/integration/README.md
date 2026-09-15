# PostgreSQL Integration Tests

This directory contains integration tests that verify enum column behavior against a real PostgreSQL database.

## Purpose

These tests were added to prevent the test/production gap that occurred with enum columns. The unit tests use SQLite, which doesn't have native ENUM type validation, so they didn't catch the bug where SQLAlchemy was sending enum names (uppercase) instead of values (lowercase) to PostgreSQL.

## Running the Tests

### Option 1: Docker PostgreSQL Container

```bash
# Start a PostgreSQL container
docker run --name test-postgres -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb -p 5432:5432 -d postgres:15

# Run the integration tests
DATABASE_URL="postgresql+asyncpg://postgres:testpass@localhost/testdb" uv run pytest tests/integration/test_postgres_enums.py -v

# Clean up
docker stop test-postgres
docker rm test-postgres
```

### Option 2: Neon Test Database

```bash
# Set DATABASE_URL to your Neon test database
export DATABASE_URL="postgresql+asyncpg://user:pass@ep-xxx.aws.neon.tech/neondb?sslmode=require"

# Run the integration tests
uv run pytest tests/integration/test_postgres_enums.py -v
```

### Option 3: Local PostgreSQL

```bash
# Set DATABASE_URL to your local PostgreSQL instance
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost/testdb"

# Run the integration tests
uv run pytest tests/integration/test_postgres_enums.py -v
```

## Test Coverage

The integration tests cover all 5 enum columns:

1. **TelegramAccountStatus** (telegram_account.status)
   - ACTIVE, PAUSED, ERROR, DISCONNECTED

2. **MediaType** (media_record.media_type)
   - PHOTO, VIDEO, DOCUMENT, VOICE, VIDEO_NOTE, STICKER, UNKNOWN

3. **MediaRecordStatus** (media_record.status)
   - PENDING, SAVED, FAILED, SKIPPED

4. **SubscriptionPlan** (subscription.plan)
   - WEEKLY, MONTHLY, LIFETIME

5. **SubscriptionStatus** (subscription.status)
   - ACTIVE, EXPIRED, CANCELLED

Each test verifies:
- Creating records with enum values
- Reading records back from PostgreSQL
- Updating enum values
- That the stored value is the lowercase enum value, not the uppercase name

## Skipping Tests

If `DATABASE_URL` is not set or points to SQLite, these tests are automatically skipped with a message indicating PostgreSQL is required.
