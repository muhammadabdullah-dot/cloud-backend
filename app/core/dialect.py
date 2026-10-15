"""The handful of places where hand-written SQL has to know which engine it is talking to.

Almost none of this codebase cares. 175 `?` placeholders work unchanged on SQLite and on SQL Server, because pyodbc
uses the same qmark paramstyle, and `CAST(x AS REAL)` is legal in both. What does not survive is a short list of
functions with different names, and one piece of syntax that is in a different place in the statement.

Everything here is a function returning a SQL fragment, so a caller reads as SQL rather than as a branch:

    f"WHERE {d.days_since('s.last_sold_at')} >= 90"
    f"SELECT {d.group_concat_distinct('b.code')} AS branches"
    f"SELECT {d.top(50)} * FROM t ORDER BY x DESC {d.limit(50)}"

WHY `top` AND `limit` ARE TWO CALLS
-----------------------------------
SQLite ends a statement with `LIMIT 50`. SQL Server begins it with `SELECT TOP 50`. The two are at opposite ends, so
one helper cannot serve both and a caller has to put something in each place. Each returns an empty string on the
engine that does not use it, which keeps the statement readable on both.

`OFFSET ... FETCH NEXT` would have avoided the split, but SQLite does not have `FETCH`, so it is not a common
subset.
"""
from __future__ import annotations

from tortoise import Tortoise

SQLITE = "sqlite"
MSSQL = "mssql"


def engine() -> str:
    """Which engine the default connection is. Read from the live connection rather than from configuration, so a
    script pointed at a different database by DB_URL gets the right answer."""
    try:
        return Tortoise.get_connection("default").schema_generator.DIALECT
    except Exception:  # noqa: BLE001: before init, and in tests, SQLite is the safe assumption
        return SQLITE


def days_since(column: str) -> str:
    """Whole days between `column` and now, as a number.

    SQLite counts in days because julianday returns a day number; SQL Server has DATEDIFF, which takes the unit.
    """
    if engine() == MSSQL:
        return f"DATEDIFF(day, {column}, GETDATE())"
    return f"(julianday('now') - julianday({column}))"


def exact_number(column: str) -> str:
    """A quantity column in a form that sums the same way every time.

    The reorder suggestions summed `CAST(qty AS REAL)`. On SQLite REAL is a double and that is fine; on SQL Server
    REAL is *single* precision, 24 bits, and the column it was casting is already `decimal(16,3)`. So the cast threw
    exactness away and replaced it with noise: a real sum came back as 10526369.433015002 where the exact answer is
    10526369.433000.

    That matters beyond the stray digits. SQL Server aggregates in parallel and the order it adds the parts in is
    not fixed, so a float sum differs slightly between runs of the same query. The suggestions screen sorts by rate
    per day, and rows whose rates were genuinely equal kept swapping places because the noise broke the tie
    differently each time. 11 to 17 of 1,103 lines moved run to run.

    Decimal arithmetic is exact and therefore order-independent, so the sum is identical every time. SQLite keeps
    Tortoise decimals as text, which cannot be summed directly, so it keeps its double cast.
    """
    if engine() == MSSQL:
        return f"CAST({column} AS decimal(28, 6))"
    return f"CAST({column} AS REAL)"


def group_concat_distinct(column: str, separator: str = ",") -> str:
    """Distinct values joined into one string.

    SQL Server's STRING_AGG **rejects DISTINCT** — tested against 2025, which answers `STRING_AGG(name, ',')` and
    refuses `STRING_AGG(DISTINCT name, ',')`. So the distinct has to happen before the aggregate, which at this
    grain means a grouped subquery. Callers that need it inside a larger GROUP BY use `distinct_then_concat`.
    """
    if engine() == MSSQL:
        return f"STRING_AGG({column}, '{separator}')"
    return f"GROUP_CONCAT(DISTINCT {column})"


def top(n: int | None) -> str:
    """The row limit, for the front of a SELECT. Empty on SQLite, which puts it at the end instead.

    **Prefer `limit_offset`.** See the warning on `limit` below: this pair has to be used together or it silently
    stops limiting, and `limit_offset` says the same thing in one place.
    """
    if n is None:
        return ""
    return f"TOP {int(n)} " if engine() == MSSQL else ""


def limit(n: int | None) -> str:
    """The row limit, for the end of a SELECT. Empty on SQL Server, which puts it at the front instead.

    **Prefer `limit_offset`, which needs only one place in the statement.** This one and `top` have to appear
    together, and a statement carrying only this one still runs on SQL Server: the fragment is empty, the SQL is
    valid, and every row comes back. The branch page's "not sold" list did exactly that and returned 20,031 rows
    where it meant 50, which is not an error anywhere, just a screen with the wrong answer on it.
    """
    if n is None:
        return ""
    return "" if engine() == MSSQL else f"LIMIT {int(n)}"


def limit_offset(n: int, offset: int) -> str:
    """A page, at the end of a statement. SQL Server needs an ORDER BY in front of it and will say so if it is
    missing, which is a better failure than silently returning an arbitrary page."""
    if engine() == MSSQL:
        return f"OFFSET {int(offset)} ROWS FETCH NEXT {int(n)} ROWS ONLY"
    return f"LIMIT {int(n)} OFFSET {int(offset)}"


def page(n: int, offset: int) -> tuple[str, list[int]]:
    """The same page, but as placeholders and the values to bind, for a query that already passes its parameters.

    Returns the clause and the parameters **in the order that clause wants them**, because the two engines disagree:
    SQLite reads `LIMIT ? OFFSET ?` and SQL Server reads `OFFSET ? ROWS FETCH NEXT ? ROWS ONLY`, so the limit and
    the offset swap places. A caller that built its own list cannot know that, which is exactly the kind of thing
    that returns the wrong page on one engine and looks fine on the other, so the helper hands back both.
    """
    if engine() == MSSQL:
        return "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", [int(offset), int(n)]
    return "LIMIT ? OFFSET ?", [int(n), int(offset)]


def substr(column: str, start: int, length: int) -> str:
    """A slice of text. SQLite calls it substr, SQL Server calls it SUBSTRING; both count from 1.

    Only for a column that really is text on both engines. For the year and month of a date, use `year_month`.
    """
    name = "SUBSTRING" if engine() == MSSQL else "substr"
    return f"{name}({column}, {int(start)}, {int(length)})"


def year_month(column: str) -> str:
    """A date column as 'YYYY-MM'.

    This was `substr(col, 1, 7)`, which works on SQLite because Tortoise keeps a date there as text. On SQL Server
    the column is a real `date` and SUBSTRING refuses it outright: "Argument data type date is invalid for argument 1
    of substring function". The rollups stopped being rebuilt and the dashboard would have gone quietly back to
    reading 1.8 million day rows for a year view.

    Style 126 is ISO 8601, so `CONVERT(char(7), ...)` keeps exactly the year and the month.
    """
    if engine() == MSSQL:
        return f"CONVERT(char(7), {column}, 126)"
    return f"substr({column}, 1, 7)"


def new_id() -> str:
    """A fresh identifier, for an INSERT ... SELECT that has to make its own primary keys.

    Both produce something unique; neither is the other's format, which does not matter because nothing outside the
    row reads these. SQLite has no NEWID and SQL Server has no randomblob.
    """
    return "LOWER(CONVERT(varchar(36), NEWID()))" if engine() == MSSQL else "lower(hex(randomblob(16)))"


def now() -> str:
    return "GETDATE()" if engine() == MSSQL else "CURRENT_TIMESTAMP"


def nocase(expression: str) -> str:
    """Sort or compare this text without regard to case.

    SQLite is case sensitive by default and needs `COLLATE NOCASE` asked for. SQL Server needs nothing: the database
    was created `Latin1_General_100_CI_AS_SC_UTF8`, and CI is case insensitive already, so adding a collation here
    would only be a second way to say what the column already does. `COLLATE NOCASE` is not a collation SQL Server
    knows, so leaving it in was a hard error rather than a difference in ordering.
    """
    return expression if engine() == MSSQL else f"{expression} COLLATE NOCASE"


def json_value(column: str, path: str) -> str:
    """One value out of a JSON column. Same path syntax, different function name."""
    name = "JSON_VALUE" if engine() == MSSQL else "json_extract"
    return f"{name}({column}, '{path}')"


def all_digits(column: str) -> str:
    """True when the text is nothing but digits.

    SQLite's GLOB takes the character class; SQL Server's LIKE takes the same class but not GLOB. Both are written
    as "does not contain a non-digit" because that is one pattern rather than an anchored repetition.
    """
    if engine() == MSSQL:
        return f"{column} NOT LIKE '%[^0-9]%'"
    return f"{column} NOT GLOB '*[^0-9]*'"


def zero_pad(column: str, width: int = 20) -> str:
    """A number as text, left padded, so text ordering puts 200 before 11050010.

    SQLite has printf. SQL Server has FORMAT, which is culture-aware and slow over many rows, so this builds the
    padding with REPLICATE instead: same answer, and no per-row culture lookup.
    """
    if engine() == MSSQL:
        return (f"RIGHT(REPLICATE('0', {int(width)}) + CAST(CAST({column} AS bigint) AS varchar({int(width)})), "
                f"{int(width)})")
    return f"printf('%0{int(width)}d', CAST({column} AS INTEGER))"


def marks(values, sql_type: str = "char(36)") -> str:
    """Placeholders for a list of text keys, in a form that does not get slower with every value added.

        f"WHERE l.account_id IN ({d.marks(account_ids)})"
        f"WHERE v.book IN ({d.marks(books, 'varchar(20)')})"

    THE BUG THIS EXISTS FOR
    -----------------------
    Our keys are narrow text: an id is `char(36)`, a book code `varchar(20)`, a sku `varchar(60)`. pyodbc sends a
    Python `str` as `NVARCHAR`, and NVARCHAR has the higher data type precedence, so SQL Server resolves the
    comparison by converting the **column** rather than the parameter.

    For a single `=` the optimiser works around that and still seeks. For an `IN` list past about eight values it
    stops working around it, and from there the query costs another half second for every value in the list.
    Measured on head office's 5,412,886 voucher lines, selecting the indexed column only, each run twice:

           values in the IN list     plain `?`      with the cast
                             8          0.00s              0.00s
                            12          6.61s              0.01s
                            16          8.85s              0.01s
                            24         13.35s              0.01s
                            32         16.90s              0.01s
                            64         35.69s              0.02s

    The cast is flat because the index is seeked once per value. The plain form is a straight line through the
    origin, which is what reading the whole table once per value looks like.

    It is invisible at the call site, it is invisible in the answer, and it only shows in the plan as a
    CONVERT_IMPLICIT in a corner nobody reads. What it cost in practice: the ledger of their busiest account took
    74.31s for one book and 108.98s for five, against a browser that gives up at 15s. Both are now under five
    seconds. `ops/checks/sql_key_casts.py` fails the build if a new comparison forgets the cast, and
    `ops/checks/report_casts.py` proves the casts change no answer.

    SQLite has one text type and no such precedence, so the cast would be noise there and the plain `?` is kept.

    `sql_type` must be the column's own type or wider. Wider is safe: the comparison stays inside the varchar
    family, so the column is never the side that gets converted. **Narrower silently truncates the value** and the
    row simply stops matching, with no error, which is why this takes the type rather than guessing it.
    """
    if engine() == MSSQL:
        return ",".join(f"CAST(? AS {sql_type})" for _ in values)
    return ",".join("?" for _ in values)


def mark(sql_type: str = "char(36)") -> str:
    """One placeholder for a text key, cast the way `marks` casts a list. For `=` rather than `IN`.

        f"WHERE l.account_id = {d.mark()}"

    A single comparison is not the disaster the `IN` list is, because the optimiser still manages a seek through the
    conversion. It is not free either: counting the 1,411,233 lines on their busiest account took 0.45s as a plain
    `?` and 0.05s with the cast, repeatably. Nine times, on one comparison, is worth the three words.
    """
    return f"CAST(? AS {sql_type})" if engine() == MSSQL else "?"


def codes(values) -> str:
    """Placeholders for a list of short code values: a book, a voucher type, an account kind, an account code.

        f"WHERE v.book IN ({d.codes(books)})"

    Same reason as `marks` above, for the other half of our narrow text columns. These are `varchar(20)` (book),
    `varchar(12)` (kind, code), `varchar(10)` (status) and `varchar(4)` (vtype), and one cast serves all of them:
    `varchar(20)` is the widest of them, and casting wider than the column is the safe direction.
    """
    return marks(values, "varchar(20)")


def loop_join() -> str:
    """A join whose order is pinned: start from the left input and seek the right one per row.

        f"FROM acc_accounts a {d.loop_join()} acc_voucher_lines l ON l.account_id = a.id"

    SQL Server takes the hint as `INNER LOOP JOIN`. SQLite has no join hints at all and rejects the word outright
    with "unknown join type: INNER LOOP", which is a hard error rather than a slower plan, so there the fragment is
    a plain `INNER JOIN`. SQLite's own planner picks a nested loop here anyway, since it has nothing else.

    This exists because the hint was written inline. It is correct on the engine head office runs and it made the
    receivable and payable summaries sixteen times cheaper to read cold, but it also meant `sums_by_kind` could not
    run on SQLite at all. That is not a theoretical loss: thirteen of the checks in `ops/checks` run against a copy
    of cloud.db on SQLite, and every one of them that reaches a party summary or an ageing stopped at this word.
    """
    return "INNER LOOP JOIN" if engine() == MSSQL else "INNER JOIN"


def hash_join() -> str:
    """A join told to build a hash table rather than seek the other side once per row.

        f"FROM acc_voucher_lines l {d.hash_join()} acc_vouchers v ON v.id = l.voucher_id"

    **Only for a wide date window, and only with a lower bound.** Asked for a stretch of the ledger with both ends
    pinned, SQL Server seeks acc_vouchers by date and then looks the lines up a voucher at a time, which is the right
    plan for a few thousand vouchers and the wrong one for half a million. The cost of that plan is a straight line
    through the origin; a hash join is nearly flat. Measured over 5,412,886 lines on all five books, ending
    2026-10-02, reading the indexed column only:

           window    vouchers in it     plain JOIN     INNER HASH JOIN
            7 days            8,339          0.17s               1.93s
           30 days           46,628          1.04s               2.10s
           60 days           96,713          2.42s               2.16s
           90 days          144,548          3.52s               2.19s
          180 days          289,914          7.07s               2.50s
          365 days          582,137         13.91s               3.22s

    They cross at 60 days, which is where `_sums` switches. Below it the plain join is the better plan and the hint
    would make a month's figures twice as slow; above it the hint is the difference between the dashboard's twelve
    month view answering and timing out.

    With no lower bound at all -- the cumulative shape the trial balance and the balance sheet use -- the two plans
    are the same to within noise (3.90s against 4.15s), so nothing there asks for the hint.

    SQLite has no join hints and rejects the word, so there this is a plain `INNER JOIN`, exactly as `loop_join`.
    """
    return "INNER HASH JOIN" if engine() == MSSQL else "INNER JOIN"


def quote(name: str) -> str:
    """One identifier, quoted for the engine in hand.

    Needed for exactly one column so far: `branch_sales.at`. `AT` is a keyword in SQL Server (`AT TIME ZONE`), so
    a bare `s.at` is a syntax error there while being perfectly legal on SQLite. Double quotes would in fact serve
    both, since ODBC sets QUOTED_IDENTIFIER ON, but that is a setting a connection could arrive without, and a
    query that breaks on a server option nobody remembers setting is not worth the four characters saved.
    """
    return f"[{name}]" if engine() == MSSQL else f'"{name}"'
