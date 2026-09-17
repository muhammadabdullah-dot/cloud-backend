"""The company's one supplier list: matching, and keeping every branch up to date.

Head office holds the list (`Supplier`). Each supplier on it has a `company_id` every branch knows it by; branches keep
their own ids and codes, because their GRNs, orders and ledger accounts point at them.

**Head office → branches.** Every change here goes to each branch that has joined the list as a `supplier.upsert`
carrying the whole supplier and its revision, so a branch applies each change once and never an older one over a newer.

**Branch → head office.** A supplier a branch adds or changes arrives as a `Supplier` event. Head office finds it (by
company identity, else by the branch's own id it paired before), else matches it to the list, else adds it, and passes
the result to every branch.

**Matching** happens here and only here, so there is one answer for the whole company: the same name (spelling,
"&" and "Pvt Ltd" aside) and a phone number in common, else the same NTN. One clear match is paired. Several, or a
match that only half agrees (the same name with a different phone), is a question for the Warehouse Manager
(`SupplierQuestion`), and the candidates are held back from that branch until someone answers. Nothing is guessed and
nothing is added twice.

**Joining.** A branch joins once: it sends every supplier it has, then asks for the list (`SupplierList`). Head office
answers with the whole list, naming the branch's own supplier wherever it paired one, and from then on sends changes.
A branch set up later joins the same way.

**When both sides change the same supplier.** A branch's change says which head office revision it started from. If
head office hasn't changed the supplier since, the branch's change stands. If it has, head office's name, NTN, sales
tax number and CNIC stand, and for every other field the later change wins. Either way the result goes back to every
branch, the one that made the change included.
"""
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tortoise.transactions import atomic

from app.core import logs
from app.models import Branch, BranchMessage, Counter, Supplier, SupplierBranchLink, SupplierQuestion, User
from app.services import downstream_service

HEAD_OFFICE = "HO"
ROLLOUT = "rollout:company-suppliers"
JOINED = "supplier-list:"
MANAGERS = [("warehouse.suppliers.manage", "W")]
LINK = "/warehouse/suppliers"

# Payload name -> column, for every field the list shares. The code isn't one: each side keeps its own.
SHARED = {
    "name": "name", "contactPerson": "contact_person", "phone": "phone", "phone2": "phone2", "email": "email",
    "address": "address", "city": "city", "ntn": "ntn", "sTaxRegNo": "s_tax_reg_no", "cnic": "cnic",
    "dueDays": "due_days", "discountPercent": "discount_percent", "remarks": "remarks", "active": "active",
}
_LENGTHS = {
    "name": 160, "contact_person": 120, "phone": 30, "phone2": 30, "email": 180, "address": 255, "city": 80,
    "ntn": 40, "s_tax_reg_no": 40, "cnic": 40, "remarks": 255,
}
# What says who a supplier is. When head office and a branch both changed a supplier, head office's stand.
AUTHORITY = {"name", "ntn", "sTaxRegNo", "cnic"}


class SupplierSyncError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value) -> datetime | None:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def new_company_id() -> str:
    return uuid.uuid4().hex


# ── comparing two suppliers ──────────────────────────────────────────────────────────────────────

_NOISE_WORDS = {"pvt", "private", "ltd", "limited", "smc", "llc", "inc", "the", "co", "company"}


def normal_name(name: str | None) -> str:
    """A supplier's name as it is compared: case, punctuation, "&" and company-form words don't count."""
    text = (name or "").lower().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    if words[:2] == ["m", "s"]:
        words = words[2:]
    kept = [w for w in words if w not in _NOISE_WORDS]
    return " ".join(kept or words)


def phone_numbers(*values: str | None) -> set[str]:
    """Every number in the phone fields, digits only and without the country code or leading zero: 061-2116302,
    +92 61 2116302 and 0612116302 are one number. A field may hold several ("061-4511867 / 0300-1234567")."""
    numbers = set()
    for value in values:
        for part in re.split(r"[,/;]", value or ""):
            digits = re.sub(r"\D", "", part)
            if digits.startswith("0092"):
                digits = digits[4:]
            elif digits.startswith("92") and len(digits) >= 11:
                digits = digits[2:]
            digits = digits.lstrip("0")
            if len(digits) >= 7:
                numbers.add(digits)
    return numbers


def _numbers_meet(a: set[str], b: set[str]) -> bool:
    # A number typed without its area code still counts: 2116302 is 061-2116302.
    return any(x == y or x.endswith(y) or y.endswith(x) for x in a for y in b)


def tax_number(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    # Placeholders like 0000000-0 identify nobody.
    return digits if len(digits) >= 7 and len(set(digits)) > 1 else ""


def compare(a: dict, b: dict) -> str | None:
    """How sure it is that two suppliers are one: "strong" (the same name and a phone in common, or the same NTN),
    "weak" (the same name, nothing to check it against), "maybe" (the same name or NTN, but the rest disagrees), or
    None. Each side is {name, phone, phone2, ntn}."""
    same_name = normal_name(a.get("name")) == normal_name(b.get("name"))
    phones_a, phones_b = phone_numbers(a.get("phone"), a.get("phone2")), phone_numbers(b.get("phone"), b.get("phone2"))
    phones_disagree = bool(phones_a and phones_b) and not _numbers_meet(phones_a, phones_b)
    ntn_a, ntn_b = tax_number(a.get("ntn")), tax_number(b.get("ntn"))
    if ntn_a and ntn_b:
        if ntn_a == ntn_b:
            return "maybe" if not same_name and phones_disagree else "strong"
        return "maybe" if same_name else None
    if not same_name:
        return None
    if phones_a and phones_b:
        return "maybe" if phones_disagree else "strong"
    return "weak"


def _facts(s: Supplier) -> dict:
    return {"name": s.name, "phone": s.phone, "phone2": s.phone2, "ntn": s.ntn}


# ── what a branch is sent ────────────────────────────────────────────────────────────────────────

def payload(s: Supplier) -> dict:
    return {
        "companyId": s.company_id, "code": s.code, "name": s.name, "contactPerson": s.contact_person, "phone": s.phone,
        "phone2": s.phone2, "email": s.email, "address": s.address, "city": s.city, "ntn": s.ntn,
        "sTaxRegNo": s.s_tax_reg_no, "cnic": s.cnic, "dueDays": s.due_days,
        "discountPercent": format(Decimal(s.discount_percent or 0), "f"), "remarks": s.remarks, "active": s.active,
        "origin": s.origin, "rev": s.rev, "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
    }


async def ensure_company_id(s: Supplier) -> Supplier:
    if not s.company_id:
        s.company_id = new_company_id()
        s.created_at = s.created_at or _now()
        await s.save(update_fields=["company_id", "created_at"])
    return s


async def _joined(branch: Branch) -> bool:
    return await Counter.exists(id=f"{JOINED}{branch.id}")


async def _joined_branches() -> list[Branch]:
    ids = [row[len(JOINED):] for row in await Counter.filter(id__startswith=JOINED).values_list("id", flat=True)]
    return await Branch.filter(id__in=ids, verified_at__not_isnull=True) if ids else []


async def _held_ids(branch: Branch) -> set[str]:
    """Suppliers a person still has to decide about for this branch that it doesn't have yet: it gets them after."""
    held: set[str] = set()
    for ids in await SupplierQuestion.filter(branch=branch, status="open").values_list("held", flat=True):
        held.update(ids or [])
    return held


async def present_at(branch: Branch, except_local_id: str | None = None) -> set[str]:
    """Company suppliers this branch already has a supplier of its own for, so another of its suppliers can't be them.

    Before a branch joins, that is only the ones paired with its suppliers. Once it has joined it has been sent every
    supplier on the list except those held back for a decision, so it has all of those (a copy made when the message
    arrived, if nothing else). A supplier it adds then that looks like one of them is a question, never a pairing:
    pairing would leave the branch with two suppliers for one company supplier."""
    links = SupplierBranchLink.filter(branch=branch)
    if except_local_id:
        links = links.exclude(branch_supplier_id=except_local_id)
    present = set(await links.values_list("supplier_id", flat=True))
    if await _joined(branch):
        mine = set()
        if except_local_id:
            mine = set(await SupplierBranchLink.filter(branch=branch, branch_supplier_id=except_local_id).values_list("supplier_id", flat=True))
        present |= set(await Supplier.all().values_list("id", flat=True)) - await _held_ids(branch) - mine
    return present


async def _entry(branch: Branch, s: Supplier) -> dict:
    link = await SupplierBranchLink.filter(branch=branch, supplier=s).first()
    return {"supplier": payload(s), "localId": link.branch_supplier_id if link else None, "hold": s.id in await _held_ids(branch)}


async def publish(s: Supplier, *, only: Branch | None = None) -> int:
    """Send the supplier as it now stands to every branch on the list (or just `only`, if it is on the list)."""
    await ensure_company_id(s)
    targets = [only] if only is not None else await _joined_branches()
    sent = 0
    for branch in targets:
        if only is not None and not await _joined(branch):
            continue
        await downstream_service.enqueue(str(branch.id), "supplier.upsert", await _entry(branch, s))
        sent += 1
    return sent


async def rename_account(s: Supplier) -> None:
    """Head office's ledger account for the supplier is there from the start and carries its name."""
    from app.services.accounts_chart_service import Resolver

    await Resolver().supplier(s)


# ── from a branch ────────────────────────────────────────────────────────────────────────────────

def _clean(column: str, value):
    if column == "active":
        return bool(value)
    if column == "due_days":
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0
    if column == "discount_percent":
        try:
            return Decimal(str(value or 0))
        except InvalidOperation:
            return Decimal("0")
    if isinstance(value, str):
        return value.strip()[: _LENGTHS[column]] or None
    return value


def same_value(a, b) -> bool:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return Decimal(str(a or 0)) == Decimal(str(b or 0))
    return a == b


def _blank(value) -> bool:
    return value is None or value == "" or (isinstance(value, (int, Decimal)) and not isinstance(value, bool) and value == 0)


async def _code_for(wanted: str | None) -> str:
    from app.services.supplier_service import next_code

    code = (wanted or "").strip().upper()
    if code and len(code) <= 20 and not await Supplier.exists(code=code):
        return code
    return await next_code()


async def _add_from_branch(branch: Branch, data: dict, how: str) -> Supplier:
    now = _now()
    s = Supplier(
        id=f"sup-{uuid.uuid4().hex[:8]}", code=await _code_for(data.get("code")), company_id=new_company_id(),
        origin=branch.code[:20], rev=1, created_at=now, updated_at=_dt(data.get("updatedAt")) or now, updated_by=branch.name[:180],
    )
    for key, column in SHARED.items():
        if key in data:
            setattr(s, column, _clean(column, data[key]))
    if not s.name:
        raise SupplierSyncError("A supplier from a branch without a name.")
    await s.save(force_create=True)
    await SupplierBranchLink.create(supplier=s, branch=branch, branch_supplier_id=data["localId"], how=how)
    await rename_account(s)
    return s


async def _fill_blanks(s: Supplier, data: dict, branch: Branch) -> list[str]:
    """Pairing a branch's supplier with one on the list: what head office doesn't know yet, the branch fills in.
    Where both know something, head office's stands."""
    filled = []
    for key, column in SHARED.items():
        if key in ("name", "active") or key not in data:
            continue
        value = _clean(column, data[key])
        if _blank(getattr(s, column)) and not _blank(value):
            setattr(s, column, value)
            filled.append(key)
        elif key == "phone" and value and not _numbers_meet(phone_numbers(value), phone_numbers(s.phone, s.phone2)) and _blank(s.phone2):
            # A number head office didn't have is kept as the second one rather than lost.
            s.phone2 = value
            filled.append("phone2")
    if filled:
        s.rev += 1
        s.updated_at = _now()
        s.updated_by = branch.name[:180]
        await s.save()
    return filled


async def _merge(s: Supplier, data: dict, changed: list[str] | None, branch: Branch, link: SupplierBranchLink | None) -> list[str]:
    base = int(data.get("baseRev") or 0)
    if not data.get("companyId") and link is not None and link.how == "added":
        # Head office made it from what this branch sent, so that is what the branch's change started from.
        base = max(base, 1)
    head_office_moved = base < s.rev
    changed_at = _dt(data.get("updatedAt"))
    applied = []
    for key in changed if changed is not None else list(SHARED):
        column = SHARED.get(key)
        if column is None or key not in data:
            continue
        value = _clean(column, data[key])
        if column == "name" and not value:
            continue
        if same_value(getattr(s, column), value):
            continue
        if head_office_moved:
            if key in AUTHORITY:
                continue
            if s.updated_at and changed_at and changed_at <= s.updated_at:
                continue
        setattr(s, column, value)
        applied.append(key)
    if applied:
        s.rev += 1
        s.updated_at = changed_at if changed_at and (not s.updated_at or changed_at > s.updated_at) else _now()
        s.updated_by = branch.name[:180]
        await s.save()
        if "name" in applied:
            await rename_account(s)
    return applied


def _why(data: dict, s: Supplier) -> str:
    """What agrees and what doesn't, in a sentence."""
    same_name = normal_name(data.get("name")) == normal_name(s.name)
    ntn_a, ntn_b = tax_number(data.get("ntn")), tax_number(s.ntn)
    who = f"{s.name} ({s.code})"
    if ntn_a and ntn_b and ntn_a != ntn_b:
        return f"Has the same name as {who}, but a different NTN."
    if ntn_a and ntn_a == ntn_b and not same_name:
        return f"Has the same NTN as {who}, but a different name and phone number."
    if same_name and phone_numbers(data.get("phone"), data.get("phone2")):
        return f"Has the same name as {who}, but a different phone number."
    return f"Has the same name as {who}, with nothing else to tell them apart."


async def _match(branch: Branch, data: dict) -> tuple[Supplier | None, list[Supplier], str]:
    """(the supplier it is, the ones it could be, why it's a question)."""
    taken = await present_at(branch, data["localId"])
    grades: dict[str, list[Supplier]] = {"strong": [], "weak": [], "maybe": []}
    for s in await Supplier.all().order_by("name"):
        grade = compare(data, _facts(s))
        if grade:
            grades[grade].append(s)
    strong, weak, maybe = grades["strong"], grades["weak"], grades["maybe"]
    name = f"{branch.name}"
    if len(strong) == 1 or (not strong and len(weak) == 1 and not maybe):
        one = strong[0] if strong else weak[0]
        if one.id not in taken:
            return one, [], ""
        return None, [one], f"Looks like {one.name} ({one.code}), which {name} already has as another of its suppliers."
    could_be = strong or (weak + maybe)
    if not could_be:
        return None, [], ""
    if len(could_be) == 1:
        return None, could_be, _why(data, could_be[0])
    listed = ", ".join(f"{s.name} ({s.code})" for s in could_be[:4])
    return None, could_be, f"Could be any of {len(could_be)} suppliers on the list: {listed}."


async def _ask(branch: Branch, data: dict, could_be: list[Supplier], reason: str) -> SupplierQuestion:
    question = await SupplierQuestion.get_or_none(branch=branch, branch_supplier_id=data["localId"])
    # Only candidates the branch doesn't have yet are held back; one it already has stays where it is.
    present = await present_at(branch, data["localId"])
    candidates, held = [s.id for s in could_be], [s.id for s in could_be if s.id not in present]
    if question is None:
        question = await SupplierQuestion.create(
            branch=branch, branch_supplier_id=data["localId"], branch_supplier=data, candidates=candidates, held=held, reason=reason[:255],
        )
    else:
        question.branch_supplier, question.candidates, question.held, question.reason, question.status = data, candidates, held, reason[:255], "open"
        await question.save()
    if await _joined(branch):
        # While a branch is joining, its questions are counted in the one notice about joining instead.
        from app.services import alerts_service

        await alerts_service.notify(
            "supplier.question", f"Is this the same supplier? {branch.name} added {data.get('name')}", body=reason,
            link=LINK, tone="warning", audience_any=MANAGERS, subject=("supplier-question", str(question.id)),
        )
    return question


async def apply_from_branch(branch: Branch, event: dict) -> str:
    data = dict(event.get("supplier") or {})
    local_id = str(data.get("localId") or "").strip()
    if not local_id or not str(data.get("name") or "").strip():
        raise SupplierSyncError("A supplier event without the branch's supplier id or its name.")
    data["localId"] = local_id
    changed = event.get("changed")
    link = await SupplierBranchLink.get_or_none(branch=branch, branch_supplier_id=local_id)
    s = await Supplier.get_or_none(company_id=data["companyId"]) if data.get("companyId") else None
    if s is None and link is not None:
        s = await Supplier.get(id=link.supplier_id)
    if s is not None:
        if link is None and not await SupplierBranchLink.exists(branch=branch, supplier=s):
            # A copy head office sent, now changed at the branch: remember whose copy it is.
            link = await SupplierBranchLink.create(supplier=s, branch=branch, branch_supplier_id=local_id, how="copy")
        applied = await _merge(s, data, changed, branch, link)
        if applied:
            await publish(s)
            return "updated"
        # Nothing of the branch's change stood: put its copy back to how the list has it.
        await publish(s, only=branch)
        return "unchanged"

    question = await SupplierQuestion.get_or_none(branch=branch, branch_supplier_id=local_id)
    if question is not None and question.status == "branch-only":
        return "branch-only"
    if question is not None and question.status == "open":
        question.branch_supplier = data
        await question.save(update_fields=["branch_supplier", "updated_at"])
        return "waiting"

    one, could_be, reason = await _match(branch, data)
    if one is not None:
        await SupplierBranchLink.create(supplier=one, branch=branch, branch_supplier_id=local_id, how="matched")
        await ensure_company_id(one)
        if await _fill_blanks(one, data, branch):
            await publish(one)
        else:
            await publish(one, only=branch)
        return "matched"
    if could_be:
        await _ask(branch, data, could_be, reason)
        return "question"
    if event.get("initial") and not data.get("active", True):
        # Switched off at the branch and on no list: not worth adding. It joins if it's ever switched back on.
        return "left at branch"
    s = await _add_from_branch(branch, data, "added")
    await publish(s)
    return "added"


async def send_list(branch: Branch) -> dict:
    """The whole list for one branch, naming its own supplier wherever head office paired one. The branch is on the
    list from now on, and the Warehouse Manager hears how its suppliers matched."""
    held = await _held_ids(branch)
    links = {row.supplier_id: row.branch_supplier_id for row in await SupplierBranchLink.filter(branch=branch)}
    entries = []
    for s in await Supplier.all().order_by("name"):
        await ensure_company_id(s)
        entries.append({"supplier": payload(s), "localId": links.get(s.id), "hold": s.id in held})
    first_time = not await _joined(branch)
    await downstream_service.enqueue(str(branch.id), "supplier.list", {"suppliers": entries})
    if first_time:
        await Counter.create(id=f"{JOINED}{branch.id}", value=1)
    status = await branch_status(branch)
    logs.log.info(
        "suppliers: %s joined the company list: %s matched, %s added, %s waiting for a decision, %s sent",
        branch.code, status["matched"], status["added"], status["waiting"], len(entries),
    )
    if first_time:
        from app.services import alerts_service

        waiting = status["waiting"]

        def was(n: int) -> str:
            return f"{n} {'was' if n == 1 else 'were'}"

        await alerts_service.notify(
            "supplier.list", f"{branch.name}'s suppliers are on the company list",
            body=(f"{was(status['matched'])} already on it and {was(status['added'])} added. "
                  + (f"{waiting} {'needs' if waiting == 1 else 'need'} your decision." if waiting else "Nothing needs a decision.")),
            link=LINK, tone="warning" if waiting else "good", audience_any=MANAGERS, subject=("branch", str(branch.id)),
        )
    return status


async def branch_status(branch: Branch) -> dict:
    hows = await SupplierBranchLink.filter(branch=branch).values_list("how", flat=True)
    last = await BranchMessage.filter(branch=branch, kind="supplier.list").order_by("-seq").first()
    return {
        "branchCode": branch.code, "branchName": branch.name, "joined": await _joined(branch),
        "joinedAt": last.created_at if last else None,
        "matched": hows.count("matched"), "added": hows.count("added"), "decided": hows.count("decided"),
        "waiting": await SupplierQuestion.filter(branch=branch, status="open").count(),
    }


# ── a person's answer ────────────────────────────────────────────────────────────────────────────

@atomic()
async def answer(question_id: str, answer_kind: str, supplier_id: str | None, user: User) -> SupplierQuestion:
    """All in one go: a failure part way leaves no extra supplier on the list, and a second click (or a second person
    answering at the same time) finds the question already answered instead of adding the supplier again."""
    question = await SupplierQuestion.get_or_none(id=question_id)
    if question is None or question.status != "open":
        raise SupplierSyncError("That question has already been answered.", 409)
    if answer_kind in ("same", "different") and await SupplierBranchLink.exists(branch_id=question.branch_id, branch_supplier_id=question.branch_supplier_id):
        raise SupplierSyncError("That branch's supplier is already on the list, so this question has been settled.", 409)
    branch = await Branch.get(id=question.branch_id)
    data = dict(question.branch_supplier or {})
    chosen: Supplier | None = None
    if answer_kind == "same":
        if not supplier_id or supplier_id not in (question.candidates or []):
            raise SupplierSyncError("Choose which supplier on the list it is.")
        chosen = await Supplier.get(id=supplier_id)
        if chosen.id in await present_at(branch, question.branch_supplier_id):
            raise SupplierSyncError(
                f"{branch.name} already has {chosen.name} as another of its suppliers, so this one can't be it too. "
                "Add it to the list, or keep it at that branch only."
            )
        await SupplierBranchLink.create(supplier=chosen, branch=branch, branch_supplier_id=question.branch_supplier_id, how="decided")
        await ensure_company_id(chosen)
        if await _fill_blanks(chosen, data, branch):
            await publish(chosen)
    elif answer_kind == "different":
        chosen = await _add_from_branch(branch, data, "decided")
        await publish(chosen)
    question.status = answer_kind
    question.answer_supplier = chosen
    question.decided_by = user.name[:180] if user.name else None
    question.decided_at = _now()
    await question.save()
    # What was held back from this branch while the question was open goes to it now.
    for candidate in await Supplier.filter(id__in=list(question.candidates or [])):
        await publish(candidate, only=branch)
    if chosen is not None and chosen.id not in (question.candidates or []):
        await publish(chosen, only=branch)
    return question


# ── once ─────────────────────────────────────────────────────────────────────────────────────────

async def prepare_company_list() -> dict | None:
    """Once (a `counters` row): every supplier head office has gets its company identity, and pairs already on the
    list that look like the same supplier are reported to the Warehouse Manager. They aren't merged: each may have
    GRNs and a ledger account of its own, so that is a person's call. Branches join afterwards, one by one."""
    if await Counter.exists(id=ROLLOUT):
        return None
    now = _now()
    identified = 0
    suppliers = await Supplier.all().order_by("name")
    for s in suppliers:
        if not s.company_id:
            s.company_id = new_company_id()
            s.created_at = s.created_at or now
            s.updated_at = s.updated_at or now
            await s.save(update_fields=["company_id", "created_at", "updated_at"])
            identified += 1
    look_alike = []
    for i, a in enumerate(suppliers):
        for b in suppliers[i + 1:]:
            if compare(_facts(a), _facts(b)) in ("strong", "weak"):
                look_alike.append(f"{a.name} ({a.code}) and {b.name} ({b.code})")
    await Counter.create(id=ROLLOUT, value=1)
    if look_alike:
        from app.services import alerts_service

        await alerts_service.notify(
            "supplier.look-alike", f"{len(look_alike)} pair(s) of suppliers on the list look like the same supplier",
            body="; ".join(look_alike)[:500], link=LINK, tone="warning", audience_any=MANAGERS,
        )
    return {"identified": identified, "lookAlike": look_alike}
