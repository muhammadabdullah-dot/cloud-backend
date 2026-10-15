"""Every analytical dashboard, one module each, found rather than listed.

A dashboard module exports two things and nothing else has to know about it:

    META   {"label", "blurb", "resource", "pages": [{"id", "label"}]}
    build(page_id, scope)  ->  {"cards": [...], "visuals": [...]}

Dropping a file in this folder adds a dashboard. There is no list to append to, no route to add and no frontend
change at all, because the app renders whatever `visuals` describes and the server already says which resource
gates it. That is the whole reason this is a package and not a dict in a controller: several of these are being
written at once, and a shared list is the one file they would all have to fight over.

`resource` is an existing permission, never a new one. If you can open the screen, you can see the picture of it.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from app.core.resources import DASHBOARD_RESOURCES

_LOADED: dict[str, Any] | None = None


def resource_of(key: str) -> str:
    """The access resource for a dashboard, from its module name. `accounts_trade` -> `dashboards.accounts-trade`.

    Derived rather than declared in each module's META, so the two can never drift: a dashboard cannot name a
    resource belonging to a different one, and renaming the file renames the tick, which `registry()` then
    refuses until `resources.py` agrees.
    """
    return f"dashboards.{key.replace('_', '-')}"


def registry() -> dict[str, Any]:
    """Every module in this package that looks like a dashboard, by its module name."""
    global _LOADED
    if _LOADED is None:
        found: dict[str, Any] = {}
        for info in pkgutil.iter_modules(__path__):
            if info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{__name__}.{info.name}")
            if hasattr(module, "META") and hasattr(module, "build"):
                # "preview" is taken. `/dashboards/{dashboard}/preview` serves the board's cards and is declared
                # before `/dashboards/{dashboard}/{page}`, so a page by that name would be unreachable: the
                # preview route would answer every request for it. Refused here, loudly, at import, rather than
                # shipped as a page that silently returns somebody else's answer.
                pages = {p["id"] for p in module.META.get("pages", [])}
                if "preview" in pages:
                    raise RuntimeError(
                        f"{info.name}: a dashboard page cannot be called 'preview'. That path belongs to the "
                        f"board's card preview; rename the page.")
                # EVERY DASHBOARD HAS A TICK OF ITS OWN on the access screen, and it has to exist before the
                # dashboard loads. Refused here rather than at request time, because the two ways this goes
                # wrong are both silent: a resource nobody added means a dashboard nobody can ever be given,
                # and a typo means one that is gated by a resource no role will ever hold. Both look like an
                # empty board to whoever is wondering where their dashboard went.
                if resource_of(info.name) not in set(DASHBOARD_RESOURCES):
                    raise RuntimeError(
                        f"{info.name}: no access resource. Add {resource_of(info.name)!r} to "
                        f"DASHBOARD_RESOURCES in app/core/resources.py, or the dashboard cannot be granted "
                        f"to anybody.")
                found[info.name] = module
        _LOADED = found
    return _LOADED


def visible(granted: set[str]) -> list[dict]:
    """The dashboards these grants reach, in a stable order so the board does not shuffle between visits.

    ONE TICK EACH, and it is the only gate. This used to read `META["resource"]`, the resource of the screen
    the figures come from, so holding `executive.dashboard` handed you Sales Analysis whether anybody meant
    it or not and there was no way to give one person ABC Analysis and not another. The screen resource is
    still on META, because a dashboard says which screen it belongs beside, but it no longer decides who
    sees it.
    """
    out = []
    for key, module in sorted(registry().items()):
        meta = module.META
        resource = resource_of(key)
        if resource in granted:
            out.append({"id": key, "label": meta["label"], "blurb": meta["blurb"],
                        "resource": resource, "draws": meta["resource"], "pages": meta["pages"]})
    return out
