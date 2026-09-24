"""Agent/admin saved replies (macros). They insert editable text; they never send."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_db, require_staff
from app.errors import ApiError
from app.models import Role, SavedReply, TicketCategory, User
from app.mutations import Writer

router = APIRouter(prefix="/api/v1", tags=["saved-replies"])


class SavedReplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]
    category: TicketCategory | None = None


class SavedReplyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
    ] = None
    body: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)
    ] = None
    category: TicketCategory | None = None
    is_active: bool | None = None


class SavedReplyDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    body: str
    category: str | None
    is_active: bool
    author_id: UUID
    created_at: datetime
    updated_at: datetime


class SavedReplyList(BaseModel):
    items: list[SavedReplyDTO]


@router.get("/saved-replies", response_model=SavedReplyList)
async def list_saved_replies(
    _user: Annotated[User, Depends(require_staff)],
    db: Annotated[AsyncSession, Depends(get_db)],
    active_only: bool = Query(default=True),
) -> SavedReplyList:
    statement = select(SavedReply).order_by(SavedReply.title, SavedReply.id)
    if active_only:
        statement = statement.where(SavedReply.is_active.is_(True))
    rows = list(await db.scalars(statement))
    return SavedReplyList(items=[SavedReplyDTO.model_validate(row) for row in rows])


@router.post("/saved-replies", status_code=201, response_model=SavedReplyDTO)
async def create_saved_reply(
    payload: SavedReplyInput,
    user: Writer,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SavedReplyDTO:
    if user.role not in {Role.AGENT.value, Role.ADMIN.value}:
        raise ApiError(403, "forbidden", "Staff access required.")
    reply = SavedReply(
        author_id=user.id,
        title=payload.title,
        body=payload.body,
        category=payload.category.value if payload.category else None,
        is_active=True,
    )
    db.add(reply)
    await db.commit()
    return SavedReplyDTO.model_validate(reply)


@router.patch("/saved-replies/{reply_id}", response_model=SavedReplyDTO)
async def update_saved_reply(
    reply_id: UUID,
    payload: SavedReplyPatch,
    user: Writer,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SavedReplyDTO:
    if user.role not in {Role.AGENT.value, Role.ADMIN.value}:
        raise ApiError(403, "forbidden", "Staff access required.")
    reply = await db.get(SavedReply, reply_id)
    if reply is None:
        raise ApiError(404, "not_found", "Resource not found.")
    if user.role != Role.ADMIN.value and reply.author_id != user.id:
        raise ApiError(403, "forbidden", "Only the author or an administrator can edit this reply.")
    if (
        payload.title is None
        and payload.body is None
        and payload.category is None
        and payload.is_active is None
    ):
        raise ApiError(422, "empty_patch", "Provide a field to update.")
    if payload.title is not None:
        reply.title = payload.title
    if payload.body is not None:
        reply.body = payload.body
    if "category" in payload.model_fields_set:
        reply.category = payload.category.value if payload.category else None
    if payload.is_active is not None:
        reply.is_active = payload.is_active
    reply.updated_at = datetime.now(UTC)
    await db.commit()
    return SavedReplyDTO.model_validate(reply)
