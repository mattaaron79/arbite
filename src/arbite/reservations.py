"""Coordinator reservations over explicit ticket sets (planning key B02).

A reservation says "these tickets are this coordinator's to hand out". It is
ownership of *who may acquire*, not execution: creating one never starts an
attempt, never sets `in_progress` and never changes a ticket. Execution still
happens only through `TicketLifecycle.acquire`, which consults
`acquisition_refusal` for every origin (claim, list-next claim, adopt, --force).

Rules (see `.arbite/planning/multi-provider-job-board.md`):

- **All-or-nothing.** Create/add resolves every requested ticket first and
  refuses the whole request (`ReservationConflict`, reason `members_unavailable`,
  one `conflicts[]` entry per ticket) when any member is closed, unknown, in
  another active reservation (no overlap, so no nesting), has an active attempt
  by someone other than the owner, or is assigned to someone else.
- **Epics are snapshots.** `--epic` resolves the epic's non-closed tickets once;
  tickets added to the epic later are not members until an explicit `add`.
- **Serial outcomes.** Every write holds the store operation lock (the lock
  acquisition holds) and settles pending lifecycle transitions first, then
  writes the record and its event in one coordination transaction. So
  reserve-vs-claim resolves one way or the other: a claim that committed first
  makes the reservation refuse (`active_attempt_elsewhere`); a reservation that
  committed first makes the claim refuse (`ticket_reserved`).
- **Who may acquire a member.** The owner, or -- while a member has a published
  offer (B03) -- exactly the workers that offer admits (`offers.acquisition_grant`
  consults the offer first and falls back to `acquisition_refusal`).
- **Release / remove.** Refused while an affected member has an active attempt
  (reason `active_attempts`), unless `interrupt=True` with a reason: each such
  attempt is then ended `interrupted` through the lifecycle (claims released,
  ticket back to open) before the reservation changes. Releasing a quiescent
  reservation withdraws its published offers (same transaction) and returns its
  open members to ad-hoc availability; `remove` does the same for its members.
- **Owner-only changes.** Membership changes and release need the owner's id,
  or an explicit administrative `force` with a reason (recorded).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from . import workers
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    WORKER_ID_PATTERN,
    Event,
    Reservation,
    new_record_id,
    utc_now,
)
from .coordination_storage import check_revision
from .errors import (
    CoordinationNotFound,
    InvalidRecord,
    ReservationConflict,
    TicketNotFound,
    TicketReserved,
    AmbiguousTicketId,
)
from .query import TicketQuery

#: Shown with every reservation view: what a reservation does and does not do.
RESERVATION_NOTICE = (
    "a reservation restricts who may acquire its members; it does not start work, "
    "create attempts or set tickets in_progress"
)


@dataclass(frozen=True)
class StoredReservation:
    reservation: Reservation
    revision: int


@dataclass(frozen=True)
class ReservationChange:
    """The outcome of a reservation write."""

    stored: StoredReservation
    event: Optional[Event]
    added: List[str]
    removed: List[str]
    interrupted: List[dict]
    excluded: List[dict]
    #: Published offers withdrawn by this change (B03: release/remove).
    withdrawn_offers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Store-level helpers (used by lifecycle and list-next without a service)
# ---------------------------------------------------------------------------


def _store_is_empty(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return callable(probe) and not probe()


def active_reservations(store) -> Dict[str, Reservation]:
    """Ticket id -> the active reservation holding it (read-only)."""
    if store is None or _store_is_empty(store):
        return {}
    with store.transaction(write=False) as tx:
        found = list(tx.find("reservation", state="active"))
    held: Dict[str, Reservation] = {}
    for reservation in sorted(found, key=lambda r: (r.created, r.id)):
        for member in reservation.members:
            held.setdefault(member, reservation)
    return held


def reservation_for(store, ticket_id: str) -> Optional[Reservation]:
    """The active reservation holding `ticket_id`, or None."""
    return active_reservations(store).get(ticket_id)


def may_acquire(reservation: Optional[Reservation], worker_id: str) -> bool:
    """Whether `worker_id` may acquire a member of `reservation` directly
    (without an offer). Offers are consulted first by `offers.acquisition_grant`."""
    return reservation is None or worker_id == reservation.owner


def acquisition_refusal(store, ticket_id: str, worker_id: str) -> Optional[TicketReserved]:
    """The `TicketReserved` error to raise when `worker_id` may not acquire
    `ticket_id` because of a reservation, else None."""
    reservation = reservation_for(store, ticket_id)
    if may_acquire(reservation, worker_id):
        return None
    return TicketReserved(
        f"ticket {ticket_id} is reserved by {reservation.owner!r} (reservation "
        f"{reservation.id}); only the reservation owner may acquire it until the owner "
        "removes it or releases the reservation -- choose other work",
        details={
            "ticket_id": ticket_id,
            "worker_id": worker_id,
            "reservation_id": reservation.id,
            "owner": reservation.owner,
        },
    )


def require_acquirable(store, ticket_id: str, worker_id: str) -> None:
    refusal = acquisition_refusal(store, ticket_id, worker_id)
    if refusal is not None:
        raise refusal


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ReservationService:
    """Reservation operations bound to one `TicketLifecycle` (store + tickets)."""

    def __init__(self, lifecycle, *, actor: Optional[str] = None, clock=None):
        self.lifecycle = lifecycle
        self.store = lifecycle.coordination.store
        self.tickets = lifecycle.tickets
        self.actor = actor
        self._clock = clock or utc_now

    def now(self) -> str:
        return self._clock()

    # -- reads -----------------------------------------------------------

    def find(self, reservation_id: str) -> Optional[StoredReservation]:
        if _store_is_empty(self.store):
            return None
        with self.store.transaction(write=False) as tx:
            return self._find(tx, reservation_id)

    def get(self, reservation_id: str) -> StoredReservation:
        stored = self.find(reservation_id)
        if stored is None:
            raise CoordinationNotFound(
                f"no reservation {reservation_id!r}",
                details={"reservation_id": reservation_id},
            )
        return stored

    def list(self, *, state: str = "active", owner: Optional[str] = None,
             ticket: Optional[str] = None) -> List[StoredReservation]:
        if state not in ("active", "released", "all"):
            raise InvalidRecord(f"invalid state filter {state!r} (valid: active, released, all)")
        if _store_is_empty(self.store):
            return []
        with self.store.transaction(write=False) as tx:
            rows = [
                StoredReservation(r, tx.revision_of("reservation", r.id))
                for r in tx.find("reservation")
            ]
        rows = [
            row for row in rows
            if (state == "all" or row.reservation.state == state)
            and (owner is None or row.reservation.owner == owner)
            and (ticket is None or ticket in row.reservation.members)
        ]
        return sorted(rows, key=lambda row: (row.reservation.created, row.reservation.id))

    def view(self, stored: StoredReservation) -> dict:
        """The documented JSON shape: record fields, revision and each member's
        current ticket status/assignee/active attempt (observed now)."""
        reservation = stored.reservation
        data = reservation.to_dict()
        data["revision"] = stored.revision
        attempts = self._active_attempts()
        members = []
        for ticket_id in reservation.members:
            entry = {"ticket_id": ticket_id}
            try:
                ticket = self.tickets.get(ticket_id, unique=True)
            except (TicketNotFound, AmbiguousTicketId):
                entry.update(status="missing", title=None, assignee=None, active_attempt=None)
            else:
                attempt = attempts.get(ticket_id)
                entry.update(
                    status=ticket.status,
                    title=ticket.title,
                    assignee=ticket.assignee,
                    active_attempt=None if attempt is None else {
                        "attempt_id": attempt.id,
                        "worker_id": attempt.worker_id,
                        "generation": attempt.generation,
                    },
                )
            members.append(entry)
        data["member_status"] = members
        return data

    # -- writes ----------------------------------------------------------

    def create(
        self,
        owner: str,
        *,
        tickets: Optional[Iterable[str]] = None,
        epic: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ReservationChange:
        """Reserve an explicit ticket set, or a one-time snapshot of `epic`,
        for `owner`. All-or-nothing."""
        owner = str(owner or "").strip()
        if not WORKER_ID_PATTERN.match(owner):
            raise InvalidRecord(f"owner {owner!r} is not a valid worker id")
        with self.lifecycle.locked():
            self._require_owner_enabled(owner)
            members, excluded, source = self._resolve(tickets, epic)
            snapshots = self._snapshots(members)
            with self.store.transaction() as tx:
                self._require_available(tx, snapshots, owner=owner, reservation_id=None)
                moment = self.now()
                reservation = Reservation(
                    id=new_record_id("reservation"),
                    owner=owner,
                    members=members,
                    created=moment,
                    updated=moment,
                    source=source,
                    note=_clean(note),
                )
                tx.put(reservation, expect_revision=0)
                event = self._event(
                    tx, "reservation_created", reservation, 1, moment,
                    affected=members, extra={"source": source},
                )
        return ReservationChange(StoredReservation(reservation, 1), event, members, [], [], excluded)

    def add(
        self,
        reservation_id: str,
        *,
        agent: str,
        tickets: Optional[Iterable[str]] = None,
        epic: Optional[str] = None,
        force: bool = False,
        reason: Optional[str] = None,
        expect_revision: Optional[int] = None,
    ) -> ReservationChange:
        """Add members atomically. Existing members are left as they are."""
        with self.lifecycle.locked():
            stored = self._read_active(reservation_id, expect_revision)
            self._require_owner(stored.reservation, agent, force, reason, "add members to")
            requested, excluded, _source = self._resolve(tickets, epic)
            new = [t for t in requested if t not in stored.reservation.members]
            if not new:
                return ReservationChange(stored, None, [], [], [], excluded)
            snapshots = self._snapshots(new)
            with self.store.transaction() as tx:
                current = self._load_active(tx, stored.reservation.id, stored.revision)
                reservation = current.reservation
                self._require_available(tx, snapshots, owner=reservation.owner,
                                        reservation_id=reservation.id)
                moment = self.now()
                reservation.members = list(reservation.members) + new
                reservation.updated = moment
                tx.put(reservation, expect_revision=current.revision)
                revision = current.revision + 1
                event = self._event(
                    tx, "reservation_members_added", reservation, revision, moment,
                    affected=new, reason=reason,
                    extra={"added": new, "forced": self._forced(reservation, agent, force)},
                )
        return ReservationChange(StoredReservation(reservation, revision), event, new, [], [], excluded)

    def remove(
        self,
        reservation_id: str,
        *,
        agent: str,
        tickets: Iterable[str],
        interrupt: bool = False,
        force: bool = False,
        reason: Optional[str] = None,
        expect_revision: Optional[int] = None,
    ) -> ReservationChange:
        """Remove members atomically. Refused while a removed member has an
        active attempt unless `interrupt` (with a reason) ends it first."""
        with self.lifecycle.locked():
            stored = self._read_active(reservation_id, expect_revision)
            reservation = stored.reservation
            self._require_owner(reservation, agent, force, reason, "remove members from")
            wanted = self._resolve_ids(tickets, allow_missing=True)
            missing = [t for t in wanted if t not in reservation.members]
            if missing:
                raise ReservationConflict(
                    f"reservation {reservation.id} has no member(s) {', '.join(missing)}",
                    details={"reason": "not_a_member", "reservation_id": reservation.id,
                             "tickets": missing},
                )
            if set(wanted) >= set(reservation.members):
                raise ReservationConflict(
                    f"removing every member would leave reservation {reservation.id} empty; "
                    "release it instead",
                    details={"reason": "would_empty", "reservation_id": reservation.id},
                )
            interrupted = self._settle_active(reservation, wanted, agent=agent,
                                              interrupt=interrupt, reason=reason)
            with self.store.transaction() as tx:
                current = self._load_active(tx, reservation.id, stored.revision)
                reservation = current.reservation
                moment = self.now()
                withdrawn = self._withdraw_offers(tx, wanted, reservation, agent, reason,
                                                  moment, "removed from")
                reservation.members = [t for t in reservation.members if t not in wanted]
                reservation.updated = moment
                tx.put(reservation, expect_revision=current.revision)
                revision = current.revision + 1
                event = self._event(
                    tx, "reservation_members_removed", reservation, revision, moment,
                    affected=wanted, reason=reason,
                    extra={"removed": wanted, "interrupted": interrupted,
                           "withdrawn_offers": withdrawn,
                           "forced": self._forced(reservation, agent, force)},
                )
        return ReservationChange(StoredReservation(reservation, revision), event, [], wanted,
                                 interrupted, [], withdrawn)

    def release(
        self,
        reservation_id: str,
        *,
        agent: str,
        reason: Optional[str] = None,
        interrupt: bool = False,
        force: bool = False,
        expect_revision: Optional[int] = None,
    ) -> ReservationChange:
        """Release the whole reservation. Refused while any member has an active
        attempt unless `interrupt` (with a reason) ends those attempts first."""
        with self.lifecycle.locked():
            stored = self._read_active(reservation_id, expect_revision)
            reservation = stored.reservation
            self._require_owner(reservation, agent, force, reason, "release")
            interrupted = self._settle_active(reservation, reservation.members, agent=agent,
                                              interrupt=interrupt, reason=reason)
            with self.store.transaction() as tx:
                current = self._load_active(tx, reservation.id, stored.revision)
                reservation = current.reservation
                moment = self.now()
                withdrawn = self._withdraw_offers(tx, reservation.members, reservation, agent,
                                                  reason, moment, "released")
                reservation.state = "released"
                reservation.released = moment
                reservation.released_by = agent
                reservation.release_reason = _clean(reason)
                reservation.updated = moment
                tx.put(reservation, expect_revision=current.revision)
                revision = current.revision + 1
                event = self._event(
                    tx, "reservation_released", reservation, revision, moment,
                    affected=list(reservation.members), reason=reason,
                    extra={"interrupted": interrupted, "withdrawn_offers": withdrawn,
                           "forced": self._forced(reservation, agent, force)},
                )
        return ReservationChange(StoredReservation(reservation, revision), event, [],
                                 list(reservation.members), interrupted, [], withdrawn)

    # -- internals -------------------------------------------------------

    def _withdraw_offers(self, tx, members, reservation, agent, reason, moment, how) -> List[str]:
        """Withdraw published offers on `members` inside the reservation write."""
        from . import offers  # offers imports this module

        why = f"reservation {reservation.id} {how}" + (f": {_clean(reason)}" if _clean(reason) else "")
        if how == "removed from":
            why = f"ticket removed from reservation {reservation.id}" + (
                f": {_clean(reason)}" if _clean(reason) else "")
        return offers.withdraw_in_transaction(tx, members, actor=agent, reason=why, moment=moment)

    def _find(self, tx, reservation_id: str) -> Optional[StoredReservation]:
        record = tx.get("reservation", reservation_id)
        if record is None:
            return None
        return StoredReservation(record, tx.revision_of("reservation", record.id))

    def _load_active(self, tx, reservation_id, expect_revision) -> StoredReservation:
        stored = self._find(tx, reservation_id)
        if stored is None:
            raise CoordinationNotFound(
                f"no reservation {reservation_id!r}", details={"reservation_id": reservation_id}
            )
        check_revision("reservation", stored.reservation.id, expect_revision, stored.revision)
        if not stored.reservation.is_active:
            raise ReservationConflict(
                f"reservation {stored.reservation.id} was released at "
                f"{stored.reservation.released}; create a new one instead",
                details={"reason": "not_active", "reservation_id": stored.reservation.id},
            )
        return stored

    def _read_active(self, reservation_id, expect_revision) -> StoredReservation:
        with self.store.transaction(write=False) as tx:
            return self._load_active(tx, reservation_id, expect_revision)

    def _active_attempts(self) -> dict:
        if _store_is_empty(self.store):
            return {}
        with self.store.transaction(write=False) as tx:
            found = list(tx.find("work_attempt", state="active"))
        return {a.ticket_id: a for a in sorted(found, key=lambda a: a.generation)}

    def _require_owner_enabled(self, owner: str) -> None:
        declaration = workers.declaration_for(self.store, owner)
        if not declaration.enabled:
            from . import eligibility

            eligibility.evaluate(eligibility.Requirements(restricted=False), declaration).require(
                subject="a reservation"
            )

    def _require_owner(self, reservation, agent, force, reason, action) -> None:
        if agent == reservation.owner:
            return
        if force and _clean(reason):
            return
        hint = " (--force needs a non-empty --reason)" if force else ""
        raise ReservationConflict(
            f"reservation {reservation.id} is owned by {reservation.owner!r}; {agent!r} may "
            f"not {action} it{hint} -- pass --agent {reservation.owner}, or --force --reason "
            "<why> for an explicit administrative change",
            details={"reason": "not_owner", "reservation_id": reservation.id,
                     "owner": reservation.owner, "agent": agent},
        )

    @staticmethod
    def _forced(reservation, agent, force) -> bool:
        return bool(force) and agent != reservation.owner

    def _resolve_ids(self, terms, *, allow_missing=False) -> List[str]:
        out: List[str] = []
        for term in terms or ():
            term = str(term).strip()
            if not term:
                continue
            try:
                ticket_id = self.tickets.get(term, unique=True).id
            except TicketNotFound:
                if not allow_missing:
                    raise
                ticket_id = term
            if ticket_id not in out:
                out.append(ticket_id)
        return out

    def _snapshots(self, members) -> dict:
        return {t: self.tickets.get(t, unique=True) for t in members}

    def _resolve(self, tickets, epic):
        """(member ids, excluded entries, source) for an explicit list or epic."""
        explicit = [t for t in (tickets or ()) if str(t).strip()]
        if bool(explicit) == bool(epic):
            raise InvalidRecord("name the member tickets explicitly or give --epic, not both/neither")
        if epic:
            rows = self.tickets.query(TicketQuery(epic=epic, buckets=("*",)))
            rows = sorted(rows, key=lambda t: (t.priority_sort_key(), t.id))
            members = [t.id for t in rows if t.status != "closed"]
            excluded = [{"ticket_id": t.id, "reason": "closed"} for t in rows if t.status == "closed"]
            if not members:
                raise ReservationConflict(
                    f"epic {epic!r} has no non-closed tickets to reserve",
                    details={"reason": "empty_epic", "epic": epic, "excluded": excluded},
                )
            source = {"kind": "epic", "epic": epic, "resolved_at": self.now(),
                      "excluded": [e["ticket_id"] for e in excluded]}
            return members, excluded, source
        conflicts = []
        members: List[str] = []
        for term in explicit:
            term = str(term).strip()
            try:
                ticket_id = self.tickets.get(term, unique=True).id
            except (TicketNotFound, AmbiguousTicketId) as e:
                conflicts.append({"ticket_id": term, "reason": "not_found", "detail": str(e)})
                continue
            if ticket_id not in members:
                members.append(ticket_id)
        if conflicts:
            raise self._unavailable(conflicts)
        return members, [], {"kind": "tickets"}

    def _require_available(self, tx, snapshots, *, owner, reservation_id) -> None:
        held = {}
        for reservation in tx.find("reservation", state="active"):
            for member in reservation.members:
                held.setdefault(member, reservation)
        active = {}
        for attempt in tx.find("work_attempt", state="active"):
            active[attempt.ticket_id] = attempt
        offered = {}
        for offer in tx.find("offer"):
            if offer.is_live:
                for ticket in offer.tickets:
                    offered.setdefault(ticket, offer)
        conflicts = []
        for ticket_id, ticket in snapshots.items():
            other = held.get(ticket_id)
            attempt = active.get(ticket_id)
            if other is not None and other.id != reservation_id:
                conflicts.append({
                    "ticket_id": ticket_id, "reason": "already_reserved",
                    "reservation_id": other.id, "owner": other.owner,
                    "detail": "reservations cannot overlap or nest"
                              + ("; add it to that reservation instead" if other.owner == owner else ""),
                })
            elif ticket.status == "closed":
                conflicts.append({"ticket_id": ticket_id, "reason": "closed"})
            elif attempt is not None and attempt.worker_id != owner:
                conflicts.append({
                    "ticket_id": ticket_id, "reason": "active_attempt_elsewhere",
                    "attempt_id": attempt.id, "worker_id": attempt.worker_id,
                })
            elif ticket.assignee and ticket.assignee != owner:
                conflicts.append({
                    "ticket_id": ticket_id, "reason": "assigned_elsewhere",
                    "assignee": ticket.assignee, "status": ticket.status,
                })
            elif ticket_id in offered and offered[ticket_id].publisher != owner:
                offer = offered[ticket_id]
                conflicts.append({
                    "ticket_id": ticket_id, "reason": "offered_elsewhere",
                    "offer_id": offer.id, "publisher": offer.publisher, "state": offer.state,
                })
        if conflicts:
            raise self._unavailable(conflicts)

    @staticmethod
    def _unavailable(conflicts) -> ReservationConflict:
        summary = ", ".join(f"{c['ticket_id']} ({c['reason']})" for c in conflicts)
        return ReservationConflict(
            f"refused: {len(conflicts)} requested ticket(s) cannot be reserved: {summary}; "
            "nothing was reserved (all-or-nothing)",
            details={"reason": "members_unavailable", "conflicts": conflicts},
        )

    def _settle_active(self, reservation, members, *, agent, interrupt, reason) -> List[dict]:
        """Refuse (or, with `interrupt`, end) active attempts on `members`."""
        attempts = self._active_attempts()
        busy = [attempts[t] for t in members if t in attempts]
        if not busy:
            return []
        listing = [
            {"ticket_id": a.ticket_id, "attempt_id": a.id, "worker_id": a.worker_id}
            for a in busy
        ]
        if not interrupt:
            raise ReservationConflict(
                f"{len(busy)} member(s) of reservation {reservation.id} have active attempts "
                f"({', '.join(f'{a.ticket_id} by {a.worker_id}' for a in busy)}); refusing to "
                "detach running work -- wait for it to finish, or pass --interrupt --reason "
                "<why> for an explicit administrative interruption",
                details={"reason": "active_attempts", "reservation_id": reservation.id,
                         "active": listing},
            )
        if not _clean(reason):
            raise ReservationConflict(
                "--interrupt ends other workers' attempts and therefore requires a non-empty "
                "--reason",
                details={"reason": "reason_required", "reservation_id": reservation.id,
                         "active": listing},
            )
        for attempt in busy:
            self.lifecycle.interrupt_attempt(
                attempt, agent=agent,
                note=f"Attempt {attempt.id} ({attempt.worker_id}) interrupted by reservation "
                     f"{reservation.id}: {reason}",
                reason=f"reservation {reservation.id}: {reason}",
            )
        return listing

    def _event(self, tx, kind, reservation, revision, moment, *, affected,
               reason=None, extra=None) -> Event:
        payload = {
            "reservation_id": reservation.id,
            "owner": reservation.owner,
            "revision": revision,
            "state": reservation.state,
            "members": list(reservation.members),
            "actor": self.actor,
            "reason": _clean(reason),
        }
        payload.update(extra or {})
        event = Event(
            id=new_record_id("event"),
            cursor=None,
            kind_=kind,
            category="reservation",
            timestamp=moment,
            subject_ids=[reservation.id] + list(affected),
            operation_id=None,
            payload=payload,
            payload_version=EVENT_PAYLOAD_VERSION,
        )
        return tx.append_event(event)


__all__ = [
    "RESERVATION_NOTICE",
    "ReservationChange",
    "ReservationService",
    "StoredReservation",
    "acquisition_refusal",
    "active_reservations",
    "may_acquire",
    "require_acquirable",
    "reservation_for",
]
