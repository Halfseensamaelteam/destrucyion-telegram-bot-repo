"""
app.api.routes.telegram
~~~~~~~~~~~~~~~~~~~~~~~~
Telegram account management endpoints.

GET    /api/v1/accounts          → list caller's accounts
DELETE /api/v1/accounts/{id}     → disconnect an account (tenant-safe)
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import AccountSchema
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.account import AccountService, AccountNotFoundError

router = APIRouter(prefix="/api/v1", tags=["accounts"])


@router.get("/accounts", response_model=list[AccountSchema])
async def list_accounts(current_user: CurrentUser, session: DbSession) -> list[AccountSchema]:
    """List all Telegram accounts connected to the authenticated user."""
    repo = TelegramAccountRepository(session)
    accounts = await repo.list_by_user(current_user.id)
    return [AccountSchema.model_validate(a) for a in accounts]


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_account(
    account_id: int, current_user: CurrentUser, session: DbSession
) -> None:
    """Disconnect a Telegram account.

    Tenant isolation: only the owner can disconnect their own account.
    Raises HTTP 404 if the account does not exist or belongs to another user.
    """
    svc = AccountService(session)
    try:
        await svc.disconnect_account(account_id=account_id, user_id=current_user.id)
        await session.commit()
    except AccountNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account {account_id} not found.",
        )
