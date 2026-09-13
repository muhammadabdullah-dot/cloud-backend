"""Fixed resource catalog for the Cloud Server — mirrors frontend-baseline.md §3.1's route table.

Not user-editable data. Adding a new screen/action means adding a line here, not a migration.
"""

RESOURCES: list[str] = [
    "warehouse.dashboard",
    "warehouse.bins",
    "warehouse.requisitions",
    "warehouse.requisitions.approve",
    "warehouse.transfers",
    "warehouse.transfers.dispatch",
    "warehouse.receiving",
    "warehouse.picking",
    "warehouse.counts",
    "warehouse.counts.approve",
    "warehouse.decisions",
    "executive.dashboard",
    "executive.branches",
    "executive.stock",
    "executive.cash",
    "executive.staffing",
    "executive.sync-health",
    "admin.sync-endpoints",
    "admin.user-access",
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
    "picker": {
        "prefixes": ["warehouse"],
        "extra": [],
        "exclude": ["warehouse.requisitions.approve", "warehouse.counts.approve", "warehouse.decisions"],
    },
    "executive": {"prefixes": ["executive"], "extra": ["admin.user-access"], "exclude": []},
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
