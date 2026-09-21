"""The versioned coordination records: what the proxy stores, in storage-neutral terms.

`coordination` is deliberately a separate module tree from `schema.py`. A ticket is
a *document*: it belongs in git, a human reads it in `ls`, and `TicketSink.update`
is the single write path for one ticket. Coordination state is local runtime state
-- attempts, file claims, read observations, receipts, artifacts and events -- that
must survive a crash of the process holding it and must be discardable once the
work is closed. Keeping the two apart is what lets coordination move to another
backend later without touching ticket storage, and it is the only reason an
operation spanning several records can be expressed at all.

Three properties are enforced here rather than left to a backend:

- **Every record is versioned.** `to_dict()` writes a `record` discriminator and
  the `schema_revision` the record was written under; `from_dict()` refuses a
  record whose revision is not the one this arbite knows, by name, instead of
  half-reading fields it may be misinterpreting. A later slice migrates old
  records (tic-008f); nothing here guesses.
- **Every record validates itself** in one `validate()` that both backends call on
  write and on read, so "what a claim is" cannot differ between the file sink and
  SQLite. The invariants encoded are the ones the coordination handoff states:
  one active attempt per ticket (checked in `store.py`, which can see the whole
  set), a finished attempt always has an `ended` time, a claim path is relative
  and canonical, a read observation never authorises a write it did not earn.
- **Times are UTC, ids are opaque.** New records carry RFC 3339 UTC to the second
  (`2026-09-21T13:12:04Z`). Ticket timestamps keep their own, older format and are
  untouched by this module: legacy tickets and their timestamps stay readable.

Nothing in this module touches a filesystem or a database.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import ClassVar, Optional

from ..errors import RecordError

#: The revision of the coordination record *shape*. Stored inside every record so a
#: reader can tell a current record from one written by a newer arbite, and bumped
#: whenever a field changes meaning. Record-level revision counters for optimistic
#: concurrency are a different thing and belong to the transactional store
#: (tic-1a75): this is "which schema is this document written in".
#:
#: Revision 2 adds `ReadObservation.spent_by`: one token authorises one mutation, so
#: the token records which operation used it (tic-60c7). A revision-1 record is
#: refused by name rather than half-read, which is the deliberate cost of the bump:
#: a store written before it needs the migration pass tic-008f/C12 owns.
COORDINATION_SCHEMA_REVISION = 2

#: The record types, and the id prefix each one mints. Prefixes follow the id style
#: the examples document fixes: `ws-XXXX` workspaces, `att-XXXX` attempts and
#: `op-XXXX` operations -- a read observation and a mutation receipt are both
#: operations a caller holds a token for, so they share the `op-` space.
ID_PREFIXES = {
    "workspace": "ws",
    "attempt": "att",
    "claim": "clm",
    "observation": "op",
    "receipt": "op",
    "artifact": "art",
    "event": "evt",
}

RECORD_TYPES = tuple(ID_PREFIXES)

#: Every id arbite mints, including the ticket's own: `common_problems` in
#: `schema.ID_PATTERN` covers tickets, and a coordination record that names a
#: ticket has to accept one, so the shared check here knows both vocabularies. The
#: narrower `COORDINATION_ID_PATTERN` is what a claim or attempt id must match.
ID_PATTERN = re.compile(r"^(tic|ws|att|clm|op|art|evt)-[0-9a-f]{4}$")
COORDINATION_ID_PATTERN = re.compile(r"^(ws|att|clm|op|art|evt)-[0-9a-f]{4}$")

#: A stored timestamp: UTC, RFC 3339, to the second. Canonical form only -- parsing
#: is lenient (see `parse_utc`) but what this module *writes* is one spelling.
UTC_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The explicit marker for "this path did not exist". A digest is never a valid
#: stand-in for absence, and an empty string is never a valid stand-in for either.
ABSENT = "absent"

#: Attempt lifecycle. `active` is the only state that may hold claims or mutate
#: bytes; the other three are terminal and keep their timestamps for later audit.
ATTEMPT_ACTIVE = "active"
ATTEMPT_STATES = ("active", "finished", "released", "interrupted")

#: Claim lifecycle. A released claim is kept as history -- it is how "this path was
#: held by att-XXXX until 13:20" stays answerable after ownership is gone -- but it
#: is not in the active index, and re-acquiring the path mints a new record rather
#: than reactivating the old token.
CLAIM_ACTIVE = "active"
CLAIM_RELEASED = "released"
CLAIM_STATES = (CLAIM_ACTIVE, CLAIM_RELEASED)

#: What a receipt describes. `pending` is the recoverable-write protocol's marker:
#: intent and artifacts are persisted, the filesystem operation has not been
#: finalised yet. It is the one result `doctor` treats as an unfinished operation.
RECEIPT_PENDING = "pending"
RECEIPT_SUCCEEDED = "succeeded"
RECEIPT_FAILED = "failed"
RECEIPT_RESULTS = (RECEIPT_SUCCEEDED, RECEIPT_FAILED, RECEIPT_PENDING)

RECEIPT_KINDS = ("create", "write", "edit", "remove", "rename", "passthrough")

#: Event categories. Reads are their own category on purpose: a research-heavy
#: agent emits dozens of reads per write, so a job query must be able to exclude
#: them (`--include-reads` opts in, in the examples document).
#:
#: A category names the *stream* an event belongs to, not the verb in its kind.
#: Everything except `read` is the job stream: lifecycle, attempts, claims, file
#: activity (including a served read that is part of an operation, which is why
#: `read.file` appears in the default view of `arbite events --tail`) and
#: passthrough. `read` is the observation stream -- the reads that are about
#: bytes having been served rather than about the work changing -- which is what
#: the default view excludes.
READ_CATEGORY = "read"
EVENT_CATEGORIES = ("lifecycle", "attempt", "claim", "file", READ_CATEGORY, "passthrough", "recovery")

#: The two payload keys the events view renders: what the event is *about* (a
#: path, a ticket id) and its one-line outcome (`+18 -0`, `gen 3`, `read-only`).
#: They live in `Event.payload` rather than in new record fields, because an
#: event's detail is per kind and the payload is the versioned place for it.
EVENT_SUBJECT_KEY = "subject"
EVENT_RESULT_KEY = "result"

#: Digest prefixes in *text* are shortened for reading; JSON and stored records
#: always carry the full digest. `doctor` is the exception and prints it whole.
DIGEST_DISPLAY_LENGTH = 12


def utc_now() -> str:
    """The current time as a stored timestamp: UTC, RFC 3339, to the second."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_utc_timestamp(value) -> bool:
    """Whether `value` is in the canonical stored form this module writes."""
    return isinstance(value, str) and bool(UTC_TIMESTAMP_PATTERN.match(value))


def parse_utc(value: str) -> datetime:
    """Read a stored timestamp back as an aware UTC datetime.

    Deliberately more tolerant than `is_utc_timestamp`: a `+00:00` offset and a
    naive value (read as UTC) are accepted, because a reader that refuses to show
    a record is worse than one that reads it, and the field's meaning is
    unambiguous once the value is known to be a full timestamp. Writing stays
    canonical -- `validate()` requires the stored form."""
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError(f"'{value}' is not an RFC 3339 timestamp")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def new_id(record_type: str, existing=()) -> str:
    """A fresh opaque id for `record_type`, absent from `existing`.

    Opaque and random rather than sequential: an id is a handle a caller passes
    back, never a fact about ordering, and nothing may depend on decoding one. The
    id is *not* a uniqueness guarantee: `existing` is only what the caller could
    see, and both backends replace a record written under an id that is already
    there. A caller for whom a collision would be corruption asks
    `CoordinationStore.find_record` inside a transaction and mints another id,
    rather than relying on the store to refuse the second write."""
    prefix = ID_PREFIXES.get(record_type)
    if prefix is None:
        raise RecordError(
            f"unknown record type '{record_type}' (known: {', '.join(RECORD_TYPES)})"
        )
    taken = set(existing)
    while True:
        candidate = f"{prefix}-{uuid.uuid4().hex[:4]}"
        if candidate not in taken:
            return candidate


def derived_workspace_id(root, store_kind: str, store_root) -> str:
    """The workspace id for a located project root and a resolved store.

    **Derived, never bound.** There is no `arbite bind`: the workspace is what the
    located `.arbite/` directory plus the resolved sink say it is, so the id is a
    digest of exactly those facts and two agents in one checkout compute the same
    one without coordinating. Relocating a root -- or repointing the project at a
    different store -- therefore reads as a *new* workspace rather than a mutation
    of the old one, which is the honest answer while there is one store per
    project. Two roots reaching one store is out of scope for this epic and belongs
    to centralized storage.

    `normcase` is applied so a case-insensitive filesystem does not mint a second
    workspace for the same directory spelled differently."""
    material = "\n".join(
        [os.path.normcase(str(root)), str(store_kind), os.path.normcase(str(store_root))]
    )
    return f"ws-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:4]}"


def claim_id_for(workspace_id: str, path: str) -> str:
    """The id of the claim record for one path in one workspace.

    **Derived rather than random**, and that is what makes the claim set its own
    current-state index: a path has exactly one claim record -- active or released --
    so re-acquiring a path is replacing that record, and its stored *revision* is the
    compare-and-swap two racing acquisitions contend on. A second claim of a live path
    therefore cannot be written at all, whatever route it arrives by, and no log has to
    be replayed to find out who holds what.

    History is not lost with it: every acquisition and release appends an event, so the
    stream still answers "this path was held until 13:20". The id stays opaque like
    every other one -- nothing decodes it -- and the derivation is never an
    authorisation, only a way to find the one record that speaks for a path. A caller
    that finds this id already used by a *different* path has a digest collision and
    must say so rather than overwrite it (see the file claims module)."""
    material = f"{workspace_id}\n{path}"
    return f"clm-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:4]}"


def digest_bytes(data: bytes) -> str:
    """The stored content digest of `data`: `sha256:<full hex>`."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def short_digest(digest: str) -> str:
    """A digest as text output prints it: `sha256:` plus 12 hex characters.

    Truncation is a *display* rule only -- JSON and the records themselves always
    hold the full digest, so a comparison never depends on this."""
    text = str(digest)
    if not text.startswith("sha256:"):
        return text
    return f"sha256:{text[len('sha256:'):][:DIGEST_DISPLAY_LENGTH]}"


# ---------------------------------------------------------------------------
# Field assertions, shared by every record's `validate()`
# ---------------------------------------------------------------------------


def _require_id(value, prefix: str, label: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise RecordError(f"{label} must be an opaque id like '{prefix}-a1b2', got {value!r}")
    if not value.startswith(f"{prefix}-"):
        raise RecordError(f"{label} must be a '{prefix}-' id, got {value!r}")


def _require_text(value, label: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise RecordError(f"{label} must be a non-empty string, got {value!r}")


def _require_choice(value, allowed, label: str) -> None:
    if value not in allowed:
        raise RecordError(f"{label} must be one of {', '.join(allowed)}, got {value!r}")


def _require_timestamp(value, label: str, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not is_utc_timestamp(value):
        raise RecordError(
            f"{label} must be a UTC timestamp (YYYY-MM-DDTHH:MM:SSZ), got {value!r}"
        )


def _require_int(value, label: str, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RecordError(f"{label} must be an integer >= {minimum}, got {value!r}")


def _require_digest(value, label: str, allow_absent: bool = False) -> None:
    if allow_absent and value == ABSENT:
        return
    if not isinstance(value, str) or not DIGEST_PATTERN.match(value):
        alternative = " or 'absent'" if allow_absent else ""
        raise RecordError(f"{label} must be a full sha256 digest{alternative}, got {value!r}")


def is_canonical_relative_path(value) -> bool:
    """Whether `value` is a project-relative path a claim may name.

    The one definition of "canonical" both backends and every later slice share:
    a plain relative path with no empty, `.` or `..` segments, no leading `/`, no
    drive letter and no backslash. Absolute paths and escapes are refused, and
    symlink and hard-link policy is a *use-time* check (tic-9b57/tic-60c7) because
    whether a component is a link can change between validation and use."""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith(("/", "\\")) or "\\" in value:
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


def _require_relative_path(value, label: str) -> None:
    if not is_canonical_relative_path(value):
        raise RecordError(
            f"{label} must be a canonical project-relative path (no leading '/', no "
            f"'.'/'..' segments, no backslashes), got {value!r}"
        )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class Record:
    """What every coordination record shares: a type, a schema revision, and one
    serialisation form.

    `to_dict()` is the stored and JSON form (the `record` discriminator plus the
    fields plus `schema_revision`), so the file backend's document and SQLite's
    document are the same bytes and a migration between them is field-for-field
    exact. `from_dict()` is the only reader: it checks the discriminator, refuses
    a newer revision by name rather than half-reading it, and validates."""

    RECORD_TYPE: ClassVar[str] = "?"
    SCHEMA_REVISION: ClassVar[int] = COORDINATION_SCHEMA_REVISION

    def __post_init__(self) -> None:
        """A record that exists is valid.

        Validating on construction (as well as on read, through `from_dict`) means
        an invalid claim cannot be built, passed around and only rejected when it
        reaches storage -- by which point the caller has already made decisions
        from it."""
        self.validate()

    def to_dict(self) -> dict:
        data = {"record": self.RECORD_TYPE}
        for spec in fields(self):
            data[spec.name] = getattr(self, spec.name)
        return data

    def validate(self) -> None:
        """Raise `RecordError` unless this record obeys its own invariants."""

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        if not isinstance(data, dict):
            raise RecordError(f"a {cls.RECORD_TYPE} record must be a JSON object")
        record_type = data.get("record")
        if record_type is None:
            raise RecordError(
                f"record is missing its 'record' discriminator (expected "
                f"'{cls.RECORD_TYPE}'); refusing to guess what it is"
            )
        if record_type != cls.RECORD_TYPE:
            raise RecordError(
                f"record says it is a '{record_type}' record, not a '{cls.RECORD_TYPE}'"
            )
        revision = data.get("schema_revision")
        if revision != cls.SCHEMA_REVISION:
            raise RecordError(
                f"{cls.RECORD_TYPE} record is schema revision {revision!r}, but this arbite "
                f"writes and understands revision {cls.SCHEMA_REVISION}; refusing to read "
                "fields it may be misinterpreting (old records are migrated by tic-008f)"
            )
        known = {spec.name for spec in fields(cls)}
        unknown = sorted(set(data) - known - {"record", "schema_revision"})
        if unknown:
            raise RecordError(
                f"{cls.RECORD_TYPE} record has unknown field(s): {', '.join(unknown)}"
            )
        missing = [
            spec.name
            for spec in fields(cls)
            if spec.name not in data
            and spec.default is MISSING
            and spec.default_factory is MISSING
        ]
        if missing:
            raise RecordError(
                f"{cls.RECORD_TYPE} record is missing required field(s): {', '.join(missing)}"
            )
        try:
            record = cls(**{key: value for key, value in data.items() if key in known})
        except TypeError as e:
            raise RecordError(f"{cls.RECORD_TYPE} record has a wrong field type: {e}")
        record.validate()
        return record


@dataclass
class Workspace(Record):
    """One located project root together with the store resolved for it.

    The id is *derived* (see `derived_workspace_id`), so recording a workspace is
    not a binding decision and there is no command that changes one: a relocated
    root or a repointed store is a different workspace, and the record that
    disagrees is replaced rather than edited."""

    RECORD_TYPE: ClassVar[str] = "workspace"

    id: str
    root: str
    store_kind: str
    store_root: str
    coordination_kind: str
    coordination_root: str
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def store(self) -> dict:
        return {"kind": self.store_kind, "root": self.store_root}

    @property
    def coordination(self) -> dict:
        return {"kind": self.coordination_kind, "root": self.coordination_root}

    def validate(self) -> None:
        _require_id(self.id, "ws", "workspace id")
        _require_text(self.root, "workspace root")
        if not os.path.isabs(self.root):
            raise RecordError(f"workspace root must be an absolute path, got {self.root!r}")
        _require_text(self.store_kind, "store kind")
        _require_text(self.store_root, "store root")
        _require_text(self.coordination_kind, "coordination kind")
        _require_text(self.coordination_root, "coordination root")
        _require_int(self.schema_revision, "schema_revision", minimum=1)


@dataclass
class WorkAttempt(Record):
    """One worker's run at one ticket: the thing that owns file claims.

    An attempt is what makes "who is allowed to change these bytes" answerable
    without trusting a timestamp. Its `generation` is per attempt (so a takeover
    can revoke a whole generation), and its terminal states keep their times: the
    plan deliberately stores creation, activity and end times now so a later stale
    policy has data to decide with, and implements no expiry, heartbeat or
    automatic reassignment in this epic."""

    RECORD_TYPE: ClassVar[str] = "attempt"

    id: str
    ticket_id: str
    worker_id: str
    workspace_id: str
    generation: int
    state: str
    started: str
    last_activity: str
    ended: Optional[str] = None
    outcome: Optional[str] = None
    handoff: Optional[str] = None
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def is_active(self) -> bool:
        return self.state == ATTEMPT_ACTIVE

    def validate(self) -> None:
        _require_id(self.id, "att", "attempt id")
        _require_id(self.ticket_id, "tic", "attempt ticket_id")
        _require_id(self.workspace_id, "ws", "attempt workspace_id")
        _require_text(self.worker_id, "attempt worker_id")
        _require_int(self.generation, "attempt generation", minimum=1)
        _require_choice(self.state, ATTEMPT_STATES, "attempt state")
        _require_timestamp(self.started, "attempt started")
        _require_timestamp(self.last_activity, "attempt last_activity")
        _require_timestamp(self.ended, "attempt ended", optional=True)
        if self.state == ATTEMPT_ACTIVE and self.ended is not None:
            raise RecordError(
                f"attempt {self.id} is active but records an 'ended' time; a finished "
                "attempt is never active"
            )
        if self.state != ATTEMPT_ACTIVE and self.ended is None:
            raise RecordError(
                f"attempt {self.id} is '{self.state}' but has no 'ended' time -- nobody "
                "could tell when it stopped being claimable"
            )


@dataclass
class FileClaim(Record):
    """Exclusive writer ownership of one whole file, by one attempt.

    v1 owns a whole file: no ranges, no shared regions. The record keeps the
    observed version so a later write can be checked against the bytes it was
    authorised against, and a released claim stays in the store as history rather
    than being deleted -- `state` is what removes it from the active index."""

    RECORD_TYPE: ClassVar[str] = "claim"

    id: str
    workspace_id: str
    path: str
    ticket_id: str
    attempt_id: str
    generation: int
    acquired: str
    state: str = CLAIM_ACTIVE
    observed_version: str = ABSENT
    released: Optional[str] = None
    release_reason: Optional[str] = None
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def is_active(self) -> bool:
        return self.state == CLAIM_ACTIVE

    def held_by(self, attempt_id: Optional[str], generation: Optional[int] = None) -> bool:
        """Whether this active claim belongs to `attempt_id` at `generation`.

        The one place "is this still my claim" is decided, so a busy report and a
        write guard cannot disagree about who owns a path."""
        if not self.is_active or self.attempt_id != attempt_id:
            return False
        return generation is None or self.generation == generation

    def validate(self) -> None:
        _require_id(self.id, "clm", "claim id")
        _require_id(self.workspace_id, "ws", "claim workspace_id")
        _require_id(self.ticket_id, "tic", "claim ticket_id")
        _require_id(self.attempt_id, "att", "claim attempt_id")
        _require_relative_path(self.path, "claim path")
        _require_int(self.generation, "claim generation", minimum=1)
        _require_timestamp(self.acquired, "claim acquired")
        _require_choice(self.state, CLAIM_STATES, "claim state")
        _require_digest(self.observed_version, "claim observed_version", allow_absent=True)
        _require_timestamp(self.released, "claim released", optional=True)
        if self.state == CLAIM_ACTIVE and self.released is not None:
            raise RecordError(
                f"claim {self.id} is active but records a release time; released ownership "
                "is never reactivated by writing this record"
            )
        if self.state != CLAIM_ACTIVE and self.released is None:
            raise RecordError(
                f"claim {self.id} is '{self.state}' but has no release time"
            )


@dataclass
class ReadObservation(Record):
    """Bytes that were served to a reader -- and nothing more than that.

    A read receipt says bytes were served, not that the model understood them, and
    it is a *separate category* from a mutation receipt so ordinary job queries do
    not have to wade through reads. The digest always covers the whole file, even
    when a line range was returned, which is what stops a range from narrowing the
    version a later write is checked against.

    Read observations and mutation receipts share the `op-` id space: the id is the
    token a write presents, so a caller holds one handle and the store says what
    that handle is allowed to do (`authorizes_write`).

    `spent_by` is the other half of that handle: **one token authorises one
    mutation**, so the mutation records the operation that used it, and a second
    attempt to spend it is refused with the operation named. Nothing else is
    written to a token -- an observation is otherwise append-only evidence of bytes
    having been served."""

    RECORD_TYPE: ClassVar[str] = "observation"

    id: str
    path: str
    digest: str
    observed_at: str
    attempt_id: Optional[str] = None
    actor: Optional[str] = None
    claim_generation: int = 0
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    spent_by: Optional[str] = None
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def is_ranged(self) -> bool:
        return self.line_start is not None or self.line_end is not None

    @property
    def is_spent(self) -> bool:
        """Whether a mutation has already used this token."""
        return self.spent_by is not None

    def spent(self, operation_id: str) -> "ReadObservation":
        """This observation, marked as spent by `operation_id`.

        Returns a new record rather than mutating in place: the value a caller holds
        is the token as it was validated, and the store's copy is the one that says
        it is spent."""
        return replace(self, spent_by=operation_id)

    def authorizes_write(self, claim: Optional[FileClaim]) -> bool:
        """Whether this observation may authorise a change to *existing* bytes.

        Deliberately strict, and the reason pre-claim reads are useless to a
        writer: the observation must have been taken while *this* attempt held the
        path at the generation it observed, and it must be an observation of that
        same path. `claim_generation` 0 means "observed while unclaimed", which
        authorises nothing -- a writer claims first and reads again afterwards.

        Whether the bytes are *still* the observed ones is a separate check against
        the filesystem digest at write time (tic-60c7); this method only settles the
        ownership question. Creation of a path that did not exist is the other half,
        `authorizes_creation`."""
        if self.digest == ABSENT:
            # An absent-path probe never authorises a change to existing bytes: it
            # is evidence about a path that was not there, and a later create is
            # what it can justify.
            return False
        return self._held_by(claim)

    def authorizes_creation(self, claim: Optional[FileClaim]) -> bool:
        """Whether this observation may authorise *creating* `path`.

        True only for a probe that found the path absent while this attempt already
        held the claim: a creation written over bytes that arrived in the meantime
        would be the very overwrite this proxy exists to prevent, so C07 still
        checks the path is still absent at write time."""
        if self.digest != ABSENT:
            return False
        return self._held_by(claim)

    def _held_by(self, claim: Optional[FileClaim]) -> bool:
        """The ownership half of the two checks above.

        A spent token holds nothing, whatever the claim says: it authorised one
        mutation and is not reusable for a second (the mutation reports *which*
        operation spent it, so the caller is told why rather than merely refused)."""
        if claim is None or not claim.is_active or self.is_spent:
            return False
        return (
            claim.path == self.path
            and self.attempt_id is not None
            and claim.attempt_id == self.attempt_id
            and self.claim_generation > 0
            and claim.generation == self.claim_generation
        )

    def validate(self) -> None:
        _require_id(self.id, "op", "observation id")
        _require_relative_path(self.path, "observation path")
        # 'absent' is a legitimate observation: it is the probe that says "this path
        # was not there", which is the only thing that can authorise a creation.
        _require_digest(self.digest, "observation digest", allow_absent=True)
        _require_timestamp(self.observed_at, "observation observed_at")
        if self.attempt_id is not None:
            _require_id(self.attempt_id, "att", "observation attempt_id")
        if self.actor is not None:
            _require_text(self.actor, "observation actor")
        _require_int(self.claim_generation, "observation claim_generation")
        for label, value in (("line_start", self.line_start), ("line_end", self.line_end)):
            if value is not None:
                _require_int(value, f"observation {label}", minimum=1)
        if self.spent_by is not None:
            _require_id(self.spent_by, "op", "observation spent_by")
            if self.spent_by == self.id:
                raise RecordError(
                    f"observation {self.id} cannot be spent by itself: a token is spent "
                    "by the operation that used it"
                )
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise RecordError(
                f"observation range {self.line_start}:{self.line_end} ends before it starts"
            )


@dataclass
class OperationReceipt(Record):
    """One operation's evidence: what it was, what it touched, and both versions.

    A successful change keeps the actual bytes or an exact reversible
    representation, which is why `before`/`after` are digests (and `artifacts`),
    not prose: the receipt is what makes a change reproducible, and agent prose is
    a separate thing recorded elsewhere. `result` may be `pending` -- the
    recoverable write protocol persists intent and artifacts first, applies the
    filesystem operation, then finalises the receipt -- and a pending receipt is
    exactly what `doctor` must surface as an unfinished operation (tic-b03b owns
    the journal, its finalisation and the reconciliation)."""

    RECORD_TYPE: ClassVar[str] = "receipt"

    id: str
    kind: str
    paths: list
    result: str
    recorded_at: str
    ticket_id: Optional[str] = None
    attempt_id: Optional[str] = None
    actor: Optional[str] = None
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    claim_generation: int = 0
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def is_pending(self) -> bool:
        return self.result == RECEIPT_PENDING

    def validate(self) -> None:
        _require_id(self.id, "op", "receipt id")
        _require_choice(self.kind, RECEIPT_KINDS, "receipt kind")
        _require_choice(self.result, RECEIPT_RESULTS, "receipt result")
        _require_timestamp(self.recorded_at, "receipt recorded_at")
        if self.ticket_id is not None:
            _require_id(self.ticket_id, "tic", "receipt ticket_id")
        if self.attempt_id is not None:
            _require_id(self.attempt_id, "att", "receipt attempt_id")
        if self.actor is not None:
            _require_text(self.actor, "receipt actor")
        if not isinstance(self.paths, list):
            raise RecordError(f"receipt paths must be a list, got {self.paths!r}")
        for path in self.paths:
            _require_relative_path(path, "receipt path")
        # Passthrough is the one kind that may record no path: arbite cannot know
        # which files an arbitrary tool read, so a read-only command is an execution
        # event with a receipt that names nothing.
        if self.kind != "passthrough" and not self.paths:
            raise RecordError(f"receipt {self.id} of kind '{self.kind}' names no path")
        for name, mapping in (("before", self.before), ("after", self.after)):
            if not isinstance(mapping, dict):
                raise RecordError(f"receipt {name} must be a mapping of path to digest")
            for path, digest in mapping.items():
                _require_relative_path(path, f"receipt {name} path")
                if path not in self.paths:
                    raise RecordError(
                        f"receipt {name} names '{path}', which the receipt's paths do not"
                    )
                _require_digest(digest, f"receipt {name}['{path}']", allow_absent=True)
        if set(self.before) != set(self.after):
            raise RecordError(
                "receipt before/after must describe the same paths (a create records "
                "'absent' before, a remove records it after)"
            )
        if not isinstance(self.artifacts, list):
            raise RecordError(f"receipt artifacts must be a list, got {self.artifacts!r}")
        for artifact in self.artifacts:
            _require_id(artifact, "art", "receipt artifact id")
        _require_int(self.claim_generation, "receipt claim_generation")


@dataclass
class Artifact(Record):
    """Stored bytes, addressed by digest.

    Content is stored once by digest where practical, so an edit-then-revert keeps
    both versions as evidence without storing a file twice. An artifact record is
    the index entry for the bytes; where the bytes live is the backend's business
    (`artifact_path` is that convention for the file backend). Size limits and any
    retention policy are deliberately not decided here (tic-008f owns export,
    integrity verification and the question of pruning)."""

    RECORD_TYPE: ClassVar[str] = "artifact"

    id: str
    digest: str
    size: int
    created: str
    media_type: str = "application/octet-stream"
    operation_id: Optional[str] = None
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    def validate(self) -> None:
        _require_id(self.id, "art", "artifact id")
        _require_digest(self.digest, "artifact digest")
        _require_int(self.size, "artifact size")
        _require_timestamp(self.created, "artifact created")
        _require_text(self.media_type, "artifact media_type")
        if self.operation_id is not None:
            _require_id(self.operation_id, "op", "artifact operation_id")


@dataclass
class Event(Record):
    """One append-only line of what happened, in the order it happened.

    The cursor is monotonic per store, which is what makes `--after <cursor>`
    resumable and what orders a poll without a second command. The category
    separates reads from everything else, and the payload is versioned with the
    record so a consumer can tell what shape it holds. The cursor is allocated and
    the event committed by `CoordinationStore.append_event` (through
    `transaction()`), which is what makes it stable: a caller that builds one by
    hand chooses its own cursor, which is how a migration places records."""

    RECORD_TYPE: ClassVar[str] = "event"

    id: str
    cursor: int
    kind: str
    recorded_at: str
    category: str
    ticket_id: Optional[str] = None
    attempt_id: Optional[str] = None
    actor: Optional[str] = None
    operation_id: Optional[str] = None
    payload: dict = field(default_factory=dict)
    schema_revision: int = COORDINATION_SCHEMA_REVISION

    @property
    def subject(self) -> str:
        """What this event is about, as `arbite events` prints it.

        A convention over `payload` rather than a field, so a kind that has
        nothing to point at (a lifecycle event about a ticket, say) simply leaves
        it out and the column stays empty. An event whose payload carries the key
        with a non-string value renders that value, so a numeric subject cannot
        break the one-line-per-event rule."""
        value = self.payload.get(EVENT_SUBJECT_KEY)
        return "" if value is None else str(value)

    @property
    def result(self) -> str:
        """The event's one-line outcome, as `arbite events` prints it (see
        `subject`)."""
        value = self.payload.get(EVENT_RESULT_KEY)
        return "" if value is None else str(value)

    def validate(self) -> None:
        _require_id(self.id, "evt", "event id")
        _require_int(self.cursor, "event cursor", minimum=1)
        _require_text(self.kind, "event kind")
        _require_timestamp(self.recorded_at, "event recorded_at")
        _require_choice(self.category, EVENT_CATEGORIES, "event category")
        if self.ticket_id is not None:
            _require_id(self.ticket_id, "tic", "event ticket_id")
        if self.attempt_id is not None:
            _require_id(self.attempt_id, "att", "event attempt_id")
        if self.actor is not None:
            _require_text(self.actor, "event actor")
        if self.operation_id is not None:
            _require_id(self.operation_id, "op", "event operation_id")
        if not isinstance(self.payload, dict):
            raise RecordError(f"event payload must be a mapping, got {self.payload!r}")


RECORD_CLASSES = {
    "workspace": Workspace,
    "attempt": WorkAttempt,
    "claim": FileClaim,
    "observation": ReadObservation,
    "receipt": OperationReceipt,
    "artifact": Artifact,
    "event": Event,
}


def parse_record(data: dict) -> Record:
    """The record `data` describes, whichever type it is.

    Dispatches on the stored discriminator, so a backend reads a container without
    knowing what is in it and an unknown type fails by name instead of by
    `KeyError`."""
    if not isinstance(data, dict):
        raise RecordError("a coordination record must be a JSON object")
    record_type = data.get("record")
    if record_type is None:
        raise RecordError(
            "a coordination record must carry its 'record' discriminator; refusing to "
            "guess what it is"
        )
    if record_type not in RECORD_CLASSES:
        raise RecordError(
            f"unknown coordination record type {record_type!r} "
            f"(known: {', '.join(RECORD_TYPES)})"
        )
    return RECORD_CLASSES[record_type].from_dict(data)


def record_type_of(record: Record) -> str:
    """The stored discriminator for a record instance."""
    record_type = getattr(record, "RECORD_TYPE", None)
    if record_type not in RECORD_CLASSES:
        raise RecordError(f"{type(record).__name__} is not a coordination record")
    return record_type
