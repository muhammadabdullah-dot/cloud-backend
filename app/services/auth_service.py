from app.core.security import create_access_token, verify_password
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
