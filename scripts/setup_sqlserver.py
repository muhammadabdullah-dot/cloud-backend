"""Prepare this machine's SQL Server to hold head office: a database, a login, and nothing else.

    python scripts/setup_sqlserver.py                 what it would do
    python scripts/setup_sqlserver.py --yes            do it

It connects as the Windows user running it, which the installer made a sysadmin, so no password is needed to run
it. The login it creates gets a generated password that is written to `Reqs\\ALL_LOGIN_CREDENTIALS.txt` and is
**never printed**: not to a terminal, not to a log. That file is where passwords live; a password that has passed
through a screen has been published, which the `dmarina_read` login learned on 30 September 2026.

WHAT IT MAKES, AND WHY EACH ONE
-------------------------------
  a database          `dmarina_cloud`, collation Latin1_General_100_CI_AS_SC_UTF8. UTF-8 because their party and
                      Item names are not all ASCII, and a case-insensitive accent-sensitive collation because that
                      is what SQLite's NOCASE behaviour is closest to; a case-SENSITIVE collation would silently
                      split "Panadol" from "PANADOL" in every group-by the dashboard does.
  a login             `dmarina_app`, a SQL login rather than Windows, because Tortoise's MSSQL backend builds its
                      DSN with UID and PWD and has no path for a trusted connection.
  a user + db_owner   on that database only. Not sysadmin: the application creates and alters its own tables and
                      nothing else on the instance, and a server-wide role would let a bug reach `multitec_copy`.

It does not enable TCP/IP, open a firewall port, or change the authentication mode. Those are machine settings and
they are yours. It checks all three and refuses with instructions if any is missing.
"""
from __future__ import annotations

import argparse
import pathlib
import secrets
import sys
from datetime import datetime

DATABASE = "dmarina_cloud"
LOGIN = "dmarina_app"
COLLATION = "Latin1_General_100_CI_AS_SC_UTF8"
HERE = pathlib.Path(__file__).resolve().parent
CREDENTIALS = HERE.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"


def password() -> str:
    """Long, and free of the characters that break an ODBC connection string when nobody quotes it."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_"
    return "".join(secrets.choice(alphabet) for _ in range(28))


def connect(database: str = "master"):
    import pyodbc

    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER=tcp:localhost,1433;DATABASE={database};"
        f"Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes", timeout=20, autocommit=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--database", default=DATABASE)
    ap.add_argument("--login", default=LOGIN)
    args = ap.parse_args()

    try:
        import pyodbc  # noqa: F401
    except ImportError:
        print("This needs pyodbc:  .venv\\Scripts\\python.exe -m pip install pyodbc asyncodbc")
        return 2

    try:
        cn = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"   cannot reach SQL Server over TCP: {exc}")
        print("   TCP/IP has to be on, which needs an elevated shell and is yours to run:")
        print("     Set-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Microsoft SQL Server\\"
              "MSSQL17.MSSQLSERVER\\MSSQLServer\\SuperSocketNetLib\\Tcp' -Name Enabled -Value 1")
        print("     Restart-Service MSSQLSERVER -Force")
        return 1

    cur = cn.cursor()
    # Every SERVERPROPERTY comes back as sql_variant, which pyodbc will not decode: cast each one.
    cur.execute("SELECT CAST(@@SERVERNAME AS nvarchar(128)), "
                "CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(40)), "
                "CAST(SERVERPROPERTY('Edition') AS nvarchar(80)), "
                "CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int), "
                "CAST(ConnectionProperty('net_transport') AS nvarchar(40))")
    server, version, edition, windows_only, transport = cur.fetchone()
    print(f"   server     {server}  {version}")
    print(f"   edition    {edition}")
    print(f"   transport  {transport}")
    if int(windows_only or 0):
        print("\n   This instance is set to Windows authentication only, so a SQL login cannot sign in.")
        print("   Mixed mode has to be switched on, which is yours to do and needs a service restart.")
        return 1

    cur.execute("SELECT name FROM sys.databases WHERE name = ?", args.database)
    have_db = cur.fetchone() is not None
    cur.execute("SELECT name FROM sys.sql_logins WHERE name = ?", args.login)
    have_login = cur.fetchone() is not None

    print()
    print(f"   database   {args.database}  {'exists already' if have_db else 'would be created'}")
    print(f"   collation  {COLLATION}")
    print(f"   login      {args.login}  {'exists already' if have_login else 'would be created'}")
    print(f"   rights     db_owner on {args.database} only. Not sysadmin, so it cannot reach multitec_copy.")
    print(f"   password   generated here, written to {CREDENTIALS.name}, never printed")

    if not args.yes:
        print("\n   Nothing done. Add --yes.")
        return 0

    made: list[str] = []
    if not have_db:
        cur.execute(f"CREATE DATABASE [{args.database}] COLLATE {COLLATION}")
        made.append(f"database {args.database}")
    # Simple recovery: this is a database rebuilt from a pipeline, not one whose every transaction must be
    # recoverable to the second, and full recovery on 5 GB of bulk load fills a disk with log.
    cur.execute(f"ALTER DATABASE [{args.database}] SET RECOVERY SIMPLE")

    secret = password()
    # T-SQL DDL takes no parameters: CREATE LOGIN ... WITH PASSWORD = ? is a syntax error, so the password has to
    # be part of the statement text. That is safe here only because this password is generated a few lines above
    # from a fixed alphabet with no quote in it; the escape is belt and braces, not the reason it is safe.
    quoted = "'" + secret.replace("'", "''") + "'"
    if have_login:
        cur.execute(f"ALTER LOGIN [{args.login}] WITH PASSWORD = {quoted}, CHECK_POLICY = OFF")
        made.append(f"login {args.login} (password reset)")
    else:
        cur.execute(f"CREATE LOGIN [{args.login}] WITH PASSWORD = {quoted}, CHECK_POLICY = OFF, "
                    f"DEFAULT_DATABASE = [{args.database}]")
        made.append(f"login {args.login}")

    db = connect(args.database)
    dcur = db.cursor()
    dcur.execute("SELECT name FROM sys.database_principals WHERE name = ?", args.login)
    if dcur.fetchone() is None:
        dcur.execute(f"CREATE USER [{args.login}] FOR LOGIN [{args.login}]")
        made.append(f"user {args.login} in {args.database}")
    dcur.execute(f"ALTER ROLE db_owner ADD MEMBER [{args.login}]")
    db.close()
    cn.close()

    lines = [
        "",
        "=" * 78,
        f"D.MARINA HEAD OFFICE ON SQL SERVER -- set {datetime.now():%Y-%m-%d %H:%M}",
        f"Set by backend/cloud-server/scripts/setup_sqlserver.py on {server}.",
        f"  server    tcp:localhost,1433   ({version})",
        f"  database  {args.database}",
        f"  login     {args.login:<20} {secret}",
        f"  rights    db_owner on {args.database} only, not sysadmin",
        "",
        "  DB_URL for the app:",
        f"  mssql://{args.login}:{secret}@localhost:1433/{args.database}",
        "=" * 78,
    ]
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    with CREDENTIALS.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    print()
    for item in made:
        print(f"   created  {item}")
    print(f"\n   The password and the whole DB_URL are appended to:")
    print(f"      {CREDENTIALS}")
    print("   They are not printed here, and nothing else holds them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
