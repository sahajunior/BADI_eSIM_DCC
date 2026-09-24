"""Ticket attachments: staging, validation, message binding, and authorized download."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_db
from app.config import Settings
from app.errors import ApiError
from app.models import Attachment, Role, TicketMessage, User, Visibility
from app.mutations import Db, Writer
from app.scanning import ScannerUnavailable, ScanRejected, sanitize_filename, scan
from app.storage import LocalAttachmentStorage, new_storage_key, storage_for
from app.tickets import get_visible_ticket

router = APIRouter(prefix="/api/v1", tags=["attachments"])

MAX_ATTACHMENTS_PER_MESSAGE = 3


class AttachmentDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_name: str
    detected_type: str
    size_bytes: int
    scan_state: str


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def storage(request: Request) -> LocalAttachmentStorage:
    return storage_for(_settings(request))


async def attachments_for(
    db: AsyncSession, message_ids: list[UUID]
) -> dict[UUID, list[AttachmentDTO]]:
    if not message_ids:
        return {}
    rows = await db.scalars(
        select(Attachment)
        .where(Attachment.message_id.in_(message_ids), Attachment.scan_state == "CLEAN")
        .order_by(Attachment.created_at, Attachment.id)
    )
    grouped: dict[UUID, list[AttachmentDTO]] = {}
    for attachment in rows:
        if attachment.message_id is not None:
            grouped.setdefault(attachment.message_id, []).append(
                AttachmentDTO.model_validate(attachment)
            )
    return grouped


@router.post("/tickets/{ticket_id}/attachments", status_code=201, response_model=AttachmentDTO)
async def upload_attachment(
    ticket_id: UUID,
    request: Request,
    db: Db,
    user: Writer,
    file: Annotated[UploadFile, File()],
) -> AttachmentDTO:
    settings = _settings(request)
    await get_visible_ticket(db, user, ticket_id)

    data = await file.read(settings.attachment_max_bytes + 1)
    if len(data) > settings.attachment_max_bytes:
        raise ApiError(413, "attachment_too_large", "Attachment exceeds the size limit.")

    filename = sanitize_filename(file.filename or "attachment")
    try:
        detected_type = scan(data, filename, settings)
    except ScannerUnavailable as exc:
        raise ApiError(503, "scanning_unavailable", "Attachment scanning is unavailable.") from exc
    except ScanRejected as exc:
        raise ApiError(422, "invalid_attachment", "Attachment type is not permitted.") from exc

    now = datetime.now(UTC)
    key = new_storage_key()
    storage(request).save(key, data)
    attachment = Attachment(
        ticket_id=ticket_id,
        message_id=None,
        uploader_id=user.id,
        storage_key=key,
        original_name=filename,
        detected_type=detected_type,
        size_bytes=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
        scan_state="CLEAN",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.attachment_ttl_seconds),
    )
    db.add(attachment)
    await db.commit()
    return AttachmentDTO.model_validate(attachment)


async def bind_attachments(
    db: AsyncSession,
    user: User,
    ticket_id: UUID,
    message_id: UUID,
    attachment_ids: list[UUID],
    now: datetime,
) -> None:
    if not attachment_ids:
        return
    if len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ApiError(422, "too_many_attachments", "Too many attachments for one message.")
    for attachment_id in attachment_ids:
        attachment = await db.get(Attachment, attachment_id)
        if (
            attachment is None
            or attachment.ticket_id != ticket_id
            or attachment.uploader_id != user.id
            or attachment.scan_state != "CLEAN"
            or attachment.message_id is not None
        ):
            raise ApiError(422, "invalid_attachment", "Attachment cannot be attached here.")
        attachment.message_id = message_id
        attachment.attached_at = now
        attachment.expires_at = None


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Response:
    attachment = await db.get(Attachment, attachment_id)
    if attachment is None or attachment.scan_state != "CLEAN":
        raise ApiError(404, "not_found", "Resource not found.")
    if attachment.message_id is None:
        # Staged but unattached: only its uploader may read it.
        if attachment.uploader_id != user.id:
            raise ApiError(404, "not_found", "Resource not found.")
    else:
        message = await db.get(TicketMessage, attachment.message_id)
        if message is None:
            raise ApiError(404, "not_found", "Resource not found.")
        if message.visibility == Visibility.INTERNAL.value and user.role == Role.CUSTOMER:
            raise ApiError(404, "not_found", "Resource not found.")
        await get_visible_ticket(db, user, attachment.ticket_id)

    data = storage(request).load(attachment.storage_key)
    filename = sanitize_filename(attachment.original_name)
    return Response(
        content=data,
        media_type=attachment.detected_type,
        headers={
            # Never render active content inline.
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


async def delete_expired(db: AsyncSession, store: LocalAttachmentStorage, now: datetime) -> int:
    rows = list(
        await db.scalars(
            select(Attachment).where(
                Attachment.message_id.is_(None),
                Attachment.expires_at.is_not(None),
                Attachment.expires_at < now,
            )
        )
    )
    for attachment in rows:
        store.delete(attachment.storage_key)
        await db.delete(attachment)
    return len(rows)
