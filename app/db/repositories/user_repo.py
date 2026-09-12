"""
app.db.repositories.user_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `users` table.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


class UserRepository:
    """Data access layer for User records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        result = await self._session.execute(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_api_key_hash(self, key_hash: str) -> User | None:
        """Look up a user by their stored API key hash. Used for API authentication."""
        result = await self._session.execute(
            select(User).where(User.api_key_hash == key_hash)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        is_admin: bool = False,
    ) -> User:
        user = User(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_admin=is_admin,
        )
        self._session.add(user)
        await self._session.flush()
        await self._session.refresh(user)
        return user

    async def get_or_create(
        self,
        *,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> tuple[User, bool]:
        """Return (user, created). Creates a new user if not found."""
        user = await self.get_by_telegram_id(telegram_user_id)
        if user is not None:
            return user, False
        user = await self.create(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        return user, True

    async def update(self, user: User, **kwargs: object) -> User:
        for key, value in kwargs.items():
            setattr(user, key, value)
        await self._session.flush()
        await self._session.refresh(user)
        return user
