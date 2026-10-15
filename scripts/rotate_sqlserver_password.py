"""Give the application's SQL Server login a new password, and put the old one beyond use.

    python scripts/rotate_sqlserver_password.py              what it would do
    python scripts/rotate_sqlserver_password.py --yes         do it

The new password is generated here, written to `Reqs\\ALL_LOGIN_CREDENTIALS.txt`, and **never printed**: not to the
terminal, not to a log, not into a start script. That is the whole point of this file existing. A password that has
passed through a terminal has to be rotated again, which is why this one is being rotated at all.

WHY, AND HOW IT CANNOT LOCK YOU OUT
-----------------------------------
A connection string went into a log in clear text, from a line whose job was only to say which database was about to
be written to, so `dmarina_app`'s password is exposed and a new one is the only remedy.

Changing a password and writing it down are two steps, and a failure between them locks everybody out of a database
that is now the only copy of 16.4 million rows. So the order is: change it, prove the new one connects, then write it
down, then prove the file reads back and connects. If the new password does not work, the old one is put straight
back and nothing has moved. If the file cannot be written, the old password is put back for the same reason.

`dmarina_app` changes its own password, supplying the old one, so this needs no administrator and `sa` is not
involved.

AFTERWARDS
----------
Anything running keeps its connection until it is restarted, because the password is checked when a connection is
made and not afterwards. Head office on 4205 reads the credentials file at start through `scripts/print_db_url.py`,
so it picks the new one up on its next start and needs nothing else changed.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import secrets
import sys

CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"

# The characters that are safe in an ODBC connection string when nobody quotes it: no ; = { } ' " @ : / or space.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_"
LENGTH = 32


def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def current() -> tuple[str, str, str, str, str]:
    """The mssql line as it stands: the whole line, user, password, host part, and anything after the database."""
    if not CREDENTIALS.exists():
        raise SystemExit(f"No credentials file at {CREDENTIALS}")
    found = re.findall(r"mssql://\S+", CREDENTIALS.read_text(encoding="utf-8"))
    if not found:
        raise SystemExit("No mssql:// line in the credentials file.")
    line = found[-1].strip()
    rest = line[len("mssql://"):]
    user, _, tail = rest.partition(":")
    secret, _, hostpart = tail.rpartition("@")
    host, _, database = hostpart.partition("/")
    return line, user, secret, host, database


def dsn(user: str, secret: str, host: str, database: str) -> str:
    server, _, port = host.partition(":")
    return (f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:{server},{port or 1433};"
            f"DATABASE={database.split('?')[0]};UID={user};PWD={secret};Encrypt=no;TrustServerCertificate=yes")


def t_sql(value: str) -> str:
    """A string literal for T-SQL, which takes no parameters in a DDL statement."""
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    import pyodbc

    line, user, old, host, database = current()
    print(f"   login      {user}")
    print(f"   on         {host}/{database.split('?')[0]}")
    print(f"   file       {CREDENTIALS}")
    print(f"   new one    {LENGTH} characters, generated here, never printed")

    print("\n   checking the password on file still works ...", flush=True)
    try:
        pyodbc.connect(dsn(user, old, host, database), timeout=30).close()
        print("      it does.")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"   the password on file does not connect, so there is nothing safe to rotate from:\n"
                         f"      {str(exc)[:200]}")

    if not args.yes:
        print("\n   Nothing changed. Add --yes.")
        return 0

    fresh = new_password()

    print("\n   changing it ...", flush=True)
    conn = pyodbc.connect(dsn(user, old, host, database), timeout=30, autocommit=True)
    conn.cursor().execute(
        f"ALTER LOGIN [{user}] WITH PASSWORD = {t_sql(fresh)} OLD_PASSWORD = {t_sql(old)}")
    conn.close()
    print("      changed.")

    def put_back(why: str) -> int:
        print(f"\n   {why}")
        print("   putting the old password back so nothing is locked out ...", flush=True)
        try:
            back = pyodbc.connect(dsn(user, fresh, host, database), timeout=30, autocommit=True)
            back.cursor().execute(
                f"ALTER LOGIN [{user}] WITH PASSWORD = {t_sql(old)} OLD_PASSWORD = {t_sql(fresh)}")
            back.close()
            print("      back as it was. Nothing has moved.")
        except Exception as exc:  # noqa: BLE001
            print(f"      COULD NOT PUT IT BACK: {str(exc)[:200]}")
            print("      The login now has a password that is not written anywhere. Reset it as sa:")
            print(f"         ALTER LOGIN [{user}] WITH PASSWORD = '<something you choose>'")
            print(f"      and put the same value into {CREDENTIALS.name} on the mssql:// line.")
        return 1

    print("   proving the new one connects ...", flush=True)
    try:
        check = pyodbc.connect(dsn(user, fresh, host, database), timeout=30)
        check.cursor().execute("SELECT COUNT(*) FROM branch_sales")
        check.close()
        print("      it connects and can read.")
    except Exception as exc:  # noqa: BLE001
        return put_back(f"the new password does not connect: {str(exc)[:160]}")

    print("   writing it to the credentials file ...", flush=True)
    # The rollback is held here rather than in a .bak beside the file. A backup of this file is a second plaintext
    # copy of every password in it, not just the one being rotated, and the old copy outlives the rotation: the
    # first run of this script left one on disk holding every other live credential. The only change made below is
    # one line, `text` is the file before it, and `put_back` undoes the server side through ALTER LOGIN, so nothing
    # on disk is needed to get back to where we started.
    was = CREDENTIALS.read_text(encoding="utf-8")
    try:
        text = was
        replaced = text.replace(line, line.replace(f":{old}@", f":{fresh}@"), 1)
        if replaced == text:
            raise RuntimeError("the mssql:// line did not change, so the file would still hold the old password")
        # Rewriting the one line we know about is not the same as the old password being gone. The file also had it
        # on a `login  dmarina_app  <password>` line, which this rotation left behind: dead, still readable, and
        # still looking current to anyone who read that line instead of the URL. So check the whole file, not the
        # line we edited, and refuse rather than leave a second copy.
        if old in replaced:
            stale = [n for n, text_line in enumerate(replaced.splitlines(), 1) if old in text_line]
            raise RuntimeError(
                f"the old password is still on line(s) {', '.join(map(str, stale))} of {CREDENTIALS.name}. "
                f"The password belongs on the mssql:// line only; make those lines point at it, then run this again.")
        CREDENTIALS.write_text(replaced, encoding="utf-8")
        print("      written. No copy of the file was left on disk; the rollback is held in memory.")
    except Exception as exc:  # noqa: BLE001
        return put_back(f"the credentials file could not be written: {str(exc)[:160]}")

    print("   reading it back the way the server will ...", flush=True)
    try:
        _, u2, p2, h2, d2 = current()
        if p2 == old:
            raise RuntimeError("the file still holds the old password")
        pyodbc.connect(dsn(u2, p2, h2, d2), timeout=30).close()
        print("      the file's password is the working one.")
    except Exception as exc:  # noqa: BLE001
        CREDENTIALS.write_text(was, encoding="utf-8")   # back to the file as it was, then the server side
        return put_back(f"what the file now holds does not connect: {str(exc)[:160]}")

    print(f"\n   Done. {user} has a new password, it is only in {CREDENTIALS.name}, and the old one is dead.")
    print("   Restart head office so it picks it up:")
    print("      fresh-test\\start-headoffice.cmd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
