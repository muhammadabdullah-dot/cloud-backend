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


# role slug -> module prefixes it holds today (frontend-baseline.md §1.1), plus any explicit extra grant.
# Executive gets an explicit extra grant on admin.user-access (new capability, confirmed 2026-09-11) —
# System Admin needs no extra entry since its "admin" prefix already covers admin.user-access.
ROLE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "warehouse-manager": {"prefixes": ["warehouse"], "extra": []},
    "picker": {"prefixes": ["warehouse"], "extra": []},
    "executive": {"prefixes": ["executive"], "extra": ["admin.user-access"]},
    "system-admin": {"prefixes": ["admin"], "extra": []},
}


def resources_for_role(role_id: str) -> set[str]:
    template = ROLE_TEMPLATES.get(role_id, {"prefixes": [], "extra": []})
    granted = {r for r in RESOURCES if any(_matches(r, p) for p in template["prefixes"])}
    granted.update(template["extra"])
    return granted
