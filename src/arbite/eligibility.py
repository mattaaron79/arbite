"""Worker eligibility vocabulary and evaluation (planning key B01).

Pure policy with no storage access: *what is known about a worker* (a
`WorkerDeclaration`, built from a registered `WorkerProfile` or from an ad-hoc
worker id) is evaluated against *what a piece of work requires* (a
`Requirements`), producing an `Eligibility` with structured, stable reason codes.
Acquisition (`arbite.lifecycle`), and later offers, packages and the board, call
the same `evaluate()` so a list filter can never be more permissive than the
operation that acquires the work.

Rules the vocabulary encodes:

- **A registered profile is authoritative.** Its configured tier is what is
  evaluated. A per-call declared tier above it is refused
  (`declared_tier_exceeds_profile`); one at or below it only narrows selection.
- **Unknown is not a pass under restriction.** With `Requirements.restricted`
  (the default, for explicit offer constraints) an unknown tier, capability set,
  locality or cost fails with a `*_unknown` reason. Without it -- the legacy
  ticket `tier` classification, which ad-hoc workers have always been able to
  claim -- unknowns become advisory `notes`, while a *known* mismatch (a
  registered medium worker on a high ticket) still fails.
- **Known violations always fail**, and a disabled profile always fails.
- **Requirements, not preferences.** Nothing here ranks candidates or treats a
  preference (prefer local/low cost) as a rule; there is no pricing catalog and
  no currency conversion (differing cost units fail with `cost_unit_mismatch`).
- **Capacity is carried, not counted.** `WorkerDeclaration.capacity` is the
  declared limit; counting active attempts against it belongs to the
  acquisition-time capacity check, which is not implemented in this slice.

Declared values are attribution, not authentication; see
`coordination.ATTRIBUTION_NOTICE`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from . import schema
from .errors import InvalidRecord, WorkerIneligible

#: Tier order, lowest first (shared with ticket classification).
TIER_RANK = {tier: rank for rank, tier in enumerate(schema.TIERS)}

#: Where a declaration came from.
SOURCE_PROFILE = "profile"
SOURCE_AD_HOC = "ad_hoc"

#: Stable reason codes. Failing reasons go in `Eligibility.reasons`; the
#: `*_unverified` / `*_assumed` codes are advisory and appear only in `notes`.
REASON_CODES = (
    "worker_disabled",
    "worker_not_allowed",
    "declared_tier_exceeds_profile",
    "tier_insufficient",
    "tier_unknown",
    "tier_unverified",
    "capability_missing",
    "capabilities_unknown",
    "capabilities_unverified",
    "locality_mismatch",
    "locality_unknown",
    "locality_unverified",
    "cost_exceeds_ceiling",
    "cost_unit_mismatch",
    "cost_unknown",
    "cost_unverified",
    "cost_assumed_local",
)


def tier_rank(tier: Optional[str]) -> Optional[int]:
    """The rank of `tier`, or None for an unknown/unrecognised tier."""
    return TIER_RANK.get(tier) if tier is not None else None


@dataclass(frozen=True)
class Reason:
    """One unmet (or advisory) constraint, with a stable `code`."""

    code: str
    field: str
    message: str
    required: Any = None
    actual: Any = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.message,
            "required": self.required,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class WorkerDeclaration:
    """What is known about a worker at evaluation time.

    `None` means *unknown*: an ad-hoc worker has no known capabilities, and its
    tier is known only if the call declared one. A registered profile's
    capabilities are known even when empty (it declared none).
    """

    worker_id: str
    source: str
    tier: Optional[str] = None
    capabilities: Optional[Tuple[str, ...]] = None
    locality: str = "unknown"
    cost_class: str = "unknown"
    cost_estimate: Optional[dict] = None
    capacity: Optional[int] = None
    enabled: bool = True
    profile_id: Optional[str] = None
    profile_revision: Optional[int] = None
    declared_tier: Optional[str] = None

    @property
    def registered(self) -> bool:
        return self.source == SOURCE_PROFILE

    @classmethod
    def ad_hoc(cls, worker_id: str, *, declared_tier: Optional[str] = None) -> "WorkerDeclaration":
        """An unregistered worker. A declared tier is taken at its word -- there is
        no profile to contradict it -- and everything else is unknown."""
        _require_tier_label(declared_tier)
        return cls(
            worker_id=worker_id,
            source=SOURCE_AD_HOC,
            tier=declared_tier,
            declared_tier=declared_tier,
        )

    @classmethod
    def from_profile(
        cls,
        profile,
        *,
        revision: Optional[int] = None,
        declared_tier: Optional[str] = None,
    ) -> "WorkerDeclaration":
        """The declaration for a registered profile.

        Raises `WorkerIneligible` (`declared_tier_exceeds_profile`) when a
        per-call tier would elevate the configured one: the profile is
        authoritative, and silently accepting the higher tier is exactly the
        elevation the contract forbids."""
        _require_tier_label(declared_tier)
        reason = declared_tier_reason(profile.tier, declared_tier)
        if reason is not None:
            raise WorkerIneligible(
                f"worker {profile.worker_id}: {reason.message}",
                details={"worker_id": profile.worker_id, "reasons": [reason.to_dict()]},
            )
        return cls(
            worker_id=profile.worker_id,
            source=SOURCE_PROFILE,
            tier=profile.tier,
            capabilities=tuple(profile.capabilities or ()),
            locality=profile.locality,
            cost_class=profile.cost_class,
            cost_estimate=dict(profile.cost_estimate) if profile.cost_estimate else None,
            capacity=profile.capacity,
            enabled=bool(profile.enabled),
            profile_id=profile.id,
            profile_revision=revision,
            declared_tier=declared_tier,
        )

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "source": self.source,
            "registered": self.registered,
            "tier": self.tier,
            "declared_tier": self.declared_tier,
            "capabilities": None if self.capabilities is None else list(self.capabilities),
            "locality": self.locality,
            "cost_class": self.cost_class,
            "cost_estimate": self.cost_estimate,
            "capacity": self.capacity,
            "enabled": self.enabled,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
        }


def _require_tier_label(tier: Optional[str]) -> None:
    if tier is not None and tier not in TIER_RANK:
        raise InvalidRecord(
            f"invalid tier {tier!r} (valid: {', '.join(schema.TIERS)})",
            details={"tier": tier},
        )


def declared_tier_reason(profile_tier: str, declared_tier: Optional[str]) -> Optional[Reason]:
    """A `declared_tier_exceeds_profile` reason, or None when the declaration is
    absent or does not exceed the configured tier."""
    if declared_tier is None:
        return None
    declared, configured = tier_rank(declared_tier), tier_rank(profile_tier)
    if declared is not None and configured is not None and declared > configured:
        return Reason(
            "declared_tier_exceeds_profile",
            "tier",
            f"declared tier {declared_tier!r} exceeds the registered profile's configured "
            f"tier {profile_tier!r}; the profile is authoritative (change it explicitly "
            "with 'arbite worker update --tier')",
            required=profile_tier,
            actual=declared_tier,
        )
    return None


@dataclass(frozen=True)
class Requirements:
    """Hard constraints a worker must satisfy.

    `restricted=True` (explicit offer constraints) makes an unknown value fail;
    `restricted=False` (the legacy ticket tier classification) reports unknowns
    as advisory notes. `max_cost` is `{"amount": number, "unit": str}`.
    Preferences are deliberately not representable here.
    """

    min_tier: Optional[str] = None
    capabilities: Tuple[str, ...] = ()
    local_only: bool = False
    max_cost: Optional[dict] = None
    allowed_workers: Tuple[str, ...] = ()
    restricted: bool = True

    def __post_init__(self) -> None:
        _require_tier_label(self.min_tier)
        if self.max_cost is not None:
            amount = self.max_cost.get("amount") if isinstance(self.max_cost, dict) else None
            unit = self.max_cost.get("unit") if isinstance(self.max_cost, dict) else None
            if (
                not isinstance(amount, (int, float))
                or isinstance(amount, bool)
                or amount < 0
                or not isinstance(unit, str)
                or not unit.strip()
            ):
                raise InvalidRecord(
                    "a cost ceiling needs a non-negative amount and an explicit unit",
                    details={"max_cost": self.max_cost},
                )

    @property
    def is_empty(self) -> bool:
        return not (
            self.min_tier or self.capabilities or self.local_only
            or self.max_cost or self.allowed_workers
        )

    def to_dict(self) -> dict:
        return {
            "min_tier": self.min_tier,
            "capabilities": list(self.capabilities),
            "local_only": self.local_only,
            "max_cost": dict(self.max_cost) if self.max_cost else None,
            "allowed_workers": list(self.allowed_workers),
            "restricted": self.restricted,
        }


@dataclass(frozen=True)
class Eligibility:
    """The result of `evaluate`: `eligible`, failing `reasons`, advisory `notes`."""

    worker: WorkerDeclaration
    requirements: Requirements
    reasons: Tuple[Reason, ...] = ()
    notes: Tuple[Reason, ...] = field(default=())

    @property
    def eligible(self) -> bool:
        return not self.reasons

    def to_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "worker": self.worker.to_dict(),
            "requirements": self.requirements.to_dict(),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "notes": [note.to_dict() for note in self.notes],
        }

    def require(self, *, subject: Optional[str] = None) -> "Eligibility":
        """Return self when eligible, else raise `WorkerIneligible` with every reason."""
        if self.eligible:
            return self
        what = f" for {subject}" if subject else ""
        raise WorkerIneligible(
            f"worker {self.worker.worker_id} is not eligible{what}: "
            + "; ".join(reason.message for reason in self.reasons),
            details={
                "worker_id": self.worker.worker_id,
                "subject": subject,
                "reasons": [reason.to_dict() for reason in self.reasons],
                "notes": [note.to_dict() for note in self.notes],
            },
        )


def evaluate(requirements: Requirements, worker: WorkerDeclaration) -> Eligibility:
    """Evaluate `worker` against `requirements`. Pure; never raises for an
    ineligible worker (call `.require()` for that)."""
    reasons = []
    notes = []

    def unknown(code_unknown: str, code_note: str, field_name: str, message: str, required) -> None:
        if requirements.restricted:
            reasons.append(Reason(code_unknown, field_name, message, required=required))
        else:
            notes.append(Reason(code_note, field_name, message + " (advisory: not enforced)",
                                required=required))

    if not worker.enabled:
        reasons.append(Reason(
            "worker_disabled", "enabled",
            f"worker profile {worker.worker_id} is disabled; it keeps its history but "
            "may not acquire new work until re-enabled",
            required=True, actual=False,
        ))

    if requirements.allowed_workers and worker.worker_id not in requirements.allowed_workers:
        reasons.append(Reason(
            "worker_not_allowed", "worker_id",
            f"worker {worker.worker_id} is not among the allowed workers",
            required=list(requirements.allowed_workers), actual=worker.worker_id,
        ))

    if requirements.min_tier is not None:
        have = tier_rank(worker.tier)
        if have is None:
            unknown("tier_unknown", "tier_unverified", "tier",
                    f"worker {worker.worker_id} has no known tier (required: "
                    f"{requirements.min_tier})", requirements.min_tier)
        elif have < TIER_RANK[requirements.min_tier]:
            reasons.append(Reason(
                "tier_insufficient", "tier",
                f"worker tier {worker.tier!r} is below the required tier "
                f"{requirements.min_tier!r}",
                required=requirements.min_tier, actual=worker.tier,
            ))

    if requirements.capabilities:
        wanted = sorted(set(requirements.capabilities))
        if worker.capabilities is None:
            unknown("capabilities_unknown", "capabilities_unverified", "capabilities",
                    f"worker {worker.worker_id} has no declared capabilities (required: "
                    f"{', '.join(wanted)})", wanted)
        else:
            missing = [cap for cap in wanted if cap not in set(worker.capabilities)]
            if missing:
                reasons.append(Reason(
                    "capability_missing", "capabilities",
                    f"worker lacks required capabilities: {', '.join(missing)}",
                    required=wanted, actual=list(worker.capabilities),
                ))

    if requirements.local_only:
        if worker.locality == "remote":
            reasons.append(Reason(
                "locality_mismatch", "locality",
                "work requires local execution; worker declares remote",
                required="local", actual="remote",
            ))
        elif worker.locality != "local":
            unknown("locality_unknown", "locality_unverified", "locality",
                    f"worker {worker.worker_id} has no declared locality (required: local)",
                    "local")

    if requirements.max_cost is not None:
        ceiling = requirements.max_cost
        estimate = worker.cost_estimate
        if estimate:
            if estimate.get("unit") != ceiling["unit"]:
                reasons.append(Reason(
                    "cost_unit_mismatch", "cost_estimate",
                    f"worker cost is in {estimate.get('unit')!r}, the ceiling in "
                    f"{ceiling['unit']!r}; units are never converted",
                    required=dict(ceiling), actual=dict(estimate),
                ))
            elif estimate.get("amount", 0) > ceiling["amount"]:
                reasons.append(Reason(
                    "cost_exceeds_ceiling", "cost_estimate",
                    f"worker cost estimate {estimate.get('amount')} {estimate.get('unit')} "
                    f"exceeds the ceiling {ceiling['amount']} {ceiling['unit']}",
                    required=dict(ceiling), actual=dict(estimate),
                ))
        elif worker.cost_class == "local":
            notes.append(Reason(
                "cost_assumed_local", "cost_class",
                "no cost estimate; cost class 'local' is treated as within the ceiling",
                required=dict(ceiling), actual="local",
            ))
        else:
            unknown("cost_unknown", "cost_unverified", "cost_estimate",
                    f"worker {worker.worker_id} has no cost estimate in "
                    f"{ceiling['unit']!r} (cost class {worker.cost_class!r})", dict(ceiling))

    return Eligibility(worker, requirements, tuple(reasons), tuple(notes))


def requirements_for_ticket(ticket) -> Requirements:
    """The worker constraints a plain ticket imposes on direct acquisition.

    Today that is only its `tier` classification, evaluated unrestricted: an
    ad-hoc worker with no known tier may still claim it (legacy behaviour, noted
    as unverified), while a registered profile's configured tier must reach it.
    Offers/reservations add restricted requirements in later slices."""
    tier = getattr(ticket, "tier", None)
    return Requirements(min_tier=tier if tier in TIER_RANK else None, restricted=False)


__all__ = [
    "Eligibility",
    "REASON_CODES",
    "Reason",
    "Requirements",
    "SOURCE_AD_HOC",
    "SOURCE_PROFILE",
    "TIER_RANK",
    "WorkerDeclaration",
    "declared_tier_reason",
    "evaluate",
    "requirements_for_ticket",
    "tier_rank",
]
