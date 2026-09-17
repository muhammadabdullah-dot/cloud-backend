from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.types import Money


class CompanySupplierOut(BaseModel):
    id: str
    companyId: str | None = None
    code: str
    name: str
    contactPerson: str | None = None
    phone: str | None = None
    phone2: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    ntn: str | None = None
    sTaxRegNo: str | None = None
    cnic: str | None = None
    dueDays: int = 0
    discountPercent: Money = Decimal("0")
    remarks: str | None = None
    active: bool = True
    # "HO", or the code of the branch that added it; `originName` in words.
    origin: str = "HO"
    originName: str = "Head office"
    rev: int = 1
    updatedAt: datetime | None = None
    updatedBy: str | None = None


class SupplierFieldsIn(BaseModel):
    contactPerson: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    phone2: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=180)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=80)
    ntn: str | None = Field(default=None, max_length=40)
    sTaxRegNo: str | None = Field(default=None, max_length=40)
    cnic: str | None = Field(default=None, max_length=40)
    dueDays: int = Field(default=0, ge=0, le=365)
    discountPercent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    remarks: str | None = Field(default=None, max_length=255)


class SupplierCreateIn(SupplierFieldsIn):
    # Blank means "number it for me" (SUP0001 …). 20 characters, the most a branch's code can hold.
    code: str | None = Field(default=None, max_length=20)
    name: str = Field(min_length=1, max_length=160)


class SupplierUpdateIn(BaseModel):
    """Only the fields sent change. The code stays."""
    name: str | None = Field(default=None, min_length=1, max_length=160)
    contactPerson: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    phone2: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=180)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=80)
    ntn: str | None = Field(default=None, max_length=40)
    sTaxRegNo: str | None = Field(default=None, max_length=40)
    cnic: str | None = Field(default=None, max_length=40)
    dueDays: int | None = Field(default=None, ge=0, le=365)
    discountPercent: Decimal | None = Field(default=None, ge=0, le=100)
    remarks: str | None = Field(default=None, max_length=255)
    active: bool | None = None


class QuestionSupplierOut(BaseModel):
    id: str | None = None
    code: str | None = None
    name: str
    contactPerson: str | None = None
    phone: str | None = None
    phone2: str | None = None
    city: str | None = None
    ntn: str | None = None
    active: bool = True


class SupplierQuestionOut(BaseModel):
    id: str
    branchCode: str
    branchName: str
    reason: str
    branchSupplier: QuestionSupplierOut
    candidates: list[QuestionSupplierOut]
    # Candidates that are already this branch's copy of another of its suppliers can't be "the same".
    takenCandidateIds: list[str] = []
    askedAt: datetime


class SupplierAnswerIn(BaseModel):
    answer: Literal["same", "different", "branch-only"]
    # For "same": which candidate.
    supplierId: str | None = None


class BranchListStatusOut(BaseModel):
    branchCode: str
    branchName: str
    # Whether the branch has joined the company list (sent its suppliers and been sent the list).
    joined: bool
    joinedAt: datetime | None = None
    matched: int = 0
    added: int = 0
    decided: int = 0
    waiting: int = 0
