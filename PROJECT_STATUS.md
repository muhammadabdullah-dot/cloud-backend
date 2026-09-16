# D.Marina — Project Status

**As of:** 16 September 2026 · **State:** running live at Model Town on the shop LAN

This file is the same in all four repositories. It describes the whole system, because none of the
four halves makes sense on its own.

---

## 1. What this is

A retail ERP for D.Marina, a Pakistani grocery + pharmacy chain, built in two halves:

- **The branch** runs its own server in the shop. It keeps trading whether or not the internet is up:
  billing, the till, stock, receiving, the branch's own books.
- **Head office** runs the cloud server. It holds the godown, purchasing, every branch's reported
  figures, the company books and the Executive view.

Everything below is real and running — a FastAPI backend against a real database with the branch's
own 47,477-product catalogue imported from their legacy exports, and React apps wired to it end to
end. Nothing described as done is a mock or a stub returning invented numbers.

## 2. The four repositories

| Repository | What it is | Served on |
|---|---|---|
| `backend/branch-server` | Branch server — FastAPI + Tortoise ORM + SQLite | port 4174 |
| `branch-app` | Branch app — Vite + React 18 + TypeScript + Ant Design + zustand | built into the branch server's `dist`, same port |
| `backend/cloud-server` | Head-office server — same stack | port 4175 |
| `cloud-app` | Head-office app — same stack | built into the cloud server's `dist`, same port |

Each server serves its own app, so a till or an office PC opens one address and gets everything:
`http://<server-ip>:4174` at the branch, `http://<server-ip>:4175` for head office. On this machine
that is `172.16.35.60`. Python 3.14, Node 24.

Databases are `branch.db` and `cloud.db` beside their servers. **Neither is in git** — they hold real
trading data and are handed over separately, as are the login credentials.

## 3. Running it

```bash
# each server, from its own directory
.venv/Scripts/python.exe -m app.main          # reads .env: PORT, DB_URL, MEDIA_DIR, LOG_DIR, SYNC_*

# each app, from its own directory
npm run build                                  # writes dist/, which its server then serves
```

Schema changes are Aerich migrations (`.venv/Scripts/aerich.exe migrate --name x`, then `upgrade`).
Current heads: branch `20_20260916081348_counters`, head office `19_20260916082540_counter_duty`.
**Always back the database up first** (`VACUUM INTO 'branch.db.bak-before-<change>-<stamp>'`) and test
the migration on the copy before the live file. Aerich writes `ALTER TABLE … ADD CONSTRAINT` for new
foreign-key columns, which SQLite rejects — those migrations are hand-corrected to an inline
`REFERENCES` on `ADD COLUMN` (see branch migrations 6 and 20).

## 4. What is live today

- **Model Town (MT)** trades on its own server and reports to head office. **Fort Colony (FC)** is
  registered at head office and has no server yet.
- 622 bills from 1 August to 15 September, 49,841 stock movements, 47,477 items, 11 staff accounts.
- The books are live on both sides: 106-account chart at the branch, 462 branch vouchers, 499 at head
  office including the inter-office side.
- The godown holds 4 racks / 132 bins, 7 goods receipts and 6 purchase orders against 4 suppliers.
- Sync is healthy: every branch event delivered, no failures, snapshots current.

## 5. The branch side

**Sales counter** — Billing (scan, pack barcodes, discounts within a person's own limit, manager
override, gift vouchers, loyalty members, credit customers, hold/recall, receipts), Returns, Gift
Vouchers, Customer Payments, X/Z day close.

**Counters and the drawer** — a sales counter is a real record. Several tills can be open at once,
one per counter and one per person; every bill and return carries the drawer it was rung into, so
each counter reconciles from its own bills. **Counter Board** shows who is on each counter, whose
drawer is open, cash expected and takings today; **Staff on Duty** shows who is on the floor, since
when, and what has gone through their hands. Duty is time-bounded history — who put whom where, and
when they came off — not a flag.

**Stock** — catalogue with aliases/pack barcodes and pictures, suppliers, purchase orders, receiving
(GRN), purchase returns, batches and expiry, physical counts, adjustments, barcode labels, locations,
and transfers in from the godown with a dispatch/receive/dispute lifecycle.

**Branch office** — dashboard, approvals inbox, staff and per-person access, customers, loyalty
members, head-office sync status, backup and restore.

**Books** — full double-entry: chart of accounts, vouchers (post, cancel, reverse), ledger, trial
balance, income statement, balance sheet, month-by-month, day book, ageing, month close/reopen.
Sales, purchases, returns and cash movements post themselves every few minutes.

## 6. The head-office side

**Godown** — items, racks and bins, put-away with home bins, receiving, purchase orders with approval
limits, purchase summary, picking, counts, transfers out to branches, and a decisions queue.

**Executive view** — a KPI board with charts, and a drill-down behind every tile and every row that
keeps going: sales by day → one day → who was on and what sold → one item → **who sold it** → one
person's sales of that item; categories, brands, branches, suppliers, ABC/XYZ, dead stock, cash,
till variance, discounts, returns, stock health, branch stock, sync health. Staff on Duty now reports
who was on the floor against who rang a sale.

**Admin and books** — users and role templates, branch staff mirrored from the branches, branch
registration and pairing, loyalty settings, backups, and the company books with the same accounting
screens as the branch.

## 7. How the two halves talk

- **Branch → head office, events:** every branch write also writes an outbox event. The sync loop
  pushes them, head office stores each one and projects the kinds it mirrors (staff, transfers,
  members, loyalty, activity, accounts). A failed projection is retried every 15 minutes; an event
  head office refuses five times is set aside with its reason so the queue keeps draining.
- **Branch → head office, figures:** every couple of hours the branch sends a whole snapshot of
  itself — trading days, per-item days, per-person days, per-item-per-person days, hours, till
  closes, counter duty, tenders, discount overrides, returns, credit customers, stock alerts and item
  stock. Head office replaces that branch's picture atomically. This is what the Executive view reads.
  A row it cannot identify is skipped and counted, never fatal.
- **Head office → branch:** staff, roles and transfer notices go down a per-branch message queue the
  branch acknowledges. A message the branch cannot apply is logged, acknowledged as failed, and
  stepped over.
- Any new key in a payload must also be declared on the receiving pydantic model — pydantic drops
  what it does not declare, silently.

## 8. Access

Branch access is **not** a role allowlist: every person has their own ticks (resource × read / write /
execute), started from one of two presets (Salesperson, Branch Manager) and editable per person by a
Branch Manager. Head office uses role templates. New abilities reach existing accounts through a
one-time rollout at startup, because the per-resource backfill cannot see a newly added *action*.

## 9. Operating notes

- **Logs:** each server writes `logs/server.log` beside its database (5 MB × 10). An unexpected error
  shows the user a short reference like `H4K7-2BQP` and logs the traceback under it — ask for that
  reference and grep the log.
- **Error screens:** both apps show a plain "this screen ran into a problem" page with Reload, Go
  back, and the same reference, and report browser errors back to their server.
- **Backups:** the branch server takes a daily backup and can restore one; everything pauses while a
  restore runs. Take a manual copy before any migration or bulk change.
- **Testing:** work against copies of the real databases on ports 4184/4185/4186 with the seeded demo
  accounts — never run experiments against the live files.

## 10. Recent work (13–16 September 2026)

- The whole accounts module, at the branch and head office, with a full 1 Aug – 15 Sep story posted.
- Server logs with user-facing error references, and error screens in both apps.
- Executive drill-downs by item × person: an item opens onto the people who sold it, a person onto
  everything they sold.
- Counters, counter duty and multi-till operation; Counter Board and Staff on Duty; head office sees
  duty as well as trading.
- An audit of both apps and both servers, and the defects it found: a crash that would have wedged
  the branch's pull loop, one refused event blocking the whole queue, a leap-day crash in the
  Executive view, Billing without a permission check, three screens that crashed when permissions
  changed, till close reading credit sales from browser storage, executive alerts linking to screens
  the Executive cannot open, cycle-count variance measured against a truncated ledger, and a
  malformed row rejecting a whole branch's figures.

## 11. Not built yet

- **Deployment as a product:** an all-in-one Windows installer that installs PostgreSQL and every
  dependency, registers both servers to run all the time, and puts an icon on the desktop that opens
  the app. With it, the move from SQLite to PostgreSQL (regenerated migrations, `pg_dump` backups,
  the two SQLite-only queries rewritten, row-locked counters). **This is the next piece of work.**
- FBR/PRAL invoice registration — the receipt lays out the invoice number and QR, but nothing is
  filed.
- HR: a rota, clock-in and payroll. Counter duty records who was put where, which is not attendance.
- Head office keeps sale, till, GRN, return, adjustment and count events as raw records without
  projecting them into its own tables; the Executive view reads the snapshot instead.
- Devices register themselves but cannot be named, listed or retired from any screen.
- Cheques and customer payments exist at the branch only; the head-office build has the calls but no
  routes behind them.
- Mobile apps, e-commerce and channel integrations (Phase 4 in the roadmap).

## 12. Conventions

- Comments explain **why**, in plain English, for the person who inherits the code.
- Never commit databases, backups, logs, `.env` files or credentials. They are gitignored; keep it
  that way.
- Figures are computed on the server so every screen spells them the same way; the apps format, they
  do not calculate.
- `BRANCH_APP_STATE.md` (12 September) is an earlier, branch-only snapshot kept for history. This
  file supersedes it.
