"""Per-request auth/authorization guard. Implemented as FastAPI dependencies (not ASGI middleware)
since a required resource/action is route-specific — Depends is the idiomatic way to parameterize that.
"""
import jwt
from fastapi import Depends, Header, HTTPException, status

from app.core.security import decode_access_token
from app.models import User
from app.services.rbac_service import has_permission


async def get_current_user(authorization: str | None = Header(default=None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = await User.get_or_none(id=payload["sub"], active=True)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    return user


def require_permission(resource: str, action: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if not await has_permission(user, resource, action):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing '{action}' on '{resource}'")
        return user

    return checker


def require_any_permission(*pairs: tuple[str, str]):
    """Passes if the caller holds ANY of the given (resource, action) pairs — for read-only data
    that more than one module legitimately needs. The branch list is the clearest case: it is
    registered under Admin, but Executive ranks by it and Warehouse ships to it, and none of those
    three resources is a subset of another. Writes always stay on a single owning resource."""

    async def checker(user: User = Depends(get_current_user)) -> User:
        for resource, action in pairs:
            if await has_permission(user, resource, action):
                return user
        wanted = ", ".join(f"{a} on {r}" for r, a in pairs)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing any of: {wanted}")

    return checker
