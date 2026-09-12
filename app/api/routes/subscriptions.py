"""
app.api.routes.subscriptions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Subscription endpoints.

GET /api/v1/subscription → current user's subscription
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import SubscriptionSchema
from app.db.repositories.subscription_repo import SubscriptionRepository

router = APIRouter(prefix="/api/v1", tags=["subscription"])


@router.get("/subscription", response_model=SubscriptionSchema)
async def get_subscription(current_user: CurrentUser, session: DbSession) -> SubscriptionSchema:
    """Return the authenticated user's current subscription.

    Raises HTTP 404 if the user has no active subscription record.
    """
    repo = SubscriptionRepository(session)
    sub = await repo.get_by_user_id(current_user.id)

    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No subscription found.",
        )

    return SubscriptionSchema(
        id=sub.id,
        tier=sub.plan.value,
        status=sub.status.value,
        expires_at=sub.expires_at,
        created_at=sub.created_at,
    )
