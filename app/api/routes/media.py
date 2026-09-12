"""
app.api.routes.media
~~~~~~~~~~~~~~~~~~~~~
Media record history endpoints.

GET /api/v1/media           → paginated list of captured media
GET /api/v1/media/{id}      → single record (tenant-safe)
"""

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.api.schemas import MediaRecordSchema, PaginatedMediaSchema
from app.db.repositories.media_record_repo import MediaRecordRepository

router = APIRouter(prefix="/api/v1", tags=["media"])


@router.get("/media", response_model=PaginatedMediaSchema)
async def list_media(
    current_user: CurrentUser,
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaginatedMediaSchema:
    """Return a paginated list of the authenticated user's captured media records."""
    repo = MediaRecordRepository(session)
    records = await repo.list_by_user(current_user.id, limit=limit, offset=offset)
    items = [MediaRecordSchema.model_validate(r) for r in records]
    return PaginatedMediaSchema(items=items, total=len(items), limit=limit, offset=offset)


@router.get("/media/{record_id}", response_model=MediaRecordSchema)
async def get_media_record(
    record_id: int, current_user: CurrentUser, session: DbSession
) -> MediaRecordSchema:
    """Return a single media record.

    Tenant isolation: raises HTTP 404 if the record does not exist
    or belongs to another user.
    """
    repo = MediaRecordRepository(session)
    record = await repo.get_by_id(record_id)

    if record is None or record.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media record {record_id} not found.",
        )

    return MediaRecordSchema.model_validate(record)
