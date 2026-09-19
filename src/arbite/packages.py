"""Ordered same-worker continuity packages (planning key B04).

A package is the contract "A then B (then C...) by the same worker". It is a
record over existing tickets, not a new kind of work: every member is still
acquired through `TicketLifecycle.acquire` and gets its own attempt.

Rules (see `.arbite/planning/multi-provider-job-board.md`):

- **Order is a scheduling edge.** A member is acquirable only while it is the
  package's *current* member: the first member that is not closed. Later
  members never go in_progress early, whatever the origin (claim, list-next
  claim, --adopt, --force, `set`). `acquisition_refusal` is consulted by
  `offers.acquisition_grant`, the single decision point, before offers and
  reservations. Creation rejects a combined dependency + package-order graph
  with a cycle (`graph.combined_cycles`); dependencies outside the package are
  *external prerequisites*, shown in every view and enforced by the ordinary
  readiness check (an unmet one pauses the package without holding claims).
- **Binding.** An `open` package binds to the worker whose acquisition of its
  first ready member commits (directly, or by accepting a package offer), in the
  same cascade transaction that stores the attempt (`sync_in_transaction`).
  While `bound`, only `bound_worker` may acquire members; it needs no offer or
  reservation grant for them. Bound identity is the same worker id, not the
  same model session: file claims are released when each member closes, and the
  next member starts a fresh attempt that must read files again.
- **No auto-advance.** The package advances only when its current member is
  *closed*. A blocked, shelved, released or interrupted member stays current
  until someone resolves it explicitly.
- **Explicit handoff only.** `handoff` rebinds the remaining members to another
  worker or releases them (state `released`: members return to ordinary
  availability), always with a reason, recorded in `handoffs`. Completed
  members stay completed. An active attempt is never cancelled silently
  (`--interrupt` required). There is no timeout-based rebinding.
- **Durable notes.** `note` appends to `notes` so a resumed session (same
  worker id, new context) can pick up where the last one stopped.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from . import graph, offers, reservations, schema, workers
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    PACKAGE_STATES,
    WORKER_ID_PATTERN,
    Event,
    Package,
    new_record_id,
    utc_now,
)
from .coordination_storage import check_revision
from .errors import (
    CoordinationNotFound,
    InvalidRecord,
    PackageConflict,
    TicketPackaged,
)
from .query import TicketQuery

#: Shown with every package view.
PACKAGE_NOTICE = (
    "a package binds its remaining members to one worker id, in order; each member is a "
    "separate attempt -- file claims are released when a member closes and files must be read "
    "again. Same worker id is continuity identity, not the same model session"
)

#: `package list --state` filters.
STATE_FILTERS = tuple(PACKAGE_STATES) + ("live", "all")


@dataclass(frozen=True)
class StoredPackage:
    package: Package
    revision: int


@dataclass(frozen=True)
class PackageChange:
    stored: StoredPackage
    event: Optional[Event]
    interrupted: List[dict] = field(default_factory=list)
    ended_offers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers (shared by acquisition, list next and a later readiness layer)
# ---------------------------------------------------------------------------


def _store_is_empty(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return callable(probe) and not probe()


def _live_in(tx) -> Dict[str, StoredPackage]:
    held: Dict[str, StoredPackage] = {}
    found = [p for p in tx.find("package") if p.is_live]
    for package in sorted(found, key=lambda p: (p.created, p.id)):
        stored = StoredPackage(package, tx.revision_of("package", package.id))
        for ticket in package.tickets:
            held.setdefault(ticket, stored)
    return held


def live_packages(store) -> Dict[str, StoredPackage]:
    """Ticket id -> the live (open/bound) package it belongs to."""
    if store is None or _store_is_empty(store):
        return {}
    with store.transaction(write=False) as tx:
        return _live_in(tx)


def live_package_for(store, ticket_id: str) -> Optional[StoredPackage]:
    return live_packages(store).get(ticket_id)


def _status_fn(statuses) -> Callable[[str], Optional[str]]:
    return statuses if callable(statuses) else (lambda tid: (statuses or {}).get(tid))


def progress(package: Package, statuses) -> dict:
    """Completed/remaining members and the current one, derived from ticket
    status (`statuses`: mapping or callable ticket id -> status)."""
    status_of = _status_fn(statuses)
    completed = [t for t in package.tickets if status_of(t) == "closed"]
    remaining = [t for t in package.tickets if status_of(t) != "closed"]
    return {"completed": completed, "remaining": remaining,
            "current": remaining[0] if remaining else None}


def acquisition_refusal(package: Package, ticket_id: str, worker_id: str,
                        statuses) -> Optional[TicketPackaged]:
    """Why `worker_id` may not acquire `ticket_id` because of its live
    `package`, or None. Structured for a readiness layer: `details["reason"]`
    is `package_order` or `bound_elsewhere`."""
    state = progress(package, statuses)
    current = state["current"]
    details = {
        "ticket_id": ticket_id, "worker_id": worker_id, "package_id": package.id,
        "state": package.state, "bound_worker": package.bound_worker, "current": current,
        "position": package.tickets.index(ticket_id) + 1, "members": list(package.tickets),
    }
    if ticket_id != current:
        waiting = state["remaining"][: state["remaining"].index(ticket_id)] \
            if ticket_id in state["remaining"] else []
        return TicketPackaged(
            f"ticket {ticket_id} is member {details['position']} of package {package.id}; "
            f"members are worked in order and {current} is not closed yet -- later members "
            "are never started early (work the current member, or choose other work)",
            details=dict(details, reason="package_order", waiting_on=waiting),
        )
    if package.state == "bound" and worker_id != package.bound_worker:
        return TicketPackaged(
            f"ticket {ticket_id} belongs to package {package.id}, bound to "
            f"{package.bound_worker!r}; only that worker may acquire its members until an "
            "explicit 'arbite package handoff' rebinds or releases them -- choose other work",
            details=dict(details, reason="bound_elsewhere"),
        )
    return None


def may_pick(ticket_id: str, worker_id: Optional[str], live: Dict[str, StoredPackage],
             statuses) -> bool:
    """Selection filter for `list next`: package members only surface as the
    current member, and a bound package's only to its bound worker."""
    stored = live.get(ticket_id)
    if stored is None:
        return True
    package = stored.package
    if progress(package, statuses)["current"] != ticket_id:
        return False
    if package.state == "bound":
        return worker_id is not None and worker_id == package.bound_worker
    return True


def edit_refusal(store, ticket, *, new_status, new_assignee) -> None:
    """Refuse `set status=in_progress` / `set assignee=X` on a live package
    member: that would start or hand out a member outside the package order."""
    stored = live_package_for(store, ticket.id)
    if stored is None:
        return
    package = stored.package
    details = {"ticket_id": ticket.id, "package_id": package.id, "state": package.state,
               "bound_worker": package.bound_worker, "reason": "package_edit"}
    if new_status == "in_progress" and ticket.status != "in_progress":
        raise TicketPackaged(
            f"ticket {ticket.id} is a member of package {package.id}; refusing 'set "
            "status=in_progress' -- acquire the current member with 'arbite claim' (or "
            f"'arbite package claim {package.id}')",
            details=dict(details, field="status", value=new_status),
        )
    if new_assignee and new_assignee != ticket.assignee:
        raise TicketPackaged(
            f"ticket {ticket.id} is a member of package {package.id}; refusing 'set "
            f"assignee={new_assignee}' -- members are acquired in order by the bound worker; "
            "rebind with 'arbite package handoff'",
            details=dict(details, field="assignee", value=new_assignee),
        )


def external_prerequisites(package: Package, by_id: dict) -> List[dict]:
    """Dependencies of members that are not themselves members, with status."""
    members = set(package.tickets)
    out = []
    for member in package.tickets:
        ticket = by_id.get(member)
        for dep in (ticket.depends_on if ticket is not None else []):
            if dep in members:
                continue
            found = by_id.get(dep)
            status = found.status if found is not None else "missing"
            out.append({"member": member, "depends_on": dep, "status": status,
                        "met": status == "closed",
                        # A dependency that resolves to no ticket is ignored by
                        # readiness (doctor reports it as dangling).
                        "blocks": found is not None and status != "closed"})
    return out


def cycles_for(by_id: dict, orders: Iterable[List[str]], members: Iterable[str]) -> List[List[str]]:
    """Unsatisfiable combined-graph cycles touching any of `members`."""
    wanted = set(members)
    return [c for c in graph.combined_cycles(by_id, list(orders)) if wanted & set(c)]


# ---------------------------------------------------------------------------
# Writes inside a lifecycle cascade
# ---------------------------------------------------------------------------


def _package_event(kind, package, revision, moment, *, actor, extra=None) -> Event:
    payload = {
        "package_id": package.id,
        "ticket_ids": list(package.tickets),
        "state": package.state,
        "bound_worker": package.bound_worker,
        "current": package.current,
        "revision": revision,
        "actor": actor,
    }
    payload.update(extra or {})
    subjects = [package.id] + list(package.tickets)
    if package.reservation_id:
        subjects.append(package.reservation_id)
    # Attempt/offer ids stay in the payload (as for offers): a subject must
    # resolve inside a workspace-scoped export bundle.
    return Event(
        id=new_record_id("event"),
        cursor=None,
        kind_=kind,
        category="package",
        timestamp=moment,
        subject_ids=subjects,
        operation_id=None,
        payload=payload,
        payload_version=EVENT_PAYLOAD_VERSION,
    )


def sync_in_transaction(tx, intent, moment: str, statuses: dict) -> None:
    """Advance the ticket's live package for a lifecycle transition, inside its
    cascade `tx`. `statuses` holds every member's status *after* the ticket
    write (read by the lifecycle before this transaction opened).

    An acquisition binds an open package (or continues a bound one); a close
    advances `current` to the next member or completes the package (and its
    accepted offer). Nothing else moves the package: blocked/released members
    stay current."""
    ticket_id = intent.ticket_id
    stored = _live_in(tx).get(ticket_id)
    if stored is None:
        return
    package = stored.package
    revision = stored.revision
    statuses = dict(statuses or {})
    if intent.target_status:
        statuses[ticket_id] = intent.target_status
    state = progress(package, statuses)
    events = []
    actor = intent.actor

    attempt = intent.start_attempt
    if attempt is not None:
        worker = attempt.worker_id
        offer_id = (intent.offer_acceptance or {}).get("offer_id")
        if package.state == "open":
            package.state = "bound"
            package.bound_worker = worker
            package.bound_at = attempt.started
            events.append(("package_bound", {"worker_id": worker, "ticket_id": ticket_id,
                                             "offer_id": offer_id}))
        elif package.bound_worker != worker:
            # acquisition_grant refuses this first; a mismatch means the package
            # changed underneath the acquisition, so abort (the ticket write is
            # compensated) rather than record a member outside the contract.
            raise TicketPackaged(
                f"package {package.id} is bound to {package.bound_worker!r}; {worker!r} may "
                f"not start its member {ticket_id}",
                details={"reason": "bound_elsewhere", "package_id": package.id,
                         "ticket_id": ticket_id, "bound_worker": package.bound_worker},
            )
        events.append(("package_member_started", {
            "worker_id": worker, "ticket_id": ticket_id, "attempt_id": attempt.id,
            "position": package.tickets.index(ticket_id) + 1, "origin": intent.origin,
        }))

    ended_offers: List[str] = []
    if intent.target_status == "closed":
        if not state["remaining"]:
            package.state = "completed"
            package.outcome = f"all {len(package.tickets)} members closed"
            package.ended_at = moment
            package.ended_by = actor
            ended_offers = offers.end_package_offers(
                tx, package.id, how="complete", actor=actor, moment=moment,
                reason=f"package {package.id} completed")
            events.append(("package_completed", {"closed": ticket_id,
                                                 "completed": state["completed"],
                                                 "ended_offers": ended_offers}))
        else:
            events.append(("package_advanced", {
                "closed": ticket_id, "next": state["current"],
                "completed": state["completed"], "remaining": state["remaining"],
                "notice": "the next member is a fresh attempt: claim it, then read files again",
            }))

    if package.current == state["current"] and not events:
        return
    package.current = state["current"]
    package.updated = moment
    tx.put(package, expect_revision=revision)
    for kind, extra in events:
        tx.append_event(_package_event(kind, package, revision + 1, moment, actor=actor,
                                       extra=extra))


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_worker_id(value, label) -> str:
    value = str(value or "").strip()
    if not WORKER_ID_PATTERN.match(value):
        raise InvalidRecord(f"{label} {value!r} is not a valid worker id")
    return value


class PackageService:
    """Package operations bound to one `TicketLifecycle` (store + tickets)."""

    def __init__(self, lifecycle, *, actor: Optional[str] = None, clock=None):
        self.lifecycle = lifecycle
        self.store = lifecycle.coordination.store
        self.tickets = lifecycle.tickets
        self.actor = actor
        self._clock = clock or utc_now

    def now(self) -> str:
        return self._clock()

    # -- reads -----------------------------------------------------------

    def _all_tickets(self) -> dict:
        return {t.id: t for t in self.tickets.query(TicketQuery(buckets=("*",)))}

    def find(self, package_id: str) -> Optional[StoredPackage]:
        if _store_is_empty(self.store):
            return None
        with self.store.transaction(write=False) as tx:
            record = tx.get("package", package_id)
            if record is None:
                return None
            return StoredPackage(record, tx.revision_of("package", record.id))

    def get(self, package_id: str) -> StoredPackage:
        stored = self.find(package_id)
        if stored is None:
            raise CoordinationNotFound(f"no package {package_id!r}",
                                       details={"package_id": package_id})
        return stored

    def list(self, *, state: str = "live", ticket: Optional[str] = None,
             worker: Optional[str] = None) -> List[StoredPackage]:
        if state not in STATE_FILTERS:
            raise InvalidRecord(f"invalid state filter {state!r} (valid: {', '.join(STATE_FILTERS)})")
        if _store_is_empty(self.store):
            return []
        with self.store.transaction(write=False) as tx:
            rows = [StoredPackage(p, tx.revision_of("package", p.id)) for p in tx.find("package")]
        rows = [
            row for row in rows
            if (state == "all" or row.package.state == state
                or (state == "live" and row.package.is_live))
            and (ticket is None or ticket in row.package.tickets)
            and (worker is None or row.package.bound_worker == worker)
        ]
        return sorted(rows, key=lambda row: (row.package.created, row.package.id))

    def view(self, stored: StoredPackage, *, by_id: Optional[dict] = None) -> dict:
        """The documented JSON shape: record fields, revision, per-member status
        and latest attempt, derived progress, external prerequisites, the
        package's live offer, and any combined-graph cycle now present."""
        package = stored.package
        by_id = by_id if by_id is not None else self._all_tickets()
        data = package.to_dict()
        data["revision"] = stored.revision
        state = progress(package, lambda tid: by_id[tid].status if tid in by_id else None)
        members = []
        for position, ticket_id in enumerate(package.tickets, start=1):
            ticket = by_id.get(ticket_id)
            entry = {"ticket_id": ticket_id, "position": position,
                     "status": ticket.status if ticket else "missing",
                     "title": ticket.title if ticket else None,
                     "assignee": ticket.assignee if ticket else None,
                     "current": ticket_id == state["current"]}
            attempts = self.lifecycle.attempts_for(ticket_id)
            if attempts:
                last = attempts[-1]
                entry["latest_attempt"] = {
                    "attempt_id": last.id, "worker_id": last.worker_id, "state": last.state,
                    "generation": last.generation, "started": last.started,
                    "ended": last.ended, "outcome": last.outcome, "handoff": last.handoff,
                }
            members.append(entry)
        data["members"] = members
        data["progress"] = state
        prerequisites = external_prerequisites(package, by_id)
        data["external_prerequisites"] = prerequisites
        current = state["current"]
        data["waiting_on"] = [p for p in prerequisites if p["member"] == current and p["blocks"]]
        data["next_action"] = self._next_action(package, state, by_id, data["waiting_on"])
        live = offers.live_offers(self.store)
        offer = next((live[t].offer for t in package.tickets
                      if t in live and live[t].offer.package_id == package.id), None)
        data["offer_id"] = offer.id if offer is not None else None
        data["offer_state"] = offer.state if offer is not None else None
        orders = [s.package.tickets for s in {id(v): v for v in live_packages(self.store).values()}.values()]
        if package.is_live and package.tickets not in orders:
            orders.append(package.tickets)
        data["cycles"] = cycles_for(by_id, orders, package.tickets) if package.is_live else []
        return data

    @staticmethod
    def _next_action(package, state, by_id, waiting) -> str:
        if not package.is_live:
            return f"none: package is {package.state}"
        current = state["current"]
        ticket = by_id.get(current)
        status = ticket.status if ticket else "missing"
        who = package.bound_worker or "the first eligible worker"
        if waiting:
            return (f"paused: {current} waits on external prerequisite(s) "
                    f"{', '.join(p['depends_on'] for p in waiting)}")
        if status == "open":
            return f"{who} claims {current} (fresh attempt; read files again)"
        if status == "in_progress":
            return f"{ticket.assignee or who} is working {current}"
        return (f"{current} is {status}: needs an explicit resolution (unblock/unshelve/close, "
                "or 'arbite package handoff'); the package does not advance by itself")

    # -- writes ----------------------------------------------------------

    def create(self, tickets: Iterable[str], *, agent: str, note: Optional[str] = None,
               force: bool = False, reason: Optional[str] = None) -> PackageChange:
        """Create an open package over 2+ open tickets, in the given order.

        Refused as a whole (`package_conflict`) when a member is unavailable
        (closed/not open/assigned/active attempt/offered/already packaged/
        reserved by someone else) or the combined dependency + package-order
        graph would contain a cycle."""
        agent = _require_worker_id(agent, "agent")
        terms = [str(t).strip() for t in tickets if str(t).strip()]
        with self.lifecycle.locked():
            ids = [self.tickets.get(term, unique=True).id for term in terms]
            repeated = sorted({t for t in ids if ids.count(t) > 1})
            if repeated:
                raise PackageConflict(
                    f"a package lists each ticket once; repeated: {', '.join(repeated)}",
                    details={"reason": "duplicate_member", "ticket_ids": repeated})
            if len(ids) < 2:
                raise InvalidRecord("a package orders at least two tickets")
            by_id = self._all_tickets()
            held = reservations.active_reservations(self.store)
            live_offers_ = offers.live_offers(self.store)
            forced = bool(force and _clean(reason))
            conflicts = []
            reservation_id = None
            for ticket_id in ids:
                ticket = by_id[ticket_id]
                attempt = self.lifecycle.active_attempt(ticket_id)
                reservation = held.get(ticket_id)
                problem = None
                if ticket.status != "open":
                    problem = {"reason": "not_open", "status": ticket.status}
                elif attempt is not None:
                    problem = {"reason": "active_attempt", "attempt_id": attempt.id,
                               "worker_id": attempt.worker_id}
                elif ticket.assignee:
                    problem = {"reason": "assigned", "assignee": ticket.assignee}
                elif ticket_id in live_offers_:
                    problem = {"reason": "offered", "offer_id": live_offers_[ticket_id].offer.id}
                elif reservation is not None and reservation.owner != agent and not forced:
                    problem = {"reason": "reserved_elsewhere", "reservation_id": reservation.id,
                               "owner": reservation.owner}
                if problem is not None:
                    conflicts.append(dict({"ticket_id": ticket_id}, **problem))
                if reservation is not None and reservation_id is None:
                    reservation_id = reservation.id
            with self.store.transaction() as tx:
                live = _live_in(tx)
                for ticket_id in ids:
                    if ticket_id in live:
                        conflicts.append({"ticket_id": ticket_id, "reason": "already_packaged",
                                          "package_id": live[ticket_id].package.id})
                if conflicts:
                    summary = ", ".join(f"{c['ticket_id']} ({c['reason']})" for c in conflicts)
                    raise PackageConflict(
                        f"refused: {len(conflicts)} member(s) cannot be packaged: {summary}; "
                        "nothing was created (all-or-nothing, one package per ticket)",
                        details={"reason": "members_unavailable", "conflicts": conflicts})
                orders = [s.package.tickets for s in {id(v): v for v in live.values()}.values()]
                cycles = cycles_for(by_id, orders + [ids], ids)
                if cycles:
                    chains = [" -> ".join(c + [c[0]]) for c in cycles]
                    raise PackageConflict(
                        "refused: package order plus ticket dependencies would form a cycle, so "
                        "these members could never become workable: " + "; ".join(chains)
                        + " (package order is a scheduling edge: each member waits for the "
                        "previous one)",
                        details={"reason": "cycle", "cycles": cycles})
                moment = self.now()
                package = Package(
                    id=new_record_id("package"), tickets=list(ids), created_by=agent,
                    created=moment, updated=moment, current=ids[0],
                    reservation_id=reservation_id, note=_clean(note),
                )
                if forced:
                    package.handoffs.append({"at": moment, "by": agent, "action": "create",
                                             "forced": True, "reason": _clean(reason)})
                problems = package.validate()
                if problems:
                    raise InvalidRecord(f"invalid package: {'; '.join(problems)}",
                                        details={"problems": problems})
                tx.put(package, expect_revision=0)
                event = tx.append_event(_package_event(
                    "package_created", package, 1, moment, actor=agent,
                    extra={"external_prerequisites": external_prerequisites(package, by_id),
                           "forced": forced, "reason": _clean(reason)}))
        return PackageChange(StoredPackage(package, 1), event)

    def claim(self, package_id: str, *, worker_id: str, declared_tier: Optional[str] = None):
        """Acquire the package's current member for `worker_id` (through the
        lifecycle, so every rule applies). An open package with a published
        package offer accepts that offer. Returns `AcquisitionResult`."""
        worker_id = _require_worker_id(worker_id, "agent")
        with self.lifecycle.locked():
            stored = self.get(package_id)
            package = stored.package
            if not package.is_live:
                raise PackageConflict(
                    f"package {package.id} is {package.state}; nothing left to claim",
                    details={"reason": "not_live", "package_id": package.id,
                             "state": package.state})
            by_id = self._all_tickets()
            current = progress(package, lambda t: by_id[t].status if t in by_id else None)["current"]
            live = offers.live_offer_for(self.store, current)
            offer_id = None
            if live is not None and live.offer.package_id == package.id and live.offer.is_published:
                offer_id = live.offer.id
            return self.lifecycle.acquire(by_id[current], worker_id=worker_id,
                                          declared_tier=declared_tier, offer_id=offer_id)

    def note(self, package_id: str, *, agent: str, text: str, force: bool = False,
             reason: Optional[str] = None) -> PackageChange:
        """Append a durable continuity note (for a resumed session)."""
        agent = _require_worker_id(agent, "agent")
        text = _clean(text)
        if not text:
            raise InvalidRecord("a package note needs non-empty text")
        with self.lifecycle.locked():
            stored = self.get(package_id)
            self._require_controller(stored.package, agent, force, reason, "note on")
            with self.store.transaction() as tx:
                package = tx.get("package", package_id)
                revision = tx.revision_of("package", package_id)
                moment = self.now()
                package.notes.append({"at": moment, "by": agent, "text": text,
                                      "current": package.current})
                package.updated = moment
                tx.put(package, expect_revision=revision)
                event = tx.append_event(_package_event(
                    "package_noted", package, revision + 1, moment, actor=agent,
                    extra={"text": text}))
        return PackageChange(StoredPackage(package, revision + 1), event)

    def handoff(self, package_id: str, *, agent: str, reason: Optional[str],
                to: Optional[str] = None, release: bool = False, note: Optional[str] = None,
                interrupt: bool = False, force: bool = False,
                expect_revision: Optional[int] = None) -> PackageChange:
        """Explicit handoff of the remaining members: rebind them to `to`, or
        `release` them (package ends `released`; members return to ordinary
        availability). Completed members are untouched. An active attempt on a
        member is refused unless `interrupt` (it is then interrupted through the
        lifecycle, releasing its claims); a member the old worker still holds
        while blocked keeps its status but loses the assignee, so its later
        resolution goes to the new worker."""
        agent = _require_worker_id(agent, "agent")
        if bool(to) == bool(release):
            raise InvalidRecord("a handoff either rebinds (--to WORKER) or releases (--release)")
        if to:
            to = _require_worker_id(to, "rebind target")
        why = _clean(reason)
        with self.lifecycle.locked():
            stored = self.get(package_id)
            package = stored.package
            check_revision("package", package.id, expect_revision, stored.revision)
            if not package.is_live:
                raise PackageConflict(
                    f"package {package.id} is {package.state}; only a live package can be "
                    "handed off",
                    details={"reason": "not_live", "package_id": package.id,
                             "state": package.state})
            if not why:
                raise PackageConflict(
                    "a package handoff requires a non-empty --reason (it is recorded)",
                    details={"reason": "reason_required", "package_id": package.id})
            self._require_controller(package, agent, force, reason, "hand off")
            if to and to == package.bound_worker:
                raise PackageConflict(
                    f"package {package.id} is already bound to {to!r}",
                    details={"reason": "same_worker", "package_id": package.id})
            if to:
                self._require_rebind_eligible(package, to, force)
            by_id = self._all_tickets()
            state = progress(package, lambda t: by_id[t].status if t in by_id else None)
            active = []
            for ticket_id in state["remaining"]:
                attempt = self.lifecycle.active_attempt(ticket_id)
                if attempt is not None:
                    active.append({"ticket_id": ticket_id, "attempt_id": attempt.id,
                                   "worker_id": attempt.worker_id})
            if active and not interrupt:
                raise PackageConflict(
                    f"package {package.id} member {active[0]['ticket_id']} has active attempt "
                    f"{active[0]['attempt_id']} ({active[0]['worker_id']}); a handoff never "
                    "cancels running work silently -- let it finish/close, or pass --interrupt",
                    details={"reason": "active_attempt", "package_id": package.id,
                             "active": active})
            for entry in active:
                attempt = self.lifecycle.active_attempt(entry["ticket_id"])
                self.lifecycle.interrupt_attempt(
                    attempt, agent=agent,
                    note=f"Attempt {attempt.id} ({attempt.worker_id}) interrupted by package "
                         f"{package.id} handoff: {why}",
                    reason=f"package {package.id} handoff: {why}",
                )
            cleared = self._clear_held_members(package, state["remaining"], agent, why)
            forced = agent not in self._controllers(package)

            with self.store.transaction() as tx:
                current = tx.get("package", package.id)
                revision = tx.revision_of("package", package.id)
                check_revision("package", package.id, stored.revision, revision)
                moment = self.now()
                previous_worker = current.bound_worker
                entry = {
                    "at": moment, "by": agent, "action": "release" if release else "rebind",
                    "from_worker": previous_worker, "to_worker": to, "reason": why,
                    "note": _clean(note), "completed": state["completed"],
                    "remaining": state["remaining"], "interrupted": active,
                    "cleared_assignee": cleared,
                    "forced": forced,
                }
                current.handoffs.append(entry)
                if _clean(note):
                    current.notes.append({"at": moment, "by": agent, "text": _clean(note),
                                          "current": state["current"]})
                current.current = state["current"]
                current.updated = moment
                if release:
                    current.state = "released"
                    current.ended_at = moment
                    current.ended_by = agent
                    current.outcome = f"released by {agent}: {why}"
                    ended = offers.end_package_offers(
                        tx, current.id, how="release", actor=agent, moment=moment,
                        reason=f"package {current.id} released: {why}")
                    kind = "package_released"
                else:
                    current.state = "bound"
                    current.bound_worker = to
                    current.bound_at = moment
                    ended = offers.end_package_offers(
                        tx, current.id, how="release", actor=agent, moment=moment,
                        reason=f"package {current.id} rebound from {previous_worker} to {to}: {why}")
                    kind = "package_rebound"
                tx.put(current, expect_revision=revision)
                event = tx.append_event(_package_event(
                    kind, current, revision + 1, moment, actor=agent,
                    extra=dict(entry, ended_offers=ended)))
        return PackageChange(StoredPackage(current, revision + 1), event, active, ended)

    # -- internals -------------------------------------------------------

    def _controllers(self, package: Package) -> set:
        owners = {package.created_by}
        if package.bound_worker:
            owners.add(package.bound_worker)
        held = reservations.active_reservations(self.store)
        for ticket_id in package.tickets:
            if ticket_id in held:
                owners.add(held[ticket_id].owner)
        return owners

    def _require_controller(self, package, agent, force, reason, action) -> None:
        owners = self._controllers(package)
        if agent in owners or (force and _clean(reason)):
            return
        hint = " (--force needs a non-empty --reason)" if force else ""
        raise PackageConflict(
            f"package {package.id} is controlled by {', '.join(sorted(owners))}; {agent!r} may "
            f"not {action} it{hint}",
            details={"reason": "not_controller", "package_id": package.id,
                     "controllers": sorted(owners), "agent": agent})

    def _require_rebind_eligible(self, package, to, force) -> None:
        """A rebind may not side-step the *hard requirements* of the package's
        offer (tier, capabilities, locality, cost). The offer's `allowed_workers`
        is deliberately not consulted: it names who the current binding admits,
        and changing that binding is the point of a rebind. `force` skips even
        the hard requirements (an explicit administrative override)."""
        with self.store.transaction(write=False) as tx:
            found = [o for o in tx.find("offer")
                     if o.package_id == package.id and o.state in ("accepted", "published")]
        if not found or force:
            return
        declaration = workers.declaration_for(self.store, to)
        result = offers.evaluate(found[0], declaration, include_workers=False)
        if not result.eligible:
            raise offers._ineligible(found[0], package.tickets[0], result)

    def _clear_held_members(self, package, remaining, agent, why) -> List[str]:
        """Remaining members the old worker still holds without an attempt
        (blocked, or a legacy in_progress) keep their status but lose the
        assignee through a journaled transition, so the old worker cannot resume
        them and the new binding decides who works them next."""
        cleared = []
        old = package.bound_worker
        for ticket_id in remaining:
            ticket = self.tickets.get(ticket_id, unique=True)
            if not old or ticket.assignee != old or ticket.status == "closed":
                continue
            before = copy.deepcopy(ticket)
            schema.append_note(ticket, agent, f"Package {package.id} handoff cleared assignee "
                                              f"{old}: {why}")
            ticket.assignee = None
            ticket.updated = schema.now()
            self.lifecycle.commit_transition(
                "release", ticket, previous=before,
                events=[self.lifecycle.transition_event(
                    "package_handoff", ticket_id,
                    payload={"package_id": package.id, "from_worker": old, "reason": why})],
            )
            cleared.append(ticket_id)
        return cleared


__all__ = [
    "PACKAGE_NOTICE",
    "PackageChange",
    "PackageService",
    "STATE_FILTERS",
    "StoredPackage",
    "acquisition_refusal",
    "cycles_for",
    "edit_refusal",
    "external_prerequisites",
    "live_package_for",
    "live_packages",
    "may_pick",
    "progress",
    "sync_in_transaction",
]
