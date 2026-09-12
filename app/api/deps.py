"""
app.api.deps
~~~~~~~~~~~~
FastAPI dependency functions for the REST API.

Provides:
- get_session()     → yields AsyncSession per request.
- get_current_user() → authenticates via X-API-Key header, returns User.
- require_admin()   → enforces admin flag.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.session import get_session
from app.services.api_key import hash_api_key
from app.db.repositories.user_repo import UserRepository


async def get_current_user(
    x_api_key: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Authenticate request by hashing the X-API-Key header and looking up the user.

    Raises:
        HTTP 401 if header is missing or key is invalid.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
        )

    key_hash = hash_api_key(x_api_key)
    repo = UserRepository(session)
    user = await repo.get_by_api_key_hash(key_hash)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return user


async def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Dependency that requires the authenticated user to have is_admin=True.

    Raises:
        HTTP 403 if the user is not an admin.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required.",
        )
    return current_user


# Convenient type aliases for use in route signatures
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
