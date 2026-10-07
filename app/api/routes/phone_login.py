"""Browser API endpoints for the secure Telegram phone-login flow."""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.db.session import _get_session_factory
from app.db.repositories.user_repo import UserRepository
from app.services.account import AccountAlreadyExistsError, AccountService, AuthError
from app.services.phone_login import PhoneLoginCoordinator
from app.web.phone_login import PHONE_LOGIN_HTML

router = APIRouter(prefix="/api/v1/phone-login", tags=["phone-login"])
coordinator = PhoneLoginCoordinator()


class StartRequest(BaseModel):
    api_id: int = Field(gt=0, le=2_147_483_647)
    api_hash: str = Field(min_length=20, max_length=64)
    phone: str = Field(min_length=5, max_length=32)


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class TwoFARequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


async def _resolve_user(token: str):
    state = await coordinator.get(token=token)
    factory = _get_session_factory()
    async with factory() as session:
        user = await UserRepository(session).get_by_id(state.user_id)
    if user is None or user.telegram_user_id != state.telegram_user_id:
        raise HTTPException(status_code=400, detail="Login ticket is invalid.")
    return state, user


@router.get("/start", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse(
        PHONE_LOGIN_HTML,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@router.post("/start")
async def start_login(body: StartRequest, x_phone_login_token: str = Header(default="")):
    token = x_phone_login_token
    state, user = await _resolve_user(token)
    if state.account_id is not None:
        raise HTTPException(status_code=400, detail="Login has already started.")
    async with _get_session_factory() as session:
        svc = AccountService(session)
        try:
            account = await svc.create_or_reset_account(
                user_id=user.id, api_id=body.api_id, api_hash=body.api_hash
            )
            from app.services.phone_login_runtime import phone_login_runtime
            await phone_login_runtime.create(
                token, user_id=user.id, account_id=account.id,
                api_id=body.api_id, api_hash=body.api_hash, phone=body.phone
            )
            await session.commit()
        except AccountAlreadyExistsError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            await session.rollback()
            await phone_login_runtime.remove(token)
            raise
    await coordinator.bind_account(
        token=token, user_id=user.id,
        telegram_user_id=user.telegram_user_id, account_id=account.id
    )
    await coordinator.set_stage(
        token=token, user_id=user.id,
        telegram_user_id=user.telegram_user_id, stage="code"
    )
    return {"stage": "code", "message": "Telegram sent a login code. Enter it on this page."}


@router.post("/code")
async def verify_code(body: CodeRequest, x_phone_login_token: str = Header(default="")):
    token = x_phone_login_token
    state, user = await _resolve_user(token)
    if state.account_id is None:
        raise HTTPException(status_code=400, detail="Start the phone login first.")
    from app.services.phone_login_runtime import phone_login_runtime
    try:
        await phone_login_runtime.submit_code(token, body.code)
        item = await phone_login_runtime.get(token)
        needs_2fa = item.stage == "2fa"
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if needs_2fa:
        await coordinator.set_stage(
            token=token, user_id=user.id,
            telegram_user_id=user.telegram_user_id, stage="2fa"
        )
        return {"stage": "2fa", "message": "Enter your Telegram 2FA password on this page."}
    await phone_login_runtime.wait_for_result(token)
    await coordinator.consume(
        token=token, user_id=user.id, telegram_user_id=user.telegram_user_id
    )
    return {"stage": "complete", "message": "Telegram account connected."}


@router.post("/2fa")
async def verify_2fa(body: TwoFARequest, x_phone_login_token: str = Header(default="")):
    token = x_phone_login_token
    state, user = await _resolve_user(token)
    if state.stage != "2fa":
        raise HTTPException(status_code=400, detail="2FA is not required.")
    from app.services.phone_login_runtime import phone_login_runtime
    try:
        await phone_login_runtime.submit_password(token, body.password)
        await phone_login_runtime.wait_for_result(token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await coordinator.consume(
        token=token,
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
    )
    return {"stage": "complete", "message": "Telegram account connected."}


@router.post("/cancel")
async def cancel_login(x_phone_login_token: str = Header(default="")):
    token = x_phone_login_token
    try:
        state, _ = await _resolve_user(token)
        from app.services.phone_login_runtime import phone_login_runtime
        await phone_login_runtime.remove(token)
        await coordinator.consume(
            token=token, user_id=state.user_id,
            telegram_user_id=state.telegram_user_id
        )
    except Exception:
        pass
    return {"stage": "cancelled"}
