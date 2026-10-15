"""Check that the Item export's hand-written rows are still exactly what openpyxl would have written.

    python scripts/check_items_export.py                  the real Item master, and the awkward values
    python scripts/check_items_export.py --rows 2000      a quicker pass over the first 2,000 Items

The exit code is the verdict.

WHAT IT IS FOR
--------------
`items_service._write_xlsx` does not hand the data rows to openpyxl. openpyxl writes the workbook with the header row
and nothing else, and the rows go into its `sheetData` as XML on the way through the zip, because openpyxl's own
write-only mode builds a `Cell` object per value and serialises it one element at a time: 17.62 seconds against 2.78
for the same 67,804 rows, which is most of why this endpoint used to take over 40 seconds.

That is only safe while `_cell_xml` reproduces openpyxl's bytes rather than merely resembling them. So this writes the
sheet both ways and compares `xl/worksheets/sheet1.xml` character for character, over the real Item master and over
the values openpyxl treats specially:

  * a name beginning with "=", which openpyxl stores as a formula and not as text
  * a name that is one of Excel's error codes
  * outer whitespace, which needs `xml:space="preserve"`, and inner whitespace, which does not
  * `&`, `<` and `>`, and a newline, which openpyxl's text-mode writer turns into the platform's line ending
  * an empty string, a 40,000 character string, and control characters, which openpyxl refuses outright

Run it after touching `_cell_xml`, `_row_xml`, `_escape`, `_EXPORT_COLUMNS`, or after upgrading openpyxl. A failure
here means an exported file has changed shape, which matters because import reads these files back.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import pathlib
import sys
import time
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def openpyxl_sheet(rows: list[dict], columns: list) -> bytes:
    """The sheet openpyxl's write-only mode produces: what `_write_xlsx` has to match."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    from app.services import items_service as svc

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Items")
    for index, (_, _, width) in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"
    header = []
    for name, _, _ in columns:
        cell = WriteOnlyCell(sheet, value=name)
        cell.font = Font(bold=True)
        header.append(cell)
    sheet.append(header)
    for row in rows:
        sheet.append([svc._plain(getter(row)) for _, getter, _ in columns])
    out = io.BytesIO()
    workbook.save(out)
    return sheet_of(out.getvalue())


def sheet_of(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = next(n for n in archive.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml"))
        return archive.read(name)


def compare(label: str, rows: list[dict], columns: list) -> bool:
    """Both sheets for these rows, and where the first character differs if they do.

    A value openpyxl refuses is a pass when both refuse it the same way, which is the behaviour callers rely on.
    """
    from app.services import items_service as svc

    try:
        theirs = openpyxl_sheet(rows, columns)
    except Exception as exc:  # noqa: BLE001
        try:
            sheet_of(svc._write_xlsx(rows, columns))
        except Exception as ours:  # noqa: BLE001
            same = type(ours).__name__ == type(exc).__name__
            print(f"   {label:<30} {'both refuse it: ' + type(exc).__name__ if same else 'REFUSED DIFFERENTLY: openpyxl ' + type(exc).__name__ + ', ours ' + type(ours).__name__}")
            return same
        print(f"   {label:<30} ONLY openpyxl REFUSED IT ({type(exc).__name__})")
        return False
    at = time.monotonic()
    try:
        ours = sheet_of(svc._write_xlsx(rows, columns))
    except Exception as exc:  # noqa: BLE001
        print(f"   {label:<30} ONLY OURS REFUSED IT ({type(exc).__name__}: {exc})")
        return False
    took = time.monotonic() - at
    if theirs == ours:
        print(f"   {label:<30} identical, {len(ours):>10} characters, written in {took:5.2f}s")
        return True
    print(f"   {label:<30} DIFFERS")
    a, b = theirs.decode("utf-8"), ours.decode("utf-8")
    for at_char in range(min(len(a), len(b))):
        if a[at_char] != b[at_char]:
            print(f"      at character {at_char}")
            print(f"      openpyxl wrote: {a[max(0, at_char - 80):at_char + 80]!r}")
            print(f"      we wrote:       {b[max(0, at_char - 80):at_char + 80]!r}")
            return False
    print(f"      same up to the end of the shorter: openpyxl {len(a)}, ours {len(b)}")
    return False


def awkward(fields) -> list[tuple[str, dict]]:
    """One row per thing openpyxl does something special with, each with a name this can print.

    The label is written out rather than taken from the row, because some of these rows hold characters a Windows
    console cannot encode, and a check that falls over printing its own progress is no use.
    """
    blank = dict.fromkeys(fields)
    blank["alias_codes"] = None
    return [
        ("looks like a formula", {**blank, "sku": "=SUM(A1:A2)", "name": "=1+1"}),
        ("an Excel error code", {**blank, "sku": "#N/A", "name": "#DIV/0!"}),
        ("outer spaces and tabs", {**blank, "sku": "  spaced  ", "name": "\ttabbed\t"}),
        ("nothing but spaces", {**blank, "sku": " ", "name": "   "}),
        ("spaces inside only", {**blank, "sku": "inner spaces", "name": "two  spaces inside"}),
        ("empty strings", {**blank, "sku": "", "name": ""}),
        ("the XML characters", {**blank, "sku": "a&b<c>d", "name": 'quote " and \' apostrophe'}),
        ("longer than Excel allows", {**blank, "sku": "x" * 40000, "name": "far too long"}),
        ("arabic and an emoji", {**blank, "sku": "العربية",
                                 "name": "unicode \U0001f600"}),
        ("a newline in the name", {**blank, "name": "newline" + chr(10) + "in name", "sku": "nl"}),
        ("a carriage return", {**blank, "name": "carriage" + chr(13) + "return", "sku": "cr"}),
        ("nothing at all", {**blank, "name": None, "sku": None}),
        ("booleans", {**blank, "name": "yes and no", "lock_disc": True, "active": False}),
        ("every shape of number", {**blank, "name": "numbers", "price": 0, "rpp": -12.5, "tax_rate": 1e-07,
                                   "avg_cost": 1234567.891234, "pack_size": 0, "disc_percent": 100.0,
                                   "disc_flat": 1e22}),
        ("a vertical tab, refused", {**blank, "sku": "vertical tab", "name": "a" + chr(11) + "b"}),
        ("a null byte, refused", {**blank, "sku": "null byte", "name": "a" + chr(0) + "b"}),
    ]


async def run(args) -> int:
    from tortoise import Tortoise

    from app.core.config import TORTOISE_ORM, settings

    print(f"   {settings.db_url.split('@')[-1] if '@' in settings.db_url else settings.db_url}")
    await Tortoise.init(config=TORTOISE_ORM)
    good = True
    try:
        from app.models import Product, ProductAlias
        from app.services import items_service as svc

        columns = list(svc._EXPORT_COLUMNS)
        query = Product.all().order_by("name", "id")
        if args.rows:
            query = query.limit(args.rows)
        rows = await query.values(*svc._EXPORT_FIELDS)
        aliases: dict[str, list[str]] = {}
        for product_id, code in await ProductAlias.all().values_list("product_id", "code"):
            aliases.setdefault(product_id, []).append(code)
        for row in rows:
            row["alias_codes"] = ", ".join(aliases.get(row["id"], [])) or None

        print(f"   {len(rows)} Item(s), {len(columns)} columns\n")
        print("   the Item master")
        good &= compare("every Item", rows, columns)

        print("\n   the values openpyxl treats specially")
        for label, row in awkward(svc._EXPORT_FIELDS):
            good &= compare(label, [row], columns)

        print("\n   the finished file, read back")
        content = svc._write_xlsx(rows, columns)
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        read = [list(r) for r in book[book.sheetnames[0]].iter_rows(values_only=True)]
        titles_match = [c[0] for c in columns] == list(read[0])
        print(f"      openpyxl reads {len(read)} row(s); the titles {'match' if titles_match else 'DO NOT MATCH'}")
        good &= titles_match and len(read) == len(rows) + 1

        from app.services.import_service import parse_rows

        parsed = parse_rows("godown-items.xlsx", content)
        print(f"      import reads it back as {len(parsed)} row(s)")
        good &= len(parsed) == len(rows)
    finally:
        await Tortoise.close_connections()
    print("\n   The export writes exactly what openpyxl wrote." if good else
          "\n   THE EXPORT HAS CHANGED. See the differences above before shipping it.")
    return 0 if good else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=0, help="only the first N Items, for a quicker pass")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
