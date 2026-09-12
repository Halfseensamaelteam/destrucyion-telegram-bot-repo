"""
app.api.routes.admin
~~~~~~~~~~~~~~~~~~~~~
Admin-only endpoints. Requires is_admin=True.

GET  /api/v1/admin/users                          → list all users
POST /api/v1/admin/users/{user_id}/grant-sub      → grant a subscription manually
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import AdminUser, DbSession
from app.api.schemas import AdminUserSchema, SubscriptionSchema
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus, Subscription
from app.db.repositories.user_repo import UserRepository
from app.db.repositories.subscription_repo import SubscriptionRepository
from sqlalchemy import select
from app.db.models.user import User

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserSchema])
async def list_all_users(
    admin: AdminUser, session: DbSession
) -> list[AdminUserSchema]:
    """List all application users. Admin only."""
    result = await session.execute(select(User).order_by(User.created_at.desc()).limit(200))
    users = list(result.scalars().all())
    return [AdminUserSchema.model_validate(u) for u in users]


class GrantSubscriptionRequest(BaseModel):
    plan: SubscriptionPlan = SubscriptionPlan.MONTHLY
    days: int = 30


@router.post("/users/{user_id}/grant-sub", response_model=SubscriptionSchema)
async def grant_subscription(
    user_id: int,
    body: GrantSubscriptionRequest,
    admin: AdminUser,
    session: DbSession,
) -> SubscriptionSchema:
    """Manually grant a subscription to a user. Admin only."""
    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found.")

    sub_repo = SubscriptionRepository(session)
    existing = await sub_repo.get_by_user_id(user_id)

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=body.days)

    if existing:
        existing.plan = body.plan
        existing.status = SubscriptionStatus.ACTIVE
        existing.starts_at = now
        existing.expires_at = expires
        await session.flush()
        sub = existing
    else:
        sub = Subscription(
            user_id=user_id,
            plan=body.plan,
            status=SubscriptionStatus.ACTIVE,
            starts_at=now,
            expires_at=expires,
        )
        session.add(sub)
        await session.flush()

    await session.commit()
    await session.refresh(sub)

    return SubscriptionSchema(
        id=sub.id,
        tier=sub.plan.value,
        status=sub.status.value,
        expires_at=sub.expires_at,
        created_at=sub.created_at,
    )
