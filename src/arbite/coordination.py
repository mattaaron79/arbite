"""Versioned, storage-neutral records for shared-directory coordination.

This module is to the shared-directory epic what `schema.py` is to tickets: the
place that says what a *record* is, validates it, and renders it as plain JSON --
with no filesystem or database access anywhere in it. A sink's coordination
surface stores and retrieves these records; it does not get to redefine them.

Records introduced here (planning key C01, slice 1 of
`.arbite/planning/shared-directory-coordination.md`):

- `Workspace` / `StoreBinding` -- a workspace and the one authoritative store it
  coordinates against. Two independently selected sinks for one workspace would
  split ownership, so `Workspace.bind()` refuses a conflicting binding.
- `WorkAttempt` -- one worker working one ticket, with an explicit `generation`
  so a later takeover/reopen cannot be confused with the attempt it replaced.
- `FileClaim` -- exclusive, whole-file writer ownership, carrying the claim
  `generation` and the `observed_version` a write must still match.
- `ReadObservation` -- evidence that bytes were *served* through arbite, with a
  whole-file `digest` even when only a line range was returned, and an explicit
  `write_authorizing` flag (a pre-claim read never authorizes a write).
- `Artifact` -- content-addressed stored bytes referenced by receipts.
- `OperationReceipt` -- the durable record of one operation, including explicit
  before/after digests or the `ABSENT` marker. `OperationReceipt.id` *is* the
  operation id, which is what makes retries deduplicable.
- `Event` -- an append-only, per-store monotonic `cursor` over the above, with a
  `category` so read-observation traffic can be kept out of ordinary queries.
- `RecoveryReport` -- what an incomplete operation found on inspection, so a
  failed write is reconciled honestly instead of guessed at.
- `LifecycleIntent` -- the durable journal entry joining a ticket transition to
  its coordination cascade (attempt start/end, claim release, events). The ticket
  store and the coordination store commit separately, so the intent is what lets
  the next operation finish or abandon a transition a crash interrupted.

## Compatibility restrictions (documented, enforced by the application layer)

- **Attribution, not authentication.** Ids, worker names and actors are
  self-declared. The trust model is cooperating local processes sharing one
  authoritative store for a workspace. See `ATTRIBUTION_NOTICE`.
- **New records use UTC.** `utc_now()` produces `YYYY-MM-DDTHH:MM:SSZ`.
  Existing ticket timestamps are untouched: `schema.now()` keeps producing the
  local `YYYY-MM-DDTHH:MM:SS` form and `schema.DATE_PATTERN` keeps accepting the
  legacy date-only form. The two vocabularies are deliberately separate fields,
  never coerced into one another.
- **Paths are whole-file, canonical and in-root.** `canonical_relative_path()`
  rejects absolute paths, `..` traversal and empty names; `is_protected_path()`
  marks `.arbite`/`.git`. v1 has no concurrent region ownership, no symlink
  traversal and no recursive directory deletion (`UnsupportedCoordination`).
- **No expiration, heartbeat or automatic takeover.** `WorkAttempt` carries
  creation/activity/end timestamps so future stale detection needs no
  reconstruction, but nothing here infers that a stopped worker is dead.
- **Append-only is a contract, not tamper-proofing.** Evidence is retained past
  ticket closure; a local store cannot prove who wrote a record.

Nothing in this module schedules, waits, retries or scans: it is data plus pure
helpers, and every policy decision belongs to `arbite.application`.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .errors import (
    ErrorCode,
    InvalidRecord,
    StoreBindingConflict,
    UnsupportedCoordination,
)

#: Version of the coordination contract these records implement. Stored on every
#: record so a later migration can tell which vocabulary a stored record used,
#: and reported in JSON results so an agent can adapt instead of guessing.
CONTRACT_VERSION = 1

#: Version of an `Event.payload` mapping. Payload *shapes* will be added to over
#: time; the version lets a reader know which shape it is looking at.
EVENT_PAYLOAD_VERSION = 1

#: Sentinel for "the file did not exist" in before/after digests. An explicit
#: marker rather than `None` or `""`, because absence is a fact worth recording
#: and `None` also means "not checked".
ABSENT = "<absent>"

#: The statement that ids/names identify a cooperating process, not an
#: authenticated principal. Re-exported from `errors` documentation so callers
#: quoting the trust model have one canonical sentence to point at.
ATTRIBUTION_NOTICE = (
    "Ids, worker names and actor strings are attribution, not authentication: "
    "arbite records who a cooperating local process *says* it is. Identity is "
    "not verified, and a direct filesystem change by any process cannot be "
    "attributed at all. The trust model is cooperating local processes sharing "
    "one authoritative coordination store for a workspace."
)

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

#: States of a work attempt. `active` is the only state that may claim or
#: mutate; the others are terminal and require a new attempt to resume work.
ATTEMPT_STATES = ["active", "finished", "released", "interrupted"]

#: States of a file claim. A released claim is kept as history and never
#: reactivated: reacquiring a path mints a new claim with a new generation.
CLAIM_STATES = ["active", "released"]

#: Kinds of operation a receipt can describe. `read` does not mutate; the rest
#: are the mutation/lifecycle operations the file proxy and ticket transitions
#: will record. Kept open-ended enough for later slices without inventing kinds
#: now (an unrecognised future kind can arrive with `other` plus a payload).
OPERATION_KINDS = [
    "read",
    "write",
    "edit",
    "remove",
    "rename",
    "claim",
    "release",
    "lifecycle",
    "other",
]

#: The operation kinds that change bytes on disk. Used by callers that need to
#: know whether "bytes may already have changed" applies.
MUTATION_KINDS = ["write", "edit", "remove", "rename"]

#: Result of a recorded operation.
OPERATION_RESULTS = ["ok", "error"]

#: Event categories. `read` is a separate category so a high-volume read stream
#: does not flood ordinary lifecycle/operation queries.
EVENT_CATEGORIES = ["lifecycle", "operation", "claim", "read"]

#: Event kinds. Every event is one of these; `operation_recorded` is the generic
#: carrier for a receipt, and the rest name the transitions later slices add.
#:
#: C03 (ticket acquisition and lifecycle policy) adds the four ticket-transition
#: kinds below, because routing `block`/`unblock`/`shelve`/`unshelve` through the
#: lifecycle layer made transitions that previously left no coordination trace
#: (they were plain ticket edits) first-class events. That keeps the event log an
#: honest record of what moved a ticket rather than folding real transitions into
#: the generic `operation_recorded` carrier, which is reserved for receipts.
EVENT_KINDS = [
    "workspace_bound",
    "attempt_started",
    "attempt_finished",
    "attempt_released",
    "attempt_interrupted",
    "claim_acquired",
    "claim_released",
    "operation_recorded",
    #: A filesystem operation was left ambiguous (observed bytes match neither the
    #: recorded before nor the recorded after version). Evidence is preserved and
    #: the drift is never auto-repaired (planning key C05).
    "drift_detected",
    "read_observed",
    "ticket_claimed",
    "ticket_blocked",
    "ticket_unblocked",
    "ticket_shelved",
    "ticket_unshelved",
    "ticket_closed",
    "ticket_reopened",
    "dependency_invalidated",
]

#: Recovery outcomes for an incomplete operation. `applied` and `reverted` are
#: the unambiguous cases `doctor` may repair; `drifted` and `unknown` are
#: reported and preserved, never guessed at.
RECOVERY_STATES = ["pending", "applied", "reverted", "drifted", "unknown"]

#: States of a persisted file-operation *intent* (planning key C05). The intent is
#: the durable record of "this operation is about to change these exact bytes".
#: `pending` is written *before* any filesystem change; `applied` is observed but
#: not yet receipted; `finalized` means the receipt exists; `reverted` means the
#: bytes were observed unchanged; `drifted` means observed bytes match neither the
#: recorded before nor after version and must be resolved explicitly.
INTENT_STATES = ["pending", "applied", "finalized", "reverted", "drifted"]

#: Intent states that still need reconciling by the next relevant operation.
INTENT_ACTIVE_STATES = ["pending", "applied"]

#: States of a `LifecycleIntent`. `pending` is written before the ticket write and
#: is the only state reconciliation acts on; `completed` means the coordination
#: cascade committed; `abandoned` means the ticket write never landed (or was
#: compensated), so nothing of the transition remains to apply.
LIFECYCLE_INTENT_STATES = ["pending", "completed", "abandoned"]

#: How a `LifecycleIntent` ends the attempt it names (mirrors `WorkAttempt`).
LIFECYCLE_END_STATES = ["released", "finished", "interrupted"]

#: Record kind -> opaque id prefix. The prefix is part of the id so a task can
#: tell a claim id from an attempt id at a glance without a store lookup.
ID_PREFIXES = {
    "workspace": "ws",
    "store_binding": "stb",
    "work_attempt": "att",
    "file_claim": "clm",
    "read_observation": "obs",
    "operation_receipt": "op",
    "operation_intent": "opi",
    "artifact": "art",
    "event": "evt",
    "recovery_report": "rcv",
    "lifecycle_intent": "lci",
}

#: Opaque ids are `<prefix>-<16 hex chars>` (8 random bytes). Short enough to
#: read in a log, long enough that collisions are not a practical concern.
OPAQUE_ID_PATTERN = re.compile(r"^[a-z]{2,3}-[0-9a-f]{16}$")

#: UTC timestamps for new records: `YYYY-MM-DDTHH:MM:SSZ`. Deliberately distinct
#: from the legacy ticket form (which has no `Z` and may be date-only), so a
#: validator can never silently accept one where the other is expected.
UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Directory names that are arbite/project metadata rather than workspace
#: content. The file proxy must never read or mutate through these.
PROTECTED_PREFIXES = (".arbite", ".git")

#: Digest algorithm vocabulary. A single explicit prefix keeps digests
#: comparable across sinks and leaves room for a future algorithm change.
DIGEST_PREFIX = "sha256:"


# ---------------------------------------------------------------------------
# Opaque ids
# ---------------------------------------------------------------------------


def gen_opaque_id(prefix: str, existing: Optional[set] = None) -> str:
    """A fresh opaque id for `prefix`, not present in `existing`.

    Ids are the durable handle for a record: they are never reused, never encode
    a path or an ordinal, and never change when a record's state does. A caller
    that can collide retries; `existing` makes that cheap to check.
    """
    if not prefix or not re.match(r"^[a-z]{2,3}$", prefix):
        raise UnsupportedCoordination(
            f"opaque id prefix must be 2-3 lowercase letters, got {prefix!r}"
        )
    taken = existing or set()
    while True:
        candidate = f"{prefix}-{uuid.uuid4().hex[:16]}"
        if candidate not in taken:
            return candidate


def new_record_id(kind: str, existing: Optional[set] = None) -> str:
    """A fresh opaque id for a record `kind` (see `ID_PREFIXES`)."""
    if kind not in ID_PREFIXES:
        raise UnsupportedCoordination(
            f"unknown coordination record kind {kind!r} "
            f"(valid: {', '.join(sorted(ID_PREFIXES))})"
        )
    return gen_opaque_id(ID_PREFIXES[kind], existing)


def is_opaque_id(value: Any, prefix: Optional[str] = None) -> bool:
    """True when `value` is an opaque id, optionally of one specific `prefix`."""
    if not isinstance(value, str) or not OPAQUE_ID_PATTERN.match(value):
        return False
    if prefix is None:
        return True
    return value.startswith(prefix + "-")


def id_for_kind(value: Any, kind: str) -> bool:
    """True when `value` is an opaque id of the prefix `kind` requires."""
    return is_opaque_id(value, ID_PREFIXES.get(kind, "?"))


def new_operation_id(existing: Optional[set] = None) -> str:
    """A fresh operation id. The operation id is the retry-dedup key, so it is
    minted once by the caller and reused verbatim for every retry of the same
    logical operation."""
    return new_record_id("operation_receipt", existing)


# ---------------------------------------------------------------------------
# UTC timestamps for new records
# ---------------------------------------------------------------------------


def utc_now(timestamp: Optional[datetime] = None) -> str:
    """Current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.

    New coordination records use this. It is deliberately *not* `schema.now()`:
    ticket timestamps keep their existing local, `Z`-less format for backward
    compatibility, and the two must never be substituted for one another.
    """
    moment = timestamp or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_UTC_FORMAT)


def is_utc_timestamp(value: Any) -> bool:
    """True when `value` is a `YYYY-MM-DDTHH:MM:SSZ` UTC timestamp."""
    return isinstance(value, str) and bool(UTC_PATTERN.match(value))


def parse_utc(value: str) -> datetime:
    """Parse a `YYYY-MM-DDTHH:MM:SSZ` timestamp, raising `InvalidRecord` if the
    form is wrong. Legacy ticket timestamps are intentionally rejected here: use
    `schema`/`schema.DATE_PATTERN` for those."""
    if not is_utc_timestamp(value):
        raise InvalidRecord(
            f"expected a UTC timestamp of the form YYYY-MM-DDTHH:MM:SSZ, got {value!r}"
        )
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Content digests
# ---------------------------------------------------------------------------


def digest_of_bytes(data: bytes) -> str:
    """The `sha256:<hex>` digest of `data`. Digests describe whole content, even
    when only a range was returned to a reader."""
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest_of_text(text: str, encoding: str = "utf-8") -> str:
    """The digest of `text` encoded with `encoding` (UTF-8 by default)."""
    return digest_of_bytes(text.encode(encoding))


def is_digest(value: Any) -> bool:
    """True for a `sha256:<64 hex>` content digest."""
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return False
    return bool(re.match(r"^[0-9a-f]{64}$", value[len(DIGEST_PREFIX) :]))


def is_digest_or_absent(value: Any) -> bool:
    """True for a content digest or the explicit `ABSENT` marker."""
    return value == ABSENT or is_digest(value)


def artifact_id_for_digest(digest: str) -> str:
    """The deterministic artifact id for `digest`: `art-<first 16 hex chars>`.

    Content addressing made visible in the id itself. Two operations that store
    identical bytes therefore agree on the artifact id without a lookup, which is
    what lets artifact records be written with `put_if_absent` (store content once
    per digest) and keeps a receipt's `artifact_refs` stable across a retry.
    """
    if not is_digest(digest):
        raise InvalidRecord(
            f"cannot derive an artifact id from {digest!r}: not a "
            f"{DIGEST_PREFIX}<64 hex> content digest"
        )
    return "art-" + digest[len(DIGEST_PREFIX):][:16]


# ---------------------------------------------------------------------------
# Workspace-relative paths (pure policy helpers, no filesystem access)
# ---------------------------------------------------------------------------


def canonical_relative_path(value: Any) -> str:
    """Normalise a workspace-relative path to canonical POSIX form.

    Pure string policy, deliberately independent of the filesystem so both sinks
    and the application layer agree on what a "path" is before any symlink,
    case-folding or platform question is raised. Rejects absolute paths, `..`
    traversal, empty names and `~`: a path outside the workspace root is never a
    coordination target.
    """
    if value is None:
        raise UnsupportedCoordination("a workspace-relative path is required")
    text = str(value).replace("\\", "/").strip()
    if not text:
        raise UnsupportedCoordination("a workspace-relative path must not be empty")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise UnsupportedCoordination(f"path must be relative to the workspace root: {value!r}")
    if text.startswith("~"):
        raise UnsupportedCoordination(f"path must not use '~': {value!r}")
    normalized = posixpath.normpath(text)
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        raise UnsupportedCoordination(f"path escapes the workspace root: {value!r}")
    return normalized


def is_protected_path(relative_path: str) -> bool:
    """True when a canonical path addresses protected arbite/.git metadata.

    Protected paths are reported rather than silently excluded, so a caller that
    asked for one gets a clear refusal instead of an unrecorded write.
    """
    first = relative_path.split("/", 1)[0]
    return first in PROTECTED_PREFIXES


# ---------------------------------------------------------------------------
# Documented JSON result/error vocabulary
# ---------------------------------------------------------------------------


def ok_result(data: Any = None, *, details: Optional[dict] = None, contract_version: Optional[int] = None) -> dict:
    """The documented JSON success payload `--json` commands emit.

    Shape (stable): `ok`, `code`, `message`, `data`, `details`, `retryable`,
    `bytes_may_have_changed`, `schema_version`. Callers branch on `ok`/`code`,
    never on the prose `message`.
    """
    return {
        "ok": True,
        "code": None,
        "message": "",
        "data": data,
        "details": dict(details or {}),
        "retryable": False,
        "bytes_may_have_changed": False,
        "schema_version": CONTRACT_VERSION if contract_version is None else contract_version,
    }


def error_result(
    code: str,
    message: str,
    *,
    details: Optional[dict] = None,
    retryable: bool = False,
    bytes_may_have_changed: bool = False,
    contract_version: Optional[int] = None,
) -> dict:
    """The documented JSON error payload, mirroring `ok_result`.

    `code` is a string from `errors.ErrorCode`; `bytes_may_have_changed` tells a
    caller whether a failed operation may already have altered files (so it must
    inspect before retrying), which a `stale_read` never does.
    """
    return {
        "ok": False,
        "code": code,
        "message": str(message),
        "data": None,
        "details": dict(details or {}),
        "retryable": bool(retryable),
        "bytes_may_have_changed": bool(bytes_may_have_changed),
        "schema_version": CONTRACT_VERSION if contract_version is None else contract_version,
    }


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively convert a record field into JSON-serialisable data."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _load(cls, data: Any, nested: Tuple[Tuple[str, type], ...] = ()):
    """Build a record from a mapping, ignoring unknown keys.

    Unknown keys are dropped rather than rejected so a record written by a later
    contract version stays loadable by this one (forward compatibility), while a
    missing required field surfaces as `InvalidRecord` instead of a raw
    `TypeError` from the dataclass constructor.
    """
    if not isinstance(data, dict):
        raise InvalidRecord(
            f"{cls.__name__}.from_dict expects a mapping, got {type(data).__name__}"
        )
    known = {f.name for f in fields(cls)}
    kwargs = {key: data[key] for key in known if key in data}
    for key, sub_cls in nested:
        if key in data and isinstance(data[key], dict):
            kwargs[key] = sub_cls.from_dict(data[key])
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise InvalidRecord(f"{cls.__name__} is missing required fields: {e}")


class _Record:
    """Shared behaviour for every coordination record: `kind`, opaque `id`,
    JSON round-trip, and self-validation."""

    #: Stable record kind, used as the storage key namespace and the
    #: `validate_record` dispatch tag.
    kind = ""
    #: Opaque id prefix this record requires.
    id_prefix = ""

    @classmethod
    def _nested(cls) -> Tuple[Tuple[str, type], ...]:
        return ()

    @classmethod
    def from_dict(cls, data: Any):
        """Rebuild a record from `to_dict()` output (or equivalent JSON)."""
        return _load(cls, data, nested=cls._nested())

    @property
    def record_id(self) -> str:
        return getattr(self, "id")

    def to_dict(self) -> dict:
        """Plain JSON-serialisable form: every field plus `kind`."""
        data = {f.name: _jsonable(getattr(self, f.name)) for f in fields(self)}
        data["kind"] = self.kind
        return data

    def validate(self) -> List[str]:
        """Human-readable invariant problems, or `[]` when the record is clean.
        Mirrors `schema.validate_ticket`: validation reports, it does not raise,
        so `doctor`-style aggregation can collect every problem at once."""
        return validate_record(self)


@dataclass
class StoreBinding(_Record):
    """Which store a workspace coordinates against, and where it is.

    `kind` is the sink kind and `location` is that sink's own opaque location
    string (a database path for SQLite, a directory for the file sink). The pair
    is what a conflicting-binding check compares; identity itself is the id.
    """

    kind = "store_binding"
    id_prefix = ID_PREFIXES["store_binding"]

    id: str
    workspace_id: str
    sink_kind: str
    location: str
    bound_at: str
    contract_version: int = CONTRACT_VERSION

    def matches(self, sink_kind: str, location: str) -> bool:
        """True when this binding already names the same sink and location."""
        return self.sink_kind == sink_kind and self.location == location

    def matches_binding(self, other: "StoreBinding") -> bool:
        return self.matches(other.sink_kind, other.location)


@dataclass
class Workspace(_Record):
    """One visible working directory coordinated by arbite.

    `root` is the canonical local root. The optional `store_binding` is the one
    authoritative store: `bind()` accepts an identical binding idempotently and
    refuses a different one (`StoreBindingConflict`), because a workspace split
    across two stores cannot coordinate claims safely. Root relocation is
    deliberately *not* modeled here; it is an explicit operation for a later
    slice, not something this record will do implicitly.
    """

    kind = "workspace"
    id_prefix = ID_PREFIXES["workspace"]

    id: str
    root: str
    created: str
    updated: str
    store_binding: Optional[StoreBinding] = None
    contract_version: int = CONTRACT_VERSION

    @classmethod
    def _nested(cls):
        return (("store_binding", StoreBinding),)

    @property
    def is_bound(self) -> bool:
        return self.store_binding is not None

    def bind(self, binding: StoreBinding, *, timestamp: Optional[str] = None) -> StoreBinding:
        """Attach `binding` as the workspace's authoritative store.

        Idempotent for an identical binding; raises `StoreBindingConflict` when
        the workspace already names a different sink or location. Returns the
        authoritative binding either way, so a caller can proceed with what is
        actually configured.
        """
        if binding.workspace_id != self.id:
            raise StoreBindingConflict(
                f"binding {binding.id} names workspace {binding.workspace_id}, "
                f"not {self.id}",
                details={"workspace_id": self.id, "binding_id": binding.id},
            )
        if self.store_binding is not None:
            if self.store_binding.matches_binding(binding):
                return self.store_binding
            raise StoreBindingConflict(
                f"workspace {self.id} is already bound to "
                f"{self.store_binding.sink_kind}:{self.store_binding.location}; refusing "
                f"to re-bind to {binding.sink_kind}:{binding.location}",
                details={
                    "workspace_id": self.id,
                    "bound": {
                        "sink_kind": self.store_binding.sink_kind,
                        "location": self.store_binding.location,
                    },
                    "requested": {"sink_kind": binding.sink_kind, "location": binding.location},
                },
            )
        self.store_binding = binding
        self.updated = timestamp or utc_now()
        return binding


@dataclass
class WorkAttempt(_Record):
    """One worker's attempt at one ticket.

    `generation` is explicit and monotonically increasing per ticket, so a
    later ticket claimed again (reopen, takeover) is unambiguously a *new*
    attempt rather than a mutation of the old one. State is one of
    `ATTEMPT_STATES`; only `active` may claim or mutate. Timestamps are UTC and
    are recorded now so future stale detection needs no reconstruction -- but
    nothing here infers staleness from them.

    The transition methods are provided so the application layer records
    lifecycle changes consistently; they enforce the "terminal attempts do not
    come back" invariant and nothing more.
    """

    kind = "work_attempt"
    id_prefix = ID_PREFIXES["work_attempt"]

    id: str
    ticket_id: str
    worker_id: str
    workspace_id: str
    generation: int
    started: str
    last_activity: str
    state: str = "active"
    ended: Optional[str] = None
    outcome: Optional[str] = None
    handoff: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    def touch(self, timestamp: Optional[str] = None) -> str:
        """Record activity on an active attempt. Refuses a terminal attempt, so
        a released/finished token can never be revived by a late call."""
        if not self.is_active:
            raise InvalidRecord(
                f"attempt {self.id} is {self.state} and cannot record activity"
            )
        self.last_activity = timestamp or utc_now()
        return self.last_activity

    def finish(self, timestamp: Optional[str] = None, *, outcome: Optional[str] = None,
               handoff: Optional[str] = None) -> None:
        self._end("finished", timestamp, outcome=outcome, handoff=handoff)

    def release(self, timestamp: Optional[str] = None, *, handoff: Optional[str] = None,
                outcome: Optional[str] = "released") -> None:
        self._end("released", timestamp, outcome=outcome, handoff=handoff)

    def interrupt(self, timestamp: Optional[str] = None, *, reason: Optional[str] = None) -> None:
        self._end("interrupted", timestamp, outcome=reason)

    def _end(self, state: str, timestamp: Optional[str], *, outcome=None, handoff=None) -> None:
        if not self.is_active:
            raise InvalidRecord(
                f"attempt {self.id} is already {self.state}; {state} is not a valid "
                "transition (resume with a new attempt instead)"
            )
        moment = timestamp or utc_now()
        self.state = state
        self.ended = moment
        self.last_activity = moment
        if outcome is not None:
            self.outcome = outcome
        if handoff is not None:
            self.handoff = handoff


@dataclass
class FileClaim(_Record):
    """Exclusive whole-file writer ownership of one workspace path.

    `generation` is the claim generation a mutation must present and
    `observed_version` is the whole-file digest observed when the claim was
    taken (or `ABSENT` when the file did not exist), advanced by every mutation.
    `mutation_seq` counts the mutations applied under this claim; a read records
    the value it saw, so a read token is consumed by the next mutation even when
    that mutation leaves the bytes (and therefore the digest) unchanged, or when
    later mutations return the file to an earlier version. Releasing keeps the record
    as history: `release()` sets state/timestamp and the claim is never
    reactivated -- reacquiring the path mints a new claim with a new id and
    generation, so an old token can never authorize a write.
    """

    kind = "file_claim"
    id_prefix = ID_PREFIXES["file_claim"]

    id: str
    workspace_id: str
    path: str
    ticket_id: str
    attempt_id: str
    generation: int
    acquired: str
    observed_version: str
    state: str = "active"
    released: Optional[str] = None
    mutation_seq: int = 0
    contract_version: int = CONTRACT_VERSION

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    def release(self, timestamp: Optional[str] = None) -> None:
        """Revoke active ownership. The record is retained; the token is dead."""
        if not self.is_active:
            raise InvalidRecord(f"claim {self.id} is already released")
        moment = timestamp or utc_now()
        self.state = "released"
        self.released = moment


@dataclass
class ReadObservation(_Record):
    """Evidence that bytes were served through arbite.

    `digest` covers the *entire* file even when `line_range` was returned, and
    `write_authorizing` starts False: a read observed before a claim was taken
    (or served to a different attempt) does not authorize a mutation. The
    application layer sets it True only for a read taken by the same active
    attempt that holds the claim, after the claim was acquired.

    `claim_mutation_seq` is the holder claim's `mutation_seq` when the read was
    taken; a token only authorizes while it still equals the claim's current
    value, which is what makes a token single-use. `version_only` marks a read
    that recorded the whole-file version without serving content (the receipt a
    binary file, which the text surface cannot serve, is replaced under).
    """

    kind = "read_observation"
    id_prefix = ID_PREFIXES["read_observation"]

    id: str
    operation_id: str
    path: str
    digest: str
    observed_at: str
    attempt_id: Optional[str] = None
    actor: Optional[str] = None
    claim_generation: Optional[int] = None
    line_range: Optional[Tuple[int, int]] = None
    write_authorizing: bool = False
    claim_mutation_seq: Optional[int] = None
    version_only: bool = False
    contract_version: int = CONTRACT_VERSION

    @property
    def covers_whole_file(self) -> bool:
        return self.line_range is None

    def authorize(self) -> None:
        """Mark this observation as write-authorizing. Only the application
        layer's guards should call this, after verifying the reader's attempt
        holds the claim."""
        self.write_authorizing = True


@dataclass
class Artifact(_Record):
    """Content-addressed stored bytes referenced by operation receipts.

    `location` is opaque to callers (how and where a sink stores content is its
    own business); `digest` and `size` are the verifiable identity, so content is
    stored once per digest where practical. `available` is False when a sink can
    describe evidence it can no longer serve, rather than silently omitting it.
    """

    kind = "artifact"
    id_prefix = ID_PREFIXES["artifact"]

    id: str
    digest: str
    size: int
    created: str
    location: str
    media_type: str = "application/octet-stream"
    available: bool = True
    contract_version: int = CONTRACT_VERSION


@dataclass
class OperationReceipt(_Record):
    """The durable record of one operation, `id` being the operation id.

    `before`/`after` map each affected path to its digest or the explicit
    `ABSENT` marker; `artifact_refs` point at stored content; `result` is `ok`
    or `error`. Reusing the same `id` for a retry is what makes deduplication
    possible: a store can answer "I already recorded this operation" instead of
    applying it twice. Agent prose is emphatically *not* part of this record --
    summaries live on tickets; this is evidence.
    """

    kind = "operation_receipt"
    id_prefix = ID_PREFIXES["operation_receipt"]

    id: str
    attempt_id: str
    ticket_id: str
    actor: str
    kind_: str
    timestamp: str
    before: Dict[str, str] = field(default_factory=dict)
    after: Dict[str, str] = field(default_factory=dict)
    paths: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    claim_generation: Optional[int] = None
    result: str = "ok"
    error: Optional[Dict[str, Any]] = None
    retry_of: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def operation_kind(self) -> str:
        """The operation's kind. Stored as `kind_` because `kind` is the record
        tag; exposed under the unambiguous name callers should use."""
        return self.kind_

    def to_dict(self) -> dict:
        data = super().to_dict()
        # `kind` is the record tag; spell the operation's own kind explicitly so
        # a JSON consumer never has to know about the `kind_` field name.
        data["operation_kind"] = self.kind_
        data.pop("kind_", None)
        return data

    @classmethod
    def from_dict(cls, data: Any):
        if isinstance(data, dict) and "operation_kind" in data and "kind_" not in data:
            data = dict(data)
            data["kind_"] = data.pop("operation_kind")
        return _load(cls, data)


@dataclass
class OperationIntent(_Record):
    """The durable intent of one file operation, written *before* bytes change.

    The workspace and the sink are separate durability domains: no store
    transaction can atomically commit a filesystem change and its receipt. The
    intent is the bridge. It records, in the store, exactly what the operation is
    about to do -- the canonical paths, the before/after version of each, the
    content-addressed artifacts that hold the real bytes, the claim generation and
    the operation id -- and is committed durably *before* the filesystem is
    touched. If the process dies between the filesystem change and the receipt,
    the next relevant operation reads the intent, observes the filesystem, and
    reconciles honestly (see `arbite.mutation`).

    `operation_id` is the same operation id as the eventual `OperationReceipt.id`,
    so retry deduplication and recovery look at one key. `state` is one of
    `INTENT_STATES`; an intent is reconciled while it is in `INTENT_ACTIVE_STATES`
    and is terminal once `finalized`/`reverted`/`drifted`.
    """

    kind = "operation_intent"
    id_prefix = ID_PREFIXES["operation_intent"]

    id: str
    operation_id: str
    workspace_id: str
    attempt_id: str
    ticket_id: str
    actor: str
    kind_: str
    created: str
    updated: str
    before: Dict[str, str] = field(default_factory=dict)
    after: Dict[str, str] = field(default_factory=dict)
    paths: List[str] = field(default_factory=list)
    artifact_refs: List[str] = field(default_factory=list)
    claim_generation: Optional[int] = None
    state: str = "pending"
    detail: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def operation_kind(self) -> str:
        return self.kind_

    @property
    def is_active(self) -> bool:
        """True while this intent still needs reconciling."""
        return self.state in INTENT_ACTIVE_STATES

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["operation_kind"] = self.kind_
        data.pop("kind_", None)
        return data

    @classmethod
    def from_dict(cls, data: Any):
        if isinstance(data, dict) and "operation_kind" in data and "kind_" not in data:
            data = dict(data)
            data["kind_"] = data.pop("operation_kind")
        return _load(cls, data)


@dataclass
class LifecycleIntent(_Record):
    """The durable journal entry for one ticket lifecycle transition.

    A transition changes two stores that cannot commit together: the ticket sink
    (status/assignee) and the coordination store (the attempt, its file claims and
    the transition's events). The intent is written to the coordination store
    *before* the ticket write, and names everything the coordination half will do:

    - `expected_revision` and `target_status`/`target_assignee` identify the ticket
      write, so reconciliation can tell whether it landed;
    - `end_attempt_id`/`end_state` (plus `reason`/`handoff`) end an attempt,
      releasing its claims; `supersedes_attempt_id` interrupts a taken-over one;
    - `start_attempt` is the new attempt an acquisition records;
    - `events` are the transition's own events, with ids fixed up front.

    The ticket write is the commit point. After it, the coordination half is
    applied in one transaction and the intent is marked `completed`; if that fails
    the ticket is reverted and the intent `abandoned`. A crash leaves the intent
    `pending`, and the next lifecycle operation (or `doctor --fix`) finishes it
    when the ticket shows the target state, or abandons it when it does not.
    """

    kind = "lifecycle_intent"
    id_prefix = ID_PREFIXES["lifecycle_intent"]

    id: str
    workspace_id: str
    ticket_id: str
    transition: str
    actor: str
    created: str
    updated: str
    expected_revision: Optional[int] = None
    target_status: Optional[str] = None
    target_assignee: Optional[str] = None
    end_attempt_id: Optional[str] = None
    end_state: Optional[str] = None
    touch_attempt_id: Optional[str] = None
    supersedes_attempt_id: Optional[str] = None
    start_attempt: Optional[WorkAttempt] = None
    origin: Optional[str] = None
    reason: Optional[str] = None
    handoff: Optional[str] = None
    sweep_ticket_claims: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)
    state: str = "pending"
    detail: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @classmethod
    def _nested(cls):
        return (("start_attempt", WorkAttempt),)

    @property
    def is_pending(self) -> bool:
        return self.state == "pending"


@dataclass
class Event(_Record):
    """One append-only entry in the per-store event stream.

    `cursor` is monotonic per store and is assigned by the store when the event
    is appended; `id` is globally unique. `category` lets read observations be
    queried separately from lifecycle/operation traffic, and `payload` carries
    the versioned specifics.
    """

    kind = "event"
    id_prefix = ID_PREFIXES["event"]

    id: str
    kind_: str
    category: str
    timestamp: str
    #: Assigned by the store when the event is appended (`None` means "unset").
    cursor: Optional[int] = None
    subject_ids: List[str] = field(default_factory=list)
    operation_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    payload_version: int = EVENT_PAYLOAD_VERSION
    contract_version: int = CONTRACT_VERSION

    @property
    def event_kind(self) -> str:
        return self.kind_

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["event_kind"] = self.kind_
        data.pop("kind_", None)
        return data

    @classmethod
    def from_dict(cls, data: Any):
        if isinstance(data, dict) and "event_kind" in data and "kind_" not in data:
            data = dict(data)
            data["kind_"] = data.pop("event_kind")
        return _load(cls, data)


@dataclass
class RecoveryReport(_Record):
    """What inspection of an incomplete operation found.

    Produced by a sink's recovery interface (never by a daemon): `state`
    classifies the finding, and `bytes_may_have_changed` says whether the caller
    must look at the filesystem before retrying. `drifted`/`unknown` are never
    auto-repaired -- evidence is preserved and reported.
    """

    kind = "recovery_report"
    id_prefix = ID_PREFIXES["recovery_report"]

    workspace_id: str
    operation_id: str
    state: str
    observed_at: str
    id: Optional[str] = None
    kind_: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    bytes_may_have_changed: bool = False
    detail: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = new_record_id("recovery_report")

    @property
    def operation_kind(self) -> Optional[str]:
        return self.kind_


# Record kind -> class, for `record_from_dict`.
RECORD_CLASSES = {
    cls.kind: cls
    for cls in (
        Workspace,
        StoreBinding,
        WorkAttempt,
        FileClaim,
        ReadObservation,
        Artifact,
        OperationReceipt,
        OperationIntent,
        LifecycleIntent,
        Event,
        RecoveryReport,
    )
}


def record_from_dict(data: Any):
    """Rebuild any coordination record from `to_dict()` output, dispatching on
    the `kind` tag. Raises `InvalidRecord` for an unknown or missing kind."""
    if not isinstance(data, dict):
        raise InvalidRecord(f"expected a record mapping, got {type(data).__name__}")
    kind = data.get("kind")
    if kind not in RECORD_CLASSES:
        raise InvalidRecord(
            f"unknown coordination record kind {kind!r} "
            f"(valid: {', '.join(sorted(RECORD_CLASSES))})"
        )
    return RECORD_CLASSES[kind].from_dict(data)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_VALIDATORS = {}


def _validator(kind: str):
    def decorate(func):
        _VALIDATORS[kind] = func
        return func

    return decorate


def _check(problems: list, condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def _check_id(problems: list, record, label: str = "id") -> None:
    value = getattr(record, label, None)
    _check(problems, is_opaque_id(value, record.id_prefix),
           f"{label} {value!r} is not an opaque {record.id_prefix}- id")


def _check_utc(problems: list, value, label: str) -> None:
    _check(problems, is_utc_timestamp(value), f"{label} {value!r} is not a UTC timestamp")


def _check_path(problems: list, value, label: str) -> None:
    try:
        canonical = canonical_relative_path(value)
    except UnsupportedCoordination as e:
        problems.append(f"{label}: {e}")
        return
    _check(problems, canonical == value, f"{label} {value!r} is not canonical ({canonical!r})")
    _check(problems, not is_protected_path(canonical),
           f"{label} {value!r} addresses protected arbite/.git metadata")


def _check_generation(problems: list, value, label: str) -> None:
    _check(problems, isinstance(value, int) and not isinstance(value, bool) and value >= 1,
           f"{label} must be an integer >= 1, got {value!r}")


@_validator("workspace")
def _validate_workspace(record: Workspace) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, bool(record.root), "root must not be empty")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    _check(problems, record.contract_version >= 1, "contract_version must be >= 1")
    if record.store_binding is not None:
        _check(problems, record.store_binding.workspace_id == record.id,
               "store_binding.workspace_id does not match the workspace id")
        problems.extend(
            f"store_binding: {p}" for p in record.store_binding.validate()
        )
    return problems


@_validator("store_binding")
def _validate_store_binding(record: StoreBinding) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, bool(record.workspace_id), "workspace_id must not be empty")
    _check(problems, bool(record.sink_kind), "sink_kind must not be empty")
    _check(problems, bool(record.location), "location must not be empty")
    _check_utc(problems, record.bound_at, "bound_at")
    _check(problems, record.contract_version >= 1, "contract_version must be >= 1")
    return problems


@_validator("work_attempt")
def _validate_work_attempt(record: WorkAttempt) -> list:
    problems = []
    _check_id(problems, record)
    for label in ("ticket_id", "worker_id", "workspace_id"):
        _check(problems, bool(getattr(record, label)), f"{label} must not be empty")
    _check_generation(problems, record.generation, "generation")
    _check(problems, record.state in ATTEMPT_STATES,
           f"state {record.state!r} is not one of {', '.join(ATTEMPT_STATES)}")
    _check_utc(problems, record.started, "started")
    _check_utc(problems, record.last_activity, "last_activity")
    if record.state == "active":
        _check(problems, record.ended is None, "an active attempt must not have an 'ended' time")
    else:
        _check_utc(problems, record.ended, "ended")
    if record.ended is not None:
        _check(problems, record.ended >= record.started,
               "ended must not precede started")
    return problems


@_validator("file_claim")
def _validate_file_claim(record: FileClaim) -> list:
    problems = []
    _check_id(problems, record)
    for label in ("workspace_id", "ticket_id", "attempt_id"):
        _check(problems, bool(getattr(record, label)), f"{label} must not be empty")
    _check_path(problems, record.path, "path")
    _check_generation(problems, record.generation, "generation")
    _check(problems, record.state in CLAIM_STATES,
           f"state {record.state!r} is not one of {', '.join(CLAIM_STATES)}")
    _check_utc(problems, record.acquired, "acquired")
    _check(problems, is_digest_or_absent(record.observed_version),
           f"observed_version {record.observed_version!r} must be a digest or {ABSENT!r}")
    if record.state == "active":
        _check(problems, record.released is None, "an active claim must not have a 'released' time")
    else:
        _check_utc(problems, record.released, "released")
    _check(problems, isinstance(record.mutation_seq, int) and not isinstance(record.mutation_seq, bool)
           and record.mutation_seq >= 0,
           f"mutation_seq must be an integer >= 0, got {record.mutation_seq!r}")
    return problems


@_validator("read_observation")
def _validate_read_observation(record: ReadObservation) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, bool(record.operation_id), "operation_id must not be empty")
    _check_path(problems, record.path, "path")
    _check(problems, is_digest(record.digest),
           f"digest {record.digest!r} must be a {DIGEST_PREFIX}<hex> digest covering the whole file")
    _check_utc(problems, record.observed_at, "observed_at")
    if record.claim_generation is not None:
        _check_generation(problems, record.claim_generation, "claim_generation")
    if record.claim_mutation_seq is not None:
        _check(problems, isinstance(record.claim_mutation_seq, int)
               and not isinstance(record.claim_mutation_seq, bool) and record.claim_mutation_seq >= 0,
               f"claim_mutation_seq must be an integer >= 0, got {record.claim_mutation_seq!r}")
    if record.line_range is not None:
        ok = (
            isinstance(record.line_range, (list, tuple))
            and len(record.line_range) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in record.line_range)
        )
        _check(problems, ok, f"line_range {record.line_range!r} must be [start, end] with start >= 1")
        if ok:
            _check(problems, record.line_range[0] <= record.line_range[1],
                   "line_range start must not exceed end")
    return problems


@_validator("artifact")
def _validate_artifact(record: Artifact) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, is_digest(record.digest), f"digest {record.digest!r} must be a content digest")
    _check(problems, isinstance(record.size, int) and not isinstance(record.size, bool) and record.size >= 0,
           f"size must be an integer >= 0, got {record.size!r}")
    _check(problems, bool(record.location), "location must not be empty")
    _check_utc(problems, record.created, "created")
    return problems


@_validator("operation_receipt")
def _validate_operation_receipt(record: OperationReceipt) -> list:
    problems = []
    _check_id(problems, record)
    for label in ("attempt_id", "ticket_id", "actor"):
        _check(problems, bool(getattr(record, label)), f"{label} must not be empty")
    _check(problems, record.kind_ in OPERATION_KINDS,
           f"operation kind {record.kind_!r} is not one of {', '.join(OPERATION_KINDS)}")
    _check_utc(problems, record.timestamp, "timestamp")
    _check(problems, record.result in OPERATION_RESULTS,
           f"result {record.result!r} is not one of {', '.join(OPERATION_RESULTS)}")
    for path in record.paths:
        _check_path(problems, path, "paths[]")
    for mapping_label in ("before", "after"):
        for path, version in getattr(record, mapping_label).items():
            _check_path(problems, path, f"{mapping_label} key")
            _check(problems, is_digest_or_absent(version),
                   f"{mapping_label}[{path!r}] must be a digest or {ABSENT!r}, got {version!r}")
    if record.claim_generation is not None:
        _check_generation(problems, record.claim_generation, "claim_generation")
    if record.result == "error":
        _check(problems, isinstance(record.error, dict) and bool(record.error.get("code")),
               "an error receipt must carry an error mapping with a 'code'")
    return problems


@_validator("operation_intent")
def _validate_operation_intent(record: OperationIntent) -> list:
    problems = []
    _check_id(problems, record)
    for label in ("operation_id", "workspace_id", "attempt_id", "ticket_id", "actor"):
        _check(problems, bool(getattr(record, label)), f"{label} must not be empty")
    _check(problems, record.kind_ in MUTATION_KINDS,
           f"intent kind {record.kind_!r} is not a mutation kind "
           f"({', '.join(MUTATION_KINDS)})")
    _check(problems, record.state in INTENT_STATES,
           f"state {record.state!r} is not one of {', '.join(INTENT_STATES)}")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    for path in record.paths:
        _check_path(problems, path, "paths[]")
    for mapping_label in ("before", "after"):
        for path, version in getattr(record, mapping_label).items():
            _check_path(problems, path, f"{mapping_label} key")
            _check(problems, is_digest_or_absent(version),
                   f"{mapping_label}[{path!r}] must be a digest or {ABSENT!r}, got {version!r}")
    if record.claim_generation is not None:
        _check_generation(problems, record.claim_generation, "claim_generation")
    _check(problems, set(record.paths) == set(record.before) == set(record.after),
           "paths, before and after must name exactly the same paths")
    return problems


@_validator("lifecycle_intent")
def _validate_lifecycle_intent(record: LifecycleIntent) -> list:
    problems = []
    _check_id(problems, record)
    for label in ("workspace_id", "ticket_id", "transition", "actor"):
        _check(problems, bool(getattr(record, label)), f"{label} must not be empty")
    _check(problems, record.state in LIFECYCLE_INTENT_STATES,
           f"state {record.state!r} is not one of {', '.join(LIFECYCLE_INTENT_STATES)}")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    if record.expected_revision is not None:
        _check(problems, isinstance(record.expected_revision, int)
               and not isinstance(record.expected_revision, bool) and record.expected_revision >= 1,
               f"expected_revision must be an integer >= 1 or null, got {record.expected_revision!r}")
    if record.end_attempt_id is not None:
        _check(problems, record.end_state in LIFECYCLE_END_STATES,
               f"end_state {record.end_state!r} is not one of {', '.join(LIFECYCLE_END_STATES)}")
    if record.start_attempt is not None:
        for problem in validate_record(record.start_attempt):
            problems.append(f"start_attempt: {problem}")
    _check(problems, isinstance(record.events, list) and all(isinstance(e, dict) for e in record.events),
           "events must be a list of event mappings")
    return problems


@_validator("event")
def _validate_event(record: Event) -> list:
    problems = []
    _check_id(problems, record)
    _check(
        problems,
        record.cursor is None
        or (isinstance(record.cursor, int) and not isinstance(record.cursor, bool) and record.cursor >= 0),
        f"cursor must be None (unassigned) or an integer >= 0, got {record.cursor!r}",
    )
    _check(problems, record.kind_ in EVENT_KINDS,
           f"event kind {record.kind_!r} is not one of {', '.join(EVENT_KINDS)}")
    _check(problems, record.category in EVENT_CATEGORIES,
           f"category {record.category!r} is not one of {', '.join(EVENT_CATEGORIES)}")
    _check_utc(problems, record.timestamp, "timestamp")
    _check(problems, isinstance(record.payload, dict), "payload must be a mapping")
    _check(problems, record.payload_version >= 1, "payload_version must be >= 1")
    return problems


@_validator("recovery_report")
def _validate_recovery_report(record: RecoveryReport) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, bool(record.workspace_id), "workspace_id must not be empty")
    _check(problems, bool(record.operation_id), "operation_id must not be empty")
    _check(problems, record.state in RECOVERY_STATES,
           f"state {record.state!r} is not one of {', '.join(RECOVERY_STATES)}")
    _check_utc(problems, record.observed_at, "observed_at")
    for path in record.paths:
        _check_path(problems, path, "paths[]")
    return problems


def validate_record(record) -> List[str]:
    """Invariant problems for a single record, or `[]` when it is clean.

    Dispatches on `record.kind`. An object that is not a coordination record is
    reported rather than raising, so a caller aggregating problems (a doctor
    command, a test) never has to guard the call.
    """
    kind = getattr(record, "kind", None)
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return [f"unknown coordination record kind {kind!r}"]
    return list(validator(record))


def validate_records(records) -> List[str]:
    """Every individual problem across `records`, prefixed by record kind/id so
    the source is identifiable, plus the cross-record invariants."""
    problems = []
    for record in records:
        for problem in validate_record(record):
            problems.append(f"{getattr(record, 'kind', '?')} {getattr(record, 'record_id', '?')}: {problem}")
    problems.extend(validate_collection(records))
    return problems


def validate_collection(records) -> List[str]:
    """Cross-record invariants that no single record can check.

    These are the one-workspace/one-store and one-active-owner rules the plan
    states as facts about the *set* of records:

    - at most one active attempt per ticket (one active attempt per ticket
      initially);
    - at most one active claim per (workspace, path).

    Duplicate ids and duplicate event cursors are also reported, because both
    make "which record is current" unanswerable.
    """
    problems = []
    seen_ids = {}
    cursors = {}
    active_attempts = {}
    active_claims = {}

    for record in records:
        record_id = getattr(record, "record_id", None)
        if record_id is not None:
            seen_ids.setdefault(record_id, 0)
            seen_ids[record_id] += 1

        if isinstance(record, Event):
            if record.cursor in cursors:
                problems.append(
                    f"event cursor {record.cursor} is used by both {cursors[record.cursor]} "
                    f"and {record.id}; cursors must be unique per store"
                )
            else:
                cursors[record.cursor] = record.id

        if isinstance(record, WorkAttempt) and record.is_active:
            key = record.ticket_id
            if key in active_attempts:
                problems.append(
                    f"ticket {key} has two active attempts ({active_attempts[key]} and "
                    f"{record.id}); one active attempt per ticket"
                )
            else:
                active_attempts[key] = record.id

        if isinstance(record, FileClaim) and record.is_active:
            key = (record.workspace_id, record.path)
            if key in active_claims:
                problems.append(
                    f"path {record.path!r} in workspace {record.workspace_id} is actively "
                    f"claimed by both {active_claims[key]} and {record.id}; exclusive "
                    "writer ownership is per whole file"
                )
            else:
                active_claims[key] = record.id

    for record_id, count in sorted(seen_ids.items()):
        if count > 1:
            problems.append(f"record id {record_id} appears {count} times; ids are durable and unique")

    return problems


__all__ = [
    "ABSENT",
    "ATTRIBUTION_NOTICE",
    "ATTEMPT_STATES",
    "Artifact",
    "artifact_id_for_digest",
    "CLAIM_STATES",
    "INTENT_ACTIVE_STATES",
    "INTENT_STATES",
    "LIFECYCLE_END_STATES",
    "LIFECYCLE_INTENT_STATES",
    "LifecycleIntent",
    "CONTRACT_VERSION",
    "EVENT_CATEGORIES",
    "EVENT_KINDS",
    "EVENT_PAYLOAD_VERSION",
    "ErrorCode",
    "Event",
    "FileClaim",
    "ID_PREFIXES",
    "MUTATION_KINDS",
    "OPERATION_KINDS",
    "OPERATION_RESULTS",
    "OperationIntent",
    "OperationReceipt",
    "RECORD_CLASSES",
    "RECOVERY_STATES",
    "ReadObservation",
    "RecoveryReport",
    "StoreBinding",
    "WorkAttempt",
    "Workspace",
    "canonical_relative_path",
    "digest_of_bytes",
    "digest_of_text",
    "error_result",
    "gen_opaque_id",
    "is_digest",
    "is_digest_or_absent",
    "is_opaque_id",
    "is_protected_path",
    "is_utc_timestamp",
    "new_operation_id",
    "new_record_id",
    "ok_result",
    "parse_utc",
    "record_from_dict",
    "utc_now",
    "validate_collection",
    "validate_record",
    "validate_records",
]
