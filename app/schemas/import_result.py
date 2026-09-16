from pydantic import BaseModel


class ImportRowError(BaseModel):
    row: int
    message: str


class ImportSummary(BaseModel):
    created: int
    updated: int
    errors: list[ImportRowError]
