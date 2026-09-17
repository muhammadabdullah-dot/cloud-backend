"""The company's supplier list as head office edits it: add, change, switch off, import. Every change reaches every
branch (services/supplier_sync_service.py)."""
import re
import uuid
from datetime import datetime, timezone

from tortoise.transactions import atomic

from app.models import Branch, Supplier, SupplierQuestion, User
from app.schemas.import_result import ImportRowError, ImportSummary
from app.schemas.suppliers import SupplierCreateIn, SupplierUpdateIn
from app.services import supplier_sync_service as sync
from app.services.import_service import cell_decimal, cell_int, cell_str, cell_str_any, parse_rows, row_error


class SupplierError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


_FIELD_MAP = {"contactPerson": "contact_person", "sTaxRegNo": "s_tax_reg_no", "dueDays": "due_days", "discountPercent": "discount_percent"}
_TEXT_FIELDS = ("name", "contactPerson", "phone", "phone2", "email", "address", "city", "ntn", "sTaxRegNo", "cnic", "remarks")
_API_NAME = {column: key for key, column in sync.SHARED.items()}


def _columns(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key in _TEXT_FIELDS and isinstance(value, str):
            value = value.strip() or None
        out[_FIELD_MAP.get(key, key)] = value
    return out


async def list_all() -> list[Supplier]:
    return await Supplier.all().order_by("-active", "name")


async def branch_names() -> dict[str, str]:
    return {code: name for code, name in await Branch.all().values_list("code", "name")}


async def next_code() -> str:
    """SUP0001, SUP0002 … after the highest SUP-number on the list."""
    highest = 0
    for code in await Supplier.filter(code__istartswith="SUP").values_list("code", flat=True):
        match = re.fullmatch(r"SUP(\d+)", code.upper())
        if match:
            highest = max(highest, int(match.group(1)))
    code = f"SUP{highest + 1:04d}"
    while await Supplier.exists(code=code):
        highest += 1
        code = f"SUP{highest + 1:04d}"
    return code


async def _name_taken(name: str, except_id: str | None = None) -> Supplier | None:
    wanted = sync.normal_name(name)
    for supplier in await Supplier.all().only("id", "code", "name"):
        if supplier.id != except_id and sync.normal_name(supplier.name) == wanted:
            return supplier
    return None


async def get(supplier_id: str) -> Supplier:
    supplier = await Supplier.get_or_none(id=supplier_id)
    if not supplier:
        raise SupplierError("That supplier isn't on the list.", 404)
    return supplier


@atomic()
async def create(data: SupplierCreateIn, user: User) -> Supplier:
    name = data.name.strip()
    code = (data.code or "").strip().upper() or await next_code()
    if await Supplier.exists(code=code):
        raise SupplierError(f"Supplier code {code} is already in use.")
    clash = await _name_taken(name)
    if clash:
        raise SupplierError(f"{clash.name} is already on the list as {clash.code}.")
    now = datetime.now(timezone.utc)
    supplier = await Supplier.create(
        id=f"sup-{uuid.uuid4().hex[:8]}", code=code, name=name, active=True, company_id=sync.new_company_id(), origin=sync.HEAD_OFFICE,
        rev=1, created_at=now, updated_at=now, updated_by=user.name, **_columns(data.model_dump(exclude={"code", "name"})),
    )
    await sync.rename_account(supplier)
    await sync.publish(supplier)
    return supplier


async def _write(supplier: Supplier, columns: dict, user: User) -> Supplier:
    changed = [c for c, v in columns.items() if c in _API_NAME and not sync.same_value(getattr(supplier, c), v)]
    if not changed:
        return supplier
    for column in changed:
        setattr(supplier, column, columns[column])
    await sync.ensure_company_id(supplier)
    supplier.rev += 1
    supplier.updated_at = datetime.now(timezone.utc)
    supplier.updated_by = user.name
    await supplier.save()
    if "name" in changed:
        await sync.rename_account(supplier)
    await sync.publish(supplier)
    return supplier


@atomic()
async def update(supplier_id: str, data: SupplierUpdateIn, user: User) -> Supplier:
    supplier = await get(supplier_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        name = changes["name"].strip()
        clash = await _name_taken(name, except_id=supplier.id)
        if clash:
            raise SupplierError(f"{clash.name} is already on the list as {clash.code}.")
        changes["name"] = name
    columns = {
        column: value for column, value in _columns(changes).items()
        if not (column in ("name", "active", "due_days", "discount_percent") and value is None)
    }
    return await _write(supplier, columns, user)


@atomic()
async def upsert(data: SupplierCreateIn, sent: set[str], user: User) -> bool:
    """Import: a row whose code is on the list changes that supplier (only the cells the file filled), a new code adds
    one. Returns True when it added one."""
    code = (data.code or "").strip().upper()
    supplier = await Supplier.get_or_none(code=code)
    if supplier:
        clash = await _name_taken(data.name, except_id=supplier.id)
        if clash:
            raise SupplierError(f"{clash.name} is already on the list as {clash.code}.")
        values = _columns({k: v for k, v in data.model_dump(exclude={"code"}).items() if k in sent})
        await _write(supplier, {k: v for k, v in values.items() if v is not None}, user)
        return False
    await create(data, user)
    return True


async def import_suppliers(filename: str, content: bytes, user: User) -> ImportSummary:
    rows = parse_rows(filename, content)
    created = updated = 0
    errors: list[ImportRowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            code = cell_str(row, "code")
            name = cell_str(row, "name")
            if not code or not name:
                raise ValueError("code and name are required")
            cells = {
                "name": name, "contactPerson": cell_str(row, "contactPerson"), "phone": cell_str(row, "phone"),
                "phone2": cell_str_any(row, "phone2", "otherPhone", "mobile"), "email": cell_str(row, "email"),
                "address": cell_str(row, "address"), "city": cell_str(row, "city"), "ntn": cell_str(row, "ntn"),
                "sTaxRegNo": cell_str_any(row, "sTaxRegNo", "strn"), "cnic": cell_str(row, "cnic"), "remarks": cell_str(row, "remarks"),
            }
            if cell_str(row, "dueDays") is not None:
                cells["dueDays"] = cell_int(row, "dueDays", 0)
            discount = cell_decimal(row, "discountPercent")
            if discount is None:
                discount = cell_decimal(row, "discount")
            if discount is not None:
                cells["discountPercent"] = discount
            filled = {k: v for k, v in cells.items() if v is not None}
            data = SupplierCreateIn(code=code, **filled)
            if await upsert(data, set(filled), user):
                created += 1
            else:
                updated += 1
        except SupplierError as exc:
            errors.append(ImportRowError(row=i, message=exc.message))
        except (ValueError, KeyError) as exc:
            errors.append(ImportRowError(row=i, message=row_error(exc)))
    return ImportSummary(created=created, updated=updated, errors=errors)


async def open_questions() -> list[SupplierQuestion]:
    return await SupplierQuestion.filter(status="open").order_by("created_at").prefetch_related("branch")
