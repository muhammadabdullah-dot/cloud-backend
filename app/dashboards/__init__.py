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

_LOADED: dict[str, Any] | None = None


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
                found[info.name] = module
        _LOADED = found
    return _LOADED


def visible(granted: set[str]) -> list[dict]:
    """The dashboards these grants reach, in a stable order so the board does not shuffle between visits."""
    out = []
    for key, module in sorted(registry().items()):
        meta = module.META
        if meta["resource"] in granted:
            out.append({"id": key, "label": meta["label"], "blurb": meta["blurb"],
                        "resource": meta["resource"], "pages": meta["pages"]})
    return out
