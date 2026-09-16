from fastapi import HTTPException, status

from app.models import User
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse, UserOut
from app.services import auth_service
from app.services.rbac_service import effective_permissions


async def login(payload: LoginRequest) -> LoginResponse:
    user = await auth_service.authenticate(payload.email, payload.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return await auth_service.build_login_response(user)


async def logout() -> dict:
    return {"detail": "logged out"}


async def me(user: User) -> MeResponse:
    await user.fetch_related("role")
    permissions = await effective_permissions(user)
    user_out = UserOut(id=str(user.id), name=user.name, email=user.email, roleId=user.role_id, landing=user.role.landing)
    return MeResponse(user=user_out, permissions=permissions)


async def change_password(user: User, payload) -> dict:
    try:
        await auth_service.change_own_password(user, payload.currentPassword, payload.newPassword, payload.confirmPassword)
    except auth_service.AccountError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return {"detail": "Password changed"}


async def change_name(user: User, payload) -> MeResponse:
    try:
        await auth_service.change_own_name(user, payload.name)
    except auth_service.AccountError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    return await me(user)
