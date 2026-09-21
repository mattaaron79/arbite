"""Change receipts and net change views: `arbite receipt OP` and `arbite changes T`.

Two questions, deliberately different, about the evidence the engine records:

- **One operation, in full.** `arbite receipt OP` prints one ordered log entry: what it was,
  who did it, the paths it named, both versions with the shape each one had, and the evidence
  that is kept. Before printing anything it checks that evidence -- every artifact the receipt
  names is still in the store, its recorded size is the size of the content filed under its
  digest, and the content still hashes to that digest (`CoordinationStore.verify_artifact`) --
  so a digest printed here is a claim arbite checked rather than one it copied. Both versions
  are the point of the command: a successful change keeps the bytes it replaced *and* the bytes
  it wrote, which is what makes a removal, a rename, an edit and a binary write reproducible
  and not only a final text diff.
- **The net, and the log behind it.** `arbite changes T` answers "what is different now because
  of this ticket", per work attempt: one row per path, from the version the first operation
  found to the version the last one left, naming the operations that produced it. `--all` adds
  the ordered operation log, one row per operation-path, which is where a change that was
  reverted stays visible *as an operation*. A net view that printed only `no net change` would
  be true and would still hide the work, so the row that says so also says how to see both
  operations.
- **The ticket's own net.** One attempt's operations are not always the whole ticket -- a reopen
  starts a new attempt -- so when a ticket has more than one the view prints each attempt's
  section and then the ticket's net across them, which is the answer no single section gives.
- **The store, summarised for a devlog.** `arbite receipt --summary` prints every operation in
  log order with its attribution, its paths and both versions, and the evidence the store is
  still holding -- the export to take *before* anything is pruned, because coordination state
  is local and is lost with the machine while the tickets travel in git.

Three rules shape everything here:

- **No summarisation, no upload.** Nothing asks a model to describe anything and nothing leaves
  the machine: a receipt is arbite's own record of bytes, and these views are that record
  rendered. Prose about a change belongs in a ticket note, which is deliberately another thing.
- **Content is stored once per digest, and read that way.** Versions are named by digest and the
  bytes are read back only to reproduce them, so two receipts that share a version share one
  copy of it, and an edit-then-revert keeps both versions once each.
- **Order comes from the log, not from the record store.** Receipts come back in id order, which
  says nothing about when they happened, so operations are ordered by the cursor of their own
  event (`*.intent`, `*.file`); a receipt written without one -- an import, a fixture -- falls
  back to its recorded time and id, and sorts after every operation whose place is known.

The text is the report and JSON is the same facts; where the text summarises, the JSON carries
the summary *and* its parts (`_receipt_data`, `_changes_data`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..errors import CoordinationError, TicketNotFound
from .paths import Version, version_of
from .records import ABSENT, OperationReceipt, parse_utc, short_digest
from .scratch import human_size
from .results import EMPTY, OK, OperationResult, Outcome
from .store import CoordinationStore
from .writes import line_delta, version_facts

#: The word a receipt's outcome prints, and the stored `result` it stands for. A succeeded
#: operation reads `ok` in every other arbite report, and a receipt that renamed the store's
#: own vocabulary would make a reader translate two words into one fact, so `--json` carries
#: both: `result` as printed, `stored_result` as recorded.
RESULT_WORDS = {
    "succeeded": "ok",
    "failed": "failed",
    "pending": "pending",
}

#: The row letters, borrowed from the diff vocabulary every reader already has: `M` the path's
#: bytes changed (or ended where they started), `A` they appeared, `D` they left, and `!` an
#: operation that was never applied. The letters describe a path's *net endpoints*, so a create
#: that was later removed is `M` with `no net change` -- true, and pointed at the log by the
#: row's note.
STATUS_CHANGED = "M"
STATUS_CREATED = "A"
STATUS_REMOVED = "D"
STATUS_UNAPPLIED = "!"

#: How a row describes its endpoints. `created`/`removed` are the frozen EV1 rows' words for the
#: two pairs with one version to name; bytes changed in place print the line delta the write and
#: edit reports print; bytes arbite cannot read as text print the two sizes instead, because a
#: line count there would be a number nobody could reproduce.
DETAIL_CREATED = "created"
DETAIL_REMOVED = "removed"
DETAIL_NO_NET_CHANGE = "no net change"
DETAIL_REPLACED = "replaced"
DETAIL_BINARY = "{before} -> {after} bytes (binary)"
DETAIL_UNREADABLE = "changed"
DETAIL_FAILED = "failed -- never applied"
DETAIL_PENDING = "pending -- may not have been applied"

#: What a row whose net is zero says when more than one operation produced it: the row is true
#: and useless without the second half, which is the ordered view. `edit-then-revert` is the
#: shape EV1 names for an edit another edit undid; any other pair gets the same sentence without
#: the claim about what the operations were.
NOTE_EDIT_THEN_REVERT = "edit-then-revert: both operations remain in the log ('--all')"
NOTE_REVERTED = "reverted: all {count} operations remain in the log ('--all')"
NOTE_SINGLE_NO_CHANGE = "the operation changed nothing: it remains in the log ('--all')"

#: A receipt that was staged and never finalised may or may not have happened, so both views say
#: so instead of leaving it out: `arbite doctor` judges it against the bytes on disk, and nothing
#: here guesses which way that came out.
PENDING_NOTE = (
    "pending: this operation was staged and not finalized -- arbite will not guess whether its "
    "bytes are in place ('arbite doctor' judges it against the bytes on disk)"
)
PENDING_ROW = (
    "pending: {operation} staged a {kind} of {paths} and was not finalized --",
    "      '{command}' to read it, then restore or re-apply by hand",
)

#: The column padding. Two spaces is the gap the frozen EV1 rows use between columns, and the
#: version column is exactly a short-digest pair with its arrow, so a view's widths follow from
#: the rows it has.
COLUMN_GAP = 2
VERSION_COLUMN_WIDTH = 2 * (len("sha256:") + 12) + len(" -> ")
NOTE_INDENT = "      "

#: The one shape a version prints in: the digest, then the version's own description in brackets
#: -- `(570 lines)`, `(1024 bytes, binary)` -- or the explicit `absent`.
VERSION_SHAPE = "{digest} ({shape})"
VERSION_MISSING = "{digest} (evidence missing)"
VERSION_ABSENT = "absent"
NO_EVIDENCE = "no image to retain (this operation named no version)"

#: The stub every refusal from this surface ends with: a reader has to be able to tell "arbite
#: refused" from "arbite changed something and then failed".
NOTHING_CHANGED = "nothing was changed"

#: The receipt summary: every operation in the order it happened, one row per operation-path,
#: with both versions named -- which is what a devlog is generated from, because the evidence
#: itself is local and dies with the machine. `{at}` is local time for reading, the digests are
#: short, and a row with no path (a passthrough that named none) says so rather than vanishing.
SUMMARY_HEADING = "receipt summary: {operations}, {tickets}, {attempts}"
SUMMARY_TICKET_HEADING = "receipt summary for {ticket}: {operations}, {attempts}"
SUMMARY_RESULTS = "{operations}: {ok} ok, {pending} pending, {failed} failed"
SUMMARY_NO_PATH = "(no path)"
SUMMARY_RETENTION = (
    "evidence: {referenced} version(s) referenced by these operations; the store holds "
    "{artifacts} artifact record(s) ({bytes})"
)
SUMMARY_NEVER_PRUNED = (
    "note: arbite never prunes evidence and has no retention policy yet, so the store grows "
    "with the work -- take this summary before anything is deleted"
)
SUMMARY_PENDING = (
    "note: {count} operation(s) here are pending -- staged and never finalized, so arbite will "
    "not guess whether their bytes are in place ('arbite doctor' judges them)"
)
SUMMARY_EMPTY_NEXT = "arbite events --tail 20"

#: Where a receipt whose evidence is gone sends a reader. Nothing here repairs anything: which
#: version is "correct" is not a question a report may answer, so the repair is a human's, and
#: `doctor` is what reports the state it has to judge.
DRIFT_HINT = "'arbite doctor' to report the store's integrity, then restore the version by hand"


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------


def operation_order(store: CoordinationStore) -> dict:
    """`operation id -> its place in the log`, from the events the operations appended.

    The earliest event naming an operation is its position: a mutation appends `*.intent` when
    it stages and `*.file` when it lands, and both carry the operation id, so the lower cursor is
    where the operation began. Cursors are the only ordering arbite records; a receipt's own
    `recorded_at` has one-second resolution, so two operations in one second cannot be told
    apart by it."""
    positions = {}
    for event in store.events():
        if event.operation_id is None:
            continue
        current = positions.get(event.operation_id)
        if current is None or event.cursor < current:
            positions[event.operation_id] = event.cursor
    return positions


def log_order(receipts: list, positions: dict) -> list:
    """`receipts` in log order: by event cursor, then by recorded time and id.

    A receipt with no event (an import, a record a fixture wrote) sorts after every operation
    whose cursor is known, because "somewhere after the operations that happened" is what a
    missing position honestly means -- and inside that group time and id give a stable order
    rather than a random one."""
    return sorted(
        receipts,
        key=lambda receipt: (
            positions.get(receipt.id) is None,
            positions.get(receipt.id, 0),
            receipt.recorded_at,
            receipt.id,
        ),
    )


@dataclass(frozen=True)
class VersionRead:
    """One version a receipt names, as this build can reproduce it.

    `version` is the shape (size, lines) when the content could be read back, and None when it
    could not -- which the views print as `evidence missing` rather than pretending the version
    has no size. `data` is the content itself, and is what a line delta is computed from."""

    digest: str
    version: Optional[Version] = None
    data: bytes = b""

    @property
    def is_absent(self) -> bool:
        return self.digest == ABSENT

    @property
    def is_readable(self) -> bool:
        return self.version is not None

    def describe(self) -> str:
        """The version as a report prints it, shape and all."""
        if self.is_absent:
            return VERSION_ABSENT
        if self.version is None:
            return VERSION_MISSING.format(digest=short_digest(self.digest))
        return VERSION_SHAPE.format(digest=short_digest(self.digest), shape=_shape(self.version))

    def facts(self) -> Optional[dict]:
        """The version as JSON reports it, or None when it is absent -- the same keys
        `file read --json` prints, so a consumer learns one shape for a version."""
        if self.is_absent:
            return None
        if self.version is None:
            return {"digest": self.digest, "bytes": None, "lines": None}
        return version_facts(self.version)


def read_version(store: CoordinationStore, digest: str) -> VersionRead:
    """Read one version back out of the artifact store, best-effort.

    Best-effort is the right default *for a view*: a version whose content is gone degrades one
    column to `evidence missing` rather than hiding every other operation in the log. The
    receipt view does not rely on it and proves the content instead (`ChangeViews.receipt`),
    because a single receipt is the place where "the bytes are still there" is the whole
    question."""
    if digest == ABSENT:
        return VersionRead(digest)
    try:
        data = store.get_artifact_bytes(digest)
    except CoordinationError:
        return VersionRead(digest)
    return VersionRead(digest, version_of(data, digest), data)


def _shape(version: Version) -> str:
    """The bracketed half of a version: `570 lines`, or the exact size for bytes that are not
    UTF-8 text. Exact bytes rather than a human-size reading, because a reader comparing two
    versions is comparing numbers."""
    if version.lines is None:
        return f"{version.size} bytes, binary"
    noun = "line" if version.lines == 1 else "lines"
    return f"{version.lines} {noun}"


# ---------------------------------------------------------------------------
# The net of a group of operations
# ---------------------------------------------------------------------------


@dataclass
class PathNet:
    """One path's net across a group of operations, with the operations that made it.

    `before` is the version the *first* operation in the group found and `after` the version the
    last one left; `operations` keeps every operation that touched the path, in log order, so a
    row never stands for fewer operations than it covers. `kinds` is that list's operation kinds,
    which is what lets "an edit was undone by an edit" be said about the row that shows it."""

    path: str
    before: str
    after: str
    operations: list = field(default_factory=list)
    kinds: list = field(default_factory=list)

    @property
    def net_change(self) -> bool:
        return self.before != self.after

    @property
    def has_versions(self) -> bool:
        """Whether the row has two versions to print. A creation, a removal and a path that
        ended empty have one side (or none) to name, and print what happened instead."""
        return self.before != ABSENT and self.after != ABSENT

    @property
    def status(self) -> str:
        if self.before == ABSENT and self.net_change:
            return STATUS_CREATED
        if self.after == ABSENT and self.net_change:
            return STATUS_REMOVED
        return STATUS_CHANGED

    @property
    def note(self) -> Optional[str]:
        """The sentence a zero-net row needs, or None when the row speaks for itself."""
        if self.net_change or not self.operations:
            return None
        if len(self.operations) == 1:
            return NOTE_SINGLE_NO_CHANGE
        if len(self.operations) == 2 and set(self.kinds) == {"edit"}:
            return NOTE_EDIT_THEN_REVERT
        return NOTE_REVERTED.format(count=len(self.operations))


def net_of(receipts: list) -> list:
    """The net change of `receipts` (already in log order): one `PathNet` per path.

    Only **applied** operations are netted. A receipt is `succeeded` when its change happened,
    `failed` when a recovery pass proved it never did, and `pending` when nobody has judged it
    yet: a failed operation is counted in the header and listed by `--all`, but folding it into a
    change summary would attribute a change to an operation that provably changed nothing, and a
    pending one is reported separately for the same reason -- which way it went is not known."""
    entries = {}
    for receipt in receipts:
        if receipt.result != "succeeded":
            continue
        for path in receipt.paths:
            entry = entries.get(path)
            if entry is None:
                entry = PathNet(
                    path=path, before=receipt.before[path], after=receipt.after[path]
                )
                entries[path] = entry
            entry.after = receipt.after[path]
            entry.operations.append(receipt.id)
            entry.kinds.append(receipt.kind)
    # Rows are ordered by the first operation on each path, which is the order a reader
    # reconstructs the work in; path order would say nothing about it.
    where = {receipt.id: index for index, receipt in enumerate(receipts)}
    return sorted(entries.values(), key=lambda entry: (where[entry.operations[0]], entry.path))


@dataclass(frozen=True)
class Widths:
    """The column widths of one group of rows, computed from the rows themselves."""

    path: int
    version: Optional[int]
    detail: int

    @staticmethod
    def of(rows: list) -> "Widths":
        """`rows` is a list of `(path, has_versions, detail)` triples.

        The path column is measured over the rows that print a version, because those are the
        rows whose columns have to line up; a creation's path, which is the longest row in EV1,
        is allowed to overrun it. The version column is reserved for a whole group as soon as one
        row prints a version, so the columns after it start at the same offset everywhere."""
        versioned = [row for row in rows if row[1]]
        measured = versioned or rows
        return Widths(
            path=max((len(row[0]) for row in measured), default=0) + COLUMN_GAP,
            version=VERSION_COLUMN_WIDTH + COLUMN_GAP if versioned else None,
            detail=max((len(row[2]) for row in rows), default=0) + COLUMN_GAP,
        )

    def row(self, letter: str, path: str, version: str, detail: str, operations: str) -> str:
        """One laid-out row. A row with no version leaves that column blank rather than moving
        the columns after it, which is what keeps a group readable."""
        version_field = "" if self.version is None else f"{version:<{self.version}}"
        return (
            f"{letter} {path:<{self.path}}{version_field}"
            f"{detail:<{self.detail}}({operations})"
        )


# ---------------------------------------------------------------------------
# The views
# ---------------------------------------------------------------------------


class ChangeViews:
    """The receipt view and the change views, for one ticket sink and coordination store."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.store = lifecycle.store
        self.project_root = lifecycle.app.project_root

    # ------------------------------------------------------------------
    # One receipt
    # ------------------------------------------------------------------

    def receipt(self, operation_id: str) -> OperationResult:
        """One operation's evidence, reproduced and verified."""
        receipt = self._receipt_record(operation_id)
        reads = {
            digest: read_version(self.store, digest)
            for digest in _referenced_versions(receipt)
        }
        artifacts = self._checked_artifacts(receipt, reads)
        return OperationResult(
            Outcome(OK),
            self._receipt_lines(receipt, reads),
            self._receipt_data(receipt, reads, artifacts),
            [],
        )

    def _receipt_record(self, operation_id: str) -> OperationReceipt:
        """The receipt `operation_id` names, or the refusal that says what it is instead.

        Read observations and receipts share the `op-` id space on purpose -- a caller holds one
        handle for both -- so an id that names an observation is answered as what it is rather
        than as a missing operation: a read says bytes were served and holds no versions to
        reproduce."""
        record = self.store.find_record("receipt", operation_id)
        if record is not None:
            return record
        if self.store.find_record("observation", operation_id) is not None:
            command = "arbite events --tail 20 --include-reads"
            raise CoordinationError(
                f"{operation_id} is a read observation, not an operation: a read receipt says "
                "bytes were served and holds no before/after versions to reproduce",
                [command],
                text_hint=f"next: '{command}' to see the reads alongside the operations",
            )
        command = "arbite events --tail 20"
        raise CoordinationError(
            f"no operation {operation_id} in this store",
            [command, "arbite changes <ticket>"],
            text_hint=(
                f"next: '{command}' to find the operation id, or "
                "'arbite changes <ticket>' for a ticket's operations"
            ),
        )

    def _checked_artifacts(self, receipt: OperationReceipt, reads: dict) -> list:
        """The artifact records this receipt references, proved against their content.

        Three things have to hold for a receipt that claims to keep its evidence: every artifact
        it names is still in the store, each one's recorded size is the size of the content filed
        under its digest, and every version the receipt records is covered by those artifacts. A
        version with no artifact behind it is the failure this command exists to catch -- the
        receipt would reproduce a digest and nothing else -- and it is refused rather than
        printed, with nothing changed."""
        entries = []
        covered = set()
        for artifact_id in receipt.artifacts:
            artifact = self.store.find_record("artifact", artifact_id)
            if artifact is None:
                raise CoordinationError(
                    f"{receipt.id} names artifact {artifact_id}, which is not in this store, so "
                    f"the evidence it holds cannot be checked; {NOTHING_CHANGED}",
                    [DRIFT_HINT],
                    text_hint=f"next: {DRIFT_HINT}",
                )
            read = reads.get(artifact.digest)
            if read is None or not read.is_readable:
                raise CoordinationError(
                    f"{receipt.id} names artifact {artifact_id} for version "
                    f"{short_digest(artifact.digest)}, whose content is not in this store, so "
                    f"the bytes this operation changed cannot be reproduced; {NOTHING_CHANGED}",
                    [DRIFT_HINT],
                    text_hint=f"next: {DRIFT_HINT}",
                )
            if artifact.size != read.version.size:
                raise CoordinationError(
                    f"artifact {artifact_id} records {artifact.size} bytes for "
                    f"{short_digest(artifact.digest)} but the stored content is "
                    f"{read.version.size} bytes; {NOTHING_CHANGED}",
                    [DRIFT_HINT],
                    text_hint=f"next: {DRIFT_HINT}",
                )
            # The proof, not the record: the bytes are read back and hashed again.
            self.store.verify_artifact(artifact.digest)
            covered.add(artifact.digest)
            entries.append(
                {
                    "id": artifact.id,
                    "digest": artifact.digest,
                    "size": artifact.size,
                    "sides": sorted(_sides_of(receipt, artifact.digest)),
                    "verified": True,
                }
            )
        uncovered = sorted(
            digest for digest in _named_versions(receipt) if digest not in covered
        )
        if uncovered:
            raise CoordinationError(
                f"{receipt.id} records {', '.join(short_digest(digest) for digest in uncovered)} "
                f"with no artifact holding them, so its evidence is not complete; "
                f"{NOTHING_CHANGED}",
                [DRIFT_HINT],
                text_hint=f"next: {DRIFT_HINT}",
            )
        return sorted(entries, key=lambda entry: entry["digest"])

    def _receipt_lines(self, receipt: OperationReceipt, reads: dict) -> list:
        """The frozen EV6 shape: the operation, its attribution, its paths, its evidence."""
        lines = [_operation_line(receipt), _attribution_line(receipt)]
        for path in receipt.paths:
            lines.append(f"path: {path}")
            lines.append(
                f"before: {reads[receipt.before[path]].describe()}   "
                f"after: {reads[receipt.after[path]].describe()}"
            )
        lines.append(f"artifact: {_evidence_line(receipt)}")
        if receipt.is_pending:
            lines.append(PENDING_NOTE)
        return lines

    def _receipt_data(self, receipt: OperationReceipt, reads: dict, artifacts: list) -> dict:
        """The same facts as fields, plus the parts the one-line text summarises."""
        sides = sorted({side for entry in artifacts for side in entry["sides"]})
        return {
            "operation": receipt.id,
            "kind": receipt.kind,
            "result": RESULT_WORDS[receipt.result],
            "stored_result": receipt.result,
            "recorded_at": receipt.recorded_at,
            "ticket": receipt.ticket_id,
            "attempt": receipt.attempt_id,
            "actor": receipt.actor,
            "claim_generation": receipt.claim_generation,
            "paths": [
                {
                    "path": path,
                    "before": reads[receipt.before[path]].facts(),
                    "after": reads[receipt.after[path]].facts(),
                }
                for path in receipt.paths
            ],
            "artifact": {
                "image": "before" if "before" in sides else "after",
                "stored": bool(artifacts),
                "retained": bool(artifacts),
                "verified": all(entry["verified"] for entry in artifacts),
                "sides": sides,
                "entries": artifacts,
            },
        }

    # ------------------------------------------------------------------
    # The summary a devlog is generated from
    # ------------------------------------------------------------------

    def summary(self, ticket_id: Optional[str] = None) -> OperationResult:
        """The receipt summary a devlog is generated from, before any evidence is pruned.

        Evidence is local: `.arbite/coordination/` and `.arbite/arbite.db` are ignored by git and
        die with the machine, so the development record is the tickets *plus* a summary taken
        while the receipts are still there. This is that summary -- one row per operation-path,
        in log order, naming who did it, when, which ticket and attempt it belonged to, and both
        versions -- and it is deliberately a summary rather than a reproduction: `arbite receipt
        OP` reads the bytes of one operation back and proves them, while this is what survives
        the store.

        Nothing is written, nothing is uploaded and no model summarises anything: this is
        arbite's own record rendered. The retention line reports the size of the evidence the
        store is holding instead of pruning it, because there is no retention policy yet -- and
        a summary taken before pruning is the whole reason the design can afford that."""
        if ticket_id is not None:
            self._require_ticket(ticket_id)
        receipts = [
            receipt
            for receipt in log_order(self.store.receipts(), operation_order(self.store))
            if ticket_id is None or receipt.ticket_id == ticket_id
        ]
        if not receipts:
            where = f" for {ticket_id}" if ticket_id else ""
            return OperationResult(
                Outcome(EMPTY),
                [f"no operations recorded{where}"],
                {"ticket": ticket_id, "operations": 0, "receipts": []},
                [SUMMARY_EMPTY_NEXT],
            )
        lines = [_summary_heading(receipts, ticket_id), *_summary_rows(receipts)]
        lines += _summary_notes(receipts, self.store)
        return OperationResult(Outcome(OK), lines, _summary_data(receipts, ticket_id, self.store), [])

    # ------------------------------------------------------------------
    # The change views
    # ------------------------------------------------------------------

    def changes(self, ticket_id: str, include_all: bool = False) -> OperationResult:
        """The net change a ticket made, per attempt, and the ordered log behind it.

        A net view is what a reader wants ("what is different now") and the ordered log is what
        an auditor wants ("what happened"), and neither is the other: the net collapses an edit
        and its revert into one honest `no net change` row, while the log keeps both operations.
        `--all` asks for the log; the net view points at it from exactly the rows that need it."""
        self._require_ticket(ticket_id)
        receipts = [receipt for receipt in self.store.receipts() if receipt.ticket_id == ticket_id]
        if not receipts:
            return OperationResult(
                Outcome(EMPTY),
                [f"no operations recorded for {ticket_id}"],
                {"ticket": ticket_id, "operations": 0, "attempts": []},
                ["arbite events --tail 20"],
                text_hint="next: 'arbite events --tail 20' to see what this workspace recorded",
            )

        ordered = log_order(receipts, operation_order(self.store))
        groups = _attempt_groups(ordered)
        ticket_net = net_of(ordered)

        lines, sections = [], []
        for attempt_id, group in groups:
            body = self._section(ticket_id, attempt_id, group, include_all)
            lines.extend(body["lines"])
            sections.append(body["data"])
        # A ticket's own net is a different answer from any one attempt's, and only a *different*
        # answer when there is more than one attempt to add up.
        ticket_section = None
        if len(groups) > 1:
            ticket_section = self._section(ticket_id, None, ordered, include_all=False, net=True)
            lines.extend(ticket_section["lines"])
        lines.extend(_pending_lines(ordered))

        return OperationResult(
            Outcome(OK),
            lines,
            self._changes_data(ticket_id, ordered, ticket_net, sections, ticket_section),
            [],
        )

    def _require_ticket(self, ticket_id: str) -> None:
        """Refuse a ticket id this project does not have.

        A view that answered "no operations recorded" for a misspelled id would be the worst
        possible answer -- it looks exactly like "nothing happened yet" -- so the ticket is
        looked up first, and a term that matches several is refused by the sink itself."""
        try:
            self.sink.get(ticket_id, unique=True)
        except TicketNotFound:
            raise CoordinationError(
                f"no ticket {ticket_id} in this store",
                ["arbite list"],
                text_hint="next: 'arbite list' to see the ticket ids this project has",
            ) from None

    def _section(self, ticket_id, attempt_id, receipts, include_all: bool, net: bool = False) -> dict:
        """One header and its rows: an attempt's, or the ticket's net across attempts.

        `net` says which of the two a `None` attempt id means here: the ticket's own roll-up, or
        the group of receipts that recorded no attempt at all."""
        header = (
            f"{ticket_id} · ticket net · {_operations(len(receipts))}"
            if net
            else _header(ticket_id, attempt_id, receipts, self._actor_of(attempt_id, receipts))
        )
        changes = net_of(receipts)
        rows = self._ordered_rows(receipts) if include_all else self._net_rows(changes)
        data = {
            "attempt": attempt_id,
            "actor": self._actor_of(attempt_id, receipts),
            "operations": len(receipts),
            "changes": [self._net_entry(entry) for entry in changes],
        }
        if include_all:
            data["log"] = [_log_entry(receipt) for receipt in receipts]
        return {"lines": [header, *rows], "data": data}

    def _actor_of(self, attempt_id: Optional[str], receipts: list) -> Optional[str]:
        """Who a header names: the attempt's worker when the store has that attempt, and the
        actor the receipts recorded otherwise (an import, a fixture)."""
        if attempt_id is not None:
            attempt = self.store.get_attempt(attempt_id)
            if attempt is not None:
                return attempt.worker_id
        for receipt in receipts:
            if receipt.actor:
                return receipt.actor
        return None

    def _net_rows(self, net: list) -> list:
        """The net rows: one per path, with the note a zero-net row needs under it."""
        details = {entry.path: self._detail(entry) for entry in net}
        widths = Widths.of(
            [(entry.path, entry.has_versions, details[entry.path]) for entry in net]
        )
        rows = []
        for entry in net:
            rows.append(
                widths.row(
                    entry.status,
                    entry.path,
                    self._version_pair(entry) if entry.has_versions else "",
                    details[entry.path],
                    ", ".join(entry.operations),
                )
            )
            if entry.note:
                rows.append(f"{NOTE_INDENT}{entry.note}")
        return rows

    def _ordered_rows(self, receipts: list) -> list:
        """The ordered log: one row per operation-path, in the order they happened."""
        widths = Widths.of(
            [
                (path, _is_replacement(receipt, path), self._ordered_detail(receipt, path))
                for receipt in receipts
                for path in receipt.paths
            ]
        )
        rows = []
        for receipt in receipts:
            for path in receipt.paths:
                version = ""
                if _is_replacement(receipt, path):
                    version = (
                        f"{short_digest(receipt.before[path])} -> "
                        f"{short_digest(receipt.after[path])}"
                    )
                status = (
                    _net_status(receipt.before[path], receipt.after[path])
                    if receipt.result == "succeeded"
                    else STATUS_UNAPPLIED
                )
                rows.append(
                    widths.row(
                        status, path, version, self._ordered_detail(receipt, path), receipt.id
                    )
                )
        return rows

    def _version_pair(self, entry: PathNet) -> str:
        """`sha256:X -> sha256:Y` for a path whose bytes changed in place.

        Short digests and nothing else, which is the frozen EV1 shape: this column is what a
        reader compares at a glance, and the shapes the two versions have belong to the
        receipt (`arbite receipt OP`), where the row that prints them has the width for it."""
        return f"{short_digest(entry.before)} -> {short_digest(entry.after)}"

    def _detail(self, entry: PathNet) -> str:
        """A net row's own column: what happened to the path, or how much of it changed."""
        if not entry.net_change:
            return DETAIL_NO_NET_CHANGE
        if entry.before == ABSENT:
            return DETAIL_CREATED
        if entry.after == ABSENT:
            return DETAIL_REMOVED
        before = read_version(self.store, entry.before)
        after = read_version(self.store, entry.after)
        if not (before.is_readable and after.is_readable):
            return DETAIL_UNREADABLE
        if before.version.lines is not None and after.version.lines is not None:
            added, removed = line_delta(before.data, after.data)
            return f"+{added} -{removed}"
        return DETAIL_BINARY.format(before=before.version.size, after=after.version.size)

    @staticmethod
    def _ordered_detail(receipt: OperationReceipt, path: str) -> str:
        """One operation-path's own column in the ordered log."""
        if receipt.result == "failed":
            return DETAIL_FAILED
        if receipt.result == "pending":
            return DETAIL_PENDING
        before, after = receipt.before[path], receipt.after[path]
        if before == ABSENT:
            return DETAIL_CREATED
        if after == ABSENT:
            return DETAIL_REMOVED
        return DETAIL_REPLACED

    def _net_entry(self, entry: PathNet) -> dict:
        """One net row as fields: the same row the text prints, plus its parts."""
        before = read_version(self.store, entry.before)
        after = read_version(self.store, entry.after)
        added = removed = None
        if _both_text(before, after):
            added, removed = line_delta(before.data, after.data)
        return {
            "status": entry.status,
            "path": entry.path,
            "before": before.facts(),
            "after": after.facts(),
            "net_change": entry.net_change,
            "added": added,
            "removed": removed,
            "binary": None
            if not (before.is_readable and after.is_readable)
            else before.version.lines is None or after.version.lines is None,
            "operations": list(entry.operations),
            "note": entry.note,
        }

    def _changes_data(self, ticket_id, receipts, net, sections, ticket_section) -> dict:
        """The whole view as fields: the sections, the ticket net, and the pending operations."""
        return {
            "ticket": ticket_id,
            "operations": len(receipts),
            "attempts": sections,
            "ticket_net": None if ticket_section is None else ticket_section["data"]["changes"],
            "pending": [receipt.id for receipt in receipts if receipt.result == "pending"],
        }


# ---------------------------------------------------------------------------
# Row facts, shared by both views
# ---------------------------------------------------------------------------


def _operation_line(receipt: OperationReceipt) -> str:
    """`operation: op-XXXX  kind: write  result: ok  <UTC time>`.

    The timestamp is the stored value, printed as stored: this line is the record itself, and a
    reader comparing it with a `*.file` event in `arbite events` (which prints local time for
    reading) has to be able to see that the two are one fact."""
    return (
        f"operation: {receipt.id}  kind: {receipt.kind}  "
        f"result: {RESULT_WORDS[receipt.result]}  {receipt.recorded_at}"
    )


def _attribution_line(receipt: OperationReceipt) -> str:
    """The ticket, attempt, actor and claim generation, each named when it is known.

    A receipt written without an attempt (an import) prints the fields it has rather than an
    empty `attempt:`, so the line never implies an attribution nobody recorded."""
    parts = []
    if receipt.ticket_id:
        parts.append(f"ticket: {receipt.ticket_id}")
    if receipt.attempt_id:
        parts.append(f"attempt: {receipt.attempt_id}")
    if receipt.actor:
        parts.append(f"actor: {receipt.actor}")
    if receipt.claim_generation:
        parts.append(f"claim generation: {receipt.claim_generation}")
    return "  ".join(parts)


def _evidence_line(receipt: OperationReceipt) -> str:
    """Which image of the operation the receipt holds: `before image stored and retained`.

    The statement a change receipt exists to make is "the version this operation replaced is
    still here", so that is the image the line names -- for a write, an edit, a removal and a
    rename alike (a rename's bytes are one version, named once). An operation that displaced
    nothing names the image it did keep, and one that named no version at all says so. Every
    retained version, and the side each one serves, is in `--json`, because the line is one line
    by design and the JSON does not have to be."""
    if not _named_versions(receipt):
        return NO_EVIDENCE
    side = "before" if any(digest != ABSENT for digest in receipt.before.values()) else "after"
    return f"{side} image stored and retained"


def _header(ticket_id, attempt_id, receipts, actor) -> str:
    """`tic-cf9f · attempt att-91bd (claude.opus.001) · 5 operations`.

    The count is every operation the log holds -- including one that was never applied. A header
    that counted only the operations a row absorbs would answer a smaller question than the one
    it appears to answer."""
    where = f"attempt {attempt_id}" if attempt_id is not None else "no attempt recorded"
    if actor:
        where += f" ({actor})"
    return f"{ticket_id} · {where} · {_operations(len(receipts))}"


def _pending_lines(receipts: list) -> list:
    """One note per operation nobody has judged yet, and nothing for a failure.

    A `pending` receipt may or may not have changed the bytes, so a net view that did not mention
    it would invite a reader to trust a summary it cannot trust. A `failed` receipt provably
    changed nothing: it is counted in the header and listed by `--all`, and a note saying "this
    changed nothing" under a change summary would be noise."""
    lines = []
    for receipt in receipts:
        if receipt.result != "pending":
            continue
        head, tail = PENDING_ROW
        lines.append(
            head.format(operation=receipt.id, kind=receipt.kind, paths=", ".join(receipt.paths))
        )
        lines.append(tail.format(command=f"arbite receipt {receipt.id}"))
    return lines


def _summary_heading(receipts: list, ticket_id: Optional[str]) -> str:
    """The summary's first line: how many operations, over how many tickets and attempts."""
    tickets = {receipt.ticket_id for receipt in receipts if receipt.ticket_id}
    attempts = {receipt.attempt_id for receipt in receipts if receipt.attempt_id}
    counts = {
        "operations": _operations(len(receipts)),
        "tickets": _counted(len(tickets), "ticket"),
        "attempts": _counted(len(attempts), "attempt"),
    }
    if ticket_id is not None:
        return SUMMARY_TICKET_HEADING.format(ticket=ticket_id, **counts)
    return SUMMARY_HEADING.format(**counts)


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summary_rows(receipts: list) -> list:
    """One row per operation-path, columns laid out over every row.

    An operation's own columns are printed on its first row and left blank on the rest, so a
    multi-path operation reads as one entry with several paths rather than as several operations
    sharing an id -- the shape the change views use, and the reason a rename shows both paths
    under one operation."""
    cells = [
        (
            _summary_time(receipt),
            RESULT_WORDS[receipt.result],
            receipt.id,
            receipt.kind,
            _where(receipt),
            receipt.actor or "",
            path,
            _summary_versions(receipt, path),
        )
        for receipt in receipts
        for path in (receipt.paths or [SUMMARY_NO_PATH])
    ]
    widths = [max(len(cell[index]) for cell in cells) + COLUMN_GAP for index in range(7)]
    rows, seen = [], set()
    for cell in cells:
        entry = list(cell)
        if cell[2] in seen:
            entry[:6] = [""] * 6
        seen.add(cell[2])
        rows.append("".join(f"{value:<{width}}" for value, width in zip(entry[:7], widths)) + entry[7])
    return rows


def _summary_time(receipt: OperationReceipt) -> str:
    """When the operation was recorded, local time for reading (the stored value is UTC)."""
    return parse_utc(receipt.recorded_at).astimezone().strftime("%H:%M:%S")


def _where(receipt: OperationReceipt) -> str:
    """The ticket/attempt column, both when both are known (the events view's own shape)."""
    if receipt.ticket_id and receipt.attempt_id:
        return f"{receipt.ticket_id}/{receipt.attempt_id}"
    return receipt.ticket_id or receipt.attempt_id or ""


def _summary_versions(receipt: OperationReceipt, path: str) -> str:
    """Both versions of one path, and what became of them.

    A version that is not there prints `absent` rather than a digest, because that *is* the
    fact (a creation and a removal are what this column makes visible), and an operation nobody
    has judged says so: which way a pending receipt went is not something a summary may claim.
    A row printed for an operation that named no path has no versions to show."""
    if path not in receipt.before:
        return ""
    pair = f"{_summary_side(receipt.before[path])} -> {_summary_side(receipt.after[path])}"
    if receipt.result == "failed":
        return f"{pair} ({DETAIL_FAILED})"
    if receipt.result == "pending":
        return f"{pair} ({DETAIL_PENDING})"
    return pair


def _summary_side(digest: str) -> str:
    return VERSION_ABSENT if digest == ABSENT else short_digest(digest)


def _summary_notes(receipts: list, store) -> list:
    """The lines under the rows: how the work came out, what is unfinished, and what is retained.

    The retention line is the honest replacement for a pruning policy: it names the evidence the
    store is holding -- the fact that it keeps growing -- and says nothing is deleted."""
    results = _summary_result_counts(receipts)
    notes = [
        SUMMARY_RESULTS.format(
            operations=_operations(len(receipts)),
            ok=results["ok"],
            pending=results["pending"],
            failed=results["failed"],
        )
    ]
    if results["pending"]:
        notes.append(SUMMARY_PENDING.format(count=results["pending"]))
    referenced = {digest for receipt in receipts for digest in _named_versions(receipt)}
    artifacts = store.records("artifact")
    notes.append(
        SUMMARY_RETENTION.format(
            referenced=len(referenced),
            artifacts=len(artifacts),
            bytes=human_size(sum(artifact.size for artifact in artifacts)),
        )
    )
    notes.append(SUMMARY_NEVER_PRUNED)
    return notes


def _summary_data(receipts: list, ticket_id: Optional[str], store) -> dict:
    """The same facts as fields, with the full digests the text abbreviates."""
    referenced = {digest for receipt in receipts for digest in _named_versions(receipt)}
    artifacts = store.records("artifact")
    return {
        "ticket": ticket_id,
        "operations": len(receipts),
        "results": _summary_result_counts(receipts),
        "tickets": sorted({receipt.ticket_id for receipt in receipts if receipt.ticket_id}),
        "attempts": sorted({receipt.attempt_id for receipt in receipts if receipt.attempt_id}),
        "receipts": [
            {
                "operation": receipt.id,
                "kind": receipt.kind,
                "result": RESULT_WORDS[receipt.result],
                "stored_result": receipt.result,
                "recorded_at": receipt.recorded_at,
                "ticket": receipt.ticket_id,
                "attempt": receipt.attempt_id,
                "actor": receipt.actor,
                "claim_generation": receipt.claim_generation,
                "paths": [
                    {
                        "path": path,
                        "before": receipt.before[path],
                        "after": receipt.after[path],
                    }
                    for path in receipt.paths
                ],
            }
            for receipt in receipts
        ],
        "evidence": {
            "referenced_versions": sorted(referenced),
            "artifacts": len(artifacts),
            "artifact_bytes": sum(artifact.size for artifact in artifacts),
        },
    }


def _summary_result_counts(receipts: list) -> dict:
    """`{printed word: count}` over the receipt set, always naming all three words."""
    counts = {word: 0 for word in ("ok", "pending", "failed")}
    for receipt in receipts:
        counts[RESULT_WORDS[receipt.result]] += 1
    return counts


def _log_entry(receipt: OperationReceipt) -> dict:
    """One operation as the ordered log reports it, every path and both of its versions."""
    return {
        "operation": receipt.id,
        "kind": receipt.kind,
        "result": RESULT_WORDS[receipt.result],
        "stored_result": receipt.result,
        "recorded_at": receipt.recorded_at,
        "paths": {
            path: {"before": receipt.before[path], "after": receipt.after[path]}
            for path in receipt.paths
        },
    }


def _attempt_groups(receipts: list) -> list:
    """`[(attempt_id, receipts)]` in the order the attempts first appear in the log.

    Receipts with no attempt at all are their own group keyed `None`: they are still operations,
    and dropping them would hide work nobody can attribute."""
    groups, seen = [], {}
    for receipt in receipts:
        key = receipt.attempt_id
        if key not in seen:
            seen[key] = len(groups)
            groups.append((key, []))
        groups[seen[key]][1].append(receipt)
    return groups


def _referenced_versions(receipt: OperationReceipt) -> set:
    """Every version the receipt names, absence included.

    Absence is a version here for the same reason it is one in the record: "this path was not
    there" is a fact the report prints (`before: absent`), and it is the fact that separates a
    creation from a replacement."""
    return {*receipt.before.values(), *receipt.after.values()}


def _named_versions(receipt: OperationReceipt) -> set:
    """Every distinct version the receipt names *with content* behind it."""
    return {digest for digest in _referenced_versions(receipt) if digest != ABSENT}


def _sides_of(receipt: OperationReceipt, digest: str) -> set:
    """The sides a digest serves in this receipt: `before`, `after`, or both.

    A rename is why this is a *set*: the bytes that left one path are the bytes that arrived at
    another, so one stored version is the before image of one path and the after image of
    another -- and an edit-then-revert pair of receipts shares a version the same way."""
    sides = set()
    for path in receipt.paths:
        if receipt.before[path] == digest:
            sides.add("before")
        if receipt.after[path] == digest:
            sides.add("after")
    return sides


def _both_text(before: VersionRead, after: VersionRead) -> bool:
    """Whether a line delta is computable: both versions are present and read as UTF-8 text."""
    return (
        before.is_readable
        and after.is_readable
        and before.version.lines is not None
        and after.version.lines is not None
    )


def _net_status(before: str, after: str) -> str:
    """The letter for one operation's own endpoint pair, read as a one-operation net."""
    if before == after:
        return STATUS_CHANGED
    if before == ABSENT:
        return STATUS_CREATED
    if after == ABSENT:
        return STATUS_REMOVED
    return STATUS_CHANGED


def _is_replacement(receipt: OperationReceipt, path: str) -> bool:
    """Whether an operation-path has two versions to print rather than a created or removed one.

    Everything that is not a creation or a removal is a replacement of some kind: a write, an
    edit, or the destination side of a rename. A receipt that was never applied has no versions
    to compare, so it prints what happened to it instead."""
    if receipt.result != "succeeded":
        return False
    return receipt.before[path] != ABSENT and receipt.after[path] != ABSENT


def _operations(count: int) -> str:
    noun = "operation" if count == 1 else "operations"
    return f"{count} {noun}"
