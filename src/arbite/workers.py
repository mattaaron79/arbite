"""Passive worker profiles: register, show, list, update, disable (planning key B01).

A worker profile is an *optional* declaration about a worker id that already
appears in claims and attempts. Registration launches nothing, contacts no
provider and verifies nothing: provider/model/runtime are labels, and the whole
profile is an operator assertion in a cooperating local store (see
`coordination.ATTRIBUTION_NOTICE`). What registration *does* change is
acquisition: `arbite.lifecycle` evaluates a registered worker through its
profile (`declaration_for`), so the configured tier is authoritative and a
disabled profile cannot take new work.

Operation rules:

- Profiles are store-level (not per workspace) and keyed by `worker_id`; at most
  one profile per worker id. `id` is an opaque `wkr-` handle.
- Every administrative write (register/update/disable/enable) holds the store's
  operation lock -- the same lock acquisition holds -- so a profile change and a
  claim serialize: a claim that committed first keeps its attempt, a later one
  sees the new profile. Profile changes never revoke running work.
- Each administrative write records a `worker` category event with the changed
  fields; `checkin` only refreshes `last_checkin` (declared activity, not
  liveness) and records no event.
- Disable keeps the record and its history. There is no delete.
- `expect_revision` gives optimistic concurrency for update/disable/enable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from . import coordination
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    Event,
    WorkerProfile,
    new_record_id,
    utc_now,
)
from .coordination_storage import check_revision
from .eligibility import WorkerDeclaration
from .errors import CoordinationConflict, CoordinationNotFound, InvalidRecord

#: The honest description of `last_checkin`, repeated wherever it is shown.
LIVENESS_NOTICE = (
    "last_checkin is declared activity reported by the worker itself; arbite does "
    "not verify that a worker is running"
)

#: Profile fields an update may change (plus `enabled` via disable/enable).
UPDATABLE_FIELDS = (
    "tier",
    "provider",
    "model",
    "runtime",
    "capabilities",
    "locality",
    "cost_class",
    "cost_estimate",
    "capacity",
)

_UNSET = object()


@dataclass(frozen=True)
class StoredProfile:
    """A profile together with its store-local revision."""

    profile: WorkerProfile
    revision: int

    def view(self) -> dict:
        return profile_view(self.profile, self.revision)


@dataclass(frozen=True)
class ProfileChange:
    """The outcome of an administrative write."""

    stored: StoredProfile
    changes: dict
    event: Optional[Event]

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def profile_view(profile: WorkerProfile, revision: Optional[int]) -> dict:
    """The documented JSON shape of a profile: its fields, its revision and an
    explicit statement that availability is declared, not verified."""
    data = profile.to_dict()
    data["revision"] = revision
    data["state"] = "enabled" if profile.enabled else "disabled"
    data["liveness"] = "unverified"
    return data


def normalise_capabilities(values: Optional[Iterable[str]]) -> List[str]:
    """Lower-cased, de-duplicated, sorted capability labels. Comma-separated
    entries are split so `--capability a,b` and repeated flags agree."""
    out = set()
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip().lower()
            if part:
                out.add(part)
    return sorted(out)


def _clean_label(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_valid(profile: WorkerProfile) -> None:
    problems = profile.validate()
    if problems:
        details = {"worker_id": profile.worker_id, "problems": list(problems)}
        if any("credential" in problem for problem in problems):
            details["reason"] = "secret_like_value"
        raise InvalidRecord(
            f"invalid worker profile for {profile.worker_id!r}: {'; '.join(problems)}",
            details=details,
        )


def _find(tx, worker_id: str) -> Optional[StoredProfile]:
    found = list(tx.find("worker_profile", worker_id=worker_id))
    if not found:
        return None
    # One profile per worker id is enforced at registration; should a store ever
    # hold two, the oldest is authoritative and the duplicate is ignored.
    found.sort(key=lambda profile: (profile.created, profile.id))
    profile = found[0]
    return StoredProfile(profile, tx.revision_of("worker_profile", profile.id))


def _store_is_empty(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return callable(probe) and not probe()


def find_profile(store, worker_id: str) -> Optional[StoredProfile]:
    """The stored profile for `worker_id`, or None (read-only; creates nothing)."""
    if _store_is_empty(store):
        return None
    with store.transaction(write=False) as tx:
        return _find(tx, worker_id)


def declaration_for(store, worker_id: str, *, declared_tier: Optional[str] = None) -> WorkerDeclaration:
    """The eligibility declaration for `worker_id`: from its profile when one is
    registered (raising `WorkerIneligible` if `declared_tier` would elevate it),
    else an ad-hoc declaration."""
    stored = find_profile(store, worker_id) if store is not None else None
    if stored is None:
        return WorkerDeclaration.ad_hoc(worker_id, declared_tier=declared_tier)
    return WorkerDeclaration.from_profile(
        stored.profile, revision=stored.revision, declared_tier=declared_tier
    )


class WorkerProfileService:
    """Administrative operations over worker profiles in one coordination store."""

    def __init__(self, store, *, actor: Optional[str] = None, clock=None):
        self.store = store
        self.actor = actor
        self._clock = clock or utc_now

    def now(self) -> str:
        return self._clock()

    # -- reads -----------------------------------------------------------

    def find(self, worker_id: str) -> Optional[StoredProfile]:
        return find_profile(self.store, worker_id)

    def get(self, worker_id: str) -> StoredProfile:
        stored = self.find(worker_id)
        if stored is None:
            raise CoordinationNotFound(
                f"no worker profile is registered for {worker_id!r} (ad-hoc worker ids "
                "need no profile; register one with 'arbite worker register')",
                details={"worker_id": worker_id},
            )
        return stored

    def list(self, *, state: str = "all") -> List[StoredProfile]:
        """Every profile, sorted by worker id. `state` is all/enabled/disabled."""
        if state not in ("all", "enabled", "disabled"):
            raise InvalidRecord(f"invalid state filter {state!r} (valid: all, enabled, disabled)")
        if _store_is_empty(self.store):
            return []
        with self.store.transaction(write=False) as tx:
            rows = [
                StoredProfile(profile, tx.revision_of("worker_profile", profile.id))
                for profile in tx.find("worker_profile")
            ]
        if state != "all":
            wanted = state == "enabled"
            rows = [row for row in rows if bool(row.profile.enabled) == wanted]
        return sorted(rows, key=lambda row: (row.profile.worker_id, row.profile.id))

    def declaration(self, worker_id: str, *, declared_tier: Optional[str] = None) -> WorkerDeclaration:
        return declaration_for(self.store, worker_id, declared_tier=declared_tier)

    # -- writes ----------------------------------------------------------

    def register(
        self,
        worker_id: str,
        *,
        tier: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
        capabilities: Optional[Iterable[str]] = None,
        locality: str = "unknown",
        cost_class: str = "unknown",
        cost_estimate: Optional[dict] = None,
        capacity: Optional[int] = None,
    ) -> ProfileChange:
        """Register a new profile. Refuses a worker id that already has one."""
        moment = self.now()
        profile = WorkerProfile(
            id=new_record_id("worker_profile"),
            worker_id=str(worker_id or "").strip(),
            tier=tier,
            created=moment,
            updated=moment,
            provider=_clean_label(provider),
            model=_clean_label(model),
            runtime=_clean_label(runtime),
            capabilities=normalise_capabilities(capabilities),
            locality=locality or "unknown",
            cost_class=cost_class or "unknown",
            cost_estimate=cost_estimate,
            capacity=capacity,
        )
        _require_valid(profile)
        with self.store.operation_lock():
            with self.store.transaction() as tx:
                existing = _find(tx, profile.worker_id)
                if existing is not None:
                    raise CoordinationConflict(
                        f"worker {profile.worker_id!r} already has profile "
                        f"{existing.profile.id} (revision {existing.revision}); change it "
                        "with 'arbite worker update'",
                        details={
                            "worker_id": profile.worker_id,
                            "profile_id": existing.profile.id,
                            "revision": existing.revision,
                        },
                        retryable=False,
                    )
                tx.put(profile, expect_revision=0)
                changes = {
                    name: {"from": None, "to": coordination._jsonable(getattr(profile, name))}
                    for name in UPDATABLE_FIELDS
                    if getattr(profile, name) not in (None, [], "unknown")
                }
                event = self._event(tx, "worker_registered", profile, 1, changes, None, moment)
        return ProfileChange(StoredProfile(profile, 1), changes, event)

    def update(
        self,
        worker_id: str,
        *,
        expect_revision: Optional[int] = None,
        reason: Optional[str] = None,
        tier=_UNSET,
        provider=_UNSET,
        model=_UNSET,
        runtime=_UNSET,
        capabilities=_UNSET,
        add_capabilities: Optional[Iterable[str]] = None,
        remove_capabilities: Optional[Iterable[str]] = None,
        locality=_UNSET,
        cost_class=_UNSET,
        cost_estimate=_UNSET,
        capacity=_UNSET,
    ) -> ProfileChange:
        """Change profile fields explicitly. `None` clears an optional field; an
        omitted argument is untouched. A no-op update writes nothing."""

        def apply(profile: WorkerProfile) -> None:
            for name, value in (
                ("tier", tier),
                ("locality", locality),
                ("cost_class", cost_class),
                ("cost_estimate", cost_estimate),
                ("capacity", capacity),
            ):
                if value is not _UNSET:
                    setattr(profile, name, value)
            for name, value in (("provider", provider), ("model", model), ("runtime", runtime)):
                if value is not _UNSET:
                    setattr(profile, name, _clean_label(value))
            caps = set(profile.capabilities)
            if capabilities is not _UNSET:
                caps = set(normalise_capabilities(capabilities))
            caps |= set(normalise_capabilities(add_capabilities))
            caps -= set(normalise_capabilities(remove_capabilities))
            profile.capabilities = sorted(caps)

        return self._change(
            worker_id, apply, event_kind="worker_updated",
            expect_revision=expect_revision, reason=reason,
        )

    def disable(self, worker_id: str, *, reason: Optional[str] = None,
                expect_revision: Optional[int] = None) -> ProfileChange:
        """Stop new acquisitions by this worker id. History and running attempts
        are untouched; an already-disabled profile is left as it is."""

        def apply(profile: WorkerProfile) -> None:
            if profile.enabled:
                profile.enabled = False
                profile.disabled_at = self.now()
                profile.disabled_reason = _clean_label(reason)

        return self._change(
            worker_id, apply, event_kind="worker_disabled",
            expect_revision=expect_revision, reason=reason,
        )

    def enable(self, worker_id: str, *, reason: Optional[str] = None,
               expect_revision: Optional[int] = None) -> ProfileChange:
        def apply(profile: WorkerProfile) -> None:
            if not profile.enabled:
                profile.enabled = True
                profile.disabled_at = None
                profile.disabled_reason = None

        return self._change(
            worker_id, apply, event_kind="worker_enabled",
            expect_revision=expect_revision, reason=reason,
        )

    def checkin(self, worker_id: str) -> StoredProfile:
        """Record declared activity (`last_checkin`). Not an event, not liveness."""
        with self.store.transaction() as tx:
            stored = _find(tx, worker_id)
            if stored is None:
                raise CoordinationNotFound(
                    f"no worker profile is registered for {worker_id!r}",
                    details={"worker_id": worker_id},
                )
            profile = stored.profile
            profile.last_checkin = self.now()
            tx.put(profile, expect_revision=stored.revision)
        return StoredProfile(profile, stored.revision + 1)

    # -- internals -------------------------------------------------------

    def _change(self, worker_id, apply, *, event_kind, expect_revision, reason) -> ProfileChange:
        watched = UPDATABLE_FIELDS + ("enabled", "disabled_reason")
        with self.store.operation_lock():
            with self.store.transaction() as tx:
                stored = _find(tx, worker_id)
                if stored is None:
                    raise CoordinationNotFound(
                        f"no worker profile is registered for {worker_id!r}",
                        details={"worker_id": worker_id},
                    )
                check_revision("worker_profile", stored.profile.id, expect_revision, stored.revision)
                profile = stored.profile
                before = {name: coordination._jsonable(getattr(profile, name)) for name in watched}
                apply(profile)
                after = {name: coordination._jsonable(getattr(profile, name)) for name in watched}
                changes = {
                    name: {"from": before[name], "to": after[name]}
                    for name in watched
                    if before[name] != after[name]
                }
                if not changes:
                    return ProfileChange(stored, {}, None)
                moment = self.now()
                profile.updated = moment
                _require_valid(profile)
                tx.put(profile, expect_revision=stored.revision)
                revision = stored.revision + 1
                event = self._event(tx, event_kind, profile, revision, changes, reason, moment)
        return ProfileChange(StoredProfile(profile, revision), changes, event)

    def _event(self, tx, kind, profile, revision, changes, reason, moment) -> Event:
        event = Event(
            id=new_record_id("event"),
            cursor=None,
            kind_=kind,
            category="worker",
            timestamp=moment,
            subject_ids=[profile.id, profile.worker_id],
            operation_id=None,
            payload={
                "worker_id": profile.worker_id,
                "profile_id": profile.id,
                "revision": revision,
                "actor": self.actor,
                "reason": _clean_label(reason),
                "changes": changes,
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )
        return tx.append_event(event)


def parse_capacity(value) -> Optional[int]:
    """`None`/'none' clears; otherwise an integer >= 1."""
    if value is None or str(value).strip().lower() in ("", "none"):
        return None
    try:
        number = int(str(value).strip())
    except ValueError:
        raise InvalidRecord(f"capacity must be an integer >= 1 or 'none', got {value!r}")
    if number < 1:
        raise InvalidRecord(f"capacity must be an integer >= 1 or 'none', got {value!r}")
    return number


def build_cost_estimate(amount, unit, provenance) -> Optional[dict]:
    """A `cost_estimate` mapping from its three parts; all three are required
    together so a number never travels without its unit and provenance."""
    given = [part is not None for part in (amount, unit, provenance)]
    if not any(given):
        return None
    if not all(given):
        raise InvalidRecord(
            "a cost estimate needs --cost-amount, --cost-unit and --cost-provenance together "
            "(explicit units and provenance, no implied currency)"
        )
    try:
        number = float(amount)
    except (TypeError, ValueError):
        raise InvalidRecord(f"cost amount must be a number, got {amount!r}")
    if number.is_integer():
        number = int(number)
    return {"amount": number, "unit": str(unit).strip(), "provenance": str(provenance).strip()}


__all__ = [
    "LIVENESS_NOTICE",
    "ProfileChange",
    "StoredProfile",
    "UPDATABLE_FIELDS",
    "WorkerProfileService",
    "build_cost_estimate",
    "declaration_for",
    "find_profile",
    "normalise_capabilities",
    "parse_capacity",
    "profile_view",
]
