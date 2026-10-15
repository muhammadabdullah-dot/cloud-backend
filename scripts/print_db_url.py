"""Print the SQL Server connection string, for a start script to read into DB_URL.

    set DB_URL=<output>

It exists so that no start script, document or log has to carry the password. `Reqs\\ALL_LOGIN_CREDENTIALS.txt` is the
one place it lives; this reads that file and writes the URL to stdout, which the caller captures into an environment
variable that dies with the process.

It prints nothing else, ever. A message on stdout would end up assigned to DB_URL by the caller, so problems go to
stderr and the exit code.
"""
from __future__ import annotations

import pathlib
import re
import sys

CREDENTIALS = pathlib.Path(__file__).resolve().parent.parent.parent.parent / "Reqs" / "ALL_LOGIN_CREDENTIALS.txt"
DRIVER = "ODBC+Driver+17+for+SQL+Server"


def main() -> int:
    if not CREDENTIALS.exists():
        print(f"No credentials file at {CREDENTIALS}", file=sys.stderr)
        return 1
    found = re.findall(r"mssql://\S+", CREDENTIALS.read_text(encoding="utf-8"))
    if not found:
        print("No mssql:// line in the credentials file. Run scripts/setup_sqlserver.py first.", file=sys.stderr)
        return 1
    url = found[-1].strip()
    sys.stdout.write(url if "driver=" in url else f"{url}?driver={DRIVER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
