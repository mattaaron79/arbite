"""The file coordination backend: records as JSON documents beside the tickets.

The layout is deliberately boring, because everything about it has to be
explainable to someone who opens the directory:

```
<store root>/coordination/
  workspace.json          the derived workspace binding (one per store)
  attempts/att-XXXX.json  work attempts
  claims/clm-XXXX.json    active and released file claims
  observations/op-XXXX.json  read receipts (the tokens a write presents)
  receipts/op-XXXX.json   mutation receipts, including pending intent
  events/<cursor>-evt-XXXX.json  the append-only event stream
  artifacts/<digest hex>  the bytes an artifact record describes (bounded by
                          store.MAX_ARTIFACT_BYTES, refused before anything changes)
  artifacts/index/art-XXXX.json  the artifact records themselves
  revisions.json          per-record write counters (see `store.revision`)
  commit-journal.json     a commit in flight; replayed by the next write
  lock                    the ephemeral store mutex, held for one commit
```

Every one of those files is a non-`.md` document in its own reserved directory, so
the ticket scan can never mistake coordination state for a ticket and a stray
record can never be served as one; the file sink skips these directories by name
for the same reason.

What this backend *does* now claim, and what it still does not:

- **Atomic per record, and one commit for a unit of work.** A document is written
  through a temp file and `os.replace`, and a multi-record commit goes through
  `commit_transaction` below: the journal is written first, the documents and
  revision counters are applied, and the journal is removed last -- so the
  removal *is* the commit point, and a process killed at any point leaves either
  nothing or a journal the next write replays. Replaying decides nothing, because
  every write in the journal carries the revision and cursor it must end up at.
- **Serialised across processes, not across ticket durations.** `_exclusive` takes
  an advisory `flock` on `lock` for one commit. The kernel drops it when the
  holder dies, whatever killed it, so process death cannot leave the store locked;
  and no lock is ever held for the length of an agent's work -- durable *claims*
  do that job, and they are records rather than locks.
- **Not isolated reads.** A read takes no lock, so it can see a unit of work
  half-applied: that is why the multi-record guarantee is "commits together, or
  recovers deterministically", not "nobody can observe it mid-commit". A read
  reports an outstanding journal through `record_problems`, and the next write
  finishes it.
- **Not durable against power loss.** The journal is written atomically and
  fsynced, but the containing directory is not, so the guarantee is about a
  process dying (this ticket's acceptance), not about the machine losing power
  mid-rename.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..errors import CoordinationError, RecordError
from ..sinks.base import Problem
from ..sinks.file import TMP_PREFIX, write_atomic
from . import records
from .locking import LOCK_POLL_SECONDS, LOCK_TIMEOUT, StoreLock
from .records import (
    RECEIPT_PENDING,
    Event,
    Record,
    parse_record,
    record_type_of,
    utc_now,
)
from .store import (
    COMMIT_APPLIED,
    COMMIT_STAGED,
    CommitResult,
    CoordinationStore,
    CoordinationTransaction,
    PendingWrite,
    require_storable_artifact,
)

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

#: The directory this backend owns, inside the store root (`.arbite/coordination/`
#: for the default file sink).
COORDINATION_DIRNAME = "coordination"

#: One directory per record type that holds many records. The workspace is a
#: single file at the root instead, because a store belongs to exactly one.
CONTAINER_DIRS = {
    "attempt": "attempts",
    "claim": "claims",
    "observation": "observations",
    "receipt": "receipts",
    "artifact": "artifacts",
    "event": "events",
}

WORKSPACE_FILENAME = "workspace.json"

#: Where the *records* for stored bytes live, inside the artifacts directory --
#: the bytes themselves are named by their digest, one file per distinct version,
#: which is what makes an edit-then-revert keep both versions without duplication.
ARTIFACT_INDEX_DIRNAME = "index"

#: The ephemeral store mutex, taken for one commit (see `_exclusive`). Named here
#: so the lock file the plan lists and the runtime state it protects cannot drift.
LOCK_FILENAME = "lock"

#: The store's per-record revision counters, in one small document. Separate from
#: the records because a record's own document has to stay exactly the record --
#: `to_dict()` is the migration format (tic-008f) -- and because the counters are
#: rewritten in the same commit as the documents they describe.
REVISIONS_FILENAME = "revisions.json"

#: The commit journal: the unit of work, written before it is applied and removed
#: once it has been, so a process killed in between leaves the intent on disk and
#: the next write finishes it. Deliberately *not* the file-operation intent journal
#: tic-b03b owns: this one names the exact documents, revisions and cursors it will
#: store, so replaying it requires no judgement about the workspace's bytes.
JOURNAL_FILENAME = "commit-journal.json"

#: How long a writer waits for the store lock before refusing. Read when the lock is
#: taken (see `coordination.locking`), so it stays the seam a contention test patches.
LOCK_TIMEOUT = 5.0

#: How often the lock is retried while another process holds it.
LOCK_POLL_SECONDS = 0.02

RECORD_SUFFIX = ".json"

#: Event files are named `<zero-padded cursor>-<id>.json`, so a directory listing
#: is already in cursor order and a resumed poll can find its continuation.
EVENT_CURSOR_WIDTH = 8


def event_filename(cursor: int, event_id: str) -> str:
    return f"{int(cursor):0{EVENT_CURSOR_WIDTH}d}-{event_id}{RECORD_SUFFIX}"


def record_filename(record: Record) -> str:
    """The file a record is stored in.

    Events carry their cursor in the name (see `event_filename`); everything else
    is named by its id, which is opaque and therefore stable."""
    if record.RECORD_TYPE == "event":
        return event_filename(record.cursor, record.id)
    return f"{record.id}{RECORD_SUFFIX}"


def artifact_filename(digest: str) -> str:
    """The file the bytes for `digest` live in: the hex digest, no prefix.

    Storing content once by digest is what makes two receipts that share a version
    share the bytes, and verification possible (tic-008f)."""
    text = str(digest)
    return text.split(":", 1)[1] if text.startswith("sha256:") else text


def _try_lock(handle) -> bool:
    """Take this store's exclusive advisory lock on `handle`, or report that
    somebody else holds it.

    Advisory and *ephemeral*: the operating system releases it when the holder
    exits, however it exits, which is what makes "a dead process leaves no permanent
    lock" true here without any staleness heuristic -- the lock file is a rendezvous
    point and never a token, so its existence says nothing at all."""
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    import msvcrt  # pragma: no cover - Windows

    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _release_lock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt  # pragma: no cover - Windows

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def revision_key(record_type: str, record_id: str) -> str:
    """How a record is named in the revision counters: one flat map, with keys a
    human can read if they have to open the file."""
    return f"{record_type}/{record_id}"


def journal_entry(write: PendingWrite) -> dict:
    """One write as the journal stores it: the document itself, plus the absolute
    revision and cursor it must end up at.

    Absolute rather than relative on purpose -- that is what lets a replay run any
    number of times and land on the same state, so finishing an interrupted commit
    is a mechanical act rather than a decision about which version is correct."""
    return {
        "action": "delete" if write.is_delete else "put",
        "record_type": write.record_type,
        "record_id": write.record_id,
        "revision": write.revision,
        "cursor": write.cursor,
        "document": write.document,
    }


def journal_document(writes, operation_id=None) -> str:
    """The journal's own document: a unit of work, ready to be applied."""
    return json.dumps(
        {
            "operation_id": operation_id,
            "written_at": utc_now(),
            "writes": [journal_entry(write) for write in writes],
        },
        indent=2,
        ensure_ascii=False,
    ) + "\n"


class FileCoordinationStore(CoordinationStore):
    """Coordination records as JSON documents under one reserved directory."""

    kind = "file"

    def __init__(self, root):
        self._root = Path(root)
        #: The store's ephemeral mutex, shared with the SQLite backend
        #: (`coordination.locking`): an advisory lock on `lock`, held for one commit or one
        #: file operation. `limits` reads this module's timeouts when the lock is taken,
        #: which is the seam the contention tests patch.
        self._lock = StoreLock(
            self._root / LOCK_FILENAME,
            describe=self._root,
            limits=lambda: (LOCK_TIMEOUT, LOCK_POLL_SECONDS),
        )

    @property
    def root(self) -> str:
        return str(self._root)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def layout_dirs(self) -> list:
        """Every directory this backend keeps its documents in, in creation order.

        Exposed so `arbite init` (through the application layer) and any later
        integrity check create exactly this set instead of restating it."""
        return [
            *(self._root / CONTAINER_DIRS[record_type] for record_type in sorted(CONTAINER_DIRS)),
            self._root / CONTAINER_DIRS["artifact"] / ARTIFACT_INDEX_DIRNAME,
        ]

    def init(self) -> None:
        """Create the layout. Idempotent, and it never removes anything."""
        self._root.mkdir(parents=True, exist_ok=True)
        for directory in self.layout_dirs():
            directory.mkdir(parents=True, exist_ok=True)

    def check_writable_layout(self) -> None:
        """Refuse when something that is not a directory sits in the layout's way.

        A guard rather than a repair: replacing a file the user put at
        `.arbite/coordination` would destroy it, and creating a layout *around* it
        would leave a store that half exists."""
        for path in [self._root, *self.layout_dirs()]:
            if path.exists() and not path.is_dir():
                raise CoordinationError(
                    f"{path} exists and is not a directory, so the coordination layout "
                    "cannot be created there; move it aside and re-run"
                )

    def _container(self, record_type: str) -> Path:
        """The directory holding one record type's documents.

        The workspace is not here -- it is a single file at the root, because a
        store belongs to exactly one workspace -- and `record_type` has already
        been checked against the vocabulary by `CoordinationStore.records`."""
        name = CONTAINER_DIRS[record_type]
        directory = self._root / name
        if record_type == "artifact":
            directory = directory / ARTIFACT_INDEX_DIRNAME
        return directory

    def artifact_path(self, digest: str) -> Path:
        """Where the bytes for `digest` live. The content address is the filename,
        so an artifact record and its bytes cannot drift apart."""
        return self._root / CONTAINER_DIRS["artifact"] / artifact_filename(digest)

    def record_path(self, record: Record) -> Path:
        """The file a record is stored in (and would be written to)."""
        record_type = record_type_of(record)
        if record_type == "workspace":
            return self._root / WORKSPACE_FILENAME
        return self._container(record_type) / record_filename(record)

    # ------------------------------------------------------------------
    # Serialisation, revisions and the commit journal
    # ------------------------------------------------------------------

    @contextmanager
    def _exclusive(self):
        """Hold this store's commit lock for the length of one unit of work.

        Two kinds of mutex exist in this design and they are never conflated: this one is
        *ephemeral*, an advisory OS lock released when the process exits however it exits, and
        it is held for one commit rather than for a ticket's duration. A durable *claim* is the
        other kind, and only a lifecycle command releases it. A caller that has to wait longer
        than `LOCK_TIMEOUT` is refused with `Busy` -- a structured answer, with nothing written,
        rather than a command that hangs.

        The mechanism is `coordination.locking`, because the SQLite backend takes the same lock
        for the same reason; what is here is this store's lock file and its timeouts."""
        with self._lock.hold():
            yield

    def operation_lock(self):
        """The store's own commit mutex, held across a file operation's check and apply.

        Deliberately the *same* lock every commit takes rather than a second one: the
        operation's records and the bytes it changes are then one critical section against
        every other arbite process, and the lock is the ephemeral flock the kernel releases
        when its holder dies -- so a killed operation cannot leave the store locked, and no
        staleness heuristic is needed to notice (see `_exclusive`)."""
        return self._exclusive()

    def read_commit_journal(self) -> dict | None:
        """The journal of a commit that has not been applied in full, or None.

        An unreadable journal is an error naming the file rather than an empty
        answer: it is the one document whose contents decide what the next write
        does, and a store that guessed at it could store a half-applied commit
        twice."""
        path = self._root / JOURNAL_FILENAME
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise CoordinationError(f"could not read the commit journal at {path}: {e}")
        except ValueError as e:
            raise RecordError(
                f"the commit journal at {path} is not valid JSON ({e}); a commit may be "
                "half-applied, so arbite will not guess at it -- inspect that file by "
                "hand (its writes are plain coordination records)"
            )
        if not isinstance(data, dict) or not isinstance(data.get("writes"), list):
            raise RecordError(f"the commit journal at {path} is not a commit journal")
        return data

    def replay_commit_journal(self) -> list:
        """Finish a commit a dead process left behind, returning what it stored.

        Every write path takes the lock and calls this *before* doing anything else,
        which is what keeps a half-applied unit of work from being written on top
        of. It is redo rather than undo and needs no judgement: each entry carries
        the revision and cursor it must end up at, so replaying once and replaying
        twice produce the same store."""
        journal = self.read_commit_journal()
        if journal is None:
            return []
        self._apply_writes(journal["writes"])
        self._clear_commit_journal()
        return [
            revision_key(entry["record_type"], entry["record_id"])
            for entry in journal["writes"]
        ]

    def _clear_commit_journal(self) -> None:
        path = self._root / JOURNAL_FILENAME
        if path.exists():
            path.unlink()

    def _apply_writes(self, entries) -> None:
        """Apply journal entries: the documents first, then the revision counters.

        Documents are individually atomic and independent, so an interruption
        part-way leaves a prefix a replay completes; the counters are one small
        document rewritten last, which is a *lag* a replay repairs rather than
        damage. Every entry's values are absolute, so this is idempotent."""
        revisions = self._read_revisions()
        for entry in entries:
            record_type = entry["record_type"]
            record_id = entry["record_id"]
            key = revision_key(record_type, record_id)
            if entry.get("action") == "delete":
                self._remove_document(record_type, record_id)
                revisions.pop(key, None)
                continue
            document = entry.get("document")
            if not isinstance(document, dict):
                raise RecordError(
                    f"the commit journal entry for {record_type} {record_id} carries no document"
                )
            record = parse_record(document)
            if record_type_of(record) != record_type or record.id != record_id:
                raise RecordError(
                    f"the commit journal entry for {record_type} {record_id} describes "
                    f"{record_type_of(record)} {record.id}"
                )
            self._store_document(record)
            if entry.get("revision") is not None:
                revisions[key] = int(entry["revision"])
        self._write_revisions(revisions)

    def _read_revisions(self) -> dict:
        """The store's revision counters.

        Absent is ordinary rather than an error: a store written before revisions
        existed has no counters, and its records then read as revision 0 -- the same
        answer as a record that is not there, which is what a writer means by "I
        have seen no version of this"."""
        path = self._root / REVISIONS_FILENAME
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise CoordinationError(f"could not read {path}: {e}")
        except ValueError as e:
            raise RecordError(f"{path} is not valid JSON: {e}")
        if not isinstance(data, dict):
            raise RecordError(f"{path} must hold a mapping of record to revision")
        return data

    def _write_revisions(self, revisions: dict) -> None:
        write_atomic(
            json.dumps(revisions, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            self._root / REVISIONS_FILENAME,
        )

    def _revision(self, record_type: str, record_id: str) -> int:
        value = self._read_revisions().get(revision_key(record_type, record_id), 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise RecordError(
                f"the revision recorded for {record_type} {record_id} is {value!r}, which is "
                f"not a whole number ({self._root / REVISIONS_FILENAME})"
            )

    def _next_event_cursor(self) -> int:
        """One past the highest cursor in the event stream.

        Read from the *filenames*, which carry the zero-padded cursor, so this is a
        directory listing rather than a parse of every event: the name is the order,
        and the order is the name (see `event_filename`)."""
        container = self._container("event")
        highest = 0
        if container.is_dir():
            for path in container.glob(f"*{RECORD_SUFFIX}"):
                prefix = path.name.split("-", 1)[0]
                if prefix.isdigit():
                    highest = max(highest, int(prefix))
        return highest + 1

    def _operation_committed(self, operation_id: str) -> bool:
        """Whether this operation id already has a finalised receipt.

        The deduplication rule. A *pending* receipt is an operation that started and
        has not finished, so a retry must run it again -- that reconciliation is
        tic-b03b's, and treating it as done here would lose the write. A succeeded
        or failed receipt is an operation that already happened, so a retry changes
        nothing."""
        receipt = self.find_record("receipt", operation_id)
        return receipt is not None and receipt.result != RECEIPT_PENDING

    def commit_transaction(self, transaction: CoordinationTransaction) -> CommitResult:
        """Commit a unit of work: journal, apply, clear -- with the removal as the
        commit point.

        Under the store lock, so the revisions and cursors this assigns cannot be
        overtaken: a dead predecessor's journal is finished first, then this unit's
        journal is written, then every document is applied, then the journal goes.
        Both crash boundaries are crossed with a hook the durability tests use to
        kill the process there -- and either one leaves a store the next write makes
        whole, because the journal is the recipe."""
        with self._exclusive():
            self.replay_commit_journal()
            if transaction.operation_id and self._operation_committed(transaction.operation_id):
                return CommitResult(applied=False, deduplicated=True)
            writes = transaction.materialise(self._revision, self._next_event_cursor)
            if not writes:
                return CommitResult(applied=True)
            entries = [journal_entry(write) for write in writes]
            write_atomic(
                journal_document(writes, transaction.operation_id),
                self._root / JOURNAL_FILENAME,
            )
            self._crash_point(COMMIT_STAGED)
            self._apply_writes(entries)
            self._crash_point(COMMIT_APPLIED)
            self._clear_commit_journal()
            return CommitResult(
                applied=True,
                events=tuple(
                    write.record for write in writes if isinstance(write.record, Event)
                ),
                revisions={
                    revision_key(write.record_type, write.record_id): write.revision
                    for write in writes
                    if write.revision is not None
                },
            )

    # ------------------------------------------------------------------
    # Storage primitives
    # ------------------------------------------------------------------

    def _documents(self, record_type: str) -> list:
        container = self._container(record_type)
        if not container.is_dir():
            return []
        return sorted(path for path in container.glob(f"*{RECORD_SUFFIX}") if path.is_file())

    def _read_document(self, path: Path, record_type: str) -> Record:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise CoordinationError(f"could not read {path}: {e}")
        except ValueError as e:
            raise RecordError(f"{path} is not a valid JSON coordination record: {e}")
        record = parse_record(data)
        if record_type_of(record) != record_type:
            raise RecordError(
                f"{path} holds a '{record_type_of(record)}' record, but it is filed as a "
                f"'{record_type}'"
            )
        return record

    def _records(self, record_type: str) -> list:
        if record_type == "workspace":
            path = self._root / WORKSPACE_FILENAME
            if not path.is_file():
                return []
            return [self._read_document(path, "workspace")]
        stored = [self._read_document(path, record_type) for path in self._documents(record_type)]
        if record_type == "event":
            # The cursor is the order that matters -- "after this cursor" has to
            # mean something even when a clock went backwards between two writes.
            return sorted(stored, key=lambda event: (event.cursor, event.id))
        return sorted(stored, key=lambda record: record.id)

    def _raw_documents(self, record_type: str) -> list:
        """The stored JSON of one record type, without interpreting it.

        The read a migration needs (see `CoordinationStore.raw_documents`): a document
        written by an older arbite would be refused by `parse_record`, and a copy has to
        start from the bytes. It is the same file set the validating read uses and the
        same order (events by cursor, everything else by id), so two migrations of one
        store write in the same order; what differs is only that nothing is validated
        here, so the caller is the one that decides what a document means."""
        if record_type == "workspace":
            path = self._root / WORKSPACE_FILENAME
            if not path.is_file():
                return []
            document = self._raw_json(path)
            return [(records.document_id(document), document)]
        entries = []
        for path in self._documents(record_type):
            document = self._raw_json(path)
            entries.append((records.document_id(document), document))
        if record_type == "event":
            return sorted(entries, key=lambda entry: (entry[1].get("cursor") or 0, entry[0]))
        return sorted(entries, key=lambda entry: entry[0])

    def _raw_json(self, path: Path) -> dict:
        """One stored document, read but not checked: a file that is not a JSON object is
        an error naming it, because a copy that skipped it would lose a record silently."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise CoordinationError(f"could not read {path}: {e}")
        except ValueError as e:
            raise RecordError(f"{path} is not a valid JSON coordination record: {e}")
        if not isinstance(data, dict):
            raise RecordError(f"{path} does not hold a coordination record (a JSON object)")
        return data

    def _get_record(self, record_type: str, record_id: str) -> Record:
        for record in self._records(record_type):
            if record.id == record_id:
                return record
        raise CoordinationError(
            f"no {record_type} record {record_id} in {self._root}"
        )

    def put_record(self, record: Record) -> None:
        """Store `record`, replacing a record of the same type and id.

        One record, one commit -- and the *same* commit path a multi-record operation
        takes, so there is exactly one way a document is written: a dead
        predecessor's journal is finished, this write is journaled, the document and
        its revised counter are applied, and the journal goes. A caller with several
        records to write opens a transaction rather than looping here."""
        with self.transaction() as txn:
            txn.put_record(record)

    def delete_record(self, record_type: str, record_id: str) -> None:
        """Unlink a record's document and forget its revision counter.

        A record that is not there is not an error: the caller is establishing a
        state, not asserting one. Its counter goes with it, so a later record written
        under the same id starts again at 1 rather than inheriting a count from a
        record that no longer exists."""
        with self.transaction() as txn:
            txn.delete_record(record_type, record_id)

    def _store_document(self, record: Record) -> None:
        """Write a record's document, replacing any copy of the same id.

        Assumes the lock is held (`_exclusive`), because it is the apply step of a
        commit and of a replay: taking the lock here would nest it and rewriting the
        revision here would bump a counter that a replay must restore, not
        increment."""
        record_type = record_type_of(record)
        destination = self.record_path(record)
        # A record whose filename encodes something mutable (an event's cursor) can
        # move when it is rewritten, so the old file goes rather than lingering as a
        # second copy of one id.
        existing = {path for path in self._documents_for(record_type, record.id)}
        write_atomic(_dump(record), destination)
        for path in existing:
            if path != destination and path.is_file():
                path.unlink()

    def _remove_document(self, record_type: str, record_id: str) -> None:
        """Unlink a record's document, tolerating one that is not there."""
        for path in self._documents_for(record_type, record_id):
            path.unlink()

    def _documents_for(self, record_type: str, record_id: str) -> list:
        if record_type == "workspace":
            return []
        container = self._container(record_type)
        if not container.is_dir():
            return []
        return [
            path
            for path in container.glob(f"*{RECORD_SUFFIX}")
            if path.is_file() and (path.stem == record_id or path.stem.endswith(f"-{record_id}"))
        ]

    def storage_problems(self, fix: bool = False) -> list:
        """The findings only this backend's own documents can have.

        Two, both about files this backend keeps *beside* the records: a commit journal
        a dead process left behind, and revision counters naming a record that is no
        longer there. Neither is a record's meaning, which is why both live here rather
        than in the shared findings stream.

        The journal is reported and never replayed by a repair: the next *write* finishes
        it (which decides nothing -- the journal names the documents and their absolute
        revisions), and until then a reader is told that a commit is outstanding instead
        of being shown a half-applied unit of work as if it were finished. A counter
        naming nothing is unambiguous, so `fix` drops it -- the counter is this backend's
        own bookkeeping and says nothing about any record."""
        problems = []
        journal = self.read_commit_journal()
        if journal is not None:
            staged = ", ".join(
                f"{entry['record_type']} {entry['record_id']}" for entry in journal["writes"]
            )
            problems.append(
                Problem(
                    "pending_commit",
                    f"a commit staged {len(journal['writes'])} write(s) ({staged}) and did not "
                    "finish; the next write to this store replays it",
                )
            )
        orphans = self.orphan_revision_counters()
        if orphans:
            dropped = self._drop_revision_counters(orphans) if fix else False
            problems.append(
                Problem(
                    "orphan_revision_counters",
                    f"{len(orphans)} revision counter(s) name a record this store does not "
                    f"have ({', '.join(orphans)})"
                    + (
                        " -- dropped, the counters are this store's own bookkeeping"
                        if dropped
                        else " (re-run with --fix to drop them)"
                    ),
                    fixed=dropped,
                )
            )
        return problems

    def orphan_revision_counters(self) -> list:
        """The `type/id` keys in the revision counters whose record is not in the store.

        Read as text rather than through the records: that is what makes it a finding
        about *this backend's* bookkeeping rather than about a record. A counter whose
        record cannot be looked for -- an unknown type, a document that will not parse --
        is left alone, because "I could not tell" is not "it is not there"."""
        orphans = []
        for key in sorted(self._read_revisions()):
            record_type, _, record_id = key.partition("/")
            if record_type not in CONTAINER_DIRS and record_type != "workspace":
                continue
            try:
                present = self._document_present(record_type, record_id)
            except RecordError:
                continue
            if not present:
                orphans.append(key)
        return orphans

    def _document_present(self, record_type: str, record_id: str) -> bool:
        """Whether the document a counter names is on disk under the id the counter uses."""
        if record_type == "workspace":
            path = self._root / WORKSPACE_FILENAME
            if not path.is_file():
                return False
            return self._raw_json(path).get("id") == record_id
        return bool(self._documents_for(record_type, record_id))

    def _drop_revision_counters(self, keys) -> bool:
        """Forget counters for records that are not there, under the store's own lock."""
        with self._exclusive():
            revisions = self._read_revisions()
            for key in keys:
                revisions.pop(key, None)
            self._write_revisions(revisions)
        return True

    def counts(self) -> dict:
        """The totals the report needs, derived from the records themselves.

        Read strictly: a document that cannot be parsed is an error naming the
        file, not a record quietly omitted from a count. Tolerating it here would
        make a store look emptier than it is, which is the one failure mode this
        whole design is arranged to avoid -- reporting it as a finding is
        `record_problems` plus the recovery slice's job (tic-b03b)."""
        claims = self.records("claim")
        attempts = self.records("attempt")
        receipts = self.records("receipt")
        return {
            "claims_active": sum(1 for claim in claims if claim.is_active),
            "claims_released": sum(1 for claim in claims if not claim.is_active),
            "attempts_active": sum(1 for attempt in attempts if attempt.is_active),
            "events": len(self.records("event")),
            "receipts": len(receipts),
            "pending_operations": sum(1 for receipt in receipts if receipt.is_pending),
            "artifacts": len(self.records("artifact")),
        }

    def put_artifact_bytes(self, digest: str, data: bytes) -> Path:
        """Store `data` as the content for `digest`, if it is not stored already.

        The bytes file *is* the content address, so an artifact record and its
        content cannot drift apart, and a crash leaves a visible temp file rather
        than half an artifact. The size limit (`store.MAX_ARTIFACT_BYTES`) is checked
        first and in the same words on both backends, so "may this version be recorded"
        is a property of the proxy rather than of the sink holding it."""
        require_storable_artifact(digest, data)
        path = self.artifact_path(digest)
        if path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        return path

    def get_artifact_bytes(self, digest: str) -> bytes:
        """The stored bytes for `digest`. Missing bytes are reported, never read
        back as an empty file: an artifact record whose content is gone is drift."""
        path = self.artifact_path(digest)
        if not path.is_file():
            raise CoordinationError(f"artifact {digest} has no stored bytes at {path}")
        return path.read_bytes()


def _dump(record: Record) -> str:
    """A record as the JSON document it is stored as.

    One serialisation for both backends: the file backend writes these bytes and
    SQLite stores these bytes, so a migration between them is field-for-field
    exact and there is only one thing to keep versioned."""
    return json.dumps(record.to_dict(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"
