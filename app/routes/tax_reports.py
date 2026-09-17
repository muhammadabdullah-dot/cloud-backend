"""Tax reports at head office over HTTP: GST month by month with the vouchers behind it, the set-off voucher (head office's
book only), and withholding, advance and income tax, for head office's book, a branch's, or all of them together (ALL).
See services/tax_reports_service.py."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.middlewares.auth import get_current_user
from app.models import HEAD_OFFICE_BOOK, User
from app.services import accounts_areas, accounts_posting_service, accounts_reports_service, tax_reports_service, vouchers_service
from app.services.accounts_areas import access_of
from app.services.accounts_reports_service import shop_day
from app.services.rbac_service import has_permission

router = APIRouter(prefix="/accounts/tax", tags=["tax"])


async def _reader(user: User = Depends(get_current_user)) -> User:
    if not await has_permission(user, "accounts.tax", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seeing the tax reports needs its own access. Ask your manager for it.")
    return user


async def _books(user: User, book: str | None) -> tuple[list[str], bool]:
    """Head office's own book needs only the tax tick; a branch's, or all of them, needs the right to see branch books too."""
    books, together = await accounts_reports_service.resolve_books(book)
    if books != [HEAD_OFFICE_BOOK] and not await has_permission(user, "accounts.branch-books", "R"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Seeing branches' books needs the right to see branch books.")
    return books, together


def _range(from_: date | None, to: date | None) -> tuple[date, date]:
    end = to or shop_day()
    start = from_ or end.replace(day=1)
    if start > end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The start date is after the end date.")
    if (end.year - start.year) * 12 + end.month - start.month >= 24:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick up to 24 months.")
    return start, end


@router.get("/gst")
async def gst(from_: date | None = Query(None, alias="from"), to: date | None = None, book: str | None = None, user: User = Depends(_reader)) -> dict:
    books, together = await _books(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await tax_reports_service.gst(books, together, start, end)


@router.get("/withholding")
async def withholding(from_: date | None = Query(None, alias="from"), to: date | None = None, book: str | None = None, user: User = Depends(_reader)) -> dict:
    books, together = await _books(user, book)
    await accounts_posting_service.ensure_recent()
    start, end = _range(from_, to)
    return await tax_reports_service.withholding(books, together, start, end)


class SetOffIn(BaseModel):
    month: str


@router.get("/gst/set-off")
async def set_off_preview(month: str, user: User = Depends(_reader)) -> dict:
    try:
        return await tax_reports_service.set_off_plan(month)
    except tax_reports_service.TaxError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)


@router.post("/gst/set-off")
async def prepare_set_off(payload: SetOffIn, user: User = Depends(_reader)) -> dict:
    """A draft journal in head office's book: Dr GST payable, Cr GST input. Writing it is writing a voucher, on accounts in the Tax area."""
    if not await has_permission(user, "accounts.vouchers", "W"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Preparing the set-off is writing a voucher, which needs its own access. Ask your manager for it.")
    try:
        voucher = await tax_reports_service.prepare_set_off(user, payload.month, await access_of(user))
    except accounts_areas.AreaRefused as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.message)
    except (tax_reports_service.TaxError, vouchers_service.VoucherError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message)
    return await vouchers_service.voucher_out(voucher)
