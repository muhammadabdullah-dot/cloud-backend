# D.Marina — Head Office Server

The head-office ("cloud") server. It owns the godown — items, racks and bins, put-away, purchasing,
transfers out to the branches — the company books, every branch's reported figures, and the Executive
view built on them. It also serves the built
[head-office app](https://github.com/raza722/cloud-app) on the same port.

FastAPI + Tortoise ORM + SQLite, Python 3.14. Runs on port 4175.

```bash
.venv/Scripts/python.exe -m app.main     # settings come from .env
.venv/Scripts/aerich.exe upgrade         # apply schema migrations (back the database up first)
```

**[PROJECT_STATUS.md](PROJECT_STATUS.md) describes the whole system** — this server, the branch
server, both apps, how they talk to each other, what is live today and what is not built yet. Read
that first.

The database (`cloud.db`), its backups, logs and `.env` are not in git and never should be.
