from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise

from app.core.config import TORTOISE_ORM, settings
from app.core.network import get_lan_ip
from app.middlewares.error_handler import register_error_handlers
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.rbac import router as rbac_router
from app.routes.rbac import users_router
from app.services.seed_service import seed_if_empty

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(rbac_router)
app.include_router(users_router)

register_tortoise(app, config=TORTOISE_ORM, generate_schemas=False, add_exception_handlers=True)


@app.on_event("startup")
async def _seed() -> None:
    await seed_if_empty()


@app.on_event("startup")
async def _print_banner() -> None:
    lan_ip = get_lan_ip()
    print(
        "\n"
        f"  {settings.app_name}\n"
        f"  Local:   http://127.0.0.1:{settings.port}\n"
        f"  Network: http://{lan_ip}:{settings.port}   (for other devices on this LAN)\n",
        flush=True,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)
