from app.core.security import create_access_token, hash_password, verify_password
from app.models import User
from app.schemas.auth import LoginResponse, UserOut
from app.services.rbac_service import effective_permissions


async def authenticate(email: str, password: str) -> User | None:
    user = await User.get_or_none(email=email.strip().lower(), active=True).prefetch_related("role")
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def build_login_response(user: User) -> LoginResponse:
    await user.fetch_related("role")
    token = create_access_token(str(user.id))
    permissions = await effective_permissions(user)
    user_out = UserOut(id=str(user.id), name=user.name, email=user.email, roleId=user.role_id, landing=user.role.landing)
    return LoginResponse(token=token, user=user_out, permissions=permissions)


MIN_PASSWORD_LENGTH = 6


class AccountError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def check_new_password(current_hash: str | None, new: str, confirm: str) -> None:
    if len(new) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"The new password needs at least {MIN_PASSWORD_LENGTH} characters.")
    if new != confirm:
        raise AccountError("The new password and its confirmation don't match.")
    if current_hash and verify_password(new, current_hash):
        raise AccountError("The new password is the same as your current one.")


async def change_own_password(user: User, current: str, new: str, confirm: str) -> None:
    if not verify_password(current, user.password_hash):
        raise AccountError("Your current password isn't right.")
    check_new_password(user.password_hash, new, confirm)
    user.password_hash = hash_password(new)
    await user.save()


async def change_own_name(user: User, name: str) -> None:
    """Only whoever manages head office accounts changes a name — their own included."""
    from app.services.rbac_service import has_permission

    if not await has_permission(user, "admin.user-access", "W"):
        raise AccountError("Ask the System Admin to change your name.", status=403)
    user.name = name.strip()
    await user.save()
