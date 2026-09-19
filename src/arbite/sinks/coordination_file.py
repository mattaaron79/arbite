"""File coordination storage: a coarse process lock plus a write-ahead journal.

This is the file half of the C02 storage layer, and it is where the interesting
guarantee lives. Tickets are markdown files, so there is no transaction manager to
lean on: related coordination state and its events have to commit *together* or be
recoverable, using nothing but a filesystem. Two mechanisms do that:

- **A coarse POSIX `flock` lock** (`lock`) serializes every coordination
  transaction. `flock` is released by the operating system when the holding
  process dies, so a crash can never leave a permanent runtime lock -- which is
  exactly how the "process death must not leave a lock" requirement is met
  without inventing any agent-staleness policy. The wait is bounded (a few
  seconds of small sleeps) and then reports a retryable conflict; it is never
  held for an agent's whole ticket duration, only for one transaction.
- **A write-ahead journal** (`journal/<op_id>.json`) records a resolved intent --
  the record writes with their target revisions and the events with their
  pre-allocated cursors -- *before* anything is applied. Commit is: write+fsync the
  journal, apply the record files, apply the event files, remove the journal. A
  crash mid-apply leaves the journal, and the *next* transaction (read or write)
  replays it deterministically forward: an intent is applied only when the stored
  revision is exactly one less than the intent's, so re-application is idempotent
  and can never overwrite a newer record. Cursors are allocated under the lock and
  recorded in the journal, so monotonicity and uniqueness survive replay.

Layout under `.arbite/coordination/`:

```
lock                          the coarse serialization lock
records/<kind>/<id>.json      record envelopes (record + store-local revision)
events/<zero-padded cursor>.json   append-only events
events_by_operation/<op_id>.json   O(1) operation-id dedup index
bindings/<workspace_id>.json  the workspace's authoritative store binding
journal/<op_id>.json          write-ahead intent for one transaction
```

Every file write is atomic (temp file + `fsync` + `os.replace`) so a reader never
sees half a record, and every transaction replays leftover journals while holding
the lock, so a reader never sees half a *transaction* either.

This module imports no database: the file sink must not secretly require SQLite.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .. import coordination
from ..artifacts import (
    DIGEST_PREFIX,
    artifact_id_for_digest,
    check_capacity,
    digest_of_bytes,
    verify_artifact,
)
from ..coordination_storage import (
    COORDINATION_TMP_PREFIX,
    CRASH_AFTER_EVENTS,
    CRASH_AFTER_JOURNAL,
    CRASH_AFTER_RECORDS,
    check_revision,
    decode_event,
    decode_journal,
    decode_namespace,
    decode_record,
    encode_event,
    encode_record,
    journal_intent,
    namespace_record,
    safe_component,
)
from ..errors import (
    CoordinationConflict,
    InvalidRecord,
    SinkError,
    StoreBindingConflict,
)
from ..workspace import MARKER_FILENAME
from .base import CoordinationStore, CoordinationTransaction

#: Directory under the arbite root that holds all coordination state. The ticket
#: scan never reads it: it contains no `.md` files, so a ticket store and its
#: coordination state share one directory without either mistaking the other.
DIRECTORY_NAME = "coordination"

LOCK_FILENAME = "lock"
#: The coarse *operation* lock (planning key C05), deliberately a different file
#: from `LOCK_FILENAME`: the record journal takes that one per transaction, so a
#: mutation that holds the operation lock across intent -> apply -> receipt can
#: still open store transactions without self-deadlocking.
OPERATION_LOCK_FILENAME = "operation.lock"
RECORDS_DIR = "records"
EVENTS_DIR = "events"
EVENTS_BY_OPERATION_DIR = "events_by_operation"
BINDINGS_DIR = "bindings"
JOURNAL_DIR = "journal"
#: Content-addressed artifact blobs, one file per `sha256:<hex>` digest. Retained
#: indefinitely: there is no garbage collection.
ARTIFACTS_DIR = "artifacts"
#: Namespace-registry entries (planning key C11), one JSON file per imported
#: namespace. The filename is a sanitised, hash-suffixed form of the namespace;
#: the full namespace lives inside the file so it round-trips exactly.
NAMESPACES_DIR = "namespaces"

LAYOUT_DIRS = (
    RECORDS_DIR,
    EVENTS_DIR,
    EVENTS_BY_OPERATION_DIR,
    BINDINGS_DIR,
    JOURNAL_DIR,
    ARTIFACTS_DIR,
    NAMESPACES_DIR,
)

#: Maximum length of the human-readable part of a namespace filename, before the
#: short sha256 suffix that keeps two colliding sanitised names distinct.
NAMESPACE_SLUG_MAX = 64

#: Default bound on how long a transaction waits for the coarse lock. Small
#: enough that a one-shot command stays prompt, long enough that ordinary
#: contention between two agents resolves instead of failing.
DEFAULT_TIMEOUT = 5.0

#: Sleep between lock attempts. Deliberately tiny: the wait is bounded by
#: `timeout`, and a busy-wait loop with a long sleep would make the bound coarse.
LOCK_POLL_SECONDS = 0.01


class _Lock:
    """One held `flock`, released idempotently."""

    def __init__(self, fd: int, path: Path):
        self._fd = fd
        self._path = path

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - fd already unusable
            pass
        try:
            os.close(self._fd)
        except OSError:  # pragma: no cover
            pass
        self._fd = None

    def __enter__(self) -> "_Lock":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


class FileCoordinationTransaction(CoordinationTransaction):
    """One journaled unit of work, holding the coarse lock for its whole duration.

    Writes are buffered; `commit` journals and applies them; `rollback` discards
    them and leaves prior state exactly as it was. Reads see this transaction's
    own buffered writes before storage.
    """

    def __init__(self, store: "FileCoordinationStore", write: bool, lock: _Lock):
        self.store = store
        self._write = write
        self._lock = lock
        self._records: dict = {}
        self._events: list = []
        self._finished = False

    # -- helpers ----------------------------------------------------------

    def _require_write(self, what: str) -> None:
        if not self._write:
            raise InvalidRecord(
                f"this coordination transaction is read-only; {what} is not allowed"
            )

    def _event_by_id(self, event_id: str):
        for event in self._events:
            if event.id == event_id:
                return event
        return self.store._read_event_by_id(event_id)

    def _event_for_operation(self, operation_id: str):
        for event in self._events:
            if event.operation_id == operation_id:
                return event
        index = self.store._read_operation_index(operation_id)
        if index is None:
            return None
        return self.store._read_event(int(index["cursor"]))

    # -- the CoordinationTransaction surface ------------------------------

    def put(self, record, *, expect_revision: Optional[int] = None):
        self._require_write("put")
        problems = record.validate()
        if problems:
            raise InvalidRecord(
                f"invalid {record.kind}: {'; '.join(problems)}",
                details={
                    "kind": record.kind,
                    "record_id": record.record_id,
                    "problems": list(problems),
                },
            )
        current = self.revision_of(record.kind, record.record_id)
        check_revision(record.kind, record.record_id, expect_revision, current)
        self._records[(record.kind, record.record_id)] = (record, current + 1)
        return record

    def get(self, kind: str, record_id: str):
        if kind == "event":
            return self._event_by_id(record_id)
        pending = self._records.get((kind, record_id))
        if pending is not None:
            return pending[0]
        found = self.store._read_record(kind, record_id)
        return None if found is None else found[0]

    def find(self, kind: str, **fields) -> list:
        found: dict = {}
        if kind == "event":
            for event in self.store._all_events():
                found[event.id] = event
            for event in self._events:
                found[event.id] = event
        else:
            for record, _revision in self.store._all_records(kind):
                found[record.record_id] = record
            for (rec_kind, record_id), (record, _revision) in self._records.items():
                if rec_kind == kind:
                    found[record_id] = record
        matches = [
            record
            for record in found.values()
            if all(getattr(record, name, None) == value for name, value in fields.items())
        ]
        if kind == "event":
            return sorted(matches, key=lambda e: (e.cursor is None, e.cursor or 0))
        return matches

    def revision_of(self, kind: str, record_id: str) -> int:
        if kind == "event":
            return 0
        pending = self._records.get((kind, record_id))
        if pending is not None:
            return int(pending[1])
        try:
            found = self.store._read_record(kind, record_id)
        except (InvalidRecord, SinkError):
            # A stored envelope that cannot be decoded has no usable revision;
            # treating it as absent (0) lets an explicit repair (a legacy upgrade)
            # proceed rather than aborting the transaction.
            return 0
        return 0 if found is None else int(found[1])

    def append_event(self, event):
        """Buffer an event, assigning its cursor and deduplicating by id/operation id.

        The cursor is assigned here (from `1 + max(existing)`) and recorded in the
        journal at commit. This transaction holds the coarse lock from the moment
        it opened until it commits or rolls back, so no other process can pick the
        same number; recording it in the journal means a replay reuses it rather
        than re-minting one. A rolled-back transaction can therefore leave a gap in
        the cursor sequence -- monotonic and unique, not necessarily gapless.
        """
        self._require_write("append_event")
        existing = self._event_by_id(event.id)
        if existing is not None:
            return existing
        if event.operation_id:
            existing = self._event_for_operation(event.operation_id)
            if existing is not None:
                return existing
        problems = event.validate()
        if problems:
            raise InvalidRecord(
                f"invalid event: {'; '.join(problems)}", details={"problems": list(problems)}
            )
        if event.cursor is None:
            event.cursor = self._next_cursor()
        self._events.append(event)
        return event

    def _next_cursor(self) -> int:
        highest = self.store._highest_cursor()
        for event in self._events:
            if event.cursor is not None:
                highest = max(highest, event.cursor)
        return highest + 1

    def commit(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if self._records or self._events:
                self.store._commit_transaction(self)
        finally:
            self._lock.release()

    def rollback(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._records.clear()
        self._events.clear()
        self._lock.release()

    def __del__(self):  # pragma: no cover - safety net for a leaked transaction
        try:
            self.rollback()
        except Exception:
            pass


class FileCoordinationStore(CoordinationStore):
    """The coordination store for the file sink.

    Construct with the directory to use (the file sink uses
    `<arbite root>/coordination`). The layout is created lazily on first use, so
    merely obtaining the store -- or reading a legacy ticket store -- touches
    nothing on disk.
    """

    def __init__(
        self,
        root: Path,
        timeout: float = DEFAULT_TIMEOUT,
        crash_point: Optional[str] = None,
    ):
        self._root = Path(root)
        self._timeout = timeout
        #: Crash-injection hook (documented in `coordination_storage`): when it
        #: names a commit phase, the process exits at that boundary. Never set
        #: outside a crash-injection test.
        self._crash_point = crash_point
        #: Drift found while replaying a journal, reported to the next
        #: `recover_pending` call *in this process*. Process-local and therefore
        #: best-effort; the durable guarantee is that a drifted record is never
        #: overwritten (see `_apply_journal`).
        self._drift_reports: list = []

    @property
    def root(self) -> str:
        return str(self._root)

    # ------------------------------------------------------------------
    # Layout and locking
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create the coordination layout. Idempotent, and creates nothing else."""
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        try:
            for name in LAYOUT_DIRS:
                (self._root / name).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SinkError(f"could not create coordination storage at {self._root}: {e}")

    def _acquire(self, what: str) -> _Lock:
        """Take the coarse lock, waiting at most `timeout` seconds.

        `flock` is deliberately the mechanism: the kernel drops it when the
        holding process exits for any reason, so it cannot persist after a crash.
        A bounded wait then a structured, retryable refusal is the whole contention
        policy -- nothing sleeps indefinitely and no timer runs in the background.
        """
        self._ensure_layout()
        try:
            fd = os.open(str(self._root / LOCK_FILENAME), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            raise SinkError(f"could not open the coordination lock: {e}")
        deadline = time.monotonic() + max(0.0, float(self._timeout))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _Lock(fd, self._root / LOCK_FILENAME)
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise CoordinationConflict(
                        f"another process holds the coordination lock while starting "
                        f"{what}; retry shortly (a dead process cannot leave this lock "
                        "held -- the operating system releases it)",
                        details={
                            "lock": str(self._root / LOCK_FILENAME),
                            "timeout_seconds": self._timeout,
                        },
                    )
                time.sleep(LOCK_POLL_SECONDS)

    @contextmanager
    def coarse_lock(self, *, replay: bool = False):
        """Hold the coarse lock without opening a transaction.

        Used by the ticket sink, which needs the *same* lock the record store uses
        so a revision-checked ticket update is atomic against coordination
        transactions too. It creates no journal of its own.
        """
        lock = self._acquire("a coordination or ticket operation")
        try:
            if replay:
                self._replay_journals()
            yield lock
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # Atomic files
    # ------------------------------------------------------------------

    def _write_json(self, path: Path, payload) -> None:
        """Write `payload` to `path` atomically, durably, and replace-in-place.

        Adapted from `sinks.file.write_atomic` (temp file in the same directory,
        `fsync`, `os.replace`), with a coordination-specific temp prefix so a
        crash artifact here is not reported as a stranded *ticket* write. The
        containing directory is fsynced too, so the rename itself is durable.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=COORDINATION_TMP_PREFIX, dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:  # pragma: no cover - platform without dir fsync
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    def _read_json(self, path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SinkError(f"coordination storage at {path} is unreadable: {e}")

    def _write_bytes(self, path: Path, data: bytes) -> None:
        """Write `data` to `path` atomically and durably (temp + fsync + replace).

        Content-addressed artifact blobs use this exactly as record envelopes use
        `_write_json`: a reader never sees a half-written blob, and the write is
        either durable or absent.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=COORDINATION_TMP_PREFIX, dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        self._fsync_dir(path.parent)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _record_path(self, kind: str, record_id: str) -> Path:
        return (
            self._root
            / RECORDS_DIR
            / safe_component(kind, what="record kind")
            / f"{safe_component(record_id, what='record id')}.json"
        )

    def _event_path(self, cursor) -> Path:
        return self._root / EVENTS_DIR / f"{int(cursor):012d}.json"

    def _operation_index_path(self, operation_id: str) -> Path:
        return self._root / EVENTS_BY_OPERATION_DIR / (
            f"{safe_component(operation_id, what='operation id')}.json"
        )

    def _binding_path(self, workspace_id: str) -> Path:
        return self._root / BINDINGS_DIR / (
            f"{safe_component(workspace_id, what='workspace id')}.json"
        )

    def _journal_path(self, operation_id: str) -> Path:
        return self._root / JOURNAL_DIR / (
            f"{safe_component(operation_id, what='journal operation id')}.json"
        )

    def _journal_paths(self) -> list:
        directory = self._root / JOURNAL_DIR
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.json"))

    def _artifact_path(self, digest: str) -> Path:
        return self._root / ARTIFACTS_DIR / digest[len(DIGEST_PREFIX):]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _read_record(self, kind: str, record_id: str):
        payload = self._read_json(self._record_path(kind, record_id))
        return None if payload is None else decode_record(payload)

    def _all_records(self, kind: str) -> list:
        directory = self._root / RECORDS_DIR / safe_component(kind, what="record kind")
        if not directory.is_dir():
            return []
        out = []
        for path in sorted(directory.glob("*.json")):
            payload = self._read_json(path)
            if payload is not None:
                out.append(decode_record(payload))
        return out

    def _read_event(self, cursor: int):
        payload = self._read_json(self._event_path(cursor))
        return None if payload is None else decode_event(payload)

    def _all_events(self) -> list:
        directory = self._root / EVENTS_DIR
        if not directory.is_dir():
            return []
        out = []
        for path in sorted(directory.glob("*.json")):
            payload = self._read_json(path)
            if payload is not None:
                out.append(decode_event(payload))
        return sorted(out, key=lambda e: e.cursor)

    def _read_event_by_id(self, event_id: str):
        # The operation index covers the retry path; a plain id lookup has to scan,
        # which is bounded by the store's append-only event count and is the one
        # index this layout deliberately does not keep.
        for event in self._all_events():
            if event.id == event_id:
                return event
        return None

    def _read_operation_index(self, operation_id: str):
        return self._read_json(self._operation_index_path(operation_id))

    def _read_binding(self, workspace_id: str):
        payload = self._read_json(self._binding_path(workspace_id))
        return None if payload is None else decode_record(payload)[0]

    # ------------------------------------------------------------------
    # Journal protocol
    # ------------------------------------------------------------------

    def _commit_transaction(self, tx: FileCoordinationTransaction) -> None:
        """Journal the resolved intent, apply it, then remove the journal."""
        workspace_ids = set()
        records = []
        for (kind, record_id), (record, revision) in tx._records.items():
            records.append(
                {
                    "kind": kind,
                    "record_id": record_id,
                    "revision": revision,
                    "payload": encode_record(record, revision),
                }
            )
            workspace_id = getattr(record, "workspace_id", None)
            if workspace_id:
                workspace_ids.add(str(workspace_id))

        events = []
        for event in tx._events:
            events.append(
                {"cursor": event.cursor, "payload": encode_event(event, event.cursor)}
            )

        operation_id = f"txn-{uuid.uuid4().hex[:16]}"
        intent = journal_intent(
            operation_id,
            workspace_ids=sorted(workspace_ids),
            records=records,
            events=events,
        )
        path = self._journal_path(operation_id)
        # (a) persist the intent before anything is applied -- this is the whole
        #     point of the journal: a crash from here on is recoverable.
        self._write_json(path, intent)
        self._crash(CRASH_AFTER_JOURNAL)
        # (b) apply record files, then event files, each atomically.
        self._apply_journal(intent)
        # (c) the transaction is complete; the journal is no longer the truth.
        try:
            path.unlink()
        except FileNotFoundError:  # pragma: no cover - already replayed
            pass

    def _highest_cursor(self) -> int:
        """The highest cursor already stored, 0 when the event log is empty.

        The basis for allocation: the allocating transaction holds the coarse
        lock, so `highest + 1` cannot be chosen by anyone else, and the value is
        recorded in the journal so a replay reuses it instead of re-minting one.
        """
        highest = 0
        directory = self._root / EVENTS_DIR
        if directory.is_dir():
            for path in directory.glob("*.json"):
                try:
                    highest = max(highest, int(path.stem))
                except ValueError:
                    continue
        return highest

    def _apply_journal(self, intent: dict) -> None:
        """Re-apply a journal intent. Idempotent, and never overwrites newer state.

        A record intent is applied only when the stored revision is exactly one
        less than the intent's: that is the state a not-yet-applied write leaves,
        so applying it completes the transaction; re-applying it sees `revision ==
        target` and skips; a stored revision that is *ahead* is somebody else's
        newer write and is reported as drift rather than clobbered.
        """
        for entry in intent["records"]:
            kind = safe_component(entry.get("kind"), what="record kind")
            record_id = safe_component(entry.get("record_id"), what="record id")
            target = int(entry["revision"])
            current = self._stored_revision(kind, record_id)
            if current == target:
                continue
            if current == target - 1:
                decode_record(entry["payload"])  # refuse to write a corrupt envelope
                self._write_json(self._record_path(kind, record_id), entry["payload"])
                continue
            self._report_drift(intent, kind, record_id, current, target)

        self._crash(CRASH_AFTER_RECORDS)

        for entry in intent["events"]:
            cursor = int(entry["cursor"])
            path = self._event_path(cursor)
            event = decode_event(entry["payload"])
            if not path.exists():
                self._write_json(path, entry["payload"])
            if event.operation_id and not self._operation_index_path(event.operation_id).exists():
                self._write_json(
                    self._operation_index_path(event.operation_id),
                    {
                        "schema_version": 1,
                        "cursor": event.cursor,
                        "event_id": event.id,
                    },
                )

        self._crash(CRASH_AFTER_EVENTS)

    def _stored_revision(self, kind: str, record_id: str) -> int:
        try:
            found = self._read_record(kind, record_id)
        except (InvalidRecord, SinkError):
            # An envelope that cannot be decoded has no usable revision; treat it as
            # absent so a legacy upgrade's journal replays instead of aborting.
            return 0
        return 0 if found is None else int(found[1])

    def _report_drift(self, intent: dict, kind: str, record_id: str, current: int, target: int) -> None:
        self._drift_reports.append(
            coordination.RecoveryReport(
                workspace_id=(intent.get("workspace_ids") or [""])[0],
                operation_id=intent["operation_id"],
                state="drifted",
                observed_at=coordination.utc_now(),
                detail=(
                    f"{kind} {record_id} is at revision {current}, ahead of the "
                    f"journal's {target}; the newer record was preserved and the "
                    "intent was not re-applied"
                ),
            )
        )

    def _replay_journals(self) -> None:
        """Apply every leftover journal forward, then remove it.

        Called at the start of every transaction (read or write) while the coarse
        lock is held, so no reader can observe a half-applied transaction. A
        journal that cannot be decoded is *preserved*, never guessed at, so
        `recover_pending` can still report it.
        """
        for path in self._journal_paths():
            try:
                intent = decode_journal(self._read_json(path))
            except (InvalidRecord, SinkError):
                continue
            self._apply_journal(intent)
            try:
                path.unlink()
            except FileNotFoundError:  # pragma: no cover
                pass

    def _crash(self, phase: str) -> None:
        if self._crash_point == phase:
            os._exit(97)

    # ------------------------------------------------------------------
    # The CoordinationStore surface
    # ------------------------------------------------------------------

    def transaction(self, write: bool = True) -> FileCoordinationTransaction:
        """Open a serialized transaction, replaying any leftover journal first."""
        lock = self._acquire("a coordination transaction")
        try:
            self._replay_journals()
        except BaseException:
            lock.release()
            raise
        return FileCoordinationTransaction(self, write, lock)

    def bind_store(self, binding):
        problems = binding.validate()
        if problems:
            raise InvalidRecord(
                f"invalid store binding: {'; '.join(problems)}",
                details={"workspace_id": binding.workspace_id, "problems": list(problems)},
            )
        with self.coarse_lock(replay=True):
            existing = self._read_binding(binding.workspace_id)
            if existing is not None:
                if existing.matches_binding(binding):
                    return existing
                raise StoreBindingConflict(
                    f"workspace {binding.workspace_id} is already bound to "
                    f"{existing.sink_kind}:{existing.location}; refusing to re-bind to "
                    f"{binding.sink_kind}:{binding.location}",
                    details={
                        "workspace_id": binding.workspace_id,
                        "bound": {"sink_kind": existing.sink_kind, "location": existing.location},
                        "requested": {
                            "sink_kind": binding.sink_kind,
                            "location": binding.location,
                        },
                    },
                )
            self._write_json(self._binding_path(binding.workspace_id), encode_record(binding, 1))
            return binding

    def store_binding(self, workspace_id: str):
        with self.coarse_lock(replay=True):
            return self._read_binding(workspace_id)

    # ------------------------------------------------------------------
    # Cursor namespace and the namespace registry (planning key C11)
    # ------------------------------------------------------------------

    def cursor_namespace(self) -> str:
        """`file:<realpath of the coordination directory>` -- creates nothing."""
        return "file:" + os.path.realpath(self._root)

    def is_initialised(self) -> bool:
        """True when the coordination layout exists; creates nothing.

        A directory that exists but is missing some layout subdirectories is still
        "initialised": a partially-populated store is one `init()` away from
        complete, and the probe must never be the thing that completes it.
        """
        return self._root.is_dir() and any(
            (self._root / name).exists() for name in LAYOUT_DIRS
        )

    @staticmethod
    def _namespace_filename(namespace: str) -> str:
        """A deterministic, traversal-proof filename for `namespace`.

        Every character outside `[A-Za-z0-9._-]` becomes `_` and the result is
        truncated, which keeps the name readable; a short sha256 suffix of the
        *original* namespace then makes the mapping injective, so two namespaces
        that sanitise to the same slug still get distinct files. The suffix also
        means the result is never `.`/`..`, so a namespace cannot address a path
        outside `namespaces/`.
        """
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", namespace)[:NAMESPACE_SLUG_MAX].lstrip(".")
        if not slug:
            slug = "_"
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12]
        return f"{slug}-{digest}.json"

    def _namespace_path(self, namespace: str) -> Path:
        return self._root / NAMESPACES_DIR / self._namespace_filename(namespace)

    def _read_namespace(self, namespace: str):
        payload = self._read_json(self._namespace_path(namespace))
        return None if payload is None else decode_namespace(payload)

    def namespaces(self) -> list:
        """Every retained namespace entry, sorted by namespace. Creates nothing."""
        directory = self._root / NAMESPACES_DIR
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.glob("*.json")):
            payload = self._read_json(path)
            if payload is not None:
                found.append(decode_namespace(payload))
        return sorted(found, key=lambda entry: entry["namespace"])

    def record_namespace(
        self,
        namespace: str,
        *,
        imported_at=None,
        event_count: int = 0,
        cursor_map=None,
        source_contract_version=None,
    ) -> dict:
        """Upsert one namespace entry under the coarse lock, atomically.

        A repeated import for the same namespace unions the cursor maps (the new
        destination wins for a shared source cursor), adds to `event_count`, and
        takes the new `imported_at`/`source_contract_version`. The read-then-write
        happens under the same coarse lock the record transactions use, so two
        processes cannot interleave and lose one side's import.
        """
        entry = namespace_record(
            namespace,
            imported_at=imported_at or coordination.utc_now(),
            event_count=event_count,
            cursor_map={} if cursor_map is None else cursor_map,
            source_contract_version=1
            if source_contract_version is None
            else source_contract_version,
        )
        with self.coarse_lock():
            existing = self._read_namespace(namespace)
            if existing is not None:
                merged_cursor_map = dict(existing["cursor_map"])
                merged_cursor_map.update(entry["cursor_map"])
                entry = namespace_record(
                    namespace,
                    imported_at=entry["imported_at"],
                    event_count=existing["event_count"] + entry["event_count"],
                    cursor_map=merged_cursor_map,
                    source_contract_version=entry["source_contract_version"],
                )
            self._write_json(self._namespace_path(namespace), entry)
        return entry

    # ------------------------------------------------------------------
    # Content-addressed artifacts and the operation lock (planning key C05)
    # ------------------------------------------------------------------

    def operation_lock_path(self):
        """The coarse operation-lock file (distinct from the record-journal lock)."""
        return str(self._root / OPERATION_LOCK_FILENAME)

    def store_artifact_bytes(self, data: bytes, *, media_type: str = "application/octet-stream"):
        """Store `data` once, keyed by its digest, and return its descriptor."""
        check_capacity(data, self.artifact_limit())
        digest = digest_of_bytes(data)
        path = self._artifact_path(digest)
        if path.exists():
            # Content addressing means the stored blob must already be these bytes;
            # verify rather than assume, so a corrupt blob is never silently reused.
            verify_artifact(path.read_bytes(), expected_digest=digest, expected_size=len(data))
        else:
            self._write_bytes(path, data)
        return coordination.Artifact(
            id=artifact_id_for_digest(digest),
            digest=digest,
            size=len(data),
            created=coordination.utc_now(),
            location=f"{ARTIFACTS_DIR}/{digest[len(DIGEST_PREFIX):]}",
            media_type=media_type,
        )

    def read_artifact_bytes(self, digest: str):
        """Stored bytes for `digest`, verified, or None when absent."""
        path = self._artifact_path(digest)
        if not path.exists():
            return None
        data = path.read_bytes()
        verify_artifact(data, expected_digest=digest, expected_size=len(data))
        return data

    def has_artifact(self, digest: str) -> bool:
        return self._artifact_path(digest).exists()

    def recover_pending(self, workspace_id: str) -> list:
        """Inspect (never repair) incomplete store-local operations.

        A leftover journal means a transaction whose intent was written but not
        fully applied: it is reported as `pending`, and the *next* transaction
        replays it. Journals that name no workspace are reported to every caller
        rather than hidden, because "unattributed" is a finding, not a reason to
        stay silent. Drift detected by a replay in this process is added as a
        `drifted` report. Nothing here touches workspace bytes and nothing runs on
        a timer.
        """
        reports: list = []
        with self.coarse_lock():
            for path in self._journal_paths():
                try:
                    intent = decode_journal(self._read_json(path))
                except (InvalidRecord, SinkError):
                    reports.append(
                        coordination.RecoveryReport(
                            workspace_id=workspace_id,
                            operation_id=path.stem,
                            state="unknown",
                            observed_at=coordination.utc_now(),
                            detail=(
                                "a coordination journal is present but could not be "
                                "decoded; it is preserved rather than guessed at"
                            ),
                        )
                    )
                    continue
                ids = list(intent.get("workspace_ids") or [])
                pending_detail = (
                    f"{len(intent['records'])} record write(s) and "
                    f"{len(intent['events'])} event(s) journalled but not confirmed applied"
                )
                if not ids:
                    reports.append(
                        coordination.RecoveryReport(
                            workspace_id="",
                            operation_id=intent["operation_id"],
                            state="pending",
                            observed_at=coordination.utc_now(),
                            detail=(
                                pending_detail
                                + " (the journal names no workspace, so it is reported "
                                "to every caller)"
                            ),
                        )
                    )
                    continue
                for workspace in ids:
                    if workspace == workspace_id:
                        reports.append(
                            coordination.RecoveryReport(
                                workspace_id=workspace,
                                operation_id=intent["operation_id"],
                                state="pending",
                                observed_at=coordination.utc_now(),
                                detail=pending_detail,
                            )
                        )
        reports.extend(
            report
            for report in self._drift_reports
            if report.workspace_id in ("", workspace_id)
        )
        return reports

    # ------------------------------------------------------------------
    # Integrity inspection and maintenance (planning key C11)
    # ------------------------------------------------------------------
    #
    # The file sink's raw storage is `records/<kind>/*.json`,
    # `events/<cursor>.json`, `events_by_operation/<op_id>.json` (derived) and
    # `journal/*.json`. These methods expose it *unvalidated* so a later doctor can
    # show corruption rather than trip over it, and repair only the two things that
    # are safe to re-derive. No probe creates the layout: a never-used store
    # answers empty.

    def binding_location(self) -> str:
        """The `location` a `StoreBinding` for this store carries.

        `application.coordination_service_for` binds with `location=str(sink.root)`,
        and the file sink's root is the `.arbite` directory -- exactly the parent of
        this store's `coordination` directory. It is the realpath of that parent so
        it is stable across a symlinked path and directly comparable with the
        binding marker. Creates nothing.
        """
        return os.path.realpath(self._root.parent)

    def binding_marker_path(self) -> Optional[str]:
        """`<.arbite>/workspace-binding.json`: the project's binding marker."""
        return os.path.join(os.path.realpath(self._root.parent), MARKER_FILENAME)

    @staticmethod
    def _inspect_record(kind: str, record_id: str, payload) -> dict:
        """One `inspect_records` entry from an already-parsed envelope (or None).

        `kind`/`record_id` come from the path; a well-formed envelope's own
        `record.kind`/`record.id` are preferred when present. An envelope missing
        any of `schema_version`/`revision`/`record` is reported, not raised.
        """
        entry = {
            "kind": kind,
            "record_id": record_id,
            "revision": None,
            "payload": payload,
            "error": None,
        }
        if payload is None:
            entry["error"] = "the stored record envelope is missing or empty"
            return entry
        if not isinstance(payload, dict):
            entry["error"] = (
                "the stored record envelope must be a JSON object, got "
                f"{type(payload).__name__}"
            )
            return entry
        schema_version = payload.get("schema_version")
        revision = payload.get("revision")
        record = payload.get("record")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version < 1
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or not isinstance(record, dict)
        ):
            entry["error"] = (
                "the stored record envelope is unversioned or malformed "
                f"(schema_version={schema_version!r}, revision={revision!r}, "
                f"record={type(record).__name__})"
            )
            return entry
        entry["revision"] = int(revision)
        if isinstance(record.get("kind"), str) and record["kind"]:
            entry["kind"] = record["kind"]
        if isinstance(record.get("id"), str) and record["id"]:
            entry["record_id"] = record["id"]
        return entry

    def inspect_records(self) -> list:
        """Every `records/<kind>/*.json` envelope, unvalidated, sorted by key.

        Unreadable files are kept with `error` set rather than raising: a doctor's
        job is to name broken bytes, not to fall over on them.
        """
        if not self.is_initialised():
            return []
        directory = self._root / RECORDS_DIR
        entries = []
        if directory.is_dir():
            for kind_dir in sorted(path for path in directory.iterdir() if path.is_dir()):
                for path in sorted(kind_dir.glob("*.json")):
                    try:
                        payload = self._read_json(path)
                    except SinkError as error:
                        entries.append(
                            {
                                "kind": kind_dir.name,
                                "record_id": path.stem,
                                "revision": None,
                                "payload": None,
                                "error": f"the stored record envelope is unreadable: {error}",
                            }
                        )
                        continue
                    entries.append(self._inspect_record(kind_dir.name, path.stem, payload))
        return sorted(entries, key=lambda entry: (entry["kind"], entry["record_id"]))

    @staticmethod
    def _inspect_event(fallback_cursor, payload) -> dict:
        """One `inspect_events` entry, preferring the envelope's own cursor."""
        entry = {"cursor": fallback_cursor, "payload": payload, "error": None}
        if payload is None:
            entry["error"] = "the stored event envelope is missing or empty"
            return entry
        if not isinstance(payload, dict):
            entry["error"] = (
                "the stored event envelope must be a JSON object, got "
                f"{type(payload).__name__}"
            )
            return entry
        cursor = payload.get("cursor")
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 1:
            entry["cursor"] = int(cursor)
        elif fallback_cursor is None:
            entry["error"] = (
                f"the stored event envelope has no usable cursor (got {cursor!r})"
            )
        return entry

    def inspect_events(self) -> list:
        """Every `events/*.json` envelope, duplicates kept, sorted by cursor.

        The cursor comes from the envelope (the filename is only a fallback), so a
        duplicate cursor or a hand-edited envelope is visible rather than silently
        normalised away -- showing it is the point.
        """
        if not self.is_initialised():
            return []
        directory = self._root / EVENTS_DIR
        if not directory.is_dir():
            return []
        entries = []
        for path in sorted(directory.glob("*.json")):
            try:
                fallback_cursor = int(path.stem)
            except ValueError:
                fallback_cursor = None
            try:
                payload = self._read_json(path)
            except SinkError as error:
                entries.append(
                    {
                        "cursor": fallback_cursor,
                        "payload": None,
                        "error": f"the stored event envelope is unreadable: {error}",
                    }
                )
                continue
            entries.append(self._inspect_event(fallback_cursor, payload))
        indexed = list(enumerate(entries))
        indexed.sort(
            key=lambda pair: (pair[1]["cursor"] is None, pair[1]["cursor"] or 0, pair[0])
        )
        return [entry for _index, entry in indexed]

    def pending_journals(self) -> list:
        """Operation ids of leftover `journal/<op_id>.json` files. Never replays."""
        return [path.stem for path in self._journal_paths()]

    def replay_journals(self) -> list:
        """Replay leftover journals forward; return the operation ids present.

        Takes the coarse lock and runs the same replay every transaction runs, so
        the effect matches what the next transaction would have done anyway: each
        decodable journal is applied forward (idempotently) and then removed. The
        returned list is the operation ids of the journals present *before* the
        replay, sorted.

        Precise about the one honest wrinkle: a journal that cannot be decoded is
        *preserved*, never guessed at (see `_replay_journals`), so its operation id
        is in the returned list even though it was not applied. The list is
        therefore "journals this call encountered", not strictly "journals it
        applied"; the way to tell an applied journal from a preserved one is
        `pending_journals()` afterwards -- an applied journal is gone, a preserved
        one is still named. Nothing is created on a store that is not initialised,
        and replay writes only by forward-applying intents that already exist.
        """
        if not self.is_initialised():
            return []
        with self.coarse_lock():
            present = [path.stem for path in self._journal_paths()]
            self._replay_journals()
        return sorted(present)

    @staticmethod
    def _event_operation_id(payload):
        """The non-empty `operation_id` inside an event envelope, or None."""
        if not isinstance(payload, dict):
            return None
        event = payload.get("event")
        if not isinstance(event, dict):
            return None
        operation_id = event.get("operation_id")
        if isinstance(operation_id, str) and operation_id:
            return operation_id
        return None

    def missing_event_operation_indexes(self) -> list:
        """Operation ids of stored events whose derived index file is absent.

        Inspection only: it reads the event envelopes (via `inspect_events`) and
        stats the derived index path; it writes nothing and creates nothing.
        """
        missing = set()
        for entry in self.inspect_events():
            operation_id = self._event_operation_id(entry["payload"])
            if not operation_id:
                continue
            try:
                path = self._operation_index_path(operation_id)
            except InvalidRecord:
                missing.add(operation_id)
                continue
            if not path.exists():
                missing.add(operation_id)
        return sorted(missing)

    def rebuild_event_operation_index(self, operation_id: str) -> bool:
        """Recreate `events_by_operation/<op_id>.json` from the stored event.

        The index is DERIVED data: it only says "operation X is event E at cursor
        C", so losing it is repairable from the event envelope already on disk --
        and the event files themselves are never touched. Returns True when it
        wrote the index, False when no stored event carries `operation_id`, the
        index already exists, or the envelope is too broken to derive from. Writes
        nothing on an uninitialised store.
        """
        if not self.is_initialised():
            return False
        try:
            path = self._operation_index_path(operation_id)
        except InvalidRecord:
            return False
        if path.exists():
            return False
        with self.coarse_lock():
            if path.exists():
                return False
            for entry in self.inspect_events():
                if self._event_operation_id(entry["payload"]) != operation_id:
                    continue
                payload = entry["payload"]
                event = payload.get("event") if isinstance(payload, dict) else None
                event_id = event.get("id") if isinstance(event, dict) else None
                cursor = entry["cursor"]
                if not isinstance(cursor, int) or not isinstance(event_id, str) or not event_id:
                    return False
                self._write_json(
                    path,
                    {"schema_version": 1, "cursor": int(cursor), "event_id": event_id},
                )
                return True
        return False
