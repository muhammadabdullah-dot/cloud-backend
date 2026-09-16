"""Maintenance: Backup Now to a chosen folder, the daily backup, and Restore."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.middlewares.auth import require_permission
from app.models import User
from app.services import backup_service

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_read = require_permission("admin.backup", "R")
_write = require_permission("admin.backup", "W")
_restore = require_permission("admin.backup.restore", "X")


def _fail(exc: backup_service.BackupError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


class BackupIn(BaseModel):
    folder: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=200)


class SettingsIn(BaseModel):
    folder: str | None = Field(default=None, max_length=400)
    autoEnabled: bool | None = None
    autoHour: int | None = Field(default=None, ge=0, le=23)
    keepAutomatic: int | None = Field(default=None, ge=1, le=365)
    includeMedia: bool | None = None


class RestoreIn(BaseModel):
    name: str = Field(max_length=200)
    confirm: str = Field(max_length=20)
    folder: str | None = Field(default=None, max_length=400)
    includeMedia: bool = True


@router.get("/status")
async def status() -> dict:
    """Open to anyone: whether a restore is running, so the app can say why the server is briefly unavailable."""
    return {"restoring": backup_service.RESTORING}


@router.get("/backups")
async def list_backups(folder: str | None = None, user: User = Depends(_read)) -> dict:
    try:
        return {
            "settings": backup_service.load_settings(),
            "database": await backup_service.database_status(),
            "folder": str(backup_service.resolve_folder(folder)) if folder else backup_service.load_settings()["folder"],
            "backups": backup_service.list_backups(folder),
            "history": backup_service.history(30),
        }
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc


@router.post("/backups")
async def backup_now(payload: BackupIn, user: User = Depends(_write)) -> dict:
    try:
        return await backup_service.create_backup("manual", user.name, payload.note, payload.folder)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc


@router.put("/backups/settings")
async def save_settings(payload: SettingsIn, user: User = Depends(_write)) -> dict:
    try:
        return backup_service.save_settings(payload.model_dump(exclude_none=True))
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc


@router.get("/backups/{name}/download")
async def download(name: str, folder: str | None = None, user: User = Depends(_write)) -> FileResponse:
    try:
        path = backup_service.backup_file(name, folder)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.post("/backups/upload")
async def upload(file: UploadFile = File(...), user: User = Depends(_write)) -> dict:
    try:
        return await backup_service.save_upload(file, file.filename or "backup.zip", user.name)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc


@router.post("/backups/restore")
async def restore(payload: RestoreIn, user: User = Depends(_restore)) -> dict:
    try:
        return await backup_service.restore(payload.name, payload.confirm, user.name, payload.folder, payload.includeMedia)
    except backup_service.BackupError as exc:
        raise _fail(exc) from exc
