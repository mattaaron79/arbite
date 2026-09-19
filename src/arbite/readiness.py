"""One readiness evaluator for the job board, list-next and acquisition (B05).

Readiness is *derived from current state*, never remembered: every caller loads
one `BoardState` (tickets, active attempts, reservations, published offers and
live packages) and evaluates a candidate against the *same* rules. `arbite board`
explains the verdict, `list next` selects with it, and `arbite.lifecycle.acquire`
re-checks the ticket axis under the operation lock before it writes.

What the vocabulary separates:

- **Hard exclusions** (`Readiness.reasons`) are grouped by `axis` so a caller can
  say *why* work is unavailable without matching on prose: `status`,
  `classification`, `dependency`, `attempt`, `reservation`, `offer`,
  `continuity`, `worker`, `capacity`. Every code in `REASON_CODES` is stable.
- **Advisory routing hints** (`Readiness.hints`) never exclude anything. The
  local/low-cost preferences carried by an offer are hints in passive
  first-eligible pickup: the first eligible claimant wins whatever the hints
  say. An owner who wants a preference *enforced* uses a hard constraint
  (`--local-only`, `--max-cost`, `--require-capability`, `--min-tier`,
  `--allowed-worker`, or an explicit `offer assign`).
- **Capacity** is the declared limit on *concurrent active attempts* for a
  worker id. Only active attempts count: a reservation is a hold, not work, and
  a later member of a continuity package has no attempt yet, so neither
  consumes capacity. A registered profile's capacity is enforced at acquisition
  (`TicketLifecycle.acquire`, under the operation lock) so a `--count N` batch or
  two concurrent claims cannot overfill a worker; a short batch is a correct
  result. Lowering `capacity` (or any other profile change) affects future
  acquisition only and never revokes a running attempt.

Profile values are operator assertions, not verified identity; see
`coordination.ATTRIBUTION_NOTICE`. A board query cannot promise that a ticket it
reports ready is still free when the caller acts on it: acquisition re-checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import eligibility, graph, offers, packages, reservations, schema, workers
from .errors import CoordinationConflict, TicketError, WorkerIneligible
from .query import sort_key

#: Stable reason codes for a hard exclusion, one per readiness axis.
REASON_CODES = (
    "ticket_not_open",
    "ticket_unclassified",
    "dependencies_unmet",
    "active_attempt",
    "worker_identity_required",
    "reserved_elsewhere",
    "offer_ineligible",
    "package_order",
    "package_bound_elsewhere",
    "worker_ineligible",
    "capacity_exhausted",
)

#: Stable codes for advisory routing hints (never blocking).
HINT_CODES = (
    "preference_satisfied",
    "preference_unsatisfied",
    "continuity_binding",
)

#: The exclusion axes, in the order a caller should explain them.
AXES = (
    "status",
    "classification",
    "dependency",
    "attempt",
    "reservation",
    "offer",
    "continuity",
    "worker",
    "capacity",
)

#: Repeated wherever `arbite board` reports a ready set.
QUERY_NOTICE = (
    "a board query explains current state and claims nothing: it cannot promise "
    "that a ticket reported ready is still free when you acquire it, and acquisition "
    "re-checks every condition under the operation lock"
)


@dataclass(frozen=True)
class ReadinessReason:
    """One exclusion (or advisory hint) with a stable `code` and an `axis`."""

    code: str
    axis: str
    message: str
    details: dict = field(default_factory=dict)
    hard: bool = True

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "axis": self.axis,
            "message": self.message,
            "hard": self.hard,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Capacity:
    """Declared concurrent-work capacity and the active attempts counted against it."""

    declared: Optional[int] = None
    active: int = 0

    @property
    def unlimited(self) -> bool:
        return self.declared is None

    @property
    def remaining(self) -> Optional[int]:
        """Free slots, or None when capacity is undeclared (unlimited)."""
        if self.declared is None:
            return None
        return max(0, self.declared - self.active)

    @property
    def exhausted(self) -> bool:
        return self.declared is not None and self.active >= self.declared

    def to_dict(self) -> dict:
        return {
            "declared": self.declared,
            "active": self.active,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
        }


@dataclass(frozen=True)
class BoardState:
    """One read of every store-level fact readiness needs.

    `by_id` holds *every* known ticket (buckets included): readiness is a
    property of the whole set, so a blocker filed in a bucket still blocks.
    """

    by_id: Dict[str, Any] = field(default_factory=dict)
    active_attempts: Dict[str, Any] = field(default_factory=dict)
    active_by_worker: Dict[str, int] = field(default_factory=dict)
    reservations: Dict[str, Any] = field(default_factory=dict)
    published_offers: Dict[str, Any] = field(default_factory=dict)
    packages: Dict[str, Any] = field(default_factory=dict)

    def status_of(self, ticket_id: str) -> Optional[str]:
        ticket = self.by_id.get(ticket_id)
        return ticket.status if ticket is not None else None

    def statuses(self) -> Dict[str, Optional[str]]:
        return {tid: ticket.status for tid, ticket in self.by_id.items()}

    def active_for(self, worker_id: Optional[str]) -> int:
        return self.active_by_worker.get(worker_id, 0) if worker_id else 0


@dataclass(frozen=True)
class Readiness:
    """The verdict for one ticket/worker pair, with hard reasons and hints."""

    ticket_id: str
    worker_id: Optional[str]
    ready: bool
    reasons: Tuple[ReadinessReason, ...] = ()
    hints: Tuple[ReadinessReason, ...] = ()
    capacity: Capacity = field(default_factory=Capacity)
    eligibility: Optional[eligibility.Eligibility] = None

    def by_axis(self, axis: str) -> List[ReadinessReason]:
        return [reason for reason in self.reasons if reason.axis == axis]

    def has_axis(self, *axes: str) -> bool:
        return any(reason.axis in axes for reason in self.reasons)

    def codes(self) -> List[str]:
        return [reason.code for reason in self.reasons]

    def to_dict(self) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "worker_id": self.worker_id,
            "ready": self.ready,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "hints": [hint.to_dict() for hint in self.hints],
            "capacity": self.capacity.to_dict(),
        }


def _store_is_empty(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return callable(probe) and not probe()


def load(store, tickets: Iterable) -> BoardState:
    """Read the store-level state readiness needs, once, for a whole query."""
    by_id = {ticket.id: ticket for ticket in tickets}
    if store is None or _store_is_empty(store):
        return BoardState(by_id=by_id)
    with store.transaction(write=False) as tx:
        found = list(tx.find("work_attempt", state="active"))
    active: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for attempt in sorted(found, key=lambda a: (a.ticket_id, a.generation)):
        # Highest generation wins if a store ever holds two active attempts.
        active[attempt.ticket_id] = attempt
        counts[attempt.worker_id] = counts.get(attempt.worker_id, 0) + 1
    return BoardState(
        by_id=by_id,
        active_attempts=active,
        active_by_worker=counts,
        reservations=reservations.active_reservations(store),
        published_offers=offers.published_offers(store),
        packages=packages.live_packages(store),
    )


# ---------------------------------------------------------------------------
# The ticket axis (shared verbatim with acquisition)
# ---------------------------------------------------------------------------


def ticket_reasons(ticket, by_id: dict, active_attempt=None) -> List[ReadinessReason]:
    """The status/classification/dependency/attempt exclusions for `ticket`.

    Pure, and the single source of truth for what `require_claimable` enforces:
    the reasons come back in the order acquisition reports them, so the first
    one is the error a refused claim carries.
    """
    reasons: List[ReadinessReason] = []
    if ticket.status != "open":
        hint = ""
        if ticket.status == "in_progress":
            hint = (
                f" -- use 'arbite claim {ticket.id} --agent <id> --adopt' for a legacy "
                "ticket with no attempt record, or '--force --reason <why>' to take over"
            )
        reasons.append(ReadinessReason(
            "ticket_not_open", "status",
            f"ticket {ticket.id} is '{ticket.status}', not 'open'; only an open ticket "
            f"can be claimed{hint}",
            {"status": ticket.status},
        ))
    for field_name in ("type", "tier", "domain"):
        value = getattr(ticket, field_name, None)
        if schema.is_placeholder(value):
            reasons.append(ReadinessReason(
                "ticket_unclassified", "classification",
                f"ticket {ticket.id} is not classified yet ({field_name} is {value!r}); "
                f"finish triage (arbite fetch / arbite set) before claiming it",
                {"field": field_name, "value": value},
            ))
    unmet = graph.unmet_dependencies(ticket, by_id)
    if unmet:
        reasons.append(ReadinessReason(
            "dependencies_unmet", "dependency",
            f"ticket {ticket.id} has unmet dependencies: {', '.join(sorted(unmet))}; "
            "close them before claiming it (readiness is enforced on the operation, "
            "not just in 'list next')",
            {"unmet": sorted(unmet)},
        ))
    if active_attempt is not None:
        reasons.append(ReadinessReason(
            "active_attempt", "attempt",
            f"ticket {ticket.id} already has an active attempt {active_attempt.id} "
            f"(worker {active_attempt.worker_id}, generation {active_attempt.generation}); "
            "only one attempt may be active per ticket -- release it first, or pass "
            "--force --reason <why> for an explicit administrative takeover",
            {
                "ticket_id": ticket.id,
                "attempt_id": active_attempt.id,
                "worker_id": active_attempt.worker_id,
                "generation": active_attempt.generation,
            },
        ))
    return reasons


def require_claimable(ticket, by_id: dict, active_attempt=None) -> None:
    """Raise unless `ticket` is ready to be freshly claimed (the acquisition guard)."""
    reasons = ticket_reasons(ticket, by_id, active_attempt)
    if not reasons:
        return
    first = reasons[0]
    if first.code == "active_attempt":
        raise CoordinationConflict(first.message, details=dict(first.details))
    raise TicketError(first.message)


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def active_attempt_count(store, worker_id: str) -> int:
    """Count `worker_id`'s active attempts, freshly (short read transaction)."""
    if store is None or _store_is_empty(store) or not worker_id:
        return 0
    with store.transaction(write=False) as tx:
        return len([
            attempt for attempt in tx.find("work_attempt", state="active")
            if attempt.worker_id == worker_id
        ])


def active_attempt_for(store, ticket_id: str):
    """The ticket's active attempt, or None (short read transaction)."""
    if store is None or _store_is_empty(store):
        return None
    with store.transaction(write=False) as tx:
        found = list(tx.find("work_attempt", ticket_id=ticket_id, state="active"))
    return max(found, key=lambda attempt: attempt.generation) if found else None


def capacity_for(state: BoardState, worker_id: Optional[str], declaration) -> Capacity:
    """Capacity as seen by a query: declared limit vs. the snapshot's active count."""
    return Capacity(declared=declaration.capacity, active=state.active_for(worker_id))


def capacity_reason(worker_id: str, capacity: Capacity, *, subject: Optional[str] = None) -> ReadinessReason:
    """The worker-wide capacity exclusion (`subject` is context, not a cause)."""
    return ReadinessReason(
        "capacity_exhausted", "capacity",
        f"worker {worker_id} has reached its declared capacity of {capacity.declared} "
        f"concurrent attempt(s) ({capacity.active} active); finish or release one "
        "before acquiring more work (capacity counts active attempts only)",
        dict(capacity.to_dict(), worker_id=worker_id, subject=subject),
    )


def require_capacity(
    store,
    worker_id: str,
    declaration,
    *,
    ticket_id: Optional[str] = None,
    superseded_attempt=None,
) -> Capacity:
    """Enforce the declaration's capacity, counting active attempts only.

    `superseded_attempt` is the live attempt this acquisition replaces (a
    takeover): when it belongs to the same worker the net occupancy is
    unchanged, so it is not counted twice. Raises `WorkerIneligible`
    (`capacity_exhausted`) when the worker is full.
    """
    counted = active_attempt_count(store, worker_id)
    if superseded_attempt is not None and getattr(superseded_attempt, "worker_id", None) == worker_id:
        counted = max(0, counted - 1)
    capacity = Capacity(declared=declaration.capacity, active=counted)
    if capacity.exhausted:
        reason = capacity_reason(worker_id, capacity,
                                 subject=f"ticket {ticket_id}" if ticket_id else None)
        raise WorkerIneligible(
            f"worker {worker_id} is at its declared capacity "
            f"({capacity.active} active attempt(s), capacity {capacity.declared}); "
            "capacity counts concurrent active attempts only -- finish or release one "
            "before acquiring more work (a short batch is a correct result)",
            details={
                "worker_id": worker_id,
                "subject": f"ticket {ticket_id}" if ticket_id else None,
                "capacity": capacity.to_dict(),
                "reasons": [reason.to_dict()],
            },
        )
    return capacity


# ---------------------------------------------------------------------------
# The full evaluator
# ---------------------------------------------------------------------------


def preference_hints(offer, declaration) -> List[ReadinessReason]:
    """Advisory hints for an offer's labelled preferences. Never blocking."""
    prefs = offer.preferences or {}
    hints: List[ReadinessReason] = []
    checks = (
        ("prefer_local", declaration.locality == "local",
         f"worker declares locality {declaration.locality!r}"),
        ("prefer_low_cost", declaration.cost_class == "local",
         f"worker declares cost class {declaration.cost_class!r}"),
        ("prefer_workers", declaration.worker_id in tuple(prefs.get("prefer_workers") or ()),
         "worker is not in the preferred list"),
    )
    for key, satisfied, why in checks:
        if not prefs.get(key):
            continue
        if key == "prefer_workers" and not prefs.get("prefer_workers"):
            continue
        hints.append(ReadinessReason(
            "preference_satisfied" if satisfied else "preference_unsatisfied",
            "offer",
            f"offer {offer.id} states {key}"
            + (f" and {why}" if satisfied else f"; {why} (a hint only -- the first "
               "eligible claimant wins, so use a hard constraint to enforce an owner's choice)"),
            {"offer_id": offer.id, "preference": key, "satisfied": bool(satisfied)},
            hard=False,
        ))
    return hints


def evaluate(
    ticket,
    state: BoardState,
    *,
    worker_id: Optional[str],
    declaration=None,
    requirements=None,
    capacity: Optional[Capacity] = None,
) -> Readiness:
    """Evaluate one candidate for one worker against the loaded state."""
    if declaration is None:
        declaration = eligibility.WorkerDeclaration.ad_hoc(worker_id or "")
    reasons: List[ReadinessReason] = list(
        ticket_reasons(ticket, state.by_id, state.active_attempts.get(ticket.id))
    )
    hints: List[ReadinessReason] = []

    if capacity is None:
        capacity = capacity_for(state, worker_id, declaration)

    # A live continuity package is consulted first, exactly like
    # `offers.acquisition_grant`: a bound package's current member is acquirable
    # by its bound worker without consulting that ticket's offer or reservation.
    package_hands_off = False
    package_stored = state.packages.get(ticket.id)
    if package_stored is not None:
        refusal = packages.acquisition_refusal(
            package_stored.package, ticket.id, worker_id or "", state.statuses()
        )
        if refusal is not None:
            reason_code = (refusal.details or {}).get("reason", "package_order")
            reasons.append(ReadinessReason(
                reason_code if reason_code in REASON_CODES else "package_order",
                "continuity", str(refusal), dict(refusal.details or {}),
            ))
        elif package_stored.package.state == "bound":
            package_hands_off = True
            hints.append(ReadinessReason(
                "continuity_binding", "continuity",
                f"ticket {ticket.id} continues package {package_stored.package.id}, bound to "
                f"{package_stored.package.bound_worker!r}; acquiring it continues the package",
                {"package_id": package_stored.package.id,
                 "bound_worker": package_stored.package.bound_worker,
                 "state": package_stored.package.state},
                hard=False,
            ))

    if not package_hands_off:
        # A published offer is consulted before the ticket's reservation, exactly
        # as `offers.acquisition_grant` does: the reservation owner delegated that
        # pickup, so an offer this worker is eligible for wins over the hold.
        stored_offer = state.published_offers.get(ticket.id)
        held = None if stored_offer is not None else state.reservations.get(ticket.id)
        if held is not None:
            details = {"ticket_id": ticket.id, "reservation_id": held.id, "owner": held.owner}
            if worker_id is None:
                reasons.append(ReadinessReason(
                    "worker_identity_required", "reservation",
                    f"ticket {ticket.id} is held by reservation {held.id} (owner {held.owner}); "
                    "pass --worker/--claim to match it against a worker id",
                    details,
                ))
            elif not reservations.may_acquire(held, worker_id):
                reasons.append(ReadinessReason(
                    "reserved_elsewhere", "reservation",
                    f"ticket {ticket.id} is reserved by {held.owner!r} (reservation {held.id}); "
                    "only the reservation owner may acquire it until the owner removes it or "
                    "releases the reservation -- choose other work",
                    dict(details, worker_id=worker_id),
                ))

        if stored_offer is not None:
            offer = stored_offer.offer
            if worker_id is None:
                reasons.append(ReadinessReason(
                    "worker_identity_required", "offer",
                    f"ticket {ticket.id} has published offer {offer.id}; pass --worker/--claim "
                    "to check whether that worker may accept it",
                    {"ticket_id": ticket.id, "offer_id": offer.id, "mode": offer.mode},
                ))
            else:
                result = offers.evaluate(offer, declaration)
                if not result.eligible:
                    what = (
                        f"directly assigned to {', '.join(offer.allowed_workers)}"
                        if offer.mode == "assigned" else "offered publicly with requirements"
                    )
                    reasons.append(ReadinessReason(
                        "offer_ineligible", "offer",
                        f"ticket {ticket.id} is {what} (offer {offer.id}); worker "
                        f"{worker_id} may not acquire it: "
                        + "; ".join(reason.message for reason in result.reasons)
                        + " -- choose other work (or ask the owner for a hard constraint change)",
                        {
                            "ticket_id": ticket.id, "offer_id": offer.id, "mode": offer.mode,
                            "allowed_workers": list(offer.allowed_workers),
                            "reasons": [r.to_dict() for r in result.reasons],
                            "notes": [n.to_dict() for n in result.notes],
                        },
                    ))
                hints.extend(preference_hints(offer, declaration))

    # The ticket's own classification requirements (the legacy tier rule), always
    # checked by acquisition.
    if requirements is None:
        requirements = eligibility.requirements_for_ticket(ticket)
    result = eligibility.evaluate(requirements, declaration)
    if not result.eligible:
        reasons.append(ReadinessReason(
            "worker_ineligible", "worker",
            f"worker {worker_id} may not acquire ticket {ticket.id}: "
            + "; ".join(reason.message for reason in result.reasons)
            + (" (a per-call tier cannot elevate a registered profile; change it with "
               "'arbite worker update --tier')"
               if any(r.code == "declared_tier_exceeds_profile" for r in result.reasons) else ""),
            {
                "worker_id": worker_id,
                "reasons": [r.to_dict() for r in result.reasons],
                "notes": [n.to_dict() for n in result.notes],
            },
        ))

    if capacity.exhausted:
        reasons.append(capacity_reason(worker_id or "", capacity))

    return Readiness(
        ticket_id=ticket.id,
        worker_id=worker_id,
        ready=not reasons,
        reasons=tuple(reasons),
        hints=tuple(hints),
        capacity=capacity,
        eligibility=result,
    )


def evaluate_all(
    tickets: Sequence,
    state: BoardState,
    *,
    worker_id: Optional[str],
    declaration=None,
    capacity: Optional[Capacity] = None,
) -> List[Tuple[Any, Readiness]]:
    """Evaluate every candidate, keeping the caller's order."""
    if declaration is None:
        declaration = eligibility.WorkerDeclaration.ad_hoc(worker_id or "")
    if capacity is None:
        capacity = capacity_for(state, worker_id, declaration)
    return [
        (ticket, evaluate(ticket, state, worker_id=worker_id, declaration=declaration,
                          capacity=capacity))
        for ticket in tickets
    ]


def suggestion_order(tickets: Sequence) -> List:
    """Ready work in the order `list next` offers it (most urgent first)."""
    return sorted(tickets, key=lambda ticket: sort_key("next", ticket))


def suggestions(results: Sequence[Tuple[Any, Readiness]], limit: Optional[int] = None) -> List:
    """The compatible ready work for a worker, most urgent first."""
    ready = [ticket for ticket, readiness in results if readiness.ready]
    ready = suggestion_order(ready)
    return ready if limit is None else ready[:limit]


__all__ = [
    "AXES",
    "BoardState",
    "Capacity",
    "HINT_CODES",
    "QUERY_NOTICE",
    "REASON_CODES",
    "Readiness",
    "ReadinessReason",
    "active_attempt_count",
    "active_attempt_for",
    "capacity_for",
    "capacity_reason",
    "evaluate",
    "evaluate_all",
    "load",
    "preference_hints",
    "require_capacity",
    "require_claimable",
    "suggestions",
    "ticket_reasons",
]
