"""Browser API endpoints for the secure Telegram phone-login flow."""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from app.db.session import _get_session_factory
from app.db.repositories.user_repo import UserRepository
from app.services.account import AccountAlreadyExistsError, AccountService
from app.services.phone_login import PhoneLoginCoordinator
from app.web.phone_login import PHONE_LOGIN_HTML

router = APIRouter(prefix="/api/v1/phone-login", tags=["phone-login"])
coordinator = PhoneLoginCoordinator()

_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_CODE_RE = re.compile(r"^\d{1,32}$")


class StartRequest(BaseModel):
    api_id: int = Field(gt=0, le=2_147_483_647)
    api_hash: str = Field(min_length=32, max_length=32)
    phone: str = Field(min_length=8, max_length=16)

    @field_validator("api_hash")
    @classmethod
    def validate_api_hash(cls, value: str) -> str:
        if not _API_HASH_RE.fullmatch(value):
            raise ValueError("API Hash must be a 32-character hexadecimal value.")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not _PHONE_RE.fullmatch(value):
            raise ValueError("Phone must use international format, e.g. +628123456789.")
        return value


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError("Login code must contain digits only.")
        return value


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


@router.get("/status")
async def login_status(x_phone_login_token: str = Header(default="")):
    state, _ = await _resolve_user(x_phone_login_token)
    return {"stage": state.stage, "active": True}


@router.post("/start")
async def start_login(body: StartRequest, x_phone_login_token: str = Header(default="")):
    token = x_phone_login_token
    state, user = await _resolve_user(token)
    if state.account_id is not None or state.stage != "phone":
        raise HTTPException(status_code=400, detail="Login ticket is invalid or already used.")
    factory = _get_session_factory()
    async with factory() as session:
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
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for _ in range(20):
        if item.error:
            raise HTTPException(status_code=400, detail=item.error)
        if item.stage == "2fa":
            await coordinator.set_stage(
                token=token, user_id=user.id,
                telegram_user_id=user.telegram_user_id, stage="2fa"
            )
            return {"stage": "2fa", "message": "Enter your Telegram 2FA password on this page."}
        if item.stage == "complete":
            break
        if item.task is not None and item.task.done():
            break
        await asyncio.sleep(0.05)

    if item.error:
        raise HTTPException(status_code=400, detail=item.error)
    if item.stage == "2fa":
        await coordinator.set_stage(
            token=token, user_id=user.id,
            telegram_user_id=user.telegram_user_id, stage="2fa"
        )
        return {"stage": "2fa", "message": "Enter your Telegram 2FA password on this page."}

    try:
        await phone_login_runtime.wait_for_result(token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        token=token, user_id=user.id, telegram_user_id=user.telegram_user_id
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
