"""Fixed resource catalog for the Cloud Server — mirrors frontend-baseline.md §3.1's route table.

Not user-editable data. Adding a new screen/action means adding a line here, not a migration.
"""

RESOURCES: list[str] = [
    "warehouse.dashboard",
    "warehouse.bins",
    # Adding racks, sizing them, and changing or switching off bins.
    "warehouse.bins.manage",
    # Putting stock away and moving it between bins — the Picker's job as much as the manager's.
    "warehouse.bins.move",
    "warehouse.requisitions",
    "warehouse.requisitions.approve",
    "warehouse.transfers",
    "warehouse.transfers.dispatch",
    # Deciding to send stock to a branch, recording a receipt for a branch without a server, closing a
    # short-receipt dispute. Dispatching what's already approved is `.dispatch` above.
    "warehouse.transfers.manage",
    "warehouse.receiving",
    # Purchase orders to suppliers, and approving them within a per-person limit.
    "warehouse.purchase-orders",
    "warehouse.purchase-orders.approve",
    "warehouse.picking",
    "warehouse.counts",
    "warehouse.counts.approve",
    "warehouse.decisions",
    # The Item master: viewing it, and adding / editing / importing Items and their files.
    "warehouse.items",
    "warehouse.items.manage",
    "executive.dashboard",
    "executive.branches",
    "executive.stock",
    "executive.cash",
    "executive.staffing",
    "executive.sync-health",
    "admin.sync-endpoints",
    "admin.user-access",
    # Branch staff across every branch: add, assign, change role and access, reset, switch off.
    "admin.branch-staff",
    # D.Marina members across every branch, and the loyalty points rules every branch uses.
    "admin.loyalty",
    # Backing head office's database up (Backup Now, the daily backup, downloads) and putting a backup back.
    "admin.backup",
    "admin.backup.restore",
    # Head office's books: seeing them; every branch's books and the company's together; making vouchers; posting and
    # reversing them; the chart of accounts; closing months.
    "accounts.books",
    "accounts.branch-books",
    "accounts.vouchers",
    "accounts.vouchers.post",
    "accounts.chart",
    "accounts.period",
]

# Resource that gates the delegated permission-management endpoints themselves (contracts.md §2.6).
RBAC_MANAGEMENT_RESOURCE = "admin.user-access"


def _matches(resource: str, prefix: str) -> bool:
    return resource == prefix or resource.startswith(prefix + ".")


# role slug -> module prefixes it holds today (frontend-baseline.md §1.1), plus any explicit extra
# grant, minus any resource the role must never hold even though a prefix would otherwise cover it.
#
# Executive gets an explicit extra grant on admin.user-access (new capability, confirmed
# 2026-09-11) — System Admin needs no extra entry since its "admin" prefix already covers it.
#
# `exclude` is a policy statement, not a preference. A Picker inherits all of `warehouse.*`, which
# swept in the three authority resources: approving a branch's requisition (a Warehouse Manager's
# commercial decision), signing off a cycle count (the person who counts must not be the person
# who accepts the variance), and the Decisions queue that exists to hold both. Dispatch is NOT
# excluded — picking and dispatching is exactly the Picker's job.
ROLE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "warehouse-manager": {"prefixes": ["warehouse"], "extra": [], "exclude": []},
    # A Picker / Packer is a floor worker (actors-and-interfaces.md §19): pick, pack, dispatch what's been
    # approved, count shelves, and find Items and bins. Everything that spends money, commits stock to a
    # branch, changes master data or signs something off belongs to the Warehouse Manager (§18).
    "picker": {
        "prefixes": ["warehouse"],
        "extra": [],
        "exclude": [
            "warehouse.dashboard",            # stock value and pending decisions: a manager's view
            "warehouse.receiving",            # supplier invoices, prices, advance tax
            "warehouse.purchase-orders",      # spending money with suppliers
            "warehouse.purchase-orders.approve",
            "warehouse.requisitions",         # a branch's request is the manager's to decide
            "warehouse.requisitions.approve",
            "warehouse.transfers.manage",     # choosing to send stock, closing disputes
            "warehouse.counts.approve",       # whoever counts must not accept the variance
            "warehouse.decisions",
            "warehouse.items.manage",
            "warehouse.bins.manage",
        ],
    },
    # The Executive holds the books until an Accountant is added — like the Branch Manager at a branch.
    "executive": {"prefixes": ["executive", "accounts"], "extra": ["admin.user-access", "admin.loyalty", "warehouse.purchase-orders.approve"], "exclude": []},
    "accountant": {"prefixes": ["accounts"], "extra": ["executive.branches"], "exclude": []},
    "system-admin": {"prefixes": ["admin"], "extra": [], "exclude": []},
}

_EMPTY_TEMPLATE: dict[str, list[str]] = {"prefixes": [], "extra": [], "exclude": []}


def resources_for_role(role_id: str) -> set[str]:
    template = ROLE_TEMPLATES.get(role_id, _EMPTY_TEMPLATE)
    granted = {r for r in RESOURCES if any(_matches(r, p) for p in template["prefixes"])}
    granted.update(template.get("extra", []))
    granted.difference_update(template.get("exclude", []))
    return granted


def excluded_resources_for_role(role_id: str) -> set[str]:
    """Resources this role must never hold — re-enforced on every startup, unlike ordinary grants
    which a System Admin is free to customize per user."""
    return set(ROLE_TEMPLATES.get(role_id, _EMPTY_TEMPLATE).get("exclude", []))
