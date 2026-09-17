"""The company's supplier list: head office adds and changes suppliers, every branch gets them."""
from fastapi import APIRouter, Depends, File, UploadFile

from app.controllers import supplier_controller
from app.middlewares.auth import require_permission
from app.models import User
from app.schemas.import_result import ImportSummary
from app.schemas.suppliers import (
    BranchListStatusOut,
    CompanySupplierOut,
    SupplierAnswerIn,
    SupplierCreateIn,
    SupplierQuestionOut,
    SupplierUpdateIn,
)

router = APIRouter(prefix="/suppliers", tags=["suppliers"])

_read = require_permission("warehouse.suppliers", "R")
_manage = require_permission("warehouse.suppliers.manage", "W")


@router.get("", response_model=list[CompanySupplierOut])
async def list_suppliers(user: User = Depends(_read)) -> list[CompanySupplierOut]:
    return await supplier_controller.list_all()


@router.post("", response_model=CompanySupplierOut)
async def create_supplier(payload: SupplierCreateIn, user: User = Depends(_manage)) -> CompanySupplierOut:
    """Add a supplier to the list. Leave `code` out to have one given (SUP0001 …). Every branch gets it."""
    return await supplier_controller.create(payload, user)


@router.post("/import", response_model=ImportSummary)
async def import_suppliers(file: UploadFile = File(...), user: User = Depends(_manage)) -> ImportSummary:
    """A CSV or Excel file: a row whose code is on the list changes that supplier, a new code adds one."""
    return await supplier_controller.import_file(file.filename, await file.read(), user)


@router.get("/questions", response_model=list[SupplierQuestionOut])
async def list_questions(user: User = Depends(_read)) -> list[SupplierQuestionOut]:
    """A branch's suppliers head office couldn't match to the list for certain, waiting for a person."""
    return await supplier_controller.questions()


@router.post("/questions/{question_id}/answer")
async def answer_question(question_id: str, payload: SupplierAnswerIn, user: User = Depends(_manage)) -> dict:
    """`same` (with the supplier it is), `different` (add it to the list) or `branch-only` (it stays at that branch)."""
    return await supplier_controller.answer(question_id, payload, user)


@router.get("/branches", response_model=list[BranchListStatusOut])
async def branch_statuses(user: User = Depends(_read)) -> list[BranchListStatusOut]:
    """Each branch: whether it has joined the list, and how its own suppliers matched."""
    return await supplier_controller.branches()


# After /import, /questions and /branches, so none of those is read as an id.
@router.patch("/{supplier_id}", response_model=CompanySupplierOut)
async def update_supplier(supplier_id: str, payload: SupplierUpdateIn, user: User = Depends(_manage)) -> CompanySupplierOut:
    """Change a supplier, or switch it off (`active: false`). Every branch gets the change."""
    return await supplier_controller.update(supplier_id, payload, user)


_prepared = False


async def _prepare_company_list() -> None:
    """Once per database (a `counters` row): every supplier gets its company identity, and look-alikes are reported.

    Started from this router so app/main.py needs only its include line. FastAPI runs a router's startup handlers
    after the database is open, and in this version runs them twice, so the flag keeps it to once per start."""
    global _prepared
    if _prepared:
        return
    _prepared = True
    from app.core import logs
    from app.services import supplier_sync_service

    try:
        done = await supplier_sync_service.prepare_company_list()
    except Exception as exc:  # noqa: BLE001 — a supplier list problem must not stop head office opening
        logs.log.error("suppliers: couldn't prepare the company supplier list", exc_info=exc)
        return
    if done:
        print(f"  suppliers: {done['identified']} supplier(s) given a company identity, {len(done['lookAlike'])} look-alike pair(s)", flush=True)


router.add_event_handler("startup", _prepare_company_list)
