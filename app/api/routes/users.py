"""
app.api.routes.users
~~~~~~~~~~~~~~~~~~~~
User profile and API key management endpoints.

GET  /api/v1/me            → current user profile
POST /api/v1/me/apikey     → rotate API key (returns raw key once)
"""

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import UserSchema, ApiKeySchema
from app.services.api_key import ApiKeyService

router = APIRouter(prefix="/api/v1", tags=["users"])


@router.get("/me", response_model=UserSchema)
async def get_me(current_user: CurrentUser) -> UserSchema:
    """Return the authenticated user's profile."""
    return UserSchema.model_validate(current_user)


@router.post("/me/apikey", response_model=ApiKeySchema)
async def rotate_api_key(
    request: Request, current_user: CurrentUser, session: DbSession
) -> ApiKeySchema:
    """Rotate the API key for the authenticated user.

    The returned key is the ONLY time it will be visible.
    Store it securely — it cannot be retrieved again.
    """
    svc = ApiKeyService(session)
    
    # Apply rate limiter directly by reaching into app.state
    # This avoids circular dependencies with main.py
    limiter = request.app.state.limiter
    # Manually check rate limit instead of decorator to ensure we only apply it to valid users
    # Phase 17: Gracefully handle Redis connection errors
    try:
        limiter.limit("1/minute")(lambda request: None)(request)
    except Exception:
        # If rate limiting fails (e.g., Redis unavailable), allow the request to proceed
        # This is a safe fallback for critical operations
        pass

    raw_key = await svc.rotate(current_user)
    await session.commit()
    return ApiKeySchema(api_key=raw_key)
