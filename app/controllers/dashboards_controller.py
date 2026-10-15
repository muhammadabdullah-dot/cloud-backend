"""The dashboards a person may open, and one page of one, as visuals the app draws without knowing them.

Nothing about any particular dashboard lives here any more. Each one is a module in `app/dashboards/`, found
rather than listed, so several can be written at once without a shared file to fight over. This only answers two
questions: which ones may this person see, and what does this page of that one say.

WHICH DASHBOARDS A PERSON SEES
------------------------------
Each names a resource that already exists. Nobody administers a second set of grants, and the rule reads the way
people expect: if you can open the screen, you can see the picture of it.
"""
from __future__ import annotations

from app import dashboards as registry
from app.models import User
from app.services import dashboards_service as ds_module
from app.services.dashboards_service import Scope

visible = registry.visible


def find(dashboard: str):
    return registry.registry().get(dashboard)


# How much of a dashboard a card on the board shows: enough to recognise the shape of the answer, not enough to
# be read instead of opening it.
PREVIEW_CARDS = 3
PREVIEW_ROWS = 5


async def preview(dashboard: str, scope: Scope) -> dict:
    """What a dashboard currently says, small enough to sit on a card.

    IT IS THE DASHBOARD'S OWN FIRST PAGE, built and then cut down, rather than a second set of queries written
    per module. Two reasons, and the first is the one that matters: a preview assembled from its own SQL is a
    figure that can disagree with the page it is advertising, and it will, the first time either side is
    changed without the other. This cannot disagree, because it IS the page.

    The second is that it costs nothing extra in practice. `dashboards_service._rows` goes through
    `query_cache`, so opening the dashboard straight after reading its card is served from the cache the card
    warmed. Measured on the 30 days to 15 October 2026: eight of the nine first pages build in under a second
    (branches 0.05s, accounts_trade 0.13s, xyz 0.32s, purchasing 0.46s, abc 0.61s, payables 0.68s, stock 0.88s,
    sales 0.95s) and finance takes 5.95s. The board fetches each card on its own so the slow one delays nothing
    but itself, and a module that ever grows past that can export its own `preview(scope)` to override this.
    """
    module = find(dashboard)
    if not module:
        raise KeyError(dashboard)
    meta = module.META
    if hasattr(module, "preview"):
        return {"id": dashboard, **await module.preview(scope)}

    first = meta["pages"][0]["id"]
    built = await module.build(first, scope)
    # The first chart with rows, whatever kind it is. A page whose opener is a table still previews, as its
    # first few rows, and a page that came back empty previews as nothing rather than as an invented zero.
    shown = next((v for v in built.get("visuals", []) if v.get("rows")), None)
    rows: list[dict] = []
    if shown:
        for row in shown["rows"][:PREVIEW_ROWS]:
            rows.append({"label": str(row.get("label") or row.get("key") or ""),
                         "value": str(row.get("value") or "")})
    return {
        "id": dashboard,
        "page": first,
        "cards": built.get("cards", [])[:PREVIEW_CARDS],
        "visual": {"title": shown["title"], "kind": shown["kind"]} if shown else None,
        "rows": rows,
    }


async def _picked_labels(scope: Scope) -> dict[str, str]:
    """What each narrowing is CALLED, so a chip reads "Branch: Model Town".

    The app can remember a label when somebody clicks a bar, and that is not enough: these filters live in the
    address so a page can be sent to somebody or switched between pages, and arriving that way there is nothing
    to remember. Most keys are their own label -- a category is called what it is -- and the two that are not, a
    branch and an Item, are looked up here.
    """
    labels = dict(scope.picked)
    if "branch" in scope.picked:
        from app.models import Branch

        branch = await Branch.filter(id=scope.picked["branch"]).first()
        if branch:
            labels["branch"] = branch.name
    if "sku" in scope.picked:
        from app.models import Product

        product = await Product.filter(sku=scope.picked["sku"]).first()
        if product:
            labels["sku"] = product.name
    return labels


async def page(dashboard: str, page_id: str, scope: Scope, user: User) -> dict:
    module = find(dashboard)
    if not module:
        raise KeyError(dashboard)
    meta = module.META
    if page_id not in {p["id"] for p in meta["pages"]}:
        raise KeyError(page_id)

    built = await module.build(page_id, scope)
    # WHAT THIS PAGE COVERS, IN THE PAGE'S OWN WORDS. This used to be five fixed keys -- items, categories,
    # brands, branches, days -- which read as nonsense on a dashboard that draws none of those: the books page
    # announced "93 Items, 4 categories, 11 brands" about a profit and loss account. A dashboard may now hand
    # back a list of {label, value} and say what it actually counted. The five are still the default, because
    # for anything folded from the Item rollup they are the right five.
    covers = built.get("covers")
    if covers is None:
        from app.services import dashboards_service as ds

        counted = await ds.headline(scope)
        covers = [
            {"label": "Items", "value": counted["skus"]},
            {"label": "categories", "value": counted["categories"]},
            {"label": "brands", "value": counted["brands"]},
            {"label": "branches", "value": counted["branches"]},
            {"label": "trading days", "value": counted["days"]},
        ]
    elif isinstance(covers, dict):
        # The older shape, kept working rather than broken: a dict of counts becomes the same list.
        order = [("items", "Items"), ("categories", "categories"), ("brands", "brands"),
                 ("branches", "branches"), ("days", "trading days")]
        covers = [{"label": label, "value": covers[key]} for key, label in order if key in covers]

    return {
        "id": dashboard, "label": meta["label"], "page": page_id, "pages": meta["pages"],
        # EVERY DASHBOARD CAN BE READ ONE BRANCH AT A TIME, so every page carries the list its picker needs. It
        # rides on the page rather than coming from `/branches` because that endpoint has a permission of its own
        # and this one does not: a dashboard is gated by the thing it draws, and somebody allowed to see Stock
        # Health by branch should not also need the branch administration grant to pick one.
        "branches": await ds_module.branch_list(),
        "scope": {
            "from": scope.start.isoformat(), "to": scope.end.isoformat(),
            "atStart": scope.at_start.isoformat() if scope.at_start else None,
            "atEnd": scope.at_end.isoformat() if scope.at_end else None,
            "picked": scope.picked,
            "pickedLabels": await _picked_labels(scope),
            "basis": "bills" if scope.timed else "day",
        },
        "covers": covers,
        "cards": built.get("cards", []),
        "visuals": built.get("visuals", []),
    }
