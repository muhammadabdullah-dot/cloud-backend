"""Godown > Item Lists: departments, categories, classes, sub-classes, manufacturers, brands, units, pack units and GST
rates of the godown's Items. See services/item_lists_service.py.

The screen has its own access. Reading the switched-on values is wider on purpose: the Item form offers them to anyone
who can open an Item, and it shouldn't break for want of a tick on the lists' own screen.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from app.middlewares.auth import require_any_permission, require_permission
from app.models import User
from app.schemas.import_result import ImportRowError, ImportSummary
from app.services import item_lists_service as svc
from app.services.import_service import parse_rows

router = APIRouter(prefix="/warehouse/item-lists", tags=["warehouse-item-lists"])

ITEM_LISTS_RESOURCE = "warehouse.item-lists"
_choices_read = require_any_permission((ITEM_LISTS_RESOURCE, "R"), ("warehouse.items", "R"), ("warehouse.items.manage", "W"))
_screen = require_permission(ITEM_LISTS_RESOURCE, "R")
_write = require_permission(ITEM_LISTS_RESOURCE, "W")


class ListEntryOut(BaseModel):
    id: str
    kind: str
    # The exact text on the Items; for a GST rate, the rate written the short way (18, 7.5).
    name: str
    active: bool
    uses: int = 0
    # After a rename or merge: how many Items changed.
    moved: int | None = None
    updatedAt: datetime | None = None
    updatedBy: str | None = None


class ListSummaryOut(BaseModel):
    kind: str
    total: int
    switchedOff: int


class ChoicesOut(BaseModel):
    choices: dict[str, list[str]]


class ListEntryCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=160)


class ListEntryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    active: bool | None = None
    # Renaming onto a value already on the list moves every Item across and keeps one entry.
    merge: bool = False


def _fail(exc: svc.ItemListError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def _out(entry, uses: int = 0, moved: int | None = None) -> ListEntryOut:
    return ListEntryOut(
        id=str(entry.id), kind=entry.kind, name=entry.code, active=entry.active, uses=uses, moved=moved,
        updatedAt=entry.updated_at, updatedBy=entry.updated_by_name,
    )


@router.get("/summary", response_model=list[ListSummaryOut])
async def list_summary(user: User = Depends(_screen)) -> list[ListSummaryOut]:
    return [ListSummaryOut(**row) for row in await svc.summary()]


@router.get("/choices", response_model=ChoicesOut)
async def list_choices(user: User = Depends(_choices_read)) -> ChoicesOut:
    """Switched-on values of every list, for the Item form."""
    return ChoicesOut(choices=await svc.choices())


@router.get("", response_model=list[ListEntryOut])
async def list_entries(kind: str = Query(...), user: User = Depends(_screen)) -> list[ListEntryOut]:
    """One list, switched-off entries included, with how many Items use each."""
    try:
        return [_out(entry, uses) for entry, uses in await svc.entries(kind)]
    except svc.ItemListError as exc:
        raise _fail(exc)


@router.post("", response_model=ListEntryOut)
async def add_entry(payload: ListEntryCreate, user: User = Depends(_write)) -> ListEntryOut:
    try:
        return _out(await svc.add_entry(payload.kind, payload.name, user))
    except svc.ItemListError as exc:
        raise _fail(exc)


@router.post("/import", response_model=ImportSummary)
async def import_entries(file: UploadFile = File(...), user: User = Depends(_write)) -> ImportSummary:
    """Columns List, Name and optionally Active (yes or no). Adds what's missing and switches entries on or off."""
    rows = parse_rows(file.filename, await file.read())
    created, updated, errors = await svc.import_lists(rows, user)
    return ImportSummary(created=created, updated=updated, errors=[ImportRowError(row=r, message=m) for r, m in errors])


@router.patch("/{entry_id}", response_model=ListEntryOut)
async def update_entry(entry_id: str, payload: ListEntryUpdate, user: User = Depends(_write)) -> ListEntryOut:
    """Rename (every Item using it changes too), merge into an entry already on the list (`merge`), or switch on or
    off. Switched-off values stay on the Items; the Item form stops offering them."""
    try:
        entry, moved, uses = await svc.update_entry(entry_id, payload.name, payload.active, payload.merge, user)
    except svc.ItemListError as exc:
        raise _fail(exc)
    return _out(entry, uses, moved)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(entry_id: str, user: User = Depends(_write)) -> None:
    """Only an entry no Item uses (a typing mistake). Anything in use is switched off instead."""
    try:
        await svc.delete_entry(entry_id)
    except svc.ItemListError as exc:
        raise _fail(exc)
