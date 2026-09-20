"""The sink interface: the standardized CRUD surface every ticket store implements.

CRUD is the spine -- Create (`create`), Read (`get`/`get_many`/`exists`/`query`/
`notes`), Update (`update`/`add_note`), Delete (`remove`) -- plus the three
operations that plain CRUD cannot express honestly, each named rather than hidden:

- `update(ticket, expect=...)`, where `Expect` is a compare-and-swap token. A
  claim, a state transition and a plain edit are all "update this ticket", and
  the only thing that differs is what the caller is willing to overwrite. Making
  the expectation an argument (instead of a separate `claim()` method) means both
  sinks implement *one* write path, and a claim cannot bypass it.
- `bucket`/`move_to_bucket`, because "where is this ticket filed" is storage's
  business, not the schema's: it is a folder for the file sink and a column for
  SQLite, and it deliberately is not a ticket field.
- `check(fix)`, because the invariants worth checking are partly storage-specific
  (folder/frontmatter drift, leftover temp files) and partly universal (invalid
  field values, deadlocked dependencies). The universal half lives here in
  `common_problems()`; a sink implements `storage_problems()` for its own half.

What is *not* in this interface is as deliberate: no paths, no folder names, no
`status_dir()`. A status transition is `update()` with a new status, and how the
store then represents the change -- moving a file, writing a row -- is the sink's
private concern. That is precisely what let the file sink keep its
folder-follows-status behavior without any other code knowing about it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .. import graph, schema
from ..errors import Conflict, TicketError
from ..query import TicketQuery, apply_limit, bucket_matches, resolve_id, sort_tickets
from ..schema import Ticket

# Sentinel for `Expect`: "do not check this field" is a different instruction
# from "check that it is None" (an unassigned ticket), and only an explicit
# sentinel can tell them apart.
UNSET = object()


@dataclass(frozen=True)
class Expect:
    """The compare-and-swap token for an update.

    `update(ticket, expect=Expect(status="open", assignee=None))` means "write
    this only if it is still open and unassigned" -- a claim. An unset field is
    not checked. Sinks enforce this *atomically*, which is the whole point: the
    file sink does it inside an `O_CREAT|O_EXCL` window, SQLite inside a
    transaction, so two agents racing produce exactly one winner and one
    `Conflict`."""

    status: object = UNSET
    assignee: object = UNSET

    @classmethod
    def unclaimed(cls, status: str = "open") -> "Expect":
        """The claim expectation: still in this status and owned by nobody."""
        return cls(status=status, assignee=None)

    def violated_by(self, ticket: Ticket) -> Optional[str]:
        """Why `ticket` does not satisfy this expectation, or None if it does."""
        if self.status is not UNSET and ticket.status != self.status:
            return f"status is '{ticket.status}', expected '{self.status}'"
        if self.assignee is not UNSET and ticket.assignee != self.assignee:
            got = ticket.assignee if ticket.assignee else "unassigned"
            want = self.assignee if self.assignee else "unassigned"
            return f"assignee is {got}, expected {want}"
        return None


def enforce_expect(ticket: Ticket, expect: Optional[Expect]) -> None:
    """Raise `Conflict` unless `ticket` satisfies `expect`.

    A helper rather than a base-class responsibility because it must be called by
    each sink *inside* its own atomic write, not before it -- a check performed
    outside the critical section would just be a race with extra steps."""
    if expect is None:
        return
    reason = expect.violated_by(ticket)
    if reason is not None:
        raise Conflict(
            f"ticket {ticket.id} is not in the expected state ({reason}); "
            f"re-read it with 'arbite show {ticket.id}' and retry"
        )


@dataclass
class Problem:
    """One integrity finding, as `arbite doctor` reports it.

    `kind` is a stable identifier (agents branch on it), `location` is where the
    ticket is in this sink, and `fixed` records whether `--fix` repaired it in
    this run."""

    kind: str
    detail: str
    ticket_id: Optional[str] = None
    location: Optional[str] = None
    fixed: bool = False

    def to_dict(self) -> dict:
        """The doctor `--json` shape. The `path` key is kept for compatibility
        with the pre-sink contract even though a non-file sink has no path."""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "id": self.ticket_id,
            "path": self.location,
            "fixed": self.fixed,
        }


@dataclass(frozen=True)
class SinkInfo:
    """What a sink is, in storage-neutral terms. `docs.py` renders the
    agent-facing guide from this, so the guide can only claim what is true of the
    active sink (notably `status_is_location`)."""

    kind: str
    root: str
    status_is_location: bool
    supports_buckets: bool
    ticket_count: int = 0
    status_counts: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "root": self.root,
            "status_is_location": self.status_is_location,
            "supports_buckets": self.supports_buckets,
            "ticket_count": self.ticket_count,
            "status_counts": dict(self.status_counts),
            "details": dict(self.details),
        }


class TicketSink(ABC):
    """A ticket store. Implementations: `FileSink`, `SqliteSink`.

    Subclasses implement the storage primitives (`ids`, `read`, `insert`,
    `update`, `remove`, `query`, `bucket`, `move_to_bucket`, `location`,
    `notes`, `storage_problems`); the verbs built on them (`get`, `create`,
    `add_note`, `check`, `describe`) are implemented once here so every sink
    answers identically to questions the schema already answers."""

    #: Short identifier used by config and shown by `arbite sink info`.
    kind: str = "?"
    #: True when a ticket's status is implied by *where* it is stored, which the
    #: docs use to decide whether to explain the folder rule.
    status_is_location: bool = False
    #: True when the sink can file a ticket somewhere other than its status
    #: location (the file sink's wishlist/ and plans/ folders).
    supports_buckets: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def init(self) -> None:
        """Create the store if it does not exist. Idempotent: `arbite init` is
        safe to re-run and must never destroy data."""

    @property
    @abstractmethod
    def root(self) -> str:
        """Human-readable location of the store (a directory or a database
        file), used in messages and in `SinkInfo`."""

    def describe(self) -> SinkInfo:
        tickets = self.query(TicketQuery())
        counts = {}
        for t in tickets:
            counts[t.status] = counts.get(t.status, 0) + 1
        return SinkInfo(
            kind=self.kind,
            root=str(self.root),
            status_is_location=self.status_is_location,
            supports_buckets=self.supports_buckets,
            ticket_count=len(tickets),
            status_counts=counts,
            details=self.details(),
        )

    def details(self) -> dict:
        """Extra sink-specific facts for `arbite sink info --json`. Empty by
        default; a database sink reports its schema version and path here."""
        return {}

    # ------------------------------------------------------------------
    # Storage primitives -- implemented by each sink
    # ------------------------------------------------------------------

    @abstractmethod
    def ids(self) -> list:
        """Every ticket id in the store, sorted. The inventory that id
        resolution, `new_id()` and the graph views are built from."""

    @abstractmethod
    def read(self, ticket_id: str) -> Ticket:
        """The ticket with this exact id. Raises TicketNotFound. Must not be
        called with a wildcard term -- `get()` resolves first."""

    @abstractmethod
    def insert(self, ticket: Ticket) -> Ticket:
        """Create a ticket. Raises Conflict if the id is already taken."""

    @abstractmethod
    def update(self, ticket: Ticket, expect: Optional[Expect] = None) -> Ticket:
        """Write a changed ticket back, honouring `expect` atomically.

        The one write path for every kind of change: a field edit, a status
        transition (the file sink relocates, the database sink does not) and a
        claim all arrive here. Raises Conflict when `expect` is not satisfied,
        TicketNotFound when the ticket is gone."""

    @abstractmethod
    def remove(self, ticket_id: str) -> None:
        """Delete a ticket outright. Irreversible, which is why the CLI gates it
        behind `--force`; the API itself does not second-guess the caller."""

    @abstractmethod
    def query(self, q: TicketQuery) -> list:
        """Every ticket matching `q`, in `q.order`, capped by `q.limit`.

        A sink may push predicates into its own storage (SQLite builds SQL), but
        must return exactly what the reference predicate
        `TicketQuery.matches()` selects -- the conformance suite asserts this."""

    @abstractmethod
    def bucket(self, ticket_id: str) -> Optional[str]:
        """Where the ticket is filed beyond its status: None when the sink keeps
        it at its status location, otherwise the bucket name ('' is the root)."""

    @abstractmethod
    def move_to_bucket(self, ticket_id: str, bucket: Optional[str]) -> Ticket:
        """File a ticket in `bucket`, or with None return it to its status
        location. Changes no field -- this is filing, not a state change."""

    @abstractmethod
    def location(self, ticket_id: str) -> str:
        """Where this ticket lives, as text. A path for the file sink, an
        identifier for a database sink -- always something a human can act on,
        and what `--json` reports as `path`."""

    def storage_locations(self, ticket_id: str) -> list:
        """Every location holding this id. Only differs from `[location(id)]`
        when a store can hold the same id twice, which is exactly the
        duplicate-id case `doctor` reports."""
        return [self.location(ticket_id)]

    def location_map(self, tickets) -> dict:
        """`{ticket_id: location}` for a batch of tickets.

        A batch method rather than a loop of `location()` in the caller because a
        sink that has to scan to answer (the file sink walks the tree) can then do
        it once: `--json` on a listing asks for every ticket's location at the same
        time, and a per-row scan would make that quadratic."""
        return {t.id: self.location(t.id) for t in tickets}

    @abstractmethod
    def notes(self, ticket_id: str) -> list:
        """The ticket's `## Notes` entries, in body order, as `schema.Note`.

        Implementations may parse the body each time (the reference) or read a
        derived index; either way the answer must match
        `schema.parse_notes(ticket.body)`, which is what makes an index safe."""

    @abstractmethod
    def storage_problems(self, fix: bool = False) -> list:
        """Integrity problems specific to this storage, already repaired when
        `fix` is true. The universal checks are added by `check()`."""

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get(self, ticket_id: str, unique: bool = False) -> Ticket:
        """Resolve a wildcard term and return the ticket.

        An exact id wins outright; with `unique=True` an ambiguous term raises
        AmbiguousTicketId (mutating commands pass it, because guessing writes to
        the wrong ticket) while read-only callers get the first match."""
        return self.read(resolve_id(self.ids(), ticket_id, unique=unique))

    def get_many(self, ticket_ids: Iterable[str]) -> list:
        return [self.read(tid) for tid in ticket_ids]

    def exists(self, ticket_id: str) -> bool:
        return ticket_id in set(self.ids())

    def render(self, ticket: Ticket) -> str:
        """The ticket's canonical text. Implemented once, from the schema,
        because the text form belongs to the ticket and not to the store: this is
        what makes `arbite show`, `arbite migrate` and the byte-identical file
        round-trip possible across sinks."""
        return ticket.to_markdown()

    def query_one(self, q: TicketQuery) -> Optional[Ticket]:
        """The first ticket matching `q`, or None. Convenience for the many
        commands that want "the next one" without slicing a list themselves."""
        rows = self.query(q.evolve(limit=1))
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Create verbs
    # ------------------------------------------------------------------

    def new_id(self) -> str:
        """A fresh id not present in the store. A racing creator can still mint
        the same id; `create()` turns that into a Conflict so the caller can
        simply try again rather than corrupting the store."""
        return schema.gen_id(set(self.ids()))

    def create(self, ticket: Ticket) -> Ticket:
        """Store a new ticket. Raises Conflict if its id is already taken."""
        if self.exists(ticket.id):
            raise Conflict(
                f"ticket {ticket.id} already exists in the {self.kind} sink; "
                "ticket ids are never reused"
            )
        return self.insert(ticket)

    # ------------------------------------------------------------------
    # Update verbs
    # ------------------------------------------------------------------

    def add_note(self, ticket_id: str, agent: str, message: str, note_date: Optional[str] = None) -> Ticket:
        """Append a timestamped, attributed note and bump `updated`.

        The note text is composed by the schema so the entry format stays
        identical to what the file sink has always written. This is a
        read-modify-write with no expectation: like the pre-sink implementation,
        two concurrent notes on one ticket resolve last-write-wins rather than
        being merged. Ticket *state* changes use `expect` and cannot race."""
        ticket = self.get(ticket_id, unique=True)
        schema.append_note(ticket, agent, message, note_date=note_date)
        ticket.updated = note_date or schema.now()
        return self.update(ticket)

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def check(self, fix: bool = False) -> list:
        """Every integrity problem in the store: the universal checks plus this
        sink's storage-specific ones. Repairing is best-effort and only for the
        unambiguous cases -- anything needing a judgement call is reported."""
        problems = common_problems(self.query(TicketQuery(buckets=("*",))), self)
        problems.extend(self.storage_problems(fix=fix))
        return problems


def common_problems(tickets: list, sink: Optional["TicketSink"] = None) -> list:
    """Integrity problems that mean the same thing in every sink.

    These are the invariants no command enforces because they can only be broken
    from *outside* arbite: a hand-edited field, a dependency on a ticket that was
    deleted, a loop of tickets waiting on each other. Storage-specific findings
    (folder drift, temp files, a stale note index) are a sink's own business.

    Cross-ticket checks need the whole set, so they are computed here rather than
    per ticket. `sink` is optional and only used to say *where* a duplicated id
    was found, which is the one finding whose value is entirely in its
    locations."""
    problems = []

    by_id = {}
    duplicates = {}
    for t in tickets:
        if t.id in by_id:
            duplicates.setdefault(t.id, 0)
            duplicates[t.id] += 1
        else:
            by_id[t.id] = t
    for tid, extra in sorted(duplicates.items()):
        where = ""
        if sink is not None:
            locations = sink.storage_locations(tid)
            if locations:
                where = f": {', '.join(locations)}"
        problems.append(
            Problem(
                "duplicate_id",
                f"{extra + 1} tickets share id {tid}{where} (resolve by hand -- arbite "
                "cannot know which is current)",
                ticket_id=tid,
            )
        )

    for t in tickets:
        for prop in ("status", "type", "tier"):
            value = getattr(t, prop, None)
            if value is None or schema.is_placeholder(value):
                # A TODO placeholder is pending triage work, not corruption.
                continue
            try:
                schema.validate_field(prop, str(value))
            except TicketError as e:
                problems.append(Problem("invalid_field", str(e), ticket_id=t.id))
        if t.priority is not None:
            try:
                schema.validate_field("priority", str(t.priority))
            except TicketError as e:
                problems.append(Problem("invalid_field", str(e), ticket_id=t.id))
        for date_field in schema.DATE_FIELDS:
            value = getattr(t, date_field, None)
            if value and not schema.DATE_PATTERN.match(str(value)):
                problems.append(
                    Problem(
                        "invalid_field",
                        f"{date_field} is not a valid date/timestamp "
                        f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): '{value}'",
                        ticket_id=t.id,
                    )
                )

        for dep in t.depends_on:
            if dep not in by_id:
                problems.append(
                    Problem(
                        "dangling_dependency",
                        f"depends_on '{dep}', which is not a known ticket "
                        "(readiness silently ignores it, so this ticket can look "
                        "workable when its real prerequisite is gone)",
                        ticket_id=t.id,
                    )
                )
        if t.id in t.depends_on:
            problems.append(Problem("self_dependency", "depends on itself", ticket_id=t.id))

        if t.status == "in_progress" and not t.assignee:
            problems.append(
                Problem(
                    "in_progress_unassigned",
                    "in_progress but has no assignee -- nobody is accountable for it and "
                    "'list next' will never offer it; release it or claim it",
                    ticket_id=t.id,
                )
            )
        if t.status == "blocked" and not t.blocked_by:
            problems.append(
                Problem(
                    "blocked_without_reason",
                    "blocked but blocked_by is empty -- nothing records what is stalling it",
                    ticket_id=t.id,
                )
            )
        if t.status == "closed" and not t.closed:
            problems.append(
                Problem("closed_without_date", "closed ticket has no 'closed' date", ticket_id=t.id)
            )
        if t.status != "closed" and t.closed:
            problems.append(
                Problem(
                    "closed_date_on_open_ticket",
                    f"not closed but has a 'closed' date of {t.closed}",
                    ticket_id=t.id,
                )
            )

    # Cycles are checked over the whole graph: an unsatisfiable loop means every
    # ticket in it is permanently unworkable, however it is filtered.
    for cycle in graph.live_cycles(by_id):
        problems.append(
            Problem(
                "dependency_cycle",
                "dependency cycle -- none of these can ever become workable: "
                + " -> ".join(cycle + [cycle[0]]),
                ticket_id=cycle[0],
            )
        )

    return problems


def filter_tickets(tickets: list, q: TicketQuery, buckets: Optional[dict] = None) -> list:
    """The reference in-memory implementation of `TicketQuery`: filter with
    `matches()`, apply the bucket part of the query from `buckets`
    (`{ticket_id: bucket_or_None}`), sort by the canonical order, cap.

    Provided so a sink with no query engine of its own (the file sink) has one
    correct implementation to call, and so a sink *with* one (SQLite) has
    something to be tested against rather than merely believed."""
    q = q.normalized()
    buckets = buckets or {}
    rows = [
        t for t in tickets if q.matches(t) and bucket_matches(buckets.get(t.id), q.buckets)
    ]
    return apply_limit(sort_tickets(rows, q.order), q.limit)
