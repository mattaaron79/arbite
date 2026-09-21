"""The SQLite coordination backend: the same records, inside the store's database.

The database is the sink's own file (`.arbite/arbite.db` by default), not a second
one: a project that keeps its tickets in SQLite has one thing to back up, one thing
to move, and no way for the tickets and the claims about them to disagree about
which file they live in.

Records are stored as the same versioned JSON documents the file backend writes,
in one table keyed by `(record_type, record_id)`. That is a deliberate v1 choice
rather than laziness:

- a migration between the backends is field-for-field exact, because both store
  the same bytes (`records.Record.to_dict()`), and there is one serialisation to
  keep versioned;
- the record shapes are still moving (this ticket defines them), and a normalised
  table per type would freeze seven projections that later slices would have to
  migrate in lockstep;
- the queries that exist today are counts, which the reference implementation
  ("read the records, filter in Python") answers identically to the file backend --
  the same reason the ticket sink pushes structured predicates into SQL but keeps
  text matching in the shared matcher.

Claim lookups by path need no projection here: the exclusive-claim slice (tic-9b57)
derives one claim record per path from the workspace and the path, so "who holds this
path" is a primary-key read of the table above and that record's revision is the
compare-and-swap two racing acquisitions contend on.

One thing is *not* here: nothing recovers a staged *file* operation (tic-b03b).
A multi-record unit of work, on the other hand, is exactly what this backend now
does natively: `commit_transaction` runs it in one `BEGIN IMMEDIATE` transaction,
so a process killed inside it leaves the database rolled back rather than
half-applied -- the same observable outcome the file backend reaches with a journal.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..errors import CoordinationError, RecordError
from .records import RECEIPT_PENDING, Record, parse_record
from .store import (
    COMMIT_APPLIED,
    COMMIT_STAGED,
    CommitResult,
    CoordinationStore,
    CoordinationTransaction,
)

#: The revision of the coordination tables in a database. Separate from the ticket
#: schema revision (`sinks.sqlite.SCHEMA_VERSION`): coordination state is local
#: runtime state that can be discarded, while tickets are the development record,
#: so the two are versioned and migrated independently. Revision 2 added the
#: per-record revision counters; a v1 database gains that table through the same
#: `CREATE TABLE IF NOT EXISTS` any write runs, and keeps every record it holds.
COORDINATION_SCHEMA_VERSION = 2

#: One document per record, plus one counter per record. `document` holds exactly
#: what the file backend writes, so `arbite migrate`'s coordination round trip
#: (tic-008f) has nothing to translate; the counters live beside it rather than in
#: the document, because a record's own document must stay exactly the record.
COORDINATION_DDL = """
CREATE TABLE IF NOT EXISTS coordination_records (
    record_type TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    document    TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id)
);

CREATE INDEX IF NOT EXISTS coordination_records_type_idx
    ON coordination_records(record_type);

CREATE TABLE IF NOT EXISTS coordination_revisions (
    record_type TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    PRIMARY KEY (record_type, record_id)
);
"""


class SqliteCoordinationStore(CoordinationStore):
    """Coordination records as documents in the ticket store's database."""

    kind = "sqlite"

    def __init__(self, path, timeout: float = 5.0):
        self._path = Path(path)
        self._timeout = timeout

    @property
    def root(self) -> str:
        return str(self._path)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @contextmanager
    def _connection(self):
        """A connection for one unit of work. Missing store, missing database and a
        failed statement all come back as `CoordinationError` with a readable
        message, because the caller is a CLI process reporting to an agent."""
        if not self._path.exists():
            raise CoordinationError(
                f"no SQLite store at {self._path} (run 'arbite init' to create it)"
            )
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as e:
            raise CoordinationError(f"could not open the SQLite store at {self._path}: {e}")
        try:
            conn.row_factory = sqlite3.Row
            # Autocommit, so "one transaction" is exactly `BEGIN` .. `COMMIT` written
            # where the caller means it. In Python's legacy isolation mode a unit of
            # work would also depend on when the driver decides to begin one, and a
            # statement outside a unit would quietly be its own commit.
            conn.isolation_level = None
            conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            yield conn
        except sqlite3.Error as e:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise CoordinationError(f"SQLite coordination error: {e}")
        finally:
            conn.close()

    @staticmethod
    def _has_table(conn, name: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _table_exists(self, conn) -> bool:
        return self._has_table(conn, "coordination_records")

    def has_records_table(self) -> bool:
        """Whether this database has the coordination tables yet.

        False is ordinary: every store built before this slice, and every store
        whose next `arbite init` has not run. Reads answer "nothing recorded" for
        such a store rather than creating tables -- a command that reports must not
        start changing things."""
        with self._connection() as conn:
            return self._table_exists(conn)

    def init(self) -> None:
        """Create the coordination tables if they are missing. Idempotent, and it
        never drops anything -- a store that already holds claims keeps them."""
        with self._connection() as conn:
            conn.executescript(COORDINATION_DDL)
            conn.commit()

    def check_writable_layout(self) -> None:
        """The coordination state *is* the database, so "can the layout be built"
        means "can the database be created or opened here".

        A path that does not exist yet is not a failure: `arbite init` checks the
        guards *before* it creates the store, and the store it is about to create is
        exactly this file. What is refused is a path that something else already
        occupies -- a directory where the database belongs -- because writing a
        database over it is not something arbite can do or undo."""
        if self._path.exists() and not self._path.is_file():
            raise CoordinationError(
                f"{self._path} exists and is not a file, so the coordination state cannot "
                "be stored there; move it aside and re-run"
            )
        if not self._path.parent.is_dir():
            raise CoordinationError(
                f"{self._path.parent} does not exist, so the SQLite store cannot be created"
            )

    # ------------------------------------------------------------------
    # Storage primitives
    # ------------------------------------------------------------------

    def _rows(self, conn, record_type: str) -> list:
        return conn.execute(
            "SELECT document FROM coordination_records WHERE record_type = ? ORDER BY record_id",
            (record_type,),
        ).fetchall()

    def _records(self, record_type: str) -> list:
        stored = []
        with self._connection() as conn:
            if not self._table_exists(conn):
                return []
            for row in self._rows(conn, record_type):
                try:
                    stored.append(parse_record(_loads(row["document"])))
                except RecordError as e:
                    raise RecordError(f"coordination_records({record_type}): {e}")
        if record_type == "event":
            return sorted(stored, key=lambda event: (event.cursor, event.id))
        return sorted(stored, key=lambda record: record.id)

    def _get_record(self, record_type: str, record_id: str) -> Record:
        with self._connection() as conn:
            row = None
            if self._table_exists(conn):
                row = conn.execute(
                    "SELECT document FROM coordination_records "
                    "WHERE record_type = ? AND record_id = ?",
                    (record_type, record_id),
                ).fetchone()
        if row is None:
            raise CoordinationError(f"no {record_type} record {record_id} in {self._path}")
        return parse_record(_loads(row["document"]))

    def put_record(self, record: Record) -> None:
        """Store `record`, replacing a record of the same type and id.

        One record, one transaction: the document and its revision counter are
        written together, so a crash cannot leave a counter that disagrees with the
        document it counts. `validate()` runs first, so an invalid record never
        reaches storage."""
        with self.transaction() as txn:
            txn.put_record(record)

    def delete_record(self, record_type: str, record_id: str) -> None:
        """Delete a record's row and its revision counter.

        A record that is not there is not an error: the caller is establishing a
        state, not asserting one. Its counter goes with it, so a later record
        written under the same id starts again at 1 instead of inheriting a count
        from a record that no longer exists. A store without the tables is left
        alone: a delete is not a reason to create anything."""
        if not self.has_records_table():
            return
        with self.transaction() as txn:
            txn.delete_record(record_type, record_id)

    def commit_transaction(self, transaction: CoordinationTransaction) -> CommitResult:
        """Commit a unit of work in one SQL transaction.

        `BEGIN IMMEDIATE` takes the write lock up front, so the revisions and cursors
        assigned here cannot be overtaken and a stale `expect_revision` is refused
        with nothing written -- including nothing written by the caller's earlier
        steps, which is what "rollback" means here. A process killed inside the block
        leaves the database as it was: SQLite discards the uncommitted transaction,
        which is this backend's route to the same outcome the file backend reaches
        with a journal.

        The tables are created *before* the transaction begins: `executescript` ends
        any open transaction, so running the DDL inside one would silently commit a
        half-applied unit."""
        with self._connection() as conn:
            if not self._table_exists(conn) or not self._has_table(conn, "coordination_revisions"):
                conn.executescript(COORDINATION_DDL)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if transaction.operation_id and self._operation_committed(
                    conn, transaction.operation_id
                ):
                    conn.execute("ROLLBACK")
                    return CommitResult(applied=False, deduplicated=True)
                writes = transaction.materialise(
                    lambda record_type, record_id: self._stored_revision(
                        conn, record_type, record_id
                    ),
                    lambda: self._next_cursor(conn),
                )
                if not writes:
                    conn.execute("ROLLBACK")
                    return CommitResult(applied=True)
                self._crash_point(COMMIT_STAGED)
                for write in writes:
                    self._apply_write(conn, write)
                self._crash_point(COMMIT_APPLIED)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return CommitResult(
            applied=True,
            events=tuple(write.record for write in writes if write.record is not None),
            revisions={
                f"{write.record_type}/{write.record_id}": write.revision
                for write in writes
                if write.revision is not None
            },
        )

    def _apply_write(self, conn, write) -> None:
        """Write one materialised record and its revision, inside the caller's
        transaction. The two statements travel together or not at all, which is what
        makes a counter that disagrees with its document unreachable."""
        if write.is_delete:
            conn.execute(
                "DELETE FROM coordination_records WHERE record_type = ? AND record_id = ?",
                (write.record_type, write.record_id),
            )
            conn.execute(
                "DELETE FROM coordination_revisions WHERE record_type = ? AND record_id = ?",
                (write.record_type, write.record_id),
            )
            return
        conn.execute(
            "INSERT INTO coordination_records (record_type, record_id, document) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(record_type, record_id) DO UPDATE SET document = excluded.document",
            (write.record_type, write.record_id, _dumps(write.record)),
        )
        conn.execute(
            "INSERT INTO coordination_revisions (record_type, record_id, revision) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(record_type, record_id) DO UPDATE SET revision = excluded.revision",
            (write.record_type, write.record_id, write.revision or 0),
        )

    def _stored_revision(self, conn, record_type: str, record_id: str) -> int:
        """A record's revision inside the caller's transaction."""
        row = conn.execute(
            "SELECT revision FROM coordination_revisions "
            "WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def _revision(self, record_type: str, record_id: str) -> int:
        with self._connection() as conn:
            if not self._has_table(conn, "coordination_revisions"):
                return 0
            return self._stored_revision(conn, record_type, record_id)

    def _operation_committed(self, conn, operation_id: str) -> bool:
        """Whether this operation id already has a finalised receipt.

        The deduplication rule, read inside the transaction that would apply the
        retry: a *pending* receipt is an operation that started and has not finished,
        so a retry must run it again (that reconciliation is tic-b03b's). A
        succeeded or failed receipt is an operation that already happened."""
        row = conn.execute(
            "SELECT document FROM coordination_records "
            "WHERE record_type = 'receipt' AND record_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return False
        return parse_record(_loads(row["document"])).result != RECEIPT_PENDING

    def _next_event_cursor(self) -> int:
        with self._connection() as conn:
            if not self._table_exists(conn):
                return 1
            return self._next_cursor(conn)

    def _next_cursor(self, conn) -> int:
        """One past the highest cursor in the event stream, inside the caller's
        transaction.

        Derived from the events themselves rather than from a counter column, so an
        event placed at an explicit cursor by an import (tic-008f) cannot be
        overtaken. That is an O(events) read per append -- the same order as
        `arbite events`, which reads the stream anyway; a projection can replace it
        when a store grows large enough to care."""
        highest = 0
        for row in self._rows(conn, "event"):
            highest = max(highest, parse_record(_loads(row["document"])).cursor)
        return highest + 1

    def counts(self) -> dict:
        """The totals the report needs, derived from the records themselves.

        Filtering happens in Python rather than in SQL -- `is_active` and
        `is_pending` are record semantics, and expressing them twice (once as a
        document field, once as a WHERE clause) is how two backends start
        disagreeing about what a claim is."""
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


def _dumps(record: Record) -> str:
    """The stored document form. Identical to the file backend's, byte for byte, so
    a migration between the backends has nothing to translate (tic-008f)."""
    return json.dumps(record.to_dict(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _loads(document: str) -> dict:
    try:
        return json.loads(document)
    except ValueError as e:
        raise RecordError(f"stored coordination document is not valid JSON: {e}")
