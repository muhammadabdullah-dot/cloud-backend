"""Services are where ORM calls happen — no repository layer. RBAC enforcement and management both live here."""
from app.core.abilities import ABILITIES, ACTIONS_OF, AREA_COLUMNS, AREAS, AREAS_GROUP, GROUPS, LABELS, normalise
from app.core.resources import resources_for_role
from app.core.resources import RESOURCES
from app.core.security import hash_password
from app.models import Role, RoleDefaultPermission, User, UserPermission
from app.schemas.auth import PermissionOut
from app.schemas.rbac import UserCreate, UserUpdate

_ACTION_FIELDS = {"R": "can_read", "W": "can_write", "X": "can_execute"}


class RbacError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _actions_from_flags(perm: UserPermission) -> list[str]:
    return [action for action, field in _ACTION_FIELDS.items() if getattr(perm, field)]


async def effective_permissions(user: User) -> list[PermissionOut]:
    rows = await UserPermission.filter(user=user)
    return [PermissionOut(resource=r.resource, actions=_actions_from_flags(r)) for r in rows if _actions_from_flags(r)]


async def has_permission(user: User, resource: str, action: str) -> bool:
    field = _ACTION_FIELDS[action]
    perm = await UserPermission.get_or_none(user=user, resource=resource)
    return bool(perm and getattr(perm, field))


async def grants_of(user: User) -> dict[str, set[str]]:
    return {p.resource: set(p.actions) for p in await effective_permissions(user)}


async def abilities_catalog() -> dict:
    """Every tick, grouped as the User Access screen shows it, and each role's standard access as a place to start from.
    The account areas also come as a grid (an area a row, See and Use the columns), which is how they read best."""
    presets = []
    for role in await Role.all().order_by("name"):
        rows = await RoleDefaultPermission.filter(role=role)
        held = {r.resource: {a for a, f in _ACTION_FIELDS.items() if getattr(r, f)} for r in rows} if rows else             {resource: {"R", "W", "X"} for resource in resources_for_role(role.id)}
        presets.append({"roleId": role.id, "label": role.name, "abilities": sorted(
            f"{resource}:{action}" for _, resource, action, _, _ in ABILITIES if action in held.get(resource, set())
        )})
    return {
        "groups": [
            {"key": key, "label": label, "abilities": [
                {"key": f"{resource}:{action}", "resource": resource, "action": action, "label": text, "hint": hint}
                for group, resource, action, text, hint in ABILITIES if group == key
            ], **({"grid": {
                "columns": [{"action": action, "label": text} for action, text in AREA_COLUMNS],
                "rows": [{"resource": f"accounts.area.{area}", "label": text, "hint": hint} for area, text, hint in AREAS],
            }} if key == AREAS_GROUP else {})}
            for key, label in GROUPS
        ],
        "presets": presets,
    }


# The System Admin looks after who can do what without doing the work themselves, so they can give any single ability
# (the project lead: "admin or manager can give access of even a single thing to one user") without holding it,
# which would also put them on every approval and alert. Anyone else who edits access gives only what they hold.
GRANTS_ANYTHING_ROLE = "system-admin"


async def _refuse_beyond_caller(caller: User | None, grants: dict[str, set[str]]) -> None:
    """Nobody gives what they don't have themselves, except the System Admin."""
    if caller is None or caller.role_id == GRANTS_ANYTHING_ROLE:
        return
    mine = await grants_of(caller)
    missing = sorted(f"{r}:{a}" for r, actions in grants.items() for a in actions if a not in mine.get(r, set()))
    if missing:
        # Name the abilities; a "see it" that only comes along with one of them isn't worth listing.
        named = [LABELS[m] for m in missing if m in LABELS] or [LABELS.get(f"{m.split(':')[0]}:R", m) for m in missing]
        names = ", ".join(named[:4])
        raise RbacError(f"You can't give access you don't have yourself: {names}{' …' if len(named) > 4 else ''}.", status=403)


# What stock cost the company is buying-side information: the people who receive it, value it or own the
# Item master see it. Floor staff (a Picker) find and move Items without it.
COST_VIEWERS = (
    ("warehouse.items.manage", "W"), ("warehouse.dashboard", "R"), ("warehouse.receiving", "R"),
    ("executive.stock", "R"), ("executive.dashboard", "R"),
)


async def can_see_costs(user: User) -> bool:
    for resource, action in COST_VIEWERS:
        if await has_permission(user, resource, action):
            return True
    return False


def list_resources() -> list[str]:
    return RESOURCES


async def list_users() -> list[User]:
    return await User.all().order_by("name")


async def list_roles() -> list[Role]:
    return await Role.all().order_by("name")


async def create_user(data: UserCreate, caller: User | None = None) -> User:
    """Creates the account AND materializes its role's permission template into real
    UserPermission rows — the same thing the seed does. Without that second half the new user can
    sign in and then see nothing, which reads as a broken login rather than a missing grant.

    Someone other than the System Admin can't make a System Admin, and gives the new person only the part of the
    role's standard access they hold themselves, exactly as "Start from" does on the screen."""
    if not await Role.exists(id=data.roleId):
        raise RbacError(f"No such role: {data.roleId}", status=422)
    if await User.filter(email=data.email).exists():
        raise RbacError(f"A user with email {data.email} already exists", status=409)
    _refuse_system_admin_role(caller, data.roleId)

    templates = await RoleDefaultPermission.filter(role_id=data.roleId)
    wanted: dict[str, set[str]] = (
        {t.resource: {a for a, f in _ACTION_FIELDS.items() if getattr(t, f)} for t in templates}
        if templates
        # A role created outside the seed has no template rows yet; fall back to the code-level
        # template so the account is still usable rather than silently permission-less.
        else {resource: {"R", "W", "X"} for resource in resources_for_role(data.roleId)}
    )
    if caller is not None and caller.role_id != GRANTS_ANYTHING_ROLE:
        mine = await grants_of(caller)
        wanted = normalise({r: actions & mine.get(r, set()) for r, actions in wanted.items()})

    user = await User.create(
        name=data.name, email=data.email, password_hash=hash_password(data.password), role_id=data.roleId,
    )
    for resource, actions in sorted(wanted.items()):
        if actions:
            await UserPermission.create(
                user=user, resource=resource, can_read="R" in actions, can_write="W" in actions,
                can_execute="X" in actions, granted_by=caller,
            )
    return user


def _refuse_system_admin_role(caller: User | None, role_id: str | None, current_role: str | None = None) -> None:
    """A System Admin can give any tick, so making (or unmaking) one is itself something only a System Admin gives."""
    if caller is None or caller.role_id == GRANTS_ANYTHING_ROLE or role_id == current_role:
        return
    if role_id == GRANTS_ANYTHING_ROLE:
        raise RbacError("Only a System Admin can make someone a System Admin.", status=403)
    if current_role == GRANTS_ANYTHING_ROLE:
        raise RbacError("Only a System Admin can change a System Admin's role.", status=403)


async def _refuse_limit_above_caller(caller: User, user: User, new_role: str, new_limit) -> None:
    """Raising someone's purchase order approval limit is held to the editor's own, as a discount limit is at a branch."""
    if caller.role_id == GRANTS_ANYTHING_ROLE:
        return
    from app.services.purchasing_service import po_limit_of, rs

    before = po_limit_of(user)
    after = new_limit if new_limit is not None else po_limit_of(User(role_id=new_role, po_limit=None))
    raised = before is not None and (after is None or after > before)
    mine = po_limit_of(caller)
    if raised and mine is not None and (after is None or after > mine):
        raise RbacError(f"You can't set a purchase order approval limit above your own ({rs(mine)}).", status=403)


async def update_user(user_id: str, data: UserUpdate, caller: User) -> User | None:
    user = await User.get_or_none(id=user_id)
    if not user:
        return None

    fields = data.model_dump(exclude_unset=True)
    is_self = str(user.id) == str(caller.id)
    if "active" in fields and is_self and fields["active"] is False:
        raise RbacError("You can't switch off your own account.", status=403)
    new_role = fields.get("roleId") or user.role_id
    # Your role and your limit are part of your access: somebody else changes them, as with the ticks.
    if is_self and new_role != user.role_id:
        raise RbacError("You can't change your own role. Ask another admin.", status=403)
    if is_self and "poLimit" in fields and fields["poLimit"] != user.po_limit:
        raise RbacError("You can't change your own purchase order approval limit. Ask another admin.", status=403)
    if new_role != user.role_id:
        if not await Role.exists(id=new_role):
            raise RbacError(f"No such role: {new_role}", status=422)
        _refuse_system_admin_role(caller, new_role, user.role_id)
    if "poLimit" in fields or new_role != user.role_id:
        await _refuse_limit_above_caller(caller, user, new_role, fields["poLimit"] if "poLimit" in fields else user.po_limit)
    user.role_id = new_role
    if "email" in fields and fields["email"] is not None:
        if await User.filter(email=fields["email"]).exclude(id=user.id).exists():
            raise RbacError(f"A user with email {fields['email']} already exists", status=409)
        user.email = fields["email"]
    if "name" in fields and fields["name"] is not None:
        user.name = fields["name"].strip() or user.name
    if "active" in fields and fields["active"] is not None:
        user.active = fields["active"]
    if "password" in fields and fields["password"] is not None:
        user.password_hash = hash_password(fields["password"])
    if "poLimit" in fields:
        user.po_limit = fields["poLimit"]

    await user.save()
    return user


async def get_user_permissions(user_id: str) -> list[PermissionOut] | None:
    user = await User.get_or_none(id=user_id)
    if not user:
        return None
    return await effective_permissions(user)


async def replace_user_permissions(
    target_user_id: str, grants: list[PermissionOut], granted_by: User
) -> list[PermissionOut] | None:
    target = await User.get_or_none(id=target_user_id)
    if not target:
        return None
    unknown = sorted({g.resource for g in grants if g.resource not in ACTIONS_OF and g.actions})
    if unknown:
        raise RbacError("One of those ticks isn't something head office gives any more. Refresh the page and try again.", status=422)
    # Only what the screen offers for each: seeing it, and whichever of changing or deciding it has.
    wanted = normalise({
        g.resource: {a for a in g.actions if a in ACTIONS_OF[g.resource] or a == "R"}
        for g in grants
    })
    # Only what changes has to be within the caller's own access: someone keeps what they already had.
    current = await grants_of(target)
    added = {r: actions - current.get(r, set()) for r, actions in wanted.items()}
    await _refuse_beyond_caller(granted_by, {r: a for r, a in added.items() if a})
    await UserPermission.filter(user=target).delete()
    for resource, actions in sorted(wanted.items()):
        await UserPermission.create(
            user=target,
            resource=resource,
            can_read="R" in actions,
            can_write="W" in actions,
            can_execute="X" in actions,
            granted_by=granted_by,
        )
    return await effective_permissions(target)
