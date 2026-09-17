"""Generic CSV/XLSX row parsing — used by every bulk-import endpoint (Products, Suppliers,
Parties, ...). Each entity's own import function maps these plain dict rows onto its schema;
this module only turns bytes into a list of {column: value} dicts.

Column names are matched loosely: case, spaces and punctuation don't count, so "S.Tax Reg No",
"sTaxRegNo" and "STAX REG NO" are the same column. That is what lets a file exported from a
listing be edited in Excel and imported straight back.
"""
import csv
import io
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache

import openpyxl
import xlrd
from pydantic import ValidationError


class ImportFormatError(Exception):
    def __init__(self, message: str):
        self.message = message


@lru_cache(maxsize=1024)
def header_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _decode_csv(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel on Windows saves "CSV" in the local code page, not UTF-8.
        return content.decode("cp1252", errors="replace")


def parse_rows(filename: str, content: bytes) -> list[dict]:
    lower = (filename or "").lower()
    if not content:
        raise ImportFormatError("The file is empty.")
    if lower.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(_decode_csv(content)))
        return [{header_key(k): v for k, v in row.items() if k is not None} for row in reader]
    if lower.endswith((".xlsx", ".xlsm")):
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 — any failure to open is the same answer for the person importing
            raise ImportFormatError("That file couldn't be opened as an Excel workbook. Open it in Excel, save it again as .xlsx (or .csv) and retry.") from exc
        sheet = workbook.active
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header = [header_key(h) if h is not None else "" for h in next(rows_iter)]
        except StopIteration:
            return []
        rows = []
        for raw_row in rows_iter:
            if all(v is None for v in raw_row):
                continue
            rows.append({header[i]: raw_row[i] for i in range(len(header)) if i < len(raw_row)})
        return rows
    if lower.endswith(".xls"):
        # Legacy binary Excel format — real exports from the old system come out this way.
        try:
            workbook = xlrd.open_workbook(file_contents=content)
        except xlrd.XLRDError as exc:
            raise ImportFormatError("That file couldn't be opened as an old-style Excel (.xls) file. Save it again as .xlsx (or .csv) and retry.") from exc
        sheet = workbook.sheet_by_index(0)
        if sheet.nrows == 0:
            return []
        header = [header_key(h) for h in sheet.row_values(0)]
        rows = []
        for r in range(1, sheet.nrows):
            values = sheet.row_values(r)
            if all(v == "" for v in values):
                continue
            rows.append({header[i]: values[i] for i in range(len(header)) if i < len(values)})
        return rows
    raise ImportFormatError(f"{filename} isn't a file this can import. Use .csv, .xlsx or .xls.")


def cell_str_any(row: dict, *keys: str) -> str | None:
    """Tries each header spelling in order — the two real catalog exports disagree on
    'SALES PRICE' vs 'SALE PRICE', 'SUBCLASS' vs 'SUB CLASS', etc."""
    for key in keys:
        value = cell_str(row, key)
        if value is not None:
            return value
    return None


def cell_str(row: dict, key: str) -> str | None:
    value = row.get(header_key(key))
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        # Excel keeps every number as a float, so an Item code 190033 arrives as 190033.0.
        value = int(value)
    text = str(value).strip()
    return text or None


def cell_bool(row: dict, key: str, default: bool = False) -> bool:
    value = cell_str(row, key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y")


def cell_int(row: dict, key: str, default: int = 0) -> int:
    value = cell_str(row, key)
    if value is None:
        return default
    try:
        return int(float(value.replace(",", "")))
    except ValueError:
        raise ValueError(f"{key} must be a whole number, but this row has {value!r}") from None


def cell_decimal(row: dict, key: str) -> Decimal | None:
    value = cell_str(row, key)
    if value is None:
        return None
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{key} must be a number, but this row has {value!r}") from None


def row_error(exc: Exception) -> str:
    """One readable line for an import row that failed, instead of a Python traceback summary."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            field = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
            message = str(err.get("msg", "is not valid"))
            parts.append(f"{field}: {message}" if field else message)
        return "; ".join(parts)
    if isinstance(exc, InvalidOperation):
        return "A number in this row isn't a valid number."
    return str(exc)
