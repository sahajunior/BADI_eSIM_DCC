from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    get_current_user,
    get_db,
    hash_session_token,
    is_valid_session_token,
    load_session,
    user_identity,
)
from app.auth.passwords import hash_password, verify_password
from app.auth.security import (
    BOOTSTRAP_LIMIT,
    CSRF_HEADER_NAME,
    LOGIN_EMAIL_LIMIT,
    LOGIN_IP_LIMIT,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    client_ip,
    csrf_matches,
    enforce_get_csrf_fetch_policy,
    enforce_post_origin,
    hit_throttle,
    make_csrf_token,
    new_session_token,
    session_expires_at,
    set_session_cookie,
)
from app.config import Settings
from app.errors import ApiError
from app.models import AuthSession, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-unknown-users")


class IdentityResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: str


class CsrfResponse(BaseModel):
    csrf_token: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email", mode="after")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        try:
            email = validate_email(
                value.strip(),
                check_deliverability=False,
                test_environment=True,
            )
        except EmailNotValidError as exc:
            raise ValueError("valid email address required") from exc
        return email.normalized.lower()


class LoginResponse(IdentityResponse):
    csrf_token: str


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    raise ApiError(
        status_code=503,
        code="settings_unavailable",
        message="Settings are not configured.",
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def _create_session(
    db: AsyncSession,
    settings: Settings,
    *,
    user_id: UUID | None,
) -> tuple[AuthSession, str]:
    raw_token = new_session_token()
    session = AuthSession(
        user_id=user_id,
        token_hash=hash_session_token(raw_token),
        expires_at=session_expires_at(settings, authenticated=user_id is not None),
    )
    db.add(session)
    await db.flush()
    return session, raw_token


async def _valid_current_session(
    db: AsyncSession,
    raw_token: str | None,
) -> AuthSession | None:
    return await load_session(db, raw_token, require_user=False)


async def _require_csrf(
    db: AsyncSession,
    settings: Settings,
    raw_session_token: str | None,
    csrf_token: str | None,
) -> AuthSession:
    session = await _valid_current_session(db, raw_session_token)
    if session is None or not is_valid_session_token(raw_session_token):
        raise ApiError(status_code=403, code="invalid_csrf", message="CSRF validation failed.")
    if not csrf_matches(settings, raw_session_token or "", csrf_token):
        raise ApiError(status_code=403, code="invalid_csrf", message="CSRF validation failed.")
    return session


@router.get("/csrf", response_model=CsrfResponse)
async def csrf(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    badi_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> CsrfResponse:
    settings = _settings(request)
    enforce_get_csrf_fetch_policy(request, settings)
    await hit_throttle(
        db,
        purpose="auth:csrf:ip",
        value=client_ip(request),
        limit=BOOTSTRAP_LIMIT,
    )

    current_session = await _valid_current_session(db, badi_session)
    raw_token = (
        badi_session
        if current_session is not None and is_valid_session_token(badi_session)
        else None
    )
    if current_session is None or raw_token is None:
        _session, raw_token = await _create_session(db, settings, user_id=None)
        await db.commit()
        set_session_cookie(response, settings, raw_token, authenticated=False)

    return CsrfResponse(csrf_token=make_csrf_token(settings, raw_token))


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    badi_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> LoginResponse:
    settings = _settings(request)
    enforce_post_origin(request, settings)
    bootstrap_session = await _require_csrf(db, settings, badi_session, csrf_token)

    await hit_throttle(
        db,
        purpose="auth:login:ip",
        value=client_ip(request),
        limit=LOGIN_IP_LIMIT,
    )
    await hit_throttle(
        db,
        purpose="auth:login:email",
        value=payload.email,
        limit=LOGIN_EMAIL_LIMIT,
    )

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = await run_in_threadpool(verify_password, payload.password, password_hash)
    if user is None or not bool(user.is_active) or not password_ok:
        raise ApiError(status_code=401, code="invalid_credentials", message="Invalid credentials.")

    locked_session = await db.scalar(
        select(AuthSession)
        .where(AuthSession.id == bootstrap_session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_session is None
        or locked_session.revoked_at is not None
        or locked_session.expires_at <= _now()
        or locked_session.user_id is not None
    ):
        raise ApiError(status_code=403, code="invalid_csrf", message="CSRF validation failed.")

    # Password verification runs outside the event loop. Recheck the account at
    # the commit boundary so deactivation/password changes during it fail closed.
    user = await db.scalar(
        select(User)
        .where(User.email == payload.email)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active or user.password_hash != password_hash:
        raise ApiError(401, "invalid_credentials", "Invalid credentials.")

    locked_session.revoked_at = _now()
    _session, raw_token = await _create_session(db, settings, user_id=user.id)
    await db.commit()
    set_session_cookie(response, settings, raw_token, authenticated=True)
    return LoginResponse(
        **user_identity(user),
        csrf_token=make_csrf_token(settings, raw_token),
    )


@router.get("/me", response_model=IdentityResponse)
async def me(user: Annotated[User, Depends(get_current_user)]) -> IdentityResponse:
    return IdentityResponse(**user_identity(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    badi_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> Response:
    del user
    settings = _settings(request)
    enforce_post_origin(request, settings)
    session = await _require_csrf(db, settings, badi_session, csrf_token)
    session.revoked_at = _now()
    await db.commit()
    clear_session_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
