"""Branch staff, both ways. See models/branch_staff.py for who wins when both sides edit.

Up: a branch reports an account (created, edited, access changed) as a `Staff` event. The higher
revision is kept here and passed on to the person's other branches; a lower one is answered by sending
the branch head office's version, so the two converge.

Down: every change made here raises the revision and goes to each branch the person is assigned to as
`staff.upsert`; a branch they're taken off gets `staff.remove`.
"""
import secrets
import uuid
from datetime import datetime, timezone

from tortoise.transactions import atomic

from app.core.security import hash_password
from app.models import Branch, BranchManifest, BranchRoleTemplate, BranchStaff, BranchStaffAssignment, User
from app.services import downstream_service


class StaffError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _norm_permissions(perms) -> list[dict]:
    out = []
    for p in perms or []:
        resource = str(p.get("resource") or "").strip()
        actions = sorted({a for a in (p.get("actions") or []) if a in ("R", "W", "X")})
        if resource and actions:
            out.append({"resource": resource, "actions": actions})
    return sorted(out, key=lambda p: p["resource"])


def payload(staff: BranchStaff) -> dict:
    return {
        "id": staff.id, "name": staff.name, "email": staff.email, "roleId": staff.role_id, "active": staff.active,
        "passwordHash": staff.password_hash, "permissions": _norm_permissions(staff.permissions), "rev": staff.rev,
        "lastChangedAt": staff.last_changed_at, "lastChangedBy": staff.last_changed_by,
    }


def _same(staff: BranchStaff, incoming: dict) -> bool:
    return (
        staff.name == incoming.get("name") and staff.email == (incoming.get("email") or "").lower()
        and staff.role_id == incoming.get("roleId") and bool(staff.active) == bool(incoming.get("active"))
        and staff.password_hash == incoming.get("passwordHash")
        and _norm_permissions(staff.permissions) == _norm_permissions(incoming.get("permissions"))
    )


async def _branch_ids(staff: BranchStaff) -> list[str]:
    return [str(a.branch_id) for a in await BranchStaffAssignment.filter(staff=staff)]


async def _send(staff: BranchStaff, branch_ids: list[str]) -> None:
    body = payload(staff)
    for branch_id in branch_ids:
        await downstream_service.enqueue(branch_id, "staff.upsert", {"user": body})


# ── up: what a branch reports ───────────────────────────────────────────────────────────────────

@atomic()
async def apply_from_branch(branch: Branch, user: dict) -> str:
    """Returns what happened: created · updated · kept-cloud · unchanged."""
    user_id = str(user.get("id") or "").strip()
    if not user_id or not user.get("email") or not user.get("passwordHash"):
        raise StaffError("A staff event needs an id, an email and a password hash.")
    incoming_rev = int(user.get("rev") or 1)
    changed_by = f"{branch.code} · {user.get('changedBy') or 'branch'}"
    staff = await BranchStaff.get_or_none(id=user_id)

    if staff is None:
        staff = await BranchStaff.create(
            id=user_id, name=user.get("name") or user["email"], email=str(user["email"]).lower(),
            role_id=user.get("roleId") or "", active=bool(user.get("active", True)), password_hash=user["passwordHash"],
            permissions=_norm_permissions(user.get("permissions")), rev=incoming_rev,
            last_changed_at="branch", last_changed_by=changed_by,
        )
        await BranchStaffAssignment.get_or_create(staff=staff, branch=branch)
        return "created"

    # A branch reporting someone it holds, whose assignment head office doesn't have yet (an account
    # that existed on the branch before this directory did), is recorded as working there.
    if not await BranchStaffAssignment.exists(staff=staff):
        await BranchStaffAssignment.get_or_create(staff=staff, branch=branch)
    elif str(branch.id) not in await _branch_ids(staff):
        # Head office took this person off that branch; the branch's change doesn't put them back.
        if user.get("active", True):
            await downstream_service.enqueue(str(branch.id), "staff.remove", {"userId": staff.id, "rev": staff.rev})
        return "not-assigned"

    if _same(staff, user):
        if incoming_rev > staff.rev:
            staff.rev = incoming_rev
            await staff.save(update_fields=["rev"])
        return "unchanged"

    if incoming_rev > staff.rev:
        staff.name = user.get("name") or staff.name
        staff.email = str(user.get("email") or staff.email).lower()
        staff.role_id = user.get("roleId") or staff.role_id
        staff.active = bool(user.get("active", staff.active))
        staff.password_hash = user.get("passwordHash") or staff.password_hash
        staff.permissions = _norm_permissions(user.get("permissions"))
        staff.rev = incoming_rev
        staff.last_changed_at = "branch"
        staff.last_changed_by = changed_by
        await staff.save()
        # The person's other branches get the change too.
        await _send(staff, [b for b in await _branch_ids(staff) if b != str(branch.id)])
        return "updated"

    # The branch is behind head office (or edited at the same revision): head office's version goes
    # back to it, so both ends end up holding the same account.
    if str(branch.id) in await _branch_ids(staff):
        await _send(staff, [str(branch.id)])
    return "kept-cloud"


# ── down: what head office changes ──────────────────────────────────────────────────────────────

async def list_staff(branch_id: str | None = None, q: str | None = None) -> list[tuple[BranchStaff, list[Branch]]]:
    qs = BranchStaff.all()
    if branch_id:
        qs = qs.filter(assignments__branch_id=branch_id)
    if q:
        term = q.strip()
        from tortoise.expressions import Q
        qs = qs.filter(Q(name__icontains=term) | Q(email__icontains=term))
    staff = await qs.distinct().order_by("name")
    out = []
    for s in staff:
        assignments = await BranchStaffAssignment.filter(staff=s).prefetch_related("branch")
        out.append((s, [a.branch for a in assignments]))
    return out


async def manifest() -> tuple[list[str], list[dict]]:
    """The grantable resources and the roles, from the most recently reported branch manifest."""
    latest = await BranchManifest.all().order_by("-updated_at").first()
    if not latest:
        return [], []
    roles = latest.roles or []
    templates = {t.role_id: t for t in await BranchRoleTemplate.all()}
    merged = []
    for role in roles:
        template = templates.get(role.get("id"))
        merged.append({
            **role,
            "resources": template.resources if template else role.get("resources", []),
            "managedByHeadOffice": template is not None,
        })
    return latest.resources or [], merged


async def _template_for(role_id: str) -> list[str]:
    template = await BranchRoleTemplate.get_or_none(role_id=role_id)
    if template:
        return list(template.resources)
    _, roles = await manifest()
    role = next((r for r in roles if r.get("id") == role_id), None)
    return list(role.get("resources", [])) if role else []


def _excluded(roles: list[dict], role_id: str) -> set[str]:
    role = next((r for r in roles if r.get("id") == role_id), None)
    return set(role.get("exclude", [])) if role else set()


async def _check_branches(branch_ids: list[str]) -> list[Branch]:
    branches = await Branch.filter(id__in=branch_ids)
    if len(branches) != len(set(branch_ids)):
        raise StaffError("One of those branches doesn't exist.")
    return branches


@atomic()
async def create(admin: User, data: dict) -> BranchStaff:
    email = (data.get("email") or "").strip().lower()
    if await BranchStaff.filter(email=email).exists():
        raise StaffError(f"{email} is already in the staff directory. Give that person another branch instead of a second account.", 409)
    resources, roles = await manifest()
    role_id = data["roleId"]
    if roles and not any(r.get("id") == role_id for r in roles):
        raise StaffError(f"No such branch role: {role_id}", 422)
    branch_ids = list(dict.fromkeys(data.get("branchIds") or []))
    if not branch_ids:
        raise StaffError("Pick at least one branch for this person.")
    await _check_branches(branch_ids)
    permissions = data.get("permissions")
    if permissions is None:
        excluded = _excluded(roles, role_id)
        permissions = [{"resource": r, "actions": ["R", "W", "X"]} for r in await _template_for(role_id) if r not in excluded]
    staff = await BranchStaff.create(
        id=str(uuid.uuid4()), name=data["name"].strip(), email=email, role_id=role_id, active=True,
        password_hash=hash_password(data["password"]), permissions=_norm_permissions(permissions), rev=1,
        last_changed_at="cloud", last_changed_by=admin.name,
    )
    for branch_id in branch_ids:
        await BranchStaffAssignment.create(staff=staff, branch_id=branch_id)
    await _send(staff, branch_ids)
    return staff


@atomic()
async def update(admin: User, staff_id: str, data: dict) -> BranchStaff:
    staff = await BranchStaff.get_or_none(id=staff_id)
    if not staff:
        raise StaffError("That person isn't in the staff directory.", 404)
    _, roles = await manifest()
    if data.get("email") is not None:
        email = data["email"].strip().lower()
        if await BranchStaff.filter(email=email).exclude(id=staff.id).exists():
            raise StaffError(f"{email} already belongs to someone else in the directory.", 409)
        staff.email = email
    if data.get("name") is not None:
        staff.name = data["name"].strip()
    if data.get("active") is not None:
        staff.active = bool(data["active"])
    if data.get("password"):
        staff.password_hash = hash_password(data["password"])
    role_changed = data.get("roleId") is not None and data["roleId"] != staff.role_id
    if role_changed:
        if roles and not any(r.get("id") == data["roleId"] for r in roles):
            raise StaffError(f"No such branch role: {data['roleId']}", 422)
        staff.role_id = data["roleId"]
    if data.get("permissions") is not None:
        excluded = _excluded(roles, staff.role_id)
        staff.permissions = _norm_permissions([p for p in data["permissions"] if p.get("resource") not in excluded])
    elif role_changed:
        # A new role starts from that role's standard access, as a new starter in it would.
        excluded = _excluded(roles, staff.role_id)
        staff.permissions = [{"resource": r, "actions": ["R", "W", "X"]} for r in sorted(await _template_for(staff.role_id)) if r not in excluded]

    before = set(await _branch_ids(staff))
    after = before
    if data.get("branchIds") is not None:
        after = set(data["branchIds"])
        if not after:
            raise StaffError("Leave the person on at least one branch, or switch them off instead.")
        await _check_branches(list(after))
        for branch_id in after - before:
            await BranchStaffAssignment.create(staff=staff, branch_id=branch_id)
        await BranchStaffAssignment.filter(staff=staff, branch_id__in=list(before - after)).delete()

    staff.rev += 1
    staff.last_changed_at = "cloud"
    staff.last_changed_by = admin.name
    await staff.save()
    await _send(staff, sorted(after))
    for branch_id in before - after:
        await downstream_service.enqueue(branch_id, "staff.remove", {"userId": staff.id, "rev": staff.rev})
    return staff


@atomic()
async def reset_password(admin: User, staff_id: str) -> tuple[BranchStaff, str]:
    """A new random password, shown once to the admin, and sent to every branch the person works at."""
    temporary = f"{secrets.token_hex(3)}-{secrets.token_hex(3)}"
    staff = await update(admin, staff_id, {"password": temporary})
    return staff, temporary


@atomic()
async def set_role_template(admin: User, role_id: str, resources: list[str], apply_to_existing: bool) -> tuple[BranchRoleTemplate, int]:
    known, roles = await manifest()
    role = next((r for r in roles if r.get("id") == role_id), None)
    if not role:
        raise StaffError(f"No such branch role: {role_id}", 404)
    excluded = set(role.get("exclude", []))
    unknown = [r for r in resources if known and r not in known]
    if unknown:
        raise StaffError(f"Branches don't have a screen or action called {unknown[0]}.", 422)
    clean = sorted(set(resources) - excluded)
    template, _ = await BranchRoleTemplate.get_or_create(role_id=role_id, defaults={"name": role.get("name") or role_id})
    template.resources = clean
    template.updated_by = admin.name
    await template.save()
    for branch in await Branch.filter(verified_at__not_isnull=True):
        await downstream_service.enqueue(str(branch.id), "role.template", {"roleId": role_id, "resources": clean})
    changed = 0
    if apply_to_existing:
        for staff in await BranchStaff.filter(role_id=role_id):
            await update(admin, staff.id, {"permissions": [{"resource": r, "actions": ["R", "W", "X"]} for r in clean]})
            changed += 1
    return template, changed


async def save_manifest(branch: Branch, resources: list[str], roles: list[dict]) -> None:
    existing = await BranchManifest.get_or_none(branch=branch)
    if existing:
        existing.resources = resources
        existing.roles = roles
        await existing.save()
    else:
        await BranchManifest.create(branch=branch, resources=resources, roles=roles)


def now() -> datetime:
    return datetime.now(timezone.utc)
