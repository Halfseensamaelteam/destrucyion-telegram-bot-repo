import asyncio
from sqlalchemy import inspect
from app.db.session import _get_engine

async def main():
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda connection: inspect(connection).get_table_names()
            )
        print(
            "nowpayments_payments exists:",
            "nowpayments_payments" in tables,
        )
    finally:
        await engine.dispose()

asyncio.run(main())
