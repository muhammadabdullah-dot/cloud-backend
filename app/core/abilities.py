"""What a person at head office can do, in the words the access screen uses.

Every tick on the User Access screen is one ability here, and each ability is exactly one permission underneath (a
resource and R, W or X), so what the screen shows and what the server enforces can't drift apart. Changing something
(W) or deciding something (X) always brings seeing it (R) along. Nobody gives a tick they don't hold themselves.

The books are a tick per screen and action, plus which accounts a person sees and uses, by area (cash and bank, tax,
expenses…). An area limits the ledger, the chart and vouchers; whole-book reports are ticks of their own and always
show the whole book. The areas are the same at every branch.
"""

# (group, resource, action, label, hint)
ABILITIES: list[tuple[str, str, str, str, str]] = [
    # ── the godown ──
    ("godown", "warehouse.dashboard", "R", "Godown dashboard", "Stock value, what's running low and what's waiting for a decision."),
    ("godown", "warehouse.items", "R", "See Items", "The Item master: names, barcodes, prices and files."),
    ("godown", "warehouse.items.manage", "W", "Add, edit and import Items", "Including their prices, files and home bins."),
    ("godown", "warehouse.item-lists", "R", "See Item Lists", "Departments, categories, brands, units and GST rates of the godown's Items."),
    ("godown", "warehouse.item-lists", "W", "Change Item Lists", "Add, rename, merge and switch off values. A rename changes every Item that has it."),
    ("godown", "warehouse.bins", "R", "Racks, bins and put-away", "Where every Item is kept."),
    ("godown", "warehouse.bins.manage", "W", "Add racks and change bins", "Sizing racks, and changing or switching off bins."),
    ("godown", "warehouse.bins.move", "W", "Put stock away and move it between bins", ""),
    ("godown", "warehouse.counts", "R", "See stock counts", ""),
    ("godown", "warehouse.counts", "W", "Count stock", ""),
    ("godown", "warehouse.counts.approve", "X", "Approve stock counts", "Whoever counts doesn't accept the difference."),
    ("godown", "warehouse.decisions", "R", "Decisions waiting", "Branch requests and counts waiting for a manager."),
    # ── buying ──
    ("buying", "warehouse.suppliers", "R", "See suppliers", "The company's supplier list, which every branch shares."),
    ("buying", "warehouse.suppliers.manage", "W", "Add and change suppliers", "Every branch gets the change. Also deciding whether a branch's supplier is one already on the list."),
    ("buying", "warehouse.purchase-orders", "R", "See purchase orders", ""),
    ("buying", "warehouse.purchase-orders", "W", "Raise purchase orders", ""),
    ("buying", "warehouse.purchase-orders.approve", "X", "Approve purchase orders", "Up to this person's own approval limit."),
    ("buying", "warehouse.receiving", "R", "See supplier receipts", "Including the Purchase Summary."),
    ("buying", "warehouse.receiving", "W", "Receive supplier goods (GRN)", "Supplier invoices, prices and advance tax."),
    # ── branch requests and shipments ──
    ("shipping", "warehouse.requisitions", "R", "See branch requests", ""),
    ("shipping", "warehouse.requisitions", "W", "Raise branch requests", ""),
    ("shipping", "warehouse.requisitions.approve", "X", "Approve branch requests", "Deciding what a branch gets."),
    ("shipping", "warehouse.transfers", "R", "See shipments", "Stock on its way to and from branches."),
    ("shipping", "warehouse.picking", "R", "Picking and dispatch list", ""),
    ("shipping", "warehouse.transfers.dispatch", "X", "Dispatch approved shipments", ""),
    ("shipping", "warehouse.transfers.manage", "X", "Send stock to branches and settle disputes",
     "Also records a receipt for a branch without a server, and shows every branch's stock."),
    # ── the company view ──
    ("company", "executive.dashboard", "R", "Company dashboard", "Sales, profit and stock across every branch."),
    ("company", "executive.branches", "R", "Branch ranking, each branch's own page and Branch Comparison", "Sales, stock, ABC and XYZ, staff and accounts per branch, and stock to move between branches."),
    ("company", "executive.stock", "R", "Stock health and branch stock", ""),
    ("company", "executive.cash", "R", "Cash position", ""),
    ("company", "executive.staffing", "R", "Staffing", ""),
    ("company", "executive.sync-health", "R", "Sync health", "Which branches are reporting in, and how recently."),
    # ── running head office ──
    ("admin", "admin.user-access", "R", "See head office users and their access", ""),
    ("admin", "admin.user-access", "W", "Add users and change what they can do", "Only ever what this person can do themselves."),
    ("admin", "admin.branch-staff", "R", "See branch staff", ""),
    ("admin", "admin.branch-staff", "W", "Add and change branch staff", "Their branches, access and passwords."),
    ("admin", "admin.loyalty", "R", "See members and loyalty rules", ""),
    ("admin", "admin.loyalty", "W", "Change members and the loyalty points rules", ""),
    ("admin", "admin.sync-endpoints", "R", "See branches and their connection", ""),
    ("admin", "admin.sync-endpoints", "W", "Add branches and set them up", "Including the key a branch server is set up with."),
    ("admin", "admin.backup", "R", "See backups", ""),
    ("admin", "admin.backup", "W", "Back up the database", "Backup Now, the daily backup and downloading a backup."),
    ("admin", "admin.backup.restore", "X", "Restore a backup", "Replaces everything since the backup was taken."),
    ("admin", "admin.office-settings", "R", "See the company details and alert timings", ""),
    ("admin", "admin.office-settings", "W", "Change the company details and alert timings", "Alert timings decide when waiting work shows as overdue for everyone."),
    # ── accounts: the books. Whole-book reports always show every account, because half a statement misleads. ──
    ("accounts-books", "accounts.desk", "R", "Accounts Desk", "Money on hand, what's owed both ways and how the month is going."),
    ("accounts-books", "accounts.desk", "X", "Post the records now", "Posts the latest godown records to the books without waiting."),
    ("accounts-books", "accounts.trial-balance", "R", "Trial Balance", "Always the whole book."),
    ("accounts-books", "accounts.income-statement", "R", "Income Statement", "Always the whole book."),
    ("accounts-books", "accounts.balance-sheet", "R", "Balance Sheet", "Always the whole book."),
    ("accounts-books", "accounts.month-by-month", "R", "Month by Month", "Always the whole book."),
    ("accounts-books", "accounts.day-book", "R", "Day Book", "Every posted voucher in full, whatever accounts it uses."),
    ("accounts-books", "accounts.ledger", "R", "Account Ledger", "Only for the accounts they can see, ticked at the bottom."),
    ("accounts-books", "accounts.receivables", "R", "Receivables", "What each branch's credit customers owe."),
    ("accounts-books", "accounts.payables", "R", "Payables", "What each supplier is owed, and for how long."),
    ("accounts-books", "accounts.tax", "R", "Tax reports", "GST and withholding tax."),
    ("accounts-books", "accounts.branch-books", "R", "Every branch's books and the whole company", "Without it, only head office's own books."),
    # ── accounts: vouchers ──
    ("accounts-vouchers", "accounts.vouchers", "R", "See vouchers", "Only vouchers whose every account they can see."),
    ("accounts-vouchers", "accounts.vouchers", "W", "Write and change draft vouchers", "Every line must be an account they can use. Drafts can be cancelled too."),
    ("accounts-vouchers", "accounts.vouchers.post", "X", "Post vouchers", "A posted voucher is in the books; after that it can only be reversed."),
    ("accounts-vouchers", "accounts.vouchers.reverse", "X", "Reverse posted vouchers", "A journal that undoes the voucher line for line. Both stay on the record."),
    ("accounts-vouchers", "accounts.opening-balances", "R", "See the opening balances", ""),
    ("accounts-vouchers", "accounts.opening-balances", "W", "Write the opening balances", "Including filling in the godown stock from the records."),
    ("accounts-vouchers", "accounts.fixed-assets", "R", "Fixed asset register", "Furniture, equipment and vehicles, and what they are worth now."),
    ("accounts-vouchers", "accounts.fixed-assets", "W", "Add, change and dispose of fixed assets", ""),
    ("accounts-vouchers", "accounts.fixed-assets", "X", "Prepare the depreciation run", ""),
    # ── accounts: setting the books up and closing months ──
    ("accounts-setup", "accounts.chart", "R", "Chart of Accounts", "Only the accounts they can see."),
    ("accounts-setup", "accounts.chart", "W", "Change the chart of accounts", "Add account groups, expense heads and bank accounts."),
    ("accounts-setup", "accounts.settings", "R", "Books Settings", "When the books start, the financial year and where each payment lands."),
    ("accounts-setup", "accounts.settings", "W", "Change the books settings", "Moving the books' start or where a payment lands posts everything again."),
    ("accounts-setup", "accounts.period", "X", "Close and reopen months", "Also posting everything again. Nothing dated in a closed month can change."),
]

# The areas every account falls into, the same at every branch (services/accounts_areas.py): (key, label, what's in it).
AREAS: list[tuple[str, str, str]] = [
    ("cash-bank", "Cash, bank and wallets", "Cash in hand, petty cash, bank accounts, card and wallet settlements."),
    ("receivables", "What customers owe", "Each credit customer's own account."),
    ("stock", "Stock", "Stock in trade and stock in transit."),
    ("advances", "Advances, deposits and other assets", "Advances to suppliers and staff, security deposits, prepaid costs."),
    ("tax", "Tax (GST and withholding)", "GST input and output, advance income tax, withholding tax."),
    ("fixed-assets", "Fixed assets and depreciation", "Furniture, equipment, vehicles and their depreciation."),
    ("inter-office", "Head office and branch accounts", "What head office and each branch owe each other."),
    ("payables", "What is owed to suppliers", "Each supplier's own account."),
    ("customer-balances", "Gift vouchers and points owed", "Gift vouchers not spent yet, and loyalty points."),
    ("other-liabilities", "Accrued costs and loans", "Salaries and bills payable, the suspense account, loans."),
    ("equity", "Capital and equity", "Owner's capital, drawings and retained earnings."),
    ("income", "Sales and other income", "Sales, returns, discounts and other income."),
    ("cost-of-sales", "Cost of sales and stock losses", "Cost of goods sold, shortages and stock written off."),
    ("expenses", "Expenses", "Salaries, rent, bills and every other running cost."),
]
AREA_LABELS = {key: label for key, label, _ in AREAS}
# One See and one Use tick per area, shown on the access screen as a grid rather than a list.
AREAS_GROUP = "accounts-areas"
AREA_COLUMNS = [("R", "See"), ("W", "Use")]
for _key, _label, _hint in AREAS:
    ABILITIES.append((AREAS_GROUP, f"accounts.area.{_key}", "R", f"{_label} (see)", _hint))
    ABILITIES.append((AREAS_GROUP, f"accounts.area.{_key}", "W", f"{_label} (use on vouchers)", _hint))

GROUPS: list[tuple[str, str]] = [
    ("godown", "Godown and stock"),
    ("buying", "Buying from suppliers"),
    ("shipping", "Branch requests and shipments"),
    ("company", "The company view"),
    ("admin", "Running head office"),
    ("accounts-books", "Accounts: books and reports"),
    ("accounts-vouchers", "Accounts: vouchers"),
    ("accounts-setup", "Accounts: chart, settings and month end"),
    (AREAS_GROUP, "Accounts: which accounts they can see and use"),
]

# What each resource can be given, from the catalog. A resource missing here can't be given at all.
ACTIONS_OF: dict[str, set[str]] = {}
for _group, _resource, _action, _text, _hint in ABILITIES:
    ACTIONS_OF.setdefault(_resource, set()).add(_action)
LABELS = {f"{resource}:{action}": text for _, resource, action, text, _ in ABILITIES}


def normalise(grants: dict[str, set[str]]) -> dict[str, set[str]]:
    """Changing or deciding something brings seeing it along."""
    out = {resource: set(actions) for resource, actions in grants.items() if actions}
    for actions in out.values():
        if actions & {"W", "X"}:
            actions.add("R")
    return out


# ── the books before they were split ────────────────────────────────────────────────────────────

def _legacy_map(screens: tuple[str, ...], vouchers_w: tuple[str, ...], post_x: tuple[str, ...], extra: dict) -> dict:
    areas = tuple(f"accounts.area.{key}" for key, _, _ in AREAS)
    return {
        ("accounts.books", "R"): {("accounts.desk", "R"), ("accounts.desk", "X")} | {(r, "R") for r in screens + areas},
        ("accounts.vouchers", "W"): {(r, "W") for r in vouchers_w + areas},
        ("accounts.vouchers.post", "X"): {(r, "X") for r in post_x},
        ("accounts.chart", "W"): {("accounts.chart", "W")},
        ("accounts.period", "X"): {("accounts.period", "X"), ("accounts.settings", "W")},
        **extra,
    }


# Head office's own: (old resource, action) -> what it now stands for. Every branch's books stays as it is.
LEGACY_ACCOUNTS = _legacy_map(
    ("accounts.trial-balance", "accounts.income-statement", "accounts.balance-sheet", "accounts.month-by-month", "accounts.day-book",
     "accounts.ledger", "accounts.vouchers", "accounts.opening-balances", "accounts.chart", "accounts.receivables", "accounts.payables",
     "accounts.fixed-assets", "accounts.tax", "accounts.settings"),
    ("accounts.vouchers", "accounts.opening-balances", "accounts.fixed-assets"),
    ("accounts.vouchers.post", "accounts.vouchers.reverse", "accounts.fixed-assets"),
    {},
)
# A branch's, for the branch staff directory and the branch role access head office keeps and sends.
BRANCH_LEGACY_ACCOUNTS = _legacy_map(
    ("accounts.trial-balance", "accounts.income-statement", "accounts.balance-sheet", "accounts.month-by-month", "accounts.day-book",
     "accounts.ledger", "accounts.vouchers", "accounts.opening-balances", "accounts.chart", "accounts.receivables", "accounts.payables",
     "accounts.cheques", "accounts.fixed-assets", "accounts.tax", "accounts.settings"),
    ("accounts.vouchers", "accounts.opening-balances", "accounts.cheques", "accounts.fixed-assets"),
    ("accounts.vouchers.post", "accounts.vouchers.reverse", "accounts.receivables", "accounts.fixed-assets"),
    {
        ("accounts.receivables", "R"): {("accounts.receivables", "R")},
        ("accounts.receivables", "W"): {("accounts.receivables", "W")},
        ("reports", "R"): {("reports.analysis", "R"), ("reports.kpis", "R")},
    },
)
RETIRED_RESOURCES = frozenset({"accounts.books"})


def is_legacy(resources) -> bool:
    return bool(RETIRED_RESOURCES & set(resources))


def translate_legacy(grants: dict[str, set[str]], mapping: dict | None = None) -> dict[str, set[str]]:
    """Old accounts ticks read as the ticks they now stand for, merged with what's there, and "See the books" gone.
    Only for access written before the split (the rollout, or `is_legacy`): on today's access it would hand back a
    tick somebody took away."""
    mapping = LEGACY_ACCOUNTS if mapping is None else mapping
    out = {resource: set(actions) for resource, actions in grants.items()}
    for (resource, action), targets in mapping.items():
        if action in grants.get(resource, set()):
            for target, target_action in targets:
                out.setdefault(target, set()).add(target_action)
    for resource in RETIRED_RESOURCES:
        out.pop(resource, None)
    return normalise(out)


def translate_legacy_resources(resources, mapping: dict | None = None) -> set[str]:
    """The same for a role's standard access, which is a list of resources given in full."""
    return set(translate_legacy({resource: {"R", "W", "X"} for resource in resources}, mapping))
