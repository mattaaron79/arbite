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
- `WorkerProfile` -- an optional, provider-neutral worker declaration (planning
  key B01, multi-provider job board): configured tier, capability labels,
  locality, cost class and declared capacity. Operator assertions, not verified
  identity; never credentials. Eligibility rules over it live in
  `arbite.eligibility`.
- `Reservation` -- a coordinator's hold over an explicit set of ticket ids
  (planning key B02). It restricts who may *acquire* its members; it never
  creates an attempt or moves a ticket. Operations live in `arbite.reservations`.
- `Offer` -- a public offer or direct assignment over a ticket (planning key
  B03). Accepting it happens inside ticket acquisition; operations live in
  `arbite.offers`.
- `Package` -- an ordered same-worker continuity package over tickets (planning
  key B04): one worker, members in order, each with its own attempt. Operations
  live in `arbite.packages`.
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

from .schema import TIERS
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
EVENT_CATEGORIES = [
    "lifecycle", "operation", "claim", "read", "worker", "reservation", "offer", "package",
]

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
    #: Worker-profile administration (planning key B01), category `worker`.
    #: Check-ins are not events: they only refresh `WorkerProfile.last_checkin`.
    "worker_registered",
    "worker_updated",
    "worker_disabled",
    "worker_enabled",
    #: Reservation administration (planning key B02), category `reservation`.
    "reservation_created",
    "reservation_members_added",
    "reservation_members_removed",
    "reservation_released",
    #: Offers and direct assignments (planning key B03), category `offer`.
    #: `offer_accepted` commits with the attempt it starts; `offer_completed` /
    #: `offer_cancelled` commit with the ticket transition that caused them.
    "offer_published",
    "offer_withdrawn",
    "offer_accepted",
    "offer_completed",
    "offer_cancelled",
    #: Continuity packages (planning key B04), category `package`. Bind/start/
    #: advance/complete commit with the ticket transition that caused them.
    "package_created",
    "package_bound",
    "package_member_started",
    "package_advanced",
    "package_completed",
    "package_rebound",
    "package_released",
    "package_noted",
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

#: Declared execution locality of a worker. `unknown` is a first-class value:
#: a restricted requirement treats it as unmet, never as a pass.
WORKER_LOCALITIES = ["local", "remote", "unknown"]

#: Declared cost class of a worker (planning key B01). No pricing catalog and no
#: currency conversion: an optional `cost_estimate` carries explicit units.
COST_CLASSES = ["local", "paid", "unknown"]

#: Reservation states (planning key B02). A released reservation is kept as
#: history; it restricts nothing.
RESERVATION_STATES = ["active", "released"]

#: How a reservation's membership was resolved at creation: an explicit ticket
#: list, or a one-time snapshot of an epic (later epic tickets are not included).
RESERVATION_SOURCES = ["tickets", "epic"]

#: Offer states (planning key B03). `published` is the only state that grants
#: acquisition; `accepted` is bound to the worker whose attempt it started;
#: `withdrawn`, `completed` and `cancelled` are terminal history.
OFFER_STATES = ["published", "accepted", "withdrawn", "completed", "cancelled"]

#: Offer states that still hold the ticket (at most one per ticket).
OFFER_LIVE_STATES = ["published", "accepted"]

#: `public`: any worker meeting the requirements may accept. `assigned`: only
#: the named `allowed_workers` may (a direct assignment).
OFFER_MODES = ["public", "assigned"]

#: What an offer covers: one ticket, or an ordered continuity package (B04;
#: `tickets` then lists the package members in order and `package_id` names it).
OFFER_TARGET_KINDS = ["ticket", "package"]

#: Package states (planning key B04). `open`: not yet bound (its first ready
#: member may be acquired, which binds the acquirer). `bound`: every remaining
#: member belongs to `bound_worker`, in order. `completed` (every member closed)
#: and `released` (explicit handoff released the remaining members) are history.
PACKAGE_STATES = ["open", "bound", "completed", "released"]

#: Package states that still restrict acquisition of their members.
PACKAGE_LIVE_STATES = ["open", "bound"]

#: Continuity policies. Only "the same worker does every member, in order".
PACKAGE_POLICIES = ["same_worker"]

#: Requirement keys an offer may carry (hard constraints, see `eligibility`).
OFFER_REQUIREMENT_KEYS = ("min_tier", "capabilities", "local_only", "max_cost")

#: Preference keys (hints only, never enforced, never a winner guarantee).
OFFER_PREFERENCE_KEYS = ("prefer_local", "prefer_low_cost", "prefer_workers")

#: Worker ids are self-declared names (`claude.opus-5.002`), not opaque ids.
WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$")

#: Capability/tool labels: lowercase tokens compared exactly.
CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:+/-]{0,63}$")

#: Value shapes that look like credentials. Profiles hold operator assertions and
#: labels only; a value matching one of these is refused rather than stored.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}"),
)

#: A long unbroken alphanumeric run mixing letters and digits reads as a key.
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9+/]{32,}")



def looks_like_secret(value: Any) -> bool:
    """True when `value` resembles a credential (API key, token, private key).

    A heuristic guard, not a scanner: it exists so a profile label can never be
    used as a place to park a key. False positives are refused loudly; the
    caller can rephrase the label."""
    if not isinstance(value, str) or not value:
        return False
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        return True
    return any(
        re.search(r"[0-9]", run) and re.search(r"[A-Za-z]", run)
        for run in _OPAQUE_TOKEN.findall(value)
    )

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
    "worker_profile": "wkr",
    "reservation": "rsv",
    "offer": "off",
    "package": "pkg",
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
    #: B03: `{"offer_id", "worker_id", "expected_revision"}` when this acquisition
    #: accepts an offer; the cascade advances the offer in the same transaction
    #: that stores `start_attempt`, so acceptance and attempt commit together.
    offer_acceptance: Optional[Dict[str, Any]] = None
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


@dataclass
class WorkerProfile(_Record):
    """An optional, provider-neutral declaration of one worker (planning key B01).

    `worker_id` is the same self-declared name used for claims and attempts;
    `id` is the opaque record handle. Every field is an operator assertion, not a
    verified fact: `tier` is authoritative for acquisition by this worker id (a
    per-call declaration cannot raise it), while `provider`/`model`/`runtime` are
    labels that never trigger any provider call. `last_checkin` is declared
    activity, never liveness. A disabled profile is retained with its history;
    it stops new acquisitions and revokes nothing already running.

    `cost_estimate`, when present, is `{"amount": number >= 0, "unit": str,
    "provenance": str}`. No credential is ever stored (`looks_like_secret`).
    """

    kind = "worker_profile"
    id_prefix = ID_PREFIXES["worker_profile"]

    id: str
    worker_id: str
    tier: str
    created: str
    updated: str
    provider: Optional[str] = None
    model: Optional[str] = None
    runtime: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    locality: str = "unknown"
    cost_class: str = "unknown"
    cost_estimate: Optional[Dict[str, Any]] = None
    capacity: Optional[int] = None
    enabled: bool = True
    disabled_at: Optional[str] = None
    disabled_reason: Optional[str] = None
    last_checkin: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)


@dataclass
class Reservation(_Record):
    """A coordinator's hold over an explicit set of tickets (planning key B02).

    `owner` is the coordinator's self-declared worker id; `members` are ticket
    ids, fixed until an explicit add/remove. While `active`, only the owner (and,
    later, workers an offer/assignment names) may acquire a member; the
    reservation itself never starts an attempt or sets `in_progress`. `source`
    records how membership was resolved at creation (`{"kind": "tickets"}` or
    `{"kind": "epic", "epic": ..., "excluded": [...]}`); an epic is resolved
    once. Released reservations are retained as history.
    """

    kind = "reservation"
    id_prefix = ID_PREFIXES["reservation"]

    id: str
    owner: str
    members: List[str]
    created: str
    updated: str
    state: str = "active"
    source: Dict[str, Any] = field(default_factory=lambda: {"kind": "tickets"})
    note: Optional[str] = None
    released: Optional[str] = None
    released_by: Optional[str] = None
    release_reason: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def is_active(self) -> bool:
        return self.state == "active"


@dataclass
class Offer(_Record):
    """Work a coordinator makes available for pickup (planning key B03).

    `tickets` is the ordered target (exactly one ticket while `target_kind` is
    `ticket`; B04 packages reuse the list). `mode` is `public` (any worker
    meeting `requirements`) or `assigned` (only `allowed_workers`: a direct
    assignment). `requirements` are hard constraints evaluated restricted at
    acquisition (unknown worker data fails); `preferences` are labelled hints
    that nothing enforces -- the first eligible claimant wins.

    A worker accepts by acquiring the ticket; the lifecycle cascade that stores
    the new attempt also moves the offer to `accepted` (`accepted_by`,
    `attempt_id`), so two workers cannot both accept. The offer then tracks its
    ticket: `completed` when the ticket closes, `cancelled` when the accepting
    worker stops holding it. `provenance` records who published it and why.
    """

    kind = "offer"
    id_prefix = ID_PREFIXES["offer"]

    id: str
    tickets: List[str]
    mode: str
    publisher: str
    created: str
    updated: str
    target_kind: str = "ticket"
    reservation_id: Optional[str] = None
    requirements: Dict[str, Any] = field(default_factory=dict)
    allowed_workers: List[str] = field(default_factory=list)
    preferences: Dict[str, Any] = field(default_factory=dict)
    state: str = "published"
    note: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    accepted_by: Optional[str] = None
    accepted_at: Optional[str] = None
    attempt_id: Optional[str] = None
    ended_at: Optional[str] = None
    ended_by: Optional[str] = None
    end_reason: Optional[str] = None
    #: B04: the package a `target_kind="package"` offer covers.
    package_id: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def is_live(self) -> bool:
        return self.state in OFFER_LIVE_STATES

    @property
    def is_published(self) -> bool:
        return self.state == "published"


@dataclass
class Package(_Record):
    """An ordered same-worker continuity package (planning key B04).

    `tickets` is the member order; the order is a scheduling edge (member N+1
    is never acquired before member N is closed) even when no `depends_on`
    duplicates it. `bound_worker` is continuity *identity* -- the same worker
    id, not a promise of the same model session: each member gets its own
    attempt and file claims are released between members. `current` caches the
    first member that is not closed (derived again from ticket status whenever
    it matters). `handoffs` records explicit rebind/release decisions with their
    reason; `notes` are durable notes for a resumed session. At most one live
    (`open`/`bound`) package per ticket.
    """

    kind = "package"
    id_prefix = ID_PREFIXES["package"]

    id: str
    tickets: List[str]
    created_by: str
    created: str
    updated: str
    policy: str = "same_worker"
    state: str = "open"
    bound_worker: Optional[str] = None
    bound_at: Optional[str] = None
    current: Optional[str] = None
    reservation_id: Optional[str] = None
    note: Optional[str] = None
    handoffs: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[Dict[str, Any]] = field(default_factory=list)
    outcome: Optional[str] = None
    ended_at: Optional[str] = None
    ended_by: Optional[str] = None
    contract_version: int = CONTRACT_VERSION

    @property
    def is_live(self) -> bool:
        return self.state in PACKAGE_LIVE_STATES


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
        WorkerProfile,
        Reservation,
        Offer,
        Package,
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
    acceptance = record.offer_acceptance
    if acceptance is not None:
        _check(problems, isinstance(acceptance, dict) and id_for_kind(acceptance.get("offer_id"), "offer")
               and bool(acceptance.get("worker_id")),
               "offer_acceptance must name an offer id and a worker id")
        _check(problems, record.start_attempt is not None,
               "offer_acceptance needs the start_attempt it accepts the offer with")
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


_LABEL_FIELDS = ("provider", "model", "runtime")


@_validator("worker_profile")
def _validate_worker_profile(record: WorkerProfile) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, isinstance(record.worker_id, str) and bool(WORKER_ID_PATTERN.match(record.worker_id)),
           f"worker_id {record.worker_id!r} must match {WORKER_ID_PATTERN.pattern}")
    _check(problems, record.tier in TIERS,
           f"tier {record.tier!r} is not one of {', '.join(TIERS)}")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    for label in _LABEL_FIELDS:
        value = getattr(record, label)
        if value is None:
            continue
        ok = isinstance(value, str) and value == value.strip() and 0 < len(value) <= 128 \
            and not any(ord(ch) < 32 for ch in value)
        _check(problems, ok, f"{label} {value!r} must be a non-empty single-line label of at most 128 chars")
    capabilities = record.capabilities
    if not isinstance(capabilities, list):
        problems.append("capabilities must be a list of labels")
        capabilities = []
    for capability in capabilities:
        _check(problems, isinstance(capability, str) and bool(CAPABILITY_PATTERN.match(capability)),
               f"capability {capability!r} must match {CAPABILITY_PATTERN.pattern}")
    _check(problems, len(set(map(str, capabilities))) == len(capabilities),
           "capabilities must not repeat")
    _check(problems, record.locality in WORKER_LOCALITIES,
           f"locality {record.locality!r} is not one of {', '.join(WORKER_LOCALITIES)}")
    _check(problems, record.cost_class in COST_CLASSES,
           f"cost_class {record.cost_class!r} is not one of {', '.join(COST_CLASSES)}")
    estimate = record.cost_estimate
    if estimate is not None:
        if not isinstance(estimate, dict):
            problems.append("cost_estimate must be a mapping or null")
        else:
            amount = estimate.get("amount")
            _check(problems, isinstance(amount, (int, float)) and not isinstance(amount, bool)
                   and amount >= 0, f"cost_estimate.amount must be a number >= 0, got {amount!r}")
            for key in ("unit", "provenance"):
                value = estimate.get(key)
                _check(problems, isinstance(value, str) and bool(value.strip()),
                       f"cost_estimate.{key} must be a non-empty string (explicit units/provenance)")
            extra = sorted(set(estimate) - {"amount", "unit", "provenance"})
            _check(problems, not extra, f"cost_estimate has unexpected keys: {', '.join(extra)}")
    if record.capacity is not None:
        _check(problems, isinstance(record.capacity, int) and not isinstance(record.capacity, bool)
               and record.capacity >= 1, f"capacity must be an integer >= 1 or null, got {record.capacity!r}")
    _check(problems, isinstance(record.enabled, bool), "enabled must be a boolean")
    if record.enabled is False:
        _check_utc(problems, record.disabled_at, "disabled_at")
    elif record.enabled is True:
        _check(problems, record.disabled_at is None, "an enabled profile must not have 'disabled_at'")
    if record.last_checkin is not None:
        _check_utc(problems, record.last_checkin, "last_checkin")
    texts = [getattr(record, label) for label in _LABEL_FIELDS] + list(capabilities)
    texts.append(record.disabled_reason)
    if isinstance(estimate, dict):
        texts.extend([estimate.get("unit"), estimate.get("provenance")])
    if any(looks_like_secret(text) for text in texts):
        problems.append("a profile value looks like a credential; profiles never store secrets")
    return problems


@_validator("reservation")
def _validate_reservation(record: Reservation) -> list:
    problems = []
    _check_id(problems, record)
    _check(problems, isinstance(record.owner, str) and bool(WORKER_ID_PATTERN.match(record.owner)),
           f"owner {record.owner!r} must match {WORKER_ID_PATTERN.pattern}")
    members = record.members
    if not isinstance(members, list):
        problems.append("members must be a list of ticket ids")
        members = []
    _check(problems, all(isinstance(m, str) and m.strip() == m and m for m in members),
           "members must be non-empty ticket id strings")
    _check(problems, len(set(map(str, members))) == len(members), "members must not repeat")
    _check(problems, record.state in RESERVATION_STATES,
           f"state {record.state!r} is not one of {', '.join(RESERVATION_STATES)}")
    if record.state == "active":
        _check(problems, bool(members), "an active reservation must have at least one member")
        _check(problems, record.released is None, "an active reservation must not have 'released'")
    elif record.state == "released":
        _check_utc(problems, record.released, "released")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    source = record.source
    _check(problems, isinstance(source, dict) and source.get("kind") in RESERVATION_SOURCES,
           f"source must be a mapping whose kind is one of {', '.join(RESERVATION_SOURCES)}")
    if isinstance(source, dict) and source.get("kind") == "epic":
        _check(problems, isinstance(source.get("epic"), str) and bool(source.get("epic")),
               "an epic source must name its epic")
    if record.note is not None:
        _check(problems, isinstance(record.note, str) and len(record.note) <= 500,
               "note must be a string of at most 500 chars")
    return problems


@_validator("offer")
def _validate_offer(record: Offer) -> list:
    problems = []
    _check_id(problems, record)
    tickets = record.tickets
    if not isinstance(tickets, list):
        problems.append("tickets must be an ordered list of ticket ids")
        tickets = []
    _check(problems, all(isinstance(t, str) and t and t.strip() == t for t in tickets),
           "tickets must be non-empty ticket id strings")
    _check(problems, len(set(map(str, tickets))) == len(tickets), "tickets must not repeat")
    _check(problems, record.target_kind in OFFER_TARGET_KINDS,
           f"target_kind {record.target_kind!r} is not one of {', '.join(OFFER_TARGET_KINDS)}")
    if record.target_kind == "ticket":
        _check(problems, len(tickets) == 1, "a ticket offer names exactly one ticket")
        _check(problems, record.package_id is None, "a ticket offer has no package_id")
    elif record.target_kind == "package":
        _check(problems, len(tickets) >= 2, "a package offer names the package's (2+) members")
        _check(problems, id_for_kind(record.package_id, "package"),
               f"target_kind 'package' requires a package_id ({record.package_id!r} is not a "
               "package id)")
    _check(problems, record.mode in OFFER_MODES,
           f"mode {record.mode!r} is not one of {', '.join(OFFER_MODES)}")
    _check(problems, isinstance(record.publisher, str) and bool(WORKER_ID_PATTERN.match(record.publisher)),
           f"publisher {record.publisher!r} must match {WORKER_ID_PATTERN.pattern}")
    if record.reservation_id is not None:
        _check(problems, id_for_kind(record.reservation_id, "reservation"),
               f"reservation_id {record.reservation_id!r} is not a reservation id")
    allowed = record.allowed_workers
    if not isinstance(allowed, list):
        problems.append("allowed_workers must be a list of worker ids")
        allowed = []
    _check(problems, all(isinstance(w, str) and WORKER_ID_PATTERN.match(w) for w in allowed),
           "allowed_workers must be valid worker ids")
    _check(problems, len(set(map(str, allowed))) == len(allowed), "allowed_workers must not repeat")
    if record.mode == "assigned":
        _check(problems, bool(allowed), "a direct assignment names at least one allowed worker")
    elif record.mode == "public":
        _check(problems, not allowed, "a public offer has no allowed_workers (use mode 'assigned')")
    requirements = record.requirements
    if not isinstance(requirements, dict):
        problems.append("requirements must be a mapping")
        requirements = {}
    extra = sorted(set(requirements) - set(OFFER_REQUIREMENT_KEYS))
    _check(problems, not extra, f"requirements has unexpected keys: {', '.join(extra)}")
    tier = requirements.get("min_tier")
    _check(problems, tier is None or tier in TIERS, f"min_tier {tier!r} is not one of {', '.join(TIERS)}")
    capabilities = requirements.get("capabilities") or []
    _check(problems, isinstance(capabilities, list)
           and all(isinstance(cap, str) and CAPABILITY_PATTERN.match(cap) for cap in capabilities),
           f"capabilities must be labels matching {CAPABILITY_PATTERN.pattern}")
    _check(problems, isinstance(requirements.get("local_only", False), bool), "local_only must be a boolean")
    ceiling = requirements.get("max_cost")
    if ceiling is not None:
        amount = ceiling.get("amount") if isinstance(ceiling, dict) else None
        unit = ceiling.get("unit") if isinstance(ceiling, dict) else None
        _check(problems, isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0
               and isinstance(unit, str) and bool(unit.strip()),
               "max_cost needs a non-negative amount and an explicit unit")
    preferences = record.preferences
    if not isinstance(preferences, dict):
        problems.append("preferences must be a mapping")
        preferences = {}
    extra = sorted(set(preferences) - set(OFFER_PREFERENCE_KEYS))
    _check(problems, not extra, f"preferences has unexpected keys: {', '.join(extra)}")
    _check(problems, record.state in OFFER_STATES,
           f"state {record.state!r} is not one of {', '.join(OFFER_STATES)}")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    if record.state in ("accepted", "completed"):
        _check(problems, bool(record.accepted_by) and id_for_kind(record.attempt_id, "work_attempt"),
               f"an {record.state} offer names the accepting worker and its attempt")
        _check_utc(problems, record.accepted_at, "accepted_at")
    if record.state in ("withdrawn", "completed", "cancelled"):
        _check_utc(problems, record.ended_at, "ended_at")
    else:
        _check(problems, record.ended_at is None, f"a {record.state} offer must not have 'ended_at'")
    if record.note is not None:
        _check(problems, isinstance(record.note, str) and len(record.note) <= 500,
               "note must be a string of at most 500 chars")
    return problems


@_validator("package")
def _validate_package(record: Package) -> list:
    problems = []
    _check_id(problems, record)
    tickets = record.tickets
    if not isinstance(tickets, list):
        problems.append("tickets must be an ordered list of ticket ids")
        tickets = []
    _check(problems, all(isinstance(t, str) and t and t.strip() == t for t in tickets),
           "tickets must be non-empty ticket id strings")
    _check(problems, len(tickets) >= 2, "a package orders at least two tickets")
    _check(problems, len(set(map(str, tickets))) == len(tickets), "tickets must not repeat")
    _check(problems, isinstance(record.created_by, str) and bool(WORKER_ID_PATTERN.match(record.created_by)),
           f"created_by {record.created_by!r} must match {WORKER_ID_PATTERN.pattern}")
    _check(problems, record.policy in PACKAGE_POLICIES,
           f"policy {record.policy!r} is not one of {', '.join(PACKAGE_POLICIES)}")
    _check(problems, record.state in PACKAGE_STATES,
           f"state {record.state!r} is not one of {', '.join(PACKAGE_STATES)}")
    if record.state == "bound":
        _check(problems, isinstance(record.bound_worker, str) and bool(WORKER_ID_PATTERN.match(record.bound_worker)),
               "a bound package names its bound_worker")
        _check_utc(problems, record.bound_at, "bound_at")
    elif record.state == "open":
        _check(problems, record.bound_worker is None, "an open package has no bound_worker")
    if record.state in ("completed", "released"):
        _check_utc(problems, record.ended_at, "ended_at")
    else:
        _check(problems, record.ended_at is None, f"a {record.state} package must not have 'ended_at'")
    _check(problems, record.current is None or record.current in tickets,
           "current must be one of the package's tickets")
    if record.reservation_id is not None:
        _check(problems, id_for_kind(record.reservation_id, "reservation"),
               f"reservation_id {record.reservation_id!r} is not a reservation id")
    for label in ("handoffs", "notes"):
        entries = getattr(record, label)
        _check(problems, isinstance(entries, list) and all(isinstance(e, dict) for e in entries),
               f"{label} must be a list of mappings")
    _check_utc(problems, record.created, "created")
    _check_utc(problems, record.updated, "updated")
    if record.note is not None:
        _check(problems, isinstance(record.note, str) and len(record.note) <= 500,
               "note must be a string of at most 500 chars")
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
    - at most one active claim per (workspace, path);
    - at most one worker profile per worker id;
    - a ticket is a member of at most one active reservation (no nesting);
    - a ticket is the target of at most one live (published/accepted) offer;
    - a ticket is a member of at most one live (open/bound) package.

    Duplicate ids and duplicate event cursors are also reported, because both
    make "which record is current" unanswerable.
    """
    problems = []
    seen_ids = {}
    cursors = {}
    active_attempts = {}
    active_claims = {}
    profiles = {}
    reserved = {}
    offered = {}
    packaged = {}

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

        if isinstance(record, WorkerProfile):
            if record.worker_id in profiles:
                problems.append(
                    f"worker {record.worker_id!r} has two profiles ({profiles[record.worker_id]} "
                    f"and {record.id}); one profile per worker id"
                )
            else:
                profiles[record.worker_id] = record.id

        if isinstance(record, Reservation) and record.is_active:
            for member in record.members:
                if member in reserved:
                    problems.append(
                        f"ticket {member} is a member of two active reservations "
                        f"({reserved[member]} and {record.id}); reservations do not overlap"
                    )
                else:
                    reserved[member] = record.id

        if isinstance(record, Offer) and record.is_live:
            for ticket in record.tickets:
                if ticket in offered:
                    problems.append(
                        f"ticket {ticket} is the target of two live offers ({offered[ticket]} "
                        f"and {record.id}); one live offer per ticket"
                    )
                else:
                    offered[ticket] = record.id

        if isinstance(record, Package) and record.is_live:
            for ticket in record.tickets:
                if ticket in packaged:
                    problems.append(
                        f"ticket {ticket} is a member of two live packages ({packaged[ticket]} "
                        f"and {record.id}); one package membership per ticket"
                    )
                else:
                    packaged[ticket] = record.id

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
    "COST_CLASSES",
    "WORKER_LOCALITIES",
    "WorkerProfile",
    "Reservation",
    "Offer",
    "Package",
    "PACKAGE_LIVE_STATES",
    "PACKAGE_POLICIES",
    "PACKAGE_STATES",
    "OFFER_LIVE_STATES",
    "OFFER_MODES",
    "OFFER_PREFERENCE_KEYS",
    "OFFER_REQUIREMENT_KEYS",
    "OFFER_STATES",
    "OFFER_TARGET_KINDS",
    "RESERVATION_SOURCES",
    "RESERVATION_STATES",
    "looks_like_secret",
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
