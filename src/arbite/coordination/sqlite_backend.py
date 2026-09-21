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

Claim lookups by path, and the current-state claim index the plan describes, are
the exclusive-claim slice's (tic-9b57) and can be added as a projection beside this
table without changing what is stored.

Two things are *not* here. The writes are not yet one transaction across records
(tic-1a75): each `put_record` is a committed statement, so a crash between two
writes leaves the earlier one. And nothing recovers a staged operation (tic-b03b).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..errors import CoordinationError, RecordError
from .records import Record, parse_record, record_type_of
from .store import CoordinationStore

#: The revision of the coordination tables in a database. Separate from the ticket
#: schema revision (`sinks.sqlite.SCHEMA_VERSION`): coordination state is local
#: runtime state that can be discarded, while tickets are the development record,
#: so the two are versioned and migrated independently.
COORDINATION_SCHEMA_VERSION = 1

#: One document per record. `document` holds exactly what the file backend writes,
#: so `arbite migrate`'s coordination round trip (tic-008f) has nothing to translate.
COORDINATION_DDL = """
CREATE TABLE IF NOT EXISTS coordination_records (
    record_type TEXT NOT NULL,
    record_id   TEXT NOT NULL,
    document    TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id)
);

CREATE INDEX IF NOT EXISTS coordination_records_type_idx
    ON coordination_records(record_type);
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
    def _table_exists(conn) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'coordination_records'"
        ).fetchone()
        return row is not None

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
        record.validate()
        record_type = record_type_of(record)
        document = _dumps(record)
        with self._connection() as conn:
            # A write may create the tables -- `arbite init` goes through here for
            # exactly that reason -- but a *read* never does, which is why this is
            # the only method that runs the DDL.
            conn.executescript(COORDINATION_DDL)
            conn.execute(
                "INSERT INTO coordination_records (record_type, record_id, document) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(record_type, record_id) DO UPDATE SET document = excluded.document",
                (record_type, record.id, document),
            )
            conn.commit()

    def delete_record(self, record_type: str, record_id: str) -> None:
        """Delete a record's row. A record that is not there is not an error: the
        caller is establishing a state, not asserting one."""
        with self._connection() as conn:
            if not self._table_exists(conn):
                return
            conn.execute(
                "DELETE FROM coordination_records WHERE record_type = ? AND record_id = ?",
                (record_type, record_id),
            )
            conn.commit()

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
