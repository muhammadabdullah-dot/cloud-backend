from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise

from app.core.config import TORTOISE_ORM, settings
from app.core.network import get_lan_ip
from app.middlewares.error_handler import register_error_handlers
from app.routes.auth import router as auth_router
from app.routes.branches import router as branches_router
from app.routes.registration import admin_router as branch_pairing_router
from app.routes.registration import router as registration_router
from app.routes.registration import sync_router
from app.routes.executive import router as executive_router
from app.routes.health import router as health_router
from app.routes.rbac import router as rbac_router
from app.routes.rbac import users_router
from app.routes.warehouse import router as warehouse_router
from app.services.seed_service import seed_branches_if_empty, seed_if_empty, sync_role_resource_grants
from app.services.warehouse_seed import seed_warehouse_if_empty

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
app.include_router(branches_router)
# Pairing sits on /branches/{id}/pairing, so it must be included after the branch router for the
# path ordering to read naturally in the docs; FastAPI matches on the full path either way.
app.include_router(branch_pairing_router)
app.include_router(registration_router)
app.include_router(sync_router)
app.include_router(warehouse_router)
app.include_router(executive_router)

register_tortoise(app, config=TORTOISE_ORM, generate_schemas=False, add_exception_handlers=True)


@app.on_event("startup")
async def _seed() -> None:
    await seed_if_empty()
    await seed_branches_if_empty()
    # Branches first — the warehouse seed points requisitions and transfers at them.
    await seed_warehouse_if_empty()
    # Backfills resources added to core/resources.py onto users who were seeded before they
    # existed — role templates only materialize into real UserPermission rows at User.create()
    # time, so without this a new screen would never reach an existing account.
    await sync_role_resource_grants()


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
