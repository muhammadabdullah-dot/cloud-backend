from fastapi import APIRouter, Depends

from app.controllers import auth_controller
from app.middlewares.auth import get_current_user
from app.models import User
from app.schemas.auth import LoginRequest, LoginResponse, MeResponse

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
