from fastapi import HTTPException

from app.models import Branch, Supplier, SupplierQuestion, User
from app.schemas.import_result import ImportSummary
from app.schemas.suppliers import (
    BranchListStatusOut,
    CompanySupplierOut,
    QuestionSupplierOut,
    SupplierAnswerIn,
    SupplierCreateIn,
    SupplierQuestionOut,
    SupplierUpdateIn,
)
from app.services import supplier_service, supplier_sync_service


def _raise(exc: supplier_service.SupplierError | supplier_sync_service.SupplierSyncError):
    raise HTTPException(exc.status, exc.message)


def _to_out(s: Supplier, names: dict[str, str]) -> CompanySupplierOut:
    origin = s.origin or supplier_sync_service.HEAD_OFFICE
    return CompanySupplierOut(
        id=s.id, companyId=s.company_id, code=s.code, name=s.name, contactPerson=s.contact_person, phone=s.phone, phone2=s.phone2,
        email=s.email, address=s.address, city=s.city, ntn=s.ntn, sTaxRegNo=s.s_tax_reg_no, cnic=s.cnic, dueDays=s.due_days,
        discountPercent=s.discount_percent, remarks=s.remarks, active=s.active, origin=origin,
        originName="Head office" if origin == supplier_sync_service.HEAD_OFFICE else names.get(origin, origin),
        rev=s.rev, updatedAt=s.updated_at, updatedBy=s.updated_by,
    )


async def list_all() -> list[CompanySupplierOut]:
    names = await supplier_service.branch_names()
    return [_to_out(s, names) for s in await supplier_service.list_all()]


async def create(data: SupplierCreateIn, user: User) -> CompanySupplierOut:
    try:
        supplier = await supplier_service.create(data, user)
    except supplier_service.SupplierError as exc:
        _raise(exc)
    return _to_out(supplier, await supplier_service.branch_names())


async def update(supplier_id: str, data: SupplierUpdateIn, user: User) -> CompanySupplierOut:
    try:
        supplier = await supplier_service.update(supplier_id, data, user)
    except supplier_service.SupplierError as exc:
        _raise(exc)
    return _to_out(supplier, await supplier_service.branch_names())


async def import_file(filename: str, content: bytes, user: User) -> ImportSummary:
    return await supplier_service.import_suppliers(filename, content, user)


def _question_supplier(data: dict) -> QuestionSupplierOut:
    return QuestionSupplierOut(
        id=data.get("localId"), code=data.get("code"), name=data.get("name") or "-", contactPerson=data.get("contactPerson"),
        phone=data.get("phone"), phone2=data.get("phone2"), city=data.get("city"), ntn=data.get("ntn"), active=bool(data.get("active", True)),
    )


async def questions() -> list[SupplierQuestionOut]:
    out = []
    for q in await supplier_service.open_questions():
        candidates = {s.id: s for s in await Supplier.filter(id__in=list(q.candidates or []))}
        taken = set(candidates) & await supplier_sync_service.present_at(q.branch, q.branch_supplier_id)
        out.append(SupplierQuestionOut(
            id=str(q.id), branchCode=q.branch.code, branchName=q.branch.name, reason=q.reason,
            branchSupplier=_question_supplier(q.branch_supplier or {}),
            candidates=[
                QuestionSupplierOut(id=s.id, code=s.code, name=s.name, contactPerson=s.contact_person, phone=s.phone, phone2=s.phone2,
                                    city=s.city, ntn=s.ntn, active=s.active)
                for cid in (q.candidates or []) if (s := candidates.get(cid))
            ],
            takenCandidateIds=sorted(taken), askedAt=q.created_at,
        ))
    return out


async def answer(question_id: str, data: SupplierAnswerIn, user: User) -> dict:
    try:
        question: SupplierQuestion = await supplier_sync_service.answer(question_id, data.answer, data.supplierId, user)
    except supplier_sync_service.SupplierSyncError as exc:
        _raise(exc)
    return {"id": str(question.id), "status": question.status, "supplierId": question.answer_supplier_id}


async def branches() -> list[BranchListStatusOut]:
    return [
        BranchListStatusOut(**await supplier_sync_service.branch_status(b))
        for b in await Branch.filter(verified_at__not_isnull=True).order_by("name")
    ]
