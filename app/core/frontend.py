"""Serves the built app from this server's own port, so one address opens both the app and its API.

`npm run build` in the app folder writes `dist/`; this server hands it out — no separate web server,
and a phone or tablet on the LAN just opens http://<server-ip>:<port>. The app's screens and the API
share paths (`/warehouse/items` is both a screen and an endpoint), so the two are told apart by what
asked: a browser opening a page asks for HTML and gets the app, the app's own requests ask for JSON
and reach the API. Files are read from disk on every request, so a rebuild shows up without a restart.
"""
from pathlib import Path

from starlette.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# FastAPI's own pages stay reachable in a browser.
_API_PAGES = ("/docs", "/redoc", "/openapi.json")


def default_dist(app_folder: str) -> Path:
    """`<repo>/<app_folder>/dist`, from `<repo>/backend/<server>/app/core/frontend.py`."""
    return Path(__file__).resolve().parents[4] / app_folder / "dist"


class FrontendMiddleware:
    def __init__(self, app: ASGIApp, dist: Path) -> None:
        self.app = app
        self.dist = dist.resolve()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        index = self.dist / "index.html"
        if path.startswith(_API_PAGES) or not index.is_file():
            await self.app(scope, receive, send)
            return

        if path != "/":
            built = (self.dist / path.lstrip("/")).resolve()
            if built.is_file() and self.dist in built.parents:
                # Vite puts a content hash in every asset name, so those can be kept for good.
                cache = "public, max-age=31536000, immutable" if "/assets/" in path else "no-cache"
                await FileResponse(built, headers={"Cache-Control": cache})(scope, receive, send)
                return

        accept = dict(scope["headers"]).get(b"accept", b"").decode("latin-1")
        if "text/html" in accept:
            await FileResponse(index, headers={"Cache-Control": "no-cache"})(scope, receive, send)
            return
        await self.app(scope, receive, send)
