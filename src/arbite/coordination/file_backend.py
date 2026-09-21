"""The file coordination backend: records as JSON documents beside the tickets.

The layout is deliberately boring, because everything about it has to be
explainable to someone who opens the directory:

```
<store root>/coordination/
  workspace.json          the derived workspace binding (one per store)
  attempts/att-XXXX.json  work attempts
  claims/claims-XXXX.json active and released file claims
  observations/op-XXXX.json  read receipts (the tokens a write presents)
  receipts/op-XXXX.json   mutation receipts, including pending intent
  events/<cursor>-evt-XXXX.json  the append-only event stream
  artifacts/<digest hex>  the bytes an artifact record describes
  artifacts/index/art-XXXX.json  the artifact records themselves
  lock                    the runtime mutex (tic-1a75)
```

Every one of those files is a non-`.md` document in its own reserved directory, so
the ticket scan can never mistake coordination state for a ticket and a stray
record can never be served as one; the file sink skips these directories by name
for the same reason.

Two properties this backend does *not* claim. Writes are per record and atomic
(stage a temp file, `os.replace`), but a sequence of them is not one transaction:
a crash between two writes leaves the earlier ones in place. And there is no
process serialization here, so two processes writing the same record race exactly
as two writers of the same file do. Both are tic-1a75 (C02): the recoverable
journal and the coarse operation lock build on this module rather than replacing
it, which is why the transaction hook lives in `store.py` and not here.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..errors import CoordinationError, RecordError
from ..sinks.file import TMP_PREFIX, write_atomic
from .records import Record, parse_record, record_type_of
from .store import CoordinationStore

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

#: The runtime mutex's name. Nothing creates it yet (tic-1a75): it is named here so
#: the lock file the plan lists and the runtime state it protects cannot drift.
LOCK_FILENAME = "lock"

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


class FileCoordinationStore(CoordinationStore):
    """Coordination records as JSON documents under one reserved directory."""

    kind = "file"

    def __init__(self, root):
        self._root = Path(root)

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

    def _get_record(self, record_type: str, record_id: str) -> Record:
        for record in self._records(record_type):
            if record.id == record_id:
                return record
        raise CoordinationError(
            f"no {record_type} record {record_id} in {self._root}"
        )

    def put_record(self, record: Record) -> None:
        record.validate()
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

    def delete_record(self, record_type: str, record_id: str) -> None:
        """Unlink a record's document. A record that is not there is not an error:
        the caller is establishing a state, not asserting one."""
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
        than half an artifact. Proving that a stored artifact still verifies against
        its digest -- and the size limits that make "store it" a decision rather
        than a default -- is the change-receipt slice's job (tic-7c42)."""
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
