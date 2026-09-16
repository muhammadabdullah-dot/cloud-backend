from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import register_tortoise

from app.core.config import TORTOISE_ORM, settings
from app.core.frontend import FrontendMiddleware, default_dist
from app.core.network import get_lan_ip
from app.core import logs
from app.middlewares.error_handler import register_error_handlers
from app.routes.client_errors import router as client_errors_router
from app.routes.accounts import router as accounts_router
from app.routes.activity import router as activity_router
from app.routes.alerts import router as alerts_router
from app.routes.purchasing import router as purchasing_router
from app.routes.auth import router as auth_router
from app.routes.branch_staff import router as branch_staff_router
from app.routes.branch_stock import router as branch_stock_router
from app.routes.branch_staff import sync_router as downstream_sync_router
from app.routes.branches import router as branches_router
from app.routes.items import router as items_router
from app.routes.loyalty import router as loyalty_router
from app.routes.racks import router as racks_router
from app.routes.putaway import router as putaway_router
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
# The built cloud-app on this same port — added last so it's the outermost layer: a browser opening a
# page gets the app before any API route or middleware sees the request.
FRONTEND_DIST = None if settings.frontend_dir.lower() == "off" else (Path(settings.frontend_dir) if settings.frontend_dir else default_dist("cloud-app"))

register_error_handlers(app)
logs.setup()


@app.on_event("startup")
async def _start_log() -> None:
    """First of the startup steps, so anything that fails while starting is in the log too."""
    path = logs.setup()
    logs.log.info("%s starting on port %s (log: %s)", settings.app_name, settings.port, path)


@app.on_event("shutdown")
async def _end_log() -> None:
    logs.log.info("%s stopped", settings.app_name)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(rbac_router)
app.include_router(users_router)
app.include_router(branches_router)
app.include_router(activity_router)
app.include_router(alerts_router)
app.include_router(purchasing_router)
app.include_router(branch_stock_router)
# Pairing sits on /branches/{id}/pairing, so it must be included after the branch router for the
# path ordering to read naturally in the docs; FastAPI matches on the full path either way.
app.include_router(branch_pairing_router)
app.include_router(registration_router)
app.include_router(sync_router)
app.include_router(downstream_sync_router)
app.include_router(branch_staff_router)
app.include_router(items_router)
app.include_router(loyalty_router)
app.include_router(racks_router)
app.include_router(putaway_router)
app.include_router(warehouse_router)
app.include_router(executive_router)
app.include_router(accounts_router)
from app.routes.maintenance import router as maintenance_router  # noqa: E402

app.include_router(maintenance_router)
app.include_router(client_errors_router)

register_tortoise(app, config=TORTOISE_ORM, generate_schemas=False, add_exception_handlers=True)


@app.on_event("startup")
async def _seed() -> None:
    await seed_if_empty()
    from app.services.seed_service import ensure_roles

    await ensure_roles()
    await seed_branches_if_empty()
    # Branches first — the warehouse seed points requisitions and transfers at them.
    await seed_warehouse_if_empty()
    from app.services import racks_service
    await racks_service.backfill_racks()
    from app.services import putaway_service
    homed = await putaway_service.backfill_home_bins()
    if homed:
        print(f"  put-away: {homed} Item(s) given a home bin", flush=True)
    # Backfills resources added to core/resources.py onto users who were seeded before they
    # existed — role templates only materialize into real UserPermission rows at User.create()
    # time, so without this a new screen would never reach an existing account.
    await sync_role_resource_grants()
    # Branch events head office couldn't apply before (a receipt, an account change) get another go.
    from app.services import accounts_chart_service

    added = await accounts_chart_service.ensure_standard_chart()
    if added:
        print(f"  accounts: {added} chart row(s) added to head office's books", flush=True)
    await accounts_chart_service.ensure_supplier_accounts()
    await accounts_chart_service.ensure_branch_accounts()
    from app.services import projector
    applied, failing = await projector.replay_failed()
    if applied or failing:
        logs.log.info("sync: replayed failed branch events: %s applied, %s still failing", applied, failing)


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
    if FRONTEND_DIST is not None:
        found = (FRONTEND_DIST / "index.html").is_file()
        print(
            f"  App:     {'served from ' + str(FRONTEND_DIST) if found else 'not built yet — run npm run build in cloud-app'}\n",
            flush=True,
        )


@app.on_event("startup")
async def _start_posting() -> None:
    from app.core import accounts_scheduler

    accounts_scheduler.start()


@app.on_event("shutdown")
async def _stop_posting() -> None:
    from app.core import accounts_scheduler, backup_scheduler

    await backup_scheduler.stop()
    await accounts_scheduler.stop()


@app.on_event("startup")
async def _start_backups() -> None:
    """The daily backup, taken by the server itself."""
    from app.core import backup_scheduler

    backup_scheduler.start()


@app.middleware("http")
async def _paused_for_restore(request, call_next):
    """While a backup is being put back, nothing else reads or writes the database."""
    from app.services import backup_service

    if backup_service.RESTORING and not request.url.path.startswith(("/health", "/maintenance/status")):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "The database is being restored from a backup. Try again in a minute."}, status_code=503)
    return await call_next(request)


if FRONTEND_DIST is not None:
    app.add_middleware(FrontendMiddleware, dist=FRONTEND_DIST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)
