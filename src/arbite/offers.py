"""Offers and direct assignments with atomic worker pickup (planning key B03).

An offer says "this ticket may be picked up by any worker meeting these
requirements" (`public`) or "by exactly these workers" (`assigned`, a direct
assignment). A reservation owner publishes offers over its members without
giving up the reservation; workers then accept directly through the store with
`arbite offer claim` (or a plain `claim` / `list next --claim`, which route
through the same offer) -- the owner need not be online.

Rules (see `.arbite/planning/multi-provider-job-board.md`):

- **Acceptance is acquisition.** There is no separate "accept" write: the offer
  is advanced by `TicketLifecycle.acquire`, whose lifecycle intent carries
  `offer_acceptance`; the cascade transaction that stores the new attempt also
  moves the offer `published -> accepted` (revision-checked). Acquisition holds
  the store operation lock and the ticket compare-and-swap decides between
  racers, so two workers can never both accept: the loser sees `offer_conflict`
  (`not_published`) or a claimed ticket, and has no attempt.
- **One decision point.** `acquisition_grant` decides who may acquire a ticket
  that is offered or reserved, for every origin (claim, list-next claim,
  --adopt, --force). A published offer admits exactly the workers that pass
  its *restricted* requirements (unknown worker data fails) -- the reservation
  owner included, so nobody bypasses a direct assignment with a plain claim or
  --force. Without a published offer the B02 reservation rule applies.
- **Requirements vs preferences.** `requirements` (min tier, capabilities,
  local-only, cost ceiling) and `allowed_workers` can refuse a worker;
  `preferences` (prefer local / low cost / named workers) are stored and shown
  but never enforced: the first eligible claimant wins (`OFFER_NOTICE`).
- **Withdraw vs accept is serial.** Withdrawal takes the same lock: an offer
  withdrawn first can no longer be accepted; an offer accepted first refuses
  withdrawal (`offer_conflict`, reason `accepted`) unless `--interrupt --reason`
  explicitly interrupts the worker's attempt through the normal lifecycle.
- **Offers track their ticket.** Every lifecycle transition syncs live offers in
  its cascade (`sync_in_transaction`): closing the ticket completes an accepted
  offer (and cancels a published one); a transition that leaves the ticket
  without the accepting worker (release, takeover, unshelve, unblock --open)
  cancels it. A cancelled offer is history; the owner may publish again.
- **Reservation release withdraws.** Releasing (or removing members from) a
  reservation withdraws its published offers in the same transaction that
  writes the reservation (`withdraw_in_transaction`).

Package offers (B04) will reuse `Offer.tickets` (ordered) and `target_kind`;
only single-ticket offers exist today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from . import eligibility, reservations, workers
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    OFFER_STATES,
    WORKER_ID_PATTERN,
    Event,
    Offer,
    new_record_id,
    utc_now,
)
from .coordination_storage import check_revision
from .errors import (
    CoordinationNotFound,
    InvalidRecord,
    OfferConflict,
    TicketOffered,
    WorkerIneligible,
)

#: Shown with every offer view: preferences are not winner guarantees.
OFFER_NOTICE = (
    "requirements are enforced at acquisition; preferences are hints only -- the first "
    "eligible worker to claim wins. Use 'arbite offer assign' to choose the worker"
)

#: `offer list --state` filters: a single state, `live` (published+accepted) or `all`.
STATE_FILTERS = tuple(OFFER_STATES) + ("live", "all")


@dataclass(frozen=True)
class StoredOffer:
    offer: Offer
    revision: int


@dataclass(frozen=True)
class OfferChange:
    """The outcome of an offer write (publish/withdraw)."""

    stored: StoredOffer
    event: Optional[Event]
    interrupted: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Store-level helpers (used by lifecycle, reservations and list-next)
# ---------------------------------------------------------------------------


def _store_is_empty(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return callable(probe) and not probe()


def _live_in(tx) -> Dict[str, StoredOffer]:
    held: Dict[str, StoredOffer] = {}
    found = [o for o in tx.find("offer") if o.is_live]
    for offer in sorted(found, key=lambda o: (o.created, o.id)):
        stored = StoredOffer(offer, tx.revision_of("offer", offer.id))
        for ticket in offer.tickets:
            held.setdefault(ticket, stored)
    return held


def live_offers(store) -> Dict[str, StoredOffer]:
    """Ticket id -> the live (published or accepted) offer targeting it."""
    if store is None or _store_is_empty(store):
        return {}
    with store.transaction(write=False) as tx:
        return _live_in(tx)


def published_offers(store) -> Dict[str, StoredOffer]:
    """Ticket id -> the published (acceptable) offer targeting it."""
    return {t: s for t, s in live_offers(store).items() if s.offer.is_published}


def live_offer_for(store, ticket_id: str) -> Optional[StoredOffer]:
    return live_offers(store).get(ticket_id)


def requirements_of(offer: Offer) -> eligibility.Requirements:
    """The offer's hard constraints, evaluated restricted (unknowns fail)."""
    req = offer.requirements or {}
    return eligibility.Requirements(
        min_tier=req.get("min_tier"),
        capabilities=tuple(req.get("capabilities") or ()),
        local_only=bool(req.get("local_only")),
        max_cost=dict(req["max_cost"]) if req.get("max_cost") else None,
        allowed_workers=tuple(offer.allowed_workers or ()),
        restricted=True,
    )


def evaluate(offer: Offer, declaration) -> eligibility.Eligibility:
    return eligibility.evaluate(requirements_of(offer), declaration)


def _ineligible(offer: Offer, ticket_id: str, result) -> WorkerIneligible:
    what = (
        f"directly assigned to {', '.join(offer.allowed_workers)}"
        if offer.mode == "assigned" else "offered publicly with requirements"
    )
    return WorkerIneligible(
        f"ticket {ticket_id} is {what} (offer {offer.id}); worker "
        f"{result.worker.worker_id} may not acquire it: "
        + "; ".join(reason.message for reason in result.reasons)
        + " -- choose other work",
        details={
            "worker_id": result.worker.worker_id,
            "ticket_id": ticket_id,
            "offer_id": offer.id,
            "mode": offer.mode,
            "allowed_workers": list(offer.allowed_workers),
            "subject": f"offer {offer.id}",
            "reasons": [reason.to_dict() for reason in result.reasons],
            "notes": [note.to_dict() for note in result.notes],
        },
    )


def _not_published(offer: Offer, revision: int) -> OfferConflict:
    detail = {
        "accepted": f"was already accepted by {offer.accepted_by} (attempt {offer.attempt_id})",
        "withdrawn": f"was withdrawn at {offer.ended_at}",
        "completed": f"is completed (accepted by {offer.accepted_by})",
        "cancelled": f"was cancelled at {offer.ended_at} ({offer.end_reason})",
    }.get(offer.state, f"is {offer.state}")
    return OfferConflict(
        f"offer {offer.id} {detail}; it cannot be accepted -- choose other work",
        details={"reason": "not_published", "offer_id": offer.id, "state": offer.state,
                 "revision": revision, "accepted_by": offer.accepted_by,
                 "ticket_ids": list(offer.tickets)},
    )


def acquisition_grant(
    store,
    ticket_id: str,
    worker_id: str,
    *,
    offer_id: Optional[str] = None,
    declared_tier: Optional[str] = None,
) -> Optional[StoredOffer]:
    """Decide whether `worker_id` may acquire `ticket_id` given offers and
    reservations. Returns the published offer the acquisition accepts, or None
    when no offer is involved; raises when refused.

    Called by `TicketLifecycle.acquire` under the operation lock, for every
    origin. `offer_id` (from `arbite offer claim`) must name the ticket's
    published offer."""
    live = live_offer_for(store, ticket_id)
    if offer_id is not None:
        if live is None or live.offer.id != offer_id:
            named = None
            if store is not None and not _store_is_empty(store):
                with store.transaction(write=False) as tx:
                    named = tx.get("offer", offer_id)
                    revision = tx.revision_of("offer", offer_id) if named else 0
            if named is None:
                raise CoordinationNotFound(f"no offer {offer_id!r}", details={"offer_id": offer_id})
            if ticket_id not in named.tickets:
                raise OfferConflict(
                    f"offer {offer_id} does not target ticket {ticket_id}",
                    details={"reason": "not_for_ticket", "offer_id": offer_id,
                             "ticket_id": ticket_id},
                )
            raise _not_published(named, revision)
        if not live.offer.is_published:
            raise _not_published(live.offer, live.revision)
    if live is not None and live.offer.is_published:
        declaration = workers.declaration_for(store, worker_id, declared_tier=declared_tier)
        result = evaluate(live.offer, declaration)
        if not result.eligible:
            raise _ineligible(live.offer, ticket_id, result)
        return live
    reservations.require_acquirable(store, ticket_id, worker_id)
    return None


def acceptance_for(grant: Optional[StoredOffer], worker_id: str) -> Optional[dict]:
    """The `LifecycleIntent.offer_acceptance` payload for a granted acquisition."""
    if grant is None:
        return None
    return {"offer_id": grant.offer.id, "worker_id": worker_id,
            "expected_revision": grant.revision}


def may_pick(ticket_id: str, worker_id: Optional[str], declaration, *, published, held) -> bool:
    """Selection filter for `list next`: whether `worker_id` could acquire
    `ticket_id` as far as offers/reservations go. `acquisition_grant` re-checks."""
    stored = published.get(ticket_id)
    if stored is not None:
        return worker_id is not None and evaluate(stored.offer, declaration).eligible
    if ticket_id in held:
        return worker_id is not None and reservations.may_acquire(held[ticket_id], worker_id)
    return True


def edit_refusal(store, ticket, *, new_status, new_assignee) -> None:
    """Refuse a `set status=in_progress` / `set assignee=X` on a ticket with a
    published offer: that would hand the ticket out without accepting it."""
    live = live_offer_for(store, ticket.id)
    if live is None or not live.offer.is_published:
        return
    offer = live.offer
    details = {"ticket_id": ticket.id, "offer_id": offer.id, "mode": offer.mode,
               "allowed_workers": list(offer.allowed_workers)}
    if new_status == "in_progress" and ticket.status != "in_progress":
        raise TicketOffered(
            f"ticket {ticket.id} has published offer {offer.id}; refusing 'set status="
            f"in_progress' -- accept it with 'arbite offer claim {offer.id} --agent <id>'",
            details=dict(details, field="status", value=new_status),
        )
    if new_assignee and new_assignee != ticket.assignee:
        raise TicketOffered(
            f"ticket {ticket.id} has published offer {offer.id}; refusing 'set assignee="
            f"{new_assignee}' -- use 'arbite offer claim' (or withdraw the offer first)",
            details=dict(details, field="assignee", value=new_assignee),
        )


# ---------------------------------------------------------------------------
# Writes inside someone else's transaction
# ---------------------------------------------------------------------------


def _offer_event(kind, offer, revision, moment, *, actor, reason=None, extra=None) -> Event:
    payload = {
        "offer_id": offer.id,
        "mode": offer.mode,
        "ticket_ids": list(offer.tickets),
        "reservation_id": offer.reservation_id,
        "allowed_workers": list(offer.allowed_workers),
        "state": offer.state,
        "revision": revision,
        "actor": actor,
        "reason": reason,
    }
    payload.update(extra or {})
    subjects = [offer.id] + list(offer.tickets)
    if offer.reservation_id:
        subjects.append(offer.reservation_id)
    # The attempt id stays in the payload: a workspace-scoped export may not
    # carry that attempt, and a subject must resolve inside the bundle.
    return Event(
        id=new_record_id("event"),
        cursor=None,
        kind_=kind,
        category="offer",
        timestamp=moment,
        subject_ids=subjects,
        operation_id=None,
        payload=payload,
        payload_version=EVENT_PAYLOAD_VERSION,
    )


def _end(tx, offer: Offer, state: str, moment: str, *, actor, reason) -> Event:
    revision = tx.revision_of("offer", offer.id)
    offer.state = state
    offer.ended_at = moment
    offer.ended_by = actor
    offer.end_reason = reason
    offer.updated = moment
    tx.put(offer, expect_revision=revision)
    kind = {"withdrawn": "offer_withdrawn", "completed": "offer_completed",
            "cancelled": "offer_cancelled"}[state]
    return tx.append_event(_offer_event(kind, offer, revision + 1, moment, actor=actor,
                                        reason=reason,
                                        extra={"accepted_by": offer.accepted_by,
                                               "attempt_id": offer.attempt_id}))


def withdraw_in_transaction(tx, ticket_ids: Iterable[str], *, actor, reason, moment) -> List[str]:
    """Withdraw every published offer targeting `ticket_ids` inside `tx` (used
    by reservation release/remove). Accepted offers are left alone: their
    workers' attempts are settled by the caller first. Returns the offer ids."""
    wanted = set(ticket_ids)
    withdrawn = []
    for offer in tx.find("offer", state="published"):
        if wanted & set(offer.tickets):
            _end(tx, offer, "withdrawn", moment, actor=actor, reason=reason)
            withdrawn.append(offer.id)
    return sorted(withdrawn)


def sync_in_transaction(tx, intent, moment: str) -> None:
    """Advance offers for a lifecycle transition, inside its cascade `tx`.

    Existing live offers follow the ticket's target state; then, when the
    intent accepts an offer, the offer is moved to `accepted` at the revision
    the acquisition validated. A mismatch raises, which aborts the cascade and
    compensates the ticket write -- so no attempt exists without its offer."""
    ticket_id = intent.ticket_id
    actor = intent.actor
    why = intent.reason
    for offer in tx.find("offer"):
        if not offer.is_live or ticket_id not in offer.tickets:
            continue
        if offer.state == "accepted":
            if intent.target_status == "closed":
                _end(tx, offer, "completed", moment, actor=actor,
                     reason=f"ticket {ticket_id} closed")
            elif intent.target_assignee != offer.accepted_by:
                _end(tx, offer, "cancelled", moment, actor=actor,
                     reason=f"{intent.transition}: {offer.accepted_by} no longer holds "
                            f"ticket {ticket_id}" + (f" ({why})" if why else ""))
        elif intent.target_status == "closed":
            _end(tx, offer, "cancelled", moment, actor=actor,
                 reason=f"ticket {ticket_id} closed before the offer was accepted")

    acceptance = intent.offer_acceptance
    if not acceptance:
        return
    offer = tx.get("offer", acceptance["offer_id"])
    revision = tx.revision_of("offer", acceptance["offer_id"]) if offer is not None else 0
    if offer is None or ticket_id not in offer.tickets:
        raise OfferConflict(
            f"offer {acceptance['offer_id']} no longer targets ticket {ticket_id}",
            details={"reason": "not_for_ticket", "offer_id": acceptance["offer_id"],
                     "ticket_id": ticket_id},
        )
    if not offer.is_published or revision != acceptance.get("expected_revision"):
        raise _not_published(offer, revision)
    attempt = intent.start_attempt
    offer.state = "accepted"
    offer.accepted_by = acceptance["worker_id"]
    offer.accepted_at = attempt.started
    offer.attempt_id = attempt.id
    offer.updated = moment
    tx.put(offer, expect_revision=revision)
    tx.append_event(_offer_event(
        "offer_accepted", offer, revision + 1, attempt.started, actor=actor,
        extra={"worker_id": offer.accepted_by, "attempt_id": attempt.id,
               "origin": intent.origin},
    ))


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_requirements(*, min_tier=None, capabilities=(), local_only=False,
                       max_cost=None) -> dict:
    """A validated `Offer.requirements` mapping (only the keys that are set)."""
    caps = workers.normalise_capabilities(capabilities)
    # Validates tier/cost shape the same way evaluation will read them.
    eligibility.Requirements(min_tier=min_tier or None, capabilities=tuple(caps),
                             local_only=bool(local_only), max_cost=max_cost)
    out: dict = {}
    if min_tier:
        out["min_tier"] = min_tier
    if caps:
        out["capabilities"] = caps
    if local_only:
        out["local_only"] = True
    if max_cost is not None:
        out["max_cost"] = {"amount": max_cost["amount"], "unit": str(max_cost["unit"]).strip()}
    return out


def build_preferences(*, prefer_local=False, prefer_low_cost=False, prefer_workers=()) -> dict:
    out: dict = {}
    if prefer_local:
        out["prefer_local"] = True
    if prefer_low_cost:
        out["prefer_low_cost"] = True
    named = _worker_ids(prefer_workers, "preferred worker")
    if named:
        out["prefer_workers"] = named
    return out


def _worker_ids(values, label) -> List[str]:
    out: List[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if not WORKER_ID_PATTERN.match(part):
                raise InvalidRecord(f"{label} {part!r} is not a valid worker id")
            if part not in out:
                out.append(part)
    return out


class OfferService:
    """Offer operations bound to one `TicketLifecycle` (store + tickets)."""

    def __init__(self, lifecycle, *, actor: Optional[str] = None, clock=None):
        self.lifecycle = lifecycle
        self.store = lifecycle.coordination.store
        self.tickets = lifecycle.tickets
        self.actor = actor
        self._clock = clock or utc_now

    def now(self) -> str:
        return self._clock()

    # -- reads -----------------------------------------------------------

    def find(self, offer_id: str) -> Optional[StoredOffer]:
        if _store_is_empty(self.store):
            return None
        with self.store.transaction(write=False) as tx:
            record = tx.get("offer", offer_id)
            if record is None:
                return None
            return StoredOffer(record, tx.revision_of("offer", record.id))

    def get(self, offer_id: str) -> StoredOffer:
        stored = self.find(offer_id)
        if stored is None:
            raise CoordinationNotFound(f"no offer {offer_id!r}", details={"offer_id": offer_id})
        return stored

    def list(self, *, state: str = "live", ticket: Optional[str] = None,
             reservation: Optional[str] = None, worker: Optional[str] = None,
             declared_tier: Optional[str] = None) -> List[StoredOffer]:
        """Offers filtered by state/ticket/reservation. `worker` keeps only
        published offers that worker is currently eligible to accept."""
        if state not in STATE_FILTERS:
            raise InvalidRecord(f"invalid state filter {state!r} (valid: {', '.join(STATE_FILTERS)})")
        if _store_is_empty(self.store):
            return []
        with self.store.transaction(write=False) as tx:
            rows = [StoredOffer(o, tx.revision_of("offer", o.id)) for o in tx.find("offer")]
        rows = [
            row for row in rows
            if (state == "all" or row.offer.state == state
                or (state == "live" and row.offer.is_live))
            and (ticket is None or ticket in row.offer.tickets)
            and (reservation is None or row.offer.reservation_id == reservation)
        ]
        if worker is not None:
            declaration = workers.declaration_for(self.store, worker, declared_tier=declared_tier)
            rows = [row for row in rows
                    if row.offer.is_published and evaluate(row.offer, declaration).eligible]
        return sorted(rows, key=lambda row: (row.offer.created, row.offer.id))

    def view(self, stored: StoredOffer, *, worker: Optional[str] = None,
             declared_tier: Optional[str] = None) -> dict:
        """The documented JSON shape: record fields, revision, the target
        tickets' current status, and (with `worker`) that worker's eligibility."""
        offer = stored.offer
        data = offer.to_dict()
        data["revision"] = stored.revision
        data["requirements_view"] = requirements_of(offer).to_dict()
        data["preferences_enforced"] = False
        targets = []
        for ticket_id in offer.tickets:
            try:
                ticket = self.tickets.get(ticket_id, unique=True)
            except Exception:
                targets.append({"ticket_id": ticket_id, "status": "missing"})
                continue
            targets.append({"ticket_id": ticket_id, "title": ticket.title,
                            "status": ticket.status, "assignee": ticket.assignee,
                            "tier": ticket.tier})
        data["ticket_status"] = targets
        if worker is not None:
            declaration = workers.declaration_for(self.store, worker, declared_tier=declared_tier)
            data["eligibility"] = evaluate(offer, declaration).to_dict()
        return data

    # -- writes ----------------------------------------------------------

    def publish(
        self,
        ticket: str,
        *,
        agent: str,
        mode: str = "public",
        allowed_workers: Iterable[str] = (),
        requirements: Optional[dict] = None,
        preferences: Optional[dict] = None,
        note: Optional[str] = None,
        force: bool = False,
        reason: Optional[str] = None,
    ) -> OfferChange:
        """Publish a public offer, or (`mode='assigned'`) a direct assignment,
        over one open ticket. A reserved ticket needs its owner (or `force` with
        a reason); the reservation is kept."""
        agent = str(agent or "").strip()
        if not WORKER_ID_PATTERN.match(agent):
            raise InvalidRecord(f"agent {agent!r} is not a valid worker id")
        if mode not in ("public", "assigned"):
            raise InvalidRecord(f"invalid offer mode {mode!r} (valid: public, assigned)")
        allowed = _worker_ids(allowed_workers, "allowed worker")
        if mode == "assigned" and not allowed:
            raise InvalidRecord("a direct assignment names at least one worker")
        if mode == "public" and allowed:
            raise InvalidRecord("a public offer has no allowed workers; use 'offer assign'")
        requirements = dict(requirements or {})
        with self.lifecycle.locked():
            current = self.tickets.get(ticket, unique=True)
            reservation = reservations.reservation_for(self.store, current.id)
            forced = False
            if reservation is not None and agent != reservation.owner:
                if not (force and _clean(reason)):
                    hint = " (--force needs a non-empty --reason)" if force else ""
                    raise OfferConflict(
                        f"ticket {current.id} is reserved by {reservation.owner!r} (reservation "
                        f"{reservation.id}); only the owner may offer or assign it{hint}",
                        details={"reason": "not_owner", "ticket_id": current.id,
                                 "reservation_id": reservation.id, "owner": reservation.owner,
                                 "agent": agent},
                    )
                forced = True
            self._require_offerable(current)
            with self.store.transaction() as tx:
                existing = _live_in(tx).get(current.id)
                if existing is not None:
                    raise OfferConflict(
                        f"ticket {current.id} already has {existing.offer.state} offer "
                        f"{existing.offer.id}; withdraw it first (one live offer per ticket)",
                        details={"reason": "already_offered", "ticket_id": current.id,
                                 "offer_id": existing.offer.id, "state": existing.offer.state},
                    )
                moment = self.now()
                offer = Offer(
                    id=new_record_id("offer"),
                    tickets=[current.id],
                    mode=mode,
                    publisher=agent,
                    created=moment,
                    updated=moment,
                    reservation_id=reservation.id if reservation is not None else None,
                    requirements=requirements,
                    allowed_workers=allowed,
                    preferences=dict(preferences or {}),
                    note=_clean(note),
                    provenance={
                        "published_by": agent,
                        "actor": self.actor,
                        "reservation_owner": reservation.owner if reservation else None,
                        "forced": forced,
                        "reason": _clean(reason),
                    },
                )
                problems = offer.validate()
                if problems:
                    raise InvalidRecord(f"invalid offer: {'; '.join(problems)}",
                                        details={"problems": problems})
                tx.put(offer, expect_revision=0)
                event = tx.append_event(_offer_event(
                    "offer_published", offer, 1, moment, actor=agent, reason=_clean(reason),
                    extra={"requirements": dict(requirements),
                           "preferences": dict(offer.preferences), "forced": forced},
                ))
        return OfferChange(StoredOffer(offer, 1), event)

    def withdraw(
        self,
        offer_id: str,
        *,
        agent: str,
        reason: Optional[str] = None,
        force: bool = False,
        interrupt: bool = False,
        expect_revision: Optional[int] = None,
    ) -> OfferChange:
        """Withdraw a published offer (it can no longer be accepted). An
        accepted offer is refused unless `interrupt` with a reason explicitly
        interrupts the worker's active attempt (the offer is then cancelled)."""
        with self.lifecycle.locked():
            stored = self.get(offer_id)
            offer = stored.offer
            check_revision("offer", offer.id, expect_revision, stored.revision)
            self._require_controller(offer, agent, force, reason)
            if offer.state == "accepted":
                return self._withdraw_accepted(stored, agent=agent, reason=reason,
                                               interrupt=interrupt)
            if offer.state != "published":
                raise OfferConflict(
                    f"offer {offer.id} is {offer.state}; only a published offer can be withdrawn",
                    details={"reason": "not_live", "offer_id": offer.id, "state": offer.state},
                )
            with self.store.transaction() as tx:
                current = tx.get("offer", offer.id)
                check_revision("offer", offer.id, stored.revision, tx.revision_of("offer", offer.id))
                moment = self.now()
                event = _end(tx, current, "withdrawn", moment, actor=agent,
                             reason=_clean(reason) or "withdrawn")
        return OfferChange(StoredOffer(current, stored.revision + 1), event)

    def claim(self, offer_id: str, *, worker_id: str, declared_tier: Optional[str] = None):
        """Accept `offer_id` for `worker_id`: acquire its ticket through the
        lifecycle, which advances the offer atomically. Returns
        `(AcquisitionResult, StoredOffer)`."""
        with self.lifecycle.locked():
            stored = self.get(offer_id)
            ticket = self.tickets.get(stored.offer.tickets[0], unique=True)
            result = self.lifecycle.acquire(
                ticket, worker_id=worker_id, declared_tier=declared_tier, offer_id=offer_id
            )
            return result, self.get(offer_id)

    # -- internals -------------------------------------------------------

    def _require_offerable(self, ticket) -> None:
        problem = None
        if ticket.status != "open":
            problem = (f"ticket {ticket.id} is '{ticket.status}'; only an open ticket can be "
                       "offered", {"status": ticket.status})
        elif ticket.assignee:
            problem = (f"ticket {ticket.id} is assigned to {ticket.assignee!r}; clear the "
                       "assignee (or release it) before offering it", {"assignee": ticket.assignee})
        else:
            attempt = self.lifecycle.active_attempt(ticket.id)
            if attempt is not None:
                problem = (f"ticket {ticket.id} has active attempt {attempt.id} by "
                           f"{attempt.worker_id}", {"attempt_id": attempt.id,
                                                     "worker_id": attempt.worker_id})
        if problem is not None:
            message, extra = problem
            raise OfferConflict(message, details=dict({"reason": "ticket_unavailable",
                                                       "ticket_id": ticket.id}, **extra))

    def _require_controller(self, offer, agent, force, reason) -> None:
        """The publisher or the ticket's current reservation owner controls an
        offer; anyone else needs `force` with a reason."""
        owners = {offer.publisher}
        for ticket_id in offer.tickets:
            reservation = reservations.reservation_for(self.store, ticket_id)
            if reservation is not None:
                owners.add(reservation.owner)
        if agent in owners or (force and _clean(reason)):
            return
        hint = " (--force needs a non-empty --reason)" if force else ""
        raise OfferConflict(
            f"offer {offer.id} is controlled by {', '.join(sorted(owners))}; {agent!r} may not "
            f"withdraw it{hint}",
            details={"reason": "not_publisher", "offer_id": offer.id,
                     "controllers": sorted(owners), "agent": agent},
        )

    def _withdraw_accepted(self, stored, *, agent, reason, interrupt) -> OfferChange:
        offer = stored.offer
        attempt = self.lifecycle.active_attempt(offer.tickets[0])
        listing = []
        if attempt is not None:
            listing = [{"ticket_id": attempt.ticket_id, "attempt_id": attempt.id,
                        "worker_id": attempt.worker_id}]
        if not interrupt:
            raise OfferConflict(
                f"offer {offer.id} was accepted by {offer.accepted_by} (attempt "
                f"{offer.attempt_id}); withdrawing never cancels an active worker silently -- "
                "let the work finish, or pass --interrupt --reason <why>",
                details={"reason": "accepted", "offer_id": offer.id,
                         "accepted_by": offer.accepted_by, "attempt_id": offer.attempt_id,
                         "active": listing},
            )
        if not _clean(reason):
            raise OfferConflict(
                "--interrupt ends another worker's attempt and therefore requires a non-empty "
                "--reason",
                details={"reason": "reason_required", "offer_id": offer.id, "active": listing},
            )
        if attempt is not None:
            # The release transition's cascade cancels the offer (the accepting
            # worker no longer holds the ticket).
            self.lifecycle.interrupt_attempt(
                attempt, agent=agent,
                note=f"Attempt {attempt.id} ({attempt.worker_id}) interrupted by withdrawing "
                     f"offer {offer.id}: {reason}",
                reason=f"offer {offer.id} withdrawn: {reason}",
            )
            return OfferChange(self.get(offer.id), None, listing)
        # Accepted but no active attempt (e.g. the ticket is blocked): cancel
        # the offer record only; the ticket's fields are left as they are.
        with self.store.transaction() as tx:
            current = tx.get("offer", offer.id)
            event = _end(tx, current, "cancelled", self.now(), actor=agent,
                         reason=f"withdrawn: {reason}")
        return OfferChange(StoredOffer(current, stored.revision + 1), event, [])


__all__ = [
    "OFFER_NOTICE",
    "OfferChange",
    "OfferService",
    "STATE_FILTERS",
    "StoredOffer",
    "acceptance_for",
    "acquisition_grant",
    "build_preferences",
    "build_requirements",
    "edit_refusal",
    "evaluate",
    "live_offer_for",
    "live_offers",
    "may_pick",
    "published_offers",
    "requirements_of",
    "sync_in_transaction",
    "withdraw_in_transaction",
]
