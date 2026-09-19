"""SQLite coordination storage: real transactions for store-local state and events.

This is the SQLite half of the C02 storage layer. It stores the storage-neutral
records from `arbite.coordination` in three small tables -- records, events and
workspace bindings -- plus one nullable `revision` column on `tickets`, and it
uses the stdlib `sqlite3` module's real transactions for every multi-record unit
of work.

Why SQLite is the easy half, stated plainly so the file sink's journal makes
sense: SQLite can commit related state and events together, so there is nothing
to recover. `recover_pending` therefore returns `[]`, and that is a statement of
fact rather than an unimplemented method: a transaction that did not commit left
no trace, and one that did is complete.

Deliberate constraints:

- **No schema-version bump and no destructive migration.** The coordination
  tables are created with `CREATE TABLE IF NOT EXISTS` by `init()` *and* lazily on
  first coordination use, so an existing v1 arbite database simply gains them.
  `sqlite.SCHEMA_VERSION` and `arbite doctor` are untouched.
- **One connection per transaction.** arbite is a short-lived CLI process, so
  opening a connection is cheap and a long-lived one would only invite a stale
  lock. Writes take `BEGIN IMMEDIATE` so the write lock is held before the
  read-modify-write a revision check depends on.
- **`put` validates before it writes.** A record whose own invariants fail is
  refused with its problems, rather than stored and discovered later.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    check_revision,
    decode_event,
    decode_namespace,
    decode_record,
    encode_event,
    encode_record,
    namespace_record,
)
from ..errors import (
    InvalidRecord,
    SinkError,
    StoreBindingConflict,
)
from ..workspace import MARKER_FILENAME
from .base import CoordinationStore, CoordinationTransaction, _lowest_cursor

#: DDL for the coordination surface. Idempotent by construction (`IF NOT EXISTS`)
#: so it can be applied to a database that only ever had tickets, and re-applied
#: on every lazy initialisation without cost.
COORDINATION_DDL = """
CREATE TABLE IF NOT EXISTS coordination_records (
    kind      TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision  INTEGER NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
);

CREATE TABLE IF NOT EXISTS coordination_events (
    cursor       INTEGER PRIMARY KEY,
    event_id     TEXT UNIQUE,
    operation_id TEXT,
    category     TEXT,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS coordination_events_category_idx
    ON coordination_events(category, cursor);
CREATE INDEX IF NOT EXISTS coordination_events_operation_idx
    ON coordination_events(operation_id);

CREATE TABLE IF NOT EXISTS coordination_bindings (
    workspace_id TEXT PRIMARY KEY,
    binding_id   TEXT,
    sink_kind    TEXT,
    location     TEXT,
    payload      TEXT NOT NULL
);

-- Content-addressed mutation evidence (planning key C05). One row per digest:
-- content is stored once per digest, retained indefinitely (no garbage
-- collection), and verified against its digest on read.
CREATE TABLE IF NOT EXISTS coordination_artifacts (
    digest     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    media_type TEXT,
    content    BLOB NOT NULL
);

-- The namespace registry (planning key C11). One row per imported namespace,
-- recording where the import's cursors came from. `cursor_map` is a JSON object
-- because a cursor map is a small open-ended mapping, not a relation. Created
-- with IF NOT EXISTS like the rest, so an existing database gains it in place.
CREATE TABLE IF NOT EXISTS coordination_namespaces (
    namespace               TEXT PRIMARY KEY,
    imported_at             TEXT NOT NULL,
    event_count             INTEGER NOT NULL,
    cursor_map              TEXT NOT NULL,
    source_contract_version INTEGER NOT NULL
);
"""


def ensure_ticket_revision_column(conn) -> None:
    """Add `tickets.revision` when it is missing, without a schema-version bump.

    A pre-C02 database has tickets but no revision column; `ALTER TABLE ... ADD
    COLUMN` inside a `PRAGMA table_info` guard adds it in place, defaulting to
    NULL (read as "no revision yet"). It is called by `init()` and by the ticket
    sink's own write connections, so a legacy store gains the column the first
    time it is written to and never needs a migration step.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
    ).fetchone()
    if row is None:
        return
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "revision" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN revision INTEGER")


class SqliteCoordinationTransaction(CoordinationTransaction):
    """One SQLite transaction over the coordination tables.

    Writes are buffered in memory and flushed in `commit` inside the connection's
    `BEGIN IMMEDIATE` transaction, so a rollback discards everything and the
    revision check a `put` performed is the same one the commit enforces.
    """

    def __init__(self, store: "SqliteCoordinationStore", conn, write: bool):
        self.store = store
        self._conn = conn
        self._write = write
        self._pending: dict = {}
        self._events: list = []
        self._finished = False

    # -- storage plumbing -------------------------------------------------

    def _require_write(self, what: str) -> None:
        if not self._write:
            raise InvalidRecord(
                f"this coordination transaction is read-only; {what} is not allowed"
            )

    def _row(self, kind: str, record_id: str):
        return self._conn.execute(
            "SELECT revision, payload FROM coordination_records "
            "WHERE kind = ? AND record_id = ?",
            (kind, record_id),
        ).fetchone()

    def _stored(self, kind: str, record_id: str):
        """`(record, revision)` from this transaction's pending writes or storage."""
        pending = self._pending.get((kind, record_id))
        if pending is not None:
            return pending
        row = self._row(kind, record_id)
        if row is None:
            return None
        return decode_record(row["payload"])

    def _event_by_id(self, event_id: str):
        for _cursor, event in self._events:
            if event.id == event_id:
                return event
        row = self._conn.execute(
            "SELECT payload FROM coordination_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else decode_event(row["payload"])

    # -- the CoordinationTransaction surface ------------------------------

    def put(self, record, *, expect_revision: Optional[int] = None):
        """Store `record`, enforcing `expect_revision` inside the transaction.

        The revision is read (from this transaction's own writes first, then from
        the database) and checked *here*, in the same `BEGIN IMMEDIATE` that
        commits, so two racing writers cannot both see revision N and both win.
        """
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
        self._pending[(record.kind, record.record_id)] = (record, current + 1)
        return record

    def get(self, kind: str, record_id: str):
        if kind == "event":
            return self._event_by_id(record_id)
        found = self._stored(kind, record_id)
        return None if found is None else found[0]

    def find(self, kind: str, **fields) -> list:
        found: dict = {}
        if kind == "event":
            rows = self._conn.execute(
                "SELECT payload FROM coordination_events ORDER BY cursor"
            ).fetchall()
            for row in rows:
                event = decode_event(row["payload"])
                found[event.id] = event
            for _cursor, event in self._events:
                found[event.id] = event
        else:
            rows = self._conn.execute(
                "SELECT payload FROM coordination_records WHERE kind = ?", (kind,)
            ).fetchall()
            for row in rows:
                record, _revision = decode_record(row["payload"])
                found[record.record_id] = record
            for (rec_kind, record_id), (record, _revision) in self._pending.items():
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
        pending = self._pending.get((kind, record_id))
        if pending is not None:
            return int(pending[1])
        row = self._row(kind, record_id)
        if row is None:
            return 0
        try:
            _record, revision = decode_record(row["payload"])
        except (InvalidRecord, ValueError, TypeError):
            # A stored envelope that cannot be decoded has no usable revision;
            # treating it as absent (0) lets an explicit repair (a legacy upgrade)
            # proceed rather than aborting the transaction.
            return 0
        return int(revision)

    def _next_cursor(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(cursor), 0) AS highest FROM coordination_events"
        ).fetchone()
        highest = int(row["highest"] or 0)
        for cursor, _event in self._events:
            highest = max(highest, cursor)
        return highest + 1

    def append_event(self, event):
        """Append `event`, assigning the next cursor, deduplicating by id/operation id."""
        self._require_write("append_event")
        existing = self._event_by_id(event.id)
        if existing is not None:
            return existing
        if event.operation_id:
            matches = self.find("event", operation_id=event.operation_id)
            if matches:
                return _lowest_cursor(matches)
        problems = event.validate()
        if problems:
            raise InvalidRecord(
                f"invalid event: {'; '.join(problems)}", details={"problems": list(problems)}
            )
        cursor = self._next_cursor()
        event.cursor = cursor
        self._events.append((cursor, event))
        return event

    def commit(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            for (kind, record_id), (record, revision) in self._pending.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO coordination_records "
                    "(kind, record_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (kind, record_id, revision, _dump(encode_record(record, revision))),
                )
            for cursor, event in self._events:
                self._conn.execute(
                    "INSERT INTO coordination_events "
                    "(cursor, event_id, operation_id, category, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        cursor,
                        event.id,
                        event.operation_id,
                        event.category,
                        _dump(encode_event(event, cursor)),
                    ),
                )
            self._conn.commit()
        except sqlite3.Error as e:
            self._safe_rollback()
            raise SinkError(f"SQLite coordination commit failed: {e}")
        finally:
            self._close()

    def rollback(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._pending.clear()
        self._events.clear()
        self._safe_rollback()
        self._close()

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:  # pragma: no cover - connection already gone
            pass

    def _close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass


def _dump(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


class SqliteCoordinationStore(CoordinationStore):
    """The coordination store for the SQLite sink.

    Construct it with the same database path the ticket sink uses, so the
    coordination records and the tickets they describe live in one file. It
    self-initialises: `init()` (called by `SqliteSink.init()`) creates the tables
    up front, and any first use creates them lazily, so an existing v1 database
    gains them without a migration.
    """

    def __init__(self, path: Path, timeout: float = 5.0):
        self._path = Path(path)
        self._timeout = timeout
        self._ensured = False

    @property
    def root(self) -> str:
        return str(self._path)

    # -- lifecycle --------------------------------------------------------

    def init(self) -> None:
        """Create the coordination tables (and the ticket revision column) if needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(COORDINATION_DDL)
                ensure_ticket_revision_column(conn)
                conn.commit()
            finally:
                conn.close()
            self._ensured = True
        except sqlite3.Error as e:
            raise SinkError(
                f"could not initialise coordination storage at {self._path}: {e}"
            )

    def _ensure(self) -> None:
        if not self._ensured:
            self.init()

    def is_initialised(self) -> bool:
        """True when the coordination tables already exist (no side effects)."""
        if self._ensured:
            return True
        if not self._path.exists():
            return False
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as e:
            raise SinkError(f"could not open the SQLite store at {self._path}: {e}")
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'coordination_records'"
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    # -- transactions -----------------------------------------------------

    def _connect(self, write: bool):
        self._ensure()
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as e:
            raise SinkError(f"could not open the SQLite store at {self._path}: {e}")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            if write:
                conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            conn.close()
            raise SinkError(f"could not start a coordination transaction: {e}")
        return conn

    def transaction(self, write: bool = True) -> SqliteCoordinationTransaction:
        """Open one serialized transaction over the coordination tables."""
        return SqliteCoordinationTransaction(self, self._connect(write=write), write)

    # -- binding ----------------------------------------------------------

    def bind_store(self, binding):
        """Record `binding` as the workspace's authoritative store (idempotent).

        Matching binding -> the stored one is returned and nothing changes;
        differing sink kind/location -> `StoreBindingConflict`, because one
        workspace cannot coordinate against two stores.
        """
        problems = binding.validate()
        if problems:
            raise InvalidRecord(
                f"invalid store binding: {'; '.join(problems)}",
                details={"workspace_id": binding.workspace_id, "problems": list(problems)},
            )
        with self.transaction() as tx:
            existing = self._binding_row(tx, binding.workspace_id)
            if existing is not None:
                stored = decode_record(existing["payload"])[0]
                if stored.matches_binding(binding):
                    return stored
                raise StoreBindingConflict(
                    f"workspace {binding.workspace_id} is already bound to "
                    f"{stored.sink_kind}:{stored.location}; refusing to re-bind to "
                    f"{binding.sink_kind}:{binding.location}",
                    details={
                        "workspace_id": binding.workspace_id,
                        "bound": {"sink_kind": stored.sink_kind, "location": stored.location},
                        "requested": {
                            "sink_kind": binding.sink_kind,
                            "location": binding.location,
                        },
                    },
                )
            tx._conn.execute(
                "INSERT OR REPLACE INTO coordination_bindings "
                "(workspace_id, binding_id, sink_kind, location, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    binding.workspace_id,
                    binding.id,
                    binding.sink_kind,
                    binding.location,
                    _dump(encode_record(binding, 1)),
                ),
            )
            return binding

    def store_binding(self, workspace_id: str):
        with self.transaction(write=False) as tx:
            row = self._binding_row(tx, workspace_id)
        if row is None:
            return None
        return decode_record(row["payload"])[0]

    @staticmethod
    def _binding_row(tx, workspace_id: str):
        return tx._conn.execute(
            "SELECT payload FROM coordination_bindings WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()

    # ------------------------------------------------------------------
    # Cursor namespace and the namespace registry (planning key C11)
    # ------------------------------------------------------------------

    def cursor_namespace(self) -> str:
        """`sqlite:<realpath of the database file>` -- creates nothing."""
        return "sqlite:" + os.path.realpath(self._path)

    @staticmethod
    def _namespace_row(row) -> dict:
        return decode_namespace(
            {
                "namespace": row["namespace"],
                "imported_at": row["imported_at"],
                "event_count": row["event_count"],
                "cursor_map": row["cursor_map"],
                "source_contract_version": row["source_contract_version"],
            }
        )

    def namespaces(self) -> list:
        """Every retained namespace entry, sorted. Never initialises the store.

        A store that does not yet have coordination tables answers `[]` without
        opening a connection, so reading the registry of a legacy database can
        never create anything.
        """
        if not self.is_initialised():
            return []
        conn = self._connect(write=False)
        try:
            rows = conn.execute(
                "SELECT namespace, imported_at, event_count, cursor_map, "
                "source_contract_version FROM coordination_namespaces"
            ).fetchall()
        except sqlite3.Error as e:  # pragma: no cover - table guarded by is_initialised
            raise SinkError(f"could not read the namespace registry: {e}")
        finally:
            conn.close()
        return sorted((self._namespace_row(row) for row in rows), key=lambda e: e["namespace"])

    def record_namespace(
        self,
        namespace: str,
        *,
        imported_at=None,
        event_count: int = 0,
        cursor_map=None,
        source_contract_version=None,
    ) -> dict:
        """Upsert one namespace entry inside a real write transaction.

        This is a write, so it *may* initialise the store; the read-modify-write
        runs under `BEGIN IMMEDIATE`, so two processes cannot lose one side's
        import. The merge rule matches the file sink exactly: union of the cursor
        maps (new destination wins for a shared source cursor), cumulative
        `event_count`, and the newest `imported_at`/`source_contract_version`.
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
        conn = self._connect(write=True)
        try:
            row = conn.execute(
                "SELECT namespace, imported_at, event_count, cursor_map, "
                "source_contract_version FROM coordination_namespaces WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            if row is not None:
                existing = self._namespace_row(row)
                merged_cursor_map = dict(existing["cursor_map"])
                merged_cursor_map.update(entry["cursor_map"])
                entry = namespace_record(
                    namespace,
                    imported_at=entry["imported_at"],
                    event_count=existing["event_count"] + entry["event_count"],
                    cursor_map=merged_cursor_map,
                    source_contract_version=entry["source_contract_version"],
                )
            conn.execute(
                "INSERT OR REPLACE INTO coordination_namespaces "
                "(namespace, imported_at, event_count, cursor_map, source_contract_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry["namespace"],
                    entry["imported_at"],
                    entry["event_count"],
                    _dump(entry["cursor_map"]),
                    entry["source_contract_version"],
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            try:
                conn.rollback()
            except sqlite3.Error:  # pragma: no cover - connection already gone
                pass
            raise SinkError(f"could not record namespace {namespace!r}: {e}")
        finally:
            conn.close()
        return entry

    # -- recovery ---------------------------------------------------------

    def recover_pending(self, workspace_id: str) -> list:
        """Always `[]`: a SQLite transaction is atomic, so nothing is ever pending.

        Store-local state and its events commit in one `COMMIT`; a process that
        died mid-transaction left no rows at all, and one that committed left a
        complete unit. There is therefore nothing to replay and nothing to
        reconcile *for store-local records*, which is why this is empty rather
        than unimplemented. (Reconciling the workspace's *files* is C05's
        separate durability domain and does not live in this store.)
        """
        return []

    # ------------------------------------------------------------------
    # Content-addressed artifacts and the operation lock (planning key C05)
    # ------------------------------------------------------------------

    def operation_lock_path(self):
        """The coarse operation-lock file (a sibling of the database file).

        SQLite transactions serialize *store* writes, but they cannot serialize a
        filesystem change against a lifecycle transition. This lock file is the
        separate, cross-process serialization for that; it never replaces the
        database's own transaction guarantees.
        """
        return str(self._path) + ".operation.lock"

    def store_artifact_bytes(self, data: bytes, *, media_type: str = "application/octet-stream"):
        """Store `data` once, keyed by its digest, and return its descriptor."""
        check_capacity(data, self.artifact_limit())
        digest = digest_of_bytes(data)
        conn = self._connect(write=False)
        try:
            row = conn.execute(
                "SELECT content, size FROM coordination_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
            if row is not None:
                verify_artifact(
                    bytes(row["content"]),
                    expected_digest=digest,
                    expected_size=int(row["size"]),
                )
            else:
                conn.execute(
                    "INSERT INTO coordination_artifacts "
                    "(digest, size, media_type, content) VALUES (?, ?, ?, ?)",
                    (digest, len(data), media_type, sqlite3.Binary(data)),
                )
                conn.commit()
        except sqlite3.Error as e:
            raise SinkError(f"could not store artifact {digest}: {e}")
        finally:
            conn.close()
        return coordination.Artifact(
            id=artifact_id_for_digest(digest),
            digest=digest,
            size=len(data),
            created=coordination.utc_now(),
            location=f"coordination_artifacts/{digest[len(DIGEST_PREFIX):]}",
            media_type=media_type,
        )

    def read_artifact_bytes(self, digest: str):
        """Stored bytes for `digest`, verified, or None when absent."""
        conn = self._connect(write=False)
        try:
            row = conn.execute(
                "SELECT content, size FROM coordination_artifacts WHERE digest = ?",
                (digest,),
            ).fetchone()
        except sqlite3.Error as e:
            raise SinkError(f"could not read artifact {digest}: {e}")
        finally:
            conn.close()
        if row is None:
            return None
        data = bytes(row["content"])
        verify_artifact(data, expected_digest=digest, expected_size=int(row["size"]))
        return data

    def has_artifact(self, digest: str) -> bool:
        conn = self._connect(write=False)
        try:
            row = conn.execute(
                "SELECT 1 FROM coordination_artifacts WHERE digest = ?", (digest,)
            ).fetchone()
        except sqlite3.Error as e:
            raise SinkError(f"could not check artifact {digest}: {e}")
        finally:
            conn.close()
        return row is not None

    def contract_version(self) -> int:
        return coordination.CONTRACT_VERSION

    # ------------------------------------------------------------------
    # Integrity inspection and maintenance (planning key C11)
    # ------------------------------------------------------------------
    #
    # The database *is* the raw storage here (`coordination_records` /
    # `coordination_events`), so the two `inspect_*` probes read the rows directly
    # and decode nothing but the payload JSON. Because SQLite commits store-local
    # state atomically and maintains its own event indexes, the journal/index half
    # of the C11 surface is honestly empty rather than unimplemented -- each of
    # those methods says so in its own docstring.

    def binding_location(self) -> str:
        """The `location` a `StoreBinding` for this store carries: the db realpath.

        `application.coordination_service_for` binds with `location=str(sink.root)`
        and the SQLite sink's root is the database path, so this is the string a
        stored binding carries. It is the realpath so it is stable across a
        symlinked path and directly comparable with the binding marker. Creates
        nothing.
        """
        return os.path.realpath(self._path)

    def binding_marker_path(self) -> Optional[str]:
        """`<db parent>/workspace-binding.json`: the project's binding marker."""
        return os.path.join(os.path.realpath(self._path.parent), MARKER_FILENAME)

    @staticmethod
    def _inspect_record(kind: str, record_id: str, raw_payload) -> dict:
        """One `inspect_records` entry; a payload that is not valid JSON is reported.

        `revision` is read from the *envelope*, not the `revision` column, so an
        unversioned payload reports `revision=None` exactly like the file sink --
        the column is not more trustworthy than the bytes it describes.
        """
        entry = {
            "kind": kind,
            "record_id": record_id,
            "revision": None,
            "payload": None,
            "error": None,
        }
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError) as error:
            entry["error"] = f"the stored record envelope is not valid JSON: {error}"
            return entry
        entry["payload"] = payload
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
        """Every row of `coordination_records`, unvalidated, sorted by key.

        A malformed payload is reported (`error`, `payload=None`) rather than
        raised. `[]` without connecting when the coordination tables do not exist,
        so probing a legacy database creates nothing.
        """
        if not self.is_initialised():
            return []
        conn = self._connect(write=False)
        try:
            rows = conn.execute(
                "SELECT kind, record_id, payload FROM coordination_records"
            ).fetchall()
        except sqlite3.Error as error:
            raise SinkError(f"could not inspect coordination records: {error}")
        finally:
            conn.close()
        return sorted(
            (
                self._inspect_record(row["kind"], row["record_id"], row["payload"])
                for row in rows
            ),
            key=lambda entry: (entry["kind"], entry["record_id"]),
        )

    @staticmethod
    def _inspect_event(fallback_cursor, raw_payload) -> dict:
        """One `inspect_events` entry; the payload's own cursor wins when present."""
        entry = {"cursor": fallback_cursor, "payload": None, "error": None}
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError) as error:
            entry["error"] = f"the stored event envelope is not valid JSON: {error}"
            return entry
        entry["payload"] = payload
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
        """Every row of `coordination_events`, duplicates kept, ordered by cursor.

        The cursor is taken from the payload envelope (the column is the
        fallback), so a hand-edited payload that duplicates another cursor stays
        visible rather than being silently normalised away. `[]` without connecting
        when the coordination tables do not exist.
        """
        if not self.is_initialised():
            return []
        conn = self._connect(write=False)
        try:
            rows = conn.execute(
                "SELECT cursor, payload FROM coordination_events ORDER BY cursor"
            ).fetchall()
        except sqlite3.Error as error:
            raise SinkError(f"could not inspect coordination events: {error}")
        finally:
            conn.close()
        return [self._inspect_event(row["cursor"], row["payload"]) for row in rows]

    def pending_journals(self) -> list:
        """Always `[]`: SQLite has no write-ahead journal to leave behind.

        Store-local state and its events commit in one atomic `COMMIT`, so a
        process that died mid-transaction left no rows and one that committed left
        a complete unit. There is no partial intent file to find: this is a
        statement of fact, not an unimplemented method."""
        return []

    def replay_journals(self) -> list:
        """Always `[]`: a SQLite commit is atomic, so there is nothing to replay.

        There is no journal to forward-apply, so this can never write."""
        return []

    def missing_event_operation_indexes(self) -> list:
        """Always `[]`: SQLite derives no per-operation event index to be missing.

        The `coordination_events_operation_idx` index is maintained by the
        database as part of the same transaction that inserts the event, so it
        cannot be out of step with the rows it covers."""
        return []

    def rebuild_event_operation_index(self, operation_id: str) -> bool:
        """Always `False`: there is no derived per-operation index to rebuild.

        The database's own index is not separately stored, so this can never
        write -- a fact about the storage, not a missing implementation."""
        return False
