from fastapi import APIRouter, Depends

from app.controllers import auth_controller
from app.middlewares.auth import get_current_user
from app.models import User
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse, MeResponse, MyNameRequest

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    return await auth_controller.login(payload)


@router.post("/auth/logout")
async def logout() -> dict:
    return await auth_controller.logout()


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(get_current_user)) -> MeResponse:
    return await auth_controller.me(user)


@router.post("/auth/change-password")
async def change_password(payload: ChangePasswordRequest, user: User = Depends(get_current_user)) -> dict:
    """Anyone changes their own password — with the current one, and the new one typed twice."""
    return await auth_controller.change_password(user, payload)


@router.patch("/me", response_model=MeResponse)
async def change_my_name(payload: MyNameRequest, user: User = Depends(get_current_user)) -> MeResponse:
    """The System Admin corrects their own name. Everyone else asks the System Admin."""
    return await auth_controller.change_name(user, payload)
