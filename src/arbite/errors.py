"""The exception hierarchy every arbite command and sink reports through.

One base class means the CLI has a single `except` that turns a failure into
`error: <message>` on stderr with exit code 1, so nothing a sink raises can
escape as a traceback. The subtypes exist so callers can distinguish "the user
asked for something impossible" (TicketError, AmbiguousTicketId, Conflict) from
"the storage layer broke" (SinkError) -- a distinction the CLI uses for exit
codes and a test suite uses to assert the *right* failure, not merely a failure.
"""

from __future__ import annotations

from typing import Optional


class ArbiteError(Exception):
    """Base for every expected arbite failure. Anything raised through this class
    is reported to the user as a one-line error, never as a traceback."""


class TicketError(ArbiteError):
    """A ticket is invalid, or an operation on one is refused: malformed
    frontmatter or YAML, a field value outside its controlled vocabulary, a
    state change that does not apply.

    The name predates the sink abstraction and is kept deliberately: `arbite
    doctor`'s report, the CLI's messages and the docs all refer to it."""


class TicketNotFound(ArbiteError):
    """No ticket matched the given id or wildcard term."""


class AmbiguousTicketId(ArbiteError):
    """An id term matched more than one ticket on a command that mutates one.

    Read-only commands keep the convenience of the first alphabetical match;
    mutations refuse and list the candidates, because guessing there writes to
    the wrong ticket."""


class Conflict(ArbiteError):
    """A compare-and-swap update lost its race.

    Raised when a ticket is not in the state the caller expected -- someone else
    claimed it first, or it moved on between the read and the write. This is the
    error a lost claim produces, so the caller can retry against a fresh read
    instead of silently overwriting the winner's work."""


class SinkError(ArbiteError):
    """The storage layer failed: an IO error, a database error, a corrupt store."""


class SinkNotInitialised(SinkError):
    """The selected sink has no store yet. The CLI reports this as 'run `arbite
    init` first' rather than as a crash."""


# ---------------------------------------------------------------------------
# Shared-directory coordination
# ---------------------------------------------------------------------------
#
# The shared-directory epic introduces records and operations that do not exist
# in the pre-coordination ticket store: workspaces, work attempts, file claims,
# read observations, operation receipts, artifacts and events. Their failures are
# separated from the ticket failures above so a caller (in particular a later
# file-proxy command) can branch on *why* an operation was refused without
# parsing prose, and so the JSON error vocabulary below is a checked contract
# rather than a convention.
#
# ATTRIBUTION, NOT AUTHENTICATION: the ids, worker names and actor strings carried
# by these errors and by the coordination records are self-declared. Nothing in
# this module verifies that the process raising an error is the process an id
# claims to be. The trust model is cooperating local processes sharing one
# authoritative store; a hostile local process can lie. See
# `arbite.coordination.ATTRIBUTION_NOTICE` for the same statement where the
# records themselves are defined.


class ErrorCode:
    """The documented JSON error vocabulary.

    These strings are the stable part of the machine-readable failure contract:
    a caller switches on `code`, never on the human-readable message. They are
    shared by `CoordinationError.to_result()` and by the application layer's
    `coordination.error_result()`, so an error raised as an exception and the
    same error rendered into a CLI `--json` payload cannot drift apart.
    """

    #: Generic coordination failure with no more specific code.
    COORDINATION_ERROR = "coordination_error"
    #: A record is missing a required field or holds an out-of-vocabulary value.
    INVALID_RECORD = "invalid_record"
    #: A requested record does not exist.
    NOT_FOUND = "not_found"
    #: A guard rejected the caller because state moved on (retryable).
    CONFLICT = "conflict"
    #: The read a mutation relied on no longer matches the current bytes.
    STALE_READ = "stale_read"
    #: A path is held by another attempt (retryable with a different path).
    FILE_BUSY = "file_busy"
    #: A claim is not held by the attempt/ticket/generation the caller passed.
    CLAIM_CONFLICT = "claim_conflict"
    #: The work attempt is not active, so it may not claim or mutate.
    ATTEMPT_INACTIVE = "attempt_inactive"
    #: The workspace is already bound to a different authoritative store.
    STORE_BINDING_CONFLICT = "store_binding_conflict"
    #: The operation is deliberately not supported (documented restriction).
    UNSUPPORTED = "unsupported"
    #: An incomplete operation needs inspection/reconciliation before continuing.
    RECOVERY_REQUIRED = "recovery_required"
    #: Required mutation evidence cannot be stored (explicit size/space limit), so
    #: the operation is refused *before* any bytes change.
    ARTIFACT_CAPACITY = "artifact_capacity"
    #: Stored evidence failed its digest/size verification on read.
    ARTIFACT_CORRUPT = "artifact_corrupt"
    #: Observed bytes match neither the recorded before nor the recorded after
    #: version of an incomplete operation; evidence is preserved and an explicit
    #: resolution is required.
    DRIFT_DETECTED = "drift_detected"
    #: A targeted edit batch selected an ambiguous, absent or overlapping region,
    #: so the whole batch was refused and no bytes changed.
    EDIT_SELECTION = "edit_selection"
    #: The storage layer itself failed.
    SINK_ERROR = "sink_error"
    #: A worker does not satisfy a ticket's/offer's worker constraints (or its
    #: registered profile is disabled). `details["reasons"]` lists each unmet
    #: constraint with a stable `code`.
    WORKER_INELIGIBLE = "worker_ineligible"
    #: The ticket is a member of an active reservation that does not grant this
    #: worker acquisition (B02). `details` names the reservation and its owner.
    TICKET_RESERVED = "ticket_reserved"
    #: A reservation operation was refused; `details["reason"]` says why
    #: (`members_unavailable`, `active_attempts`, `not_owner`, `not_active`, ...).
    RESERVATION_CONFLICT = "reservation_conflict"


class CoordinationError(ArbiteError):
    """Base for every shared-directory coordination failure.

    Carries the machine-readable pieces the rest of the system needs to react
    without guessing: `error_code` (see `ErrorCode`), whether the caller may
    sensibly retry (`retryable`), whether the filesystem bytes may *already* have
    changed despite the failure (`bytes_may_have_changed`), and an open
    `details` mapping for the identifiers a caller needs to act (holders,
    expected/observed digests, the operation to inspect).

    Ids and actor names in `details` are attribution only -- see the module
    comment above and `arbite.coordination.ATTRIBUTION_NOTICE`.
    """

    error_code = ErrorCode.COORDINATION_ERROR
    retryable = False
    bytes_may_have_changed = False

    def __init__(self, message: str, *, details=None, retryable=None, bytes_may_have_changed=None):
        super().__init__(message)
        self.details = dict(details or {})
        # A subclass default stays the norm; a raise site that knows better (a
        # recovery report, say) may override per instance.
        if retryable is not None:
            self.retryable = bool(retryable)
        if bytes_may_have_changed is not None:
            self.bytes_may_have_changed = bool(bytes_may_have_changed)

    def to_result(self, contract_version: Optional[int] = None) -> dict:
        """The documented JSON error payload for this failure.

        Imported lazily so `errors` stays free of any dependency on the
        coordination models (the models import this module, not the reverse)."""
        from .coordination import error_result

        return error_result(
            self.error_code,
            str(self),
            details=self.details,
            retryable=self.retryable,
            bytes_may_have_changed=self.bytes_may_have_changed,
            contract_version=contract_version,
        )


class InvalidRecord(CoordinationError):
    """A coordination record is missing a required field, carries an
    out-of-vocabulary value, or violates one of its own invariants."""

    error_code = ErrorCode.INVALID_RECORD


class CoordinationNotFound(CoordinationError):
    """A requested coordination record (workspace, attempt, claim, ...) is absent."""

    error_code = ErrorCode.NOT_FOUND


class CoordinationConflict(CoordinationError):
    """A guarded coordination operation lost a race or was refused because the
    state it read has moved on since. Safe to retry against fresh state."""

    error_code = ErrorCode.CONFLICT
    retryable = True


class ClaimConflict(CoordinationError):
    """A file claim is not held by the attempt/ticket/generation the caller
    supplied, or the path is not claimable as requested."""

    error_code = ErrorCode.CLAIM_CONFLICT
    retryable = True


class AttemptInactive(CoordinationError):
    """The work attempt is finished, released or interrupted, so it may not
    acquire claims or authorize mutations. A resumed ticket needs a new attempt
    (or an explicit administrative takeover), never a reactivated old token."""

    error_code = ErrorCode.ATTEMPT_INACTIVE


class StaleRead(CoordinationError):
    """The read a writer relied on no longer matches the file's current bytes.

    Raised *before* any bytes are written, so `bytes_may_have_changed` stays
    False: a stale read changes nothing and the caller re-reads and retries.
    Two writes using one read token cannot both succeed."""

    error_code = ErrorCode.STALE_READ
    retryable = True


class FileBusy(CoordinationError):
    """A path is exclusively held by another attempt. The holder's ticket/attempt
    are in `details`; arbite never waits, steals the claim, or resolves a
    deadlock automatically."""

    error_code = ErrorCode.FILE_BUSY
    retryable = True


class StoreBindingConflict(CoordinationError):
    """A workspace is already bound to a different authoritative store.

    One workspace coordinates against exactly one selected sink; re-binding to
    another store (or another location) would split ownership, so it is refused
    rather than silently accepted. Root relocation is an explicit operation, not
    a side effect of opening a different store."""

    error_code = ErrorCode.STORE_BINDING_CONFLICT


class UnsupportedCoordination(CoordinationError):
    """The operation is deliberately not supported in this version.

    Compatibility restrictions (symlinks, recursive deletion, whole-ticket
    subtree ownership, ...) are documented in `arbite.coordination` and fail
    loudly here rather than falling back to an unrecorded shell write."""

    error_code = ErrorCode.UNSUPPORTED


class RecoveryRequired(CoordinationError):
    """An operation was interrupted between the store and the filesystem and must
    be inspected/reconciled before the caller continues.

    `bytes_may_have_changed` says whether the filesystem may already hold part
    of the change, so the caller knows whether to look before retrying; the
    `operation_id` to inspect is in `details`."""

    error_code = ErrorCode.RECOVERY_REQUIRED


class ArtifactCapacityError(CoordinationError):
    """Required mutation evidence cannot be stored, so the operation is refused
    *before* any bytes change.

    `bytes_may_have_changed` is always False: this is the "fail before modifying
    bytes" guarantee. The offending size, the configured limit and the path are in
    `details`, so a caller can act instead of guessing."""

    error_code = ErrorCode.ARTIFACT_CAPACITY


class ArtifactCorrupt(CoordinationError):
    """Stored evidence failed its digest/size verification on read.

    Never silently trusted: a corrupted artifact is reported so a caller does not
    treat unverifiable evidence as proof of what the bytes were."""

    error_code = ErrorCode.ARTIFACT_CORRUPT


class DriftDetected(CoordinationError):
    """Observed bytes match neither the recorded before nor the recorded after
    version of an incomplete operation.

    Evidence is preserved, ownership is not released, and nothing is overwritten
    or guessed at: the operation is reported for an explicit resolution. Because
    an external writer may have changed the file, `bytes_may_have_changed` is
    True (the *operation's* own bytes are unknown, not merely unchanged)."""

    error_code = ErrorCode.DRIFT_DETECTED
    bytes_may_have_changed = True


class EditSelectionError(CoordinationError):
    """A targeted edit batch named a selection that cannot be applied exactly.

    Raised for an *ambiguous* selection (more matches than the occurrence rule
    allows), an *absent* selection (no match), or *overlapping* selections (two
    edits whose matched regions intersect). In every case the WHOLE batch is
    refused before any byte changes -- `bytes_may_have_changed` is False -- so
    there is no partial application and nothing to reconcile.

    `details["reason"]` is one of `ambiguous`, `absent` or `overlapping`, and
    `details["edit_index"]` names the offending edit (0-based) when the failure
    is attributable to one edit.
    """

    error_code = ErrorCode.EDIT_SELECTION
    retryable = True


class WorkerIneligible(CoordinationError):
    """A worker may not acquire this work: its profile is disabled, a per-call
    declaration tried to exceed the configured profile, or a constraint is unmet
    or unknown. `details` carries `worker_id` and `reasons` (each with a stable
    `code`); nothing was written. Not retryable as-is: the profile or the
    requirement has to change first."""

    error_code = ErrorCode.WORKER_INELIGIBLE


class TicketReserved(CoordinationError):
    """A ticket is held by another coordinator's active reservation, so this
    worker may not acquire (or `set`) it. Nothing was written. Not retryable
    as-is: pick other work, or the owner must release/remove the ticket."""

    error_code = ErrorCode.TICKET_RESERVED


class ReservationConflict(CoordinationError):
    """A reservation create/membership/release operation was refused as a whole.

    `details["reason"]` is a stable code and, for member problems,
    `details["conflicts"]` lists each ticket with its own `reason`. Nothing was
    written: reservation changes are all-or-nothing."""

    error_code = ErrorCode.RESERVATION_CONFLICT
