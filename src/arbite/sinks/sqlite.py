"""The SQLite sink: one normalized database, real SQL, and a derived note index.

The point of this sink is not merely "another place to put tickets" -- it is the
proof that the interface in `sinks/base.py` is complete. It shares no storage code
with the file sink: there are no folders to move, no temp files to strand and no
filenames to keep stable, so anything that still worked only because a ticket was
a file shows up here as a failing conformance test.

What the schema buys:

- `tickets` holds the scalar frontmatter fields, the markdown body and the bucket,
  so a query can be answered by SQL instead of by reading every ticket.
- `ticket_tags` / `ticket_deps` / `ticket_references` normalize the list fields,
  so `depends_on` and `references` are joins rather than strings that have to be
  parsed back.
- `ticket_notes` is a **derived index** of the `## Notes` section, not the
  authority for it: `body` stays the whole markdown body, exactly as a file sink
  holds it, and this table is rebuilt from it on every write by
  `schema.parse_notes()`. That keeps `render()` byte-identical across sinks,
  keeps hand-written notes in a body authoritative, makes notes queryable in SQL,
  and gives this sink a native integrity check of its own (`notes_index_drift`,
  repairable with `doctor --fix`).

Two deliberate non-shortcuts:

- **No CHECK constraints on the vocabulary fields.** A file sink can hold a
  hand-edited `status: wibble`, so it must be reported rather than rejected; the
  same rule applies here or `doctor` would mean different things per sink.
- **Text search is not pushed into `LIKE`.** SQLite folds case in ASCII only, and
  Python's `str.lower()` folds a few characters outside ASCII, so `LIKE` can miss
  a row that the reference matcher accepts. Structured predicates are pushed into
  SQL; text matching runs through the shared matcher on the rows SQL returns.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from ..errors import Conflict, SinkError, SinkNotInitialised, TicketError, TicketNotFound
from ..query import TicketQuery, sort_tickets
from ..schema import FIELD_ORDER, Note, Ticket, parse_notes
from .base import Expect, Problem, TicketSink, enforce_expect

#: Bumped when the stored vocabulary/DDL changes in a way an existing database
#: cannot satisfy as it stands. Stored in the database so `doctor` can tell an
#: old store from a corrupt one.
#:
#: 2: the status vocabulary grew a value -- "review", between "in_progress" and
#: "blocked" (see schema.STATUSES). The column type does not change, so no
#: migration is written: a v1 store still opens and works, and `doctor` reports
#: it as a version mismatch rather than pretending it is current.
#: 3: the `references` list field (root-relative plan paths under the arbite
#: dir, see schema.FIELD_ORDER) joined the schema and needs its own normalized
#: table, `ticket_references`. The `tickets` columns are unchanged, so no
#: migration is written either: a v1/v2 store still opens and works, and `doctor`
#: reports the version mismatch rather than pretending it is current.
SCHEMA_VERSION = 3

# Scalar columns, in the schema's own field order minus the list fields.
# `body` is a column as well but is deliberately NOT in FIELD_ORDER (it is the
# freeform half of a ticket, not frontmatter), so the stored set has to be stated
# explicitly rather than derived from FIELD_ORDER alone -- omitting it silently
# stored empty bodies, which the sink round-trip test caught.
SCALAR_FIELDS = tuple(f for f in FIELD_ORDER if f not in ("tags", "depends_on", "references"))
STORED_FIELDS = SCALAR_FIELDS + ("body",)

# `status` is deliberately plain `TEXT` with no CHECK constraint: the vocabulary
# is `schema.STATUSES`, validated by `schema.validate_field`/`validate_ticket`
# above the sink, so a hand-edited status is *reported* by `doctor` rather than
# rejected by the database -- otherwise `doctor` would mean something different
# per sink (see the module docstring).
DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT '',
    type       TEXT NOT NULL DEFAULT '',
    tier       TEXT NOT NULL DEFAULT '',
    domain     TEXT NOT NULL DEFAULT '',
    epic       TEXT,
    priority   INTEGER,
    assignee   TEXT,
    blocked_by TEXT,
    bucket     TEXT,
    body       TEXT NOT NULL DEFAULT '',
    created    TEXT NOT NULL DEFAULT '',
    updated    TEXT NOT NULL DEFAULT '',
    closed     TEXT
);

CREATE TABLE IF NOT EXISTS ticket_tags (
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    tag       TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, tag)
);

CREATE TABLE IF NOT EXISTS ticket_deps (
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    dep_id    TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, dep_id)
);

CREATE TABLE IF NOT EXISTS ticket_references (
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    ref_path  TEXT NOT NULL,
    ordinal   INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, ref_path)
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    ordinal   INTEGER NOT NULL,
    note_date TEXT NOT NULL DEFAULT '',
    agent     TEXT NOT NULL DEFAULT '',
    message   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ticket_id, ordinal)
);

CREATE INDEX IF NOT EXISTS tickets_status_idx   ON tickets(status, priority);
CREATE INDEX IF NOT EXISTS tickets_epic_idx     ON tickets(epic);
CREATE INDEX IF NOT EXISTS tickets_assignee_idx ON tickets(assignee);
CREATE INDEX IF NOT EXISTS ticket_deps_dep_idx  ON ticket_deps(dep_id);
CREATE INDEX IF NOT EXISTS ticket_references_ref_idx ON ticket_references(ref_path);
"""

# Canonical ordering, expressed in SQL. These must place unset priorities last and
# break ties on id exactly as `query.sort_key()` does -- the conformance suite
# compares the two, because a database that sorted differently would be a bug
# that only shows up in production.
ORDER_SQL = {
    "flat": "status, (priority IS NULL), priority, id",
    "next": "(priority IS NULL), priority, id",
    "created_asc": "created, id",
    "id": "id",
}


class SqliteSink(TicketSink):
    """Tickets as rows in a single SQLite database file."""

    kind = "sqlite"
    status_is_location = False
    supports_buckets = True

    def __init__(self, path: Path, timeout: float = 5.0):
        self._path = Path(path)
        # Seconds a writer waits for a lock before giving up. Agents run as
        # separate processes, so a lock *will* be contended occasionally; waiting
        # briefly is far better than failing a claim that would have succeeded.
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Lifecycle and connection handling
    # ------------------------------------------------------------------

    @property
    def root(self) -> str:
        return str(self._path)

    def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
            try:
                # WAL keeps a reader from blocking a writer, which matters when
                # several agent processes poll and claim concurrently.
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.executescript(DDL)
                conn.execute(
                    "INSERT INTO schema_version (version) "
                    "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
                    (SCHEMA_VERSION,),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise SinkError(f"could not initialise the SQLite sink at {self._path}: {e}")

    def _is_initialised(self, conn) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
        ).fetchone()
        return row is not None

    @contextmanager
    def _connect(self, write: bool = False):
        """A connection for one operation, in a transaction when writing.

        A connection per operation rather than a long-lived one, because arbite is
        a short-lived CLI process and sqlite is fast to open; `BEGIN IMMEDIATE`
        for writes takes the write lock up front so a compare-and-swap cannot
        succeed on a snapshot that another writer was already changing."""
        if not self._path.exists():
            raise SinkNotInitialised(
                f"no SQLite sink at {self._path} (run 'arbite init' to create it)"
            )
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._timeout)
        except sqlite3.Error as e:
            raise SinkError(f"could not open the SQLite sink at {self._path}: {e}")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            # SQLite disables foreign keys per connection by default, so without
            # this the schema's ON DELETE CASCADE would quietly do nothing and a
            # deleted ticket would leave its tag, dependency and note rows behind.
            conn.execute("PRAGMA foreign_keys = ON")
            if not self._is_initialised(conn):
                raise SinkNotInitialised(
                    f"{self._path} is not an arbite database (run 'arbite init')"
                )
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except sqlite3.Error as e:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise SinkError(f"SQLite error: {e}")
        finally:
            conn.close()

    def details(self) -> dict:
        return {
            "path": str(self._path),
            "schema_version": self.schema_version(),
            "journal_mode": self.journal_mode(),
        }

    def schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row["version"]) if row else 0

    def journal_mode(self) -> str:
        with self._connect() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else ""

    # ------------------------------------------------------------------
    # Row <-> Ticket
    # ------------------------------------------------------------------

    def _row_to_ticket(self, conn, row) -> Ticket:
        tid = row["id"]
        tags = [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM ticket_tags WHERE ticket_id = ? ORDER BY ordinal", (tid,)
            )
        ]
        deps = [
            r["dep_id"]
            for r in conn.execute(
                "SELECT dep_id FROM ticket_deps WHERE ticket_id = ? ORDER BY ordinal", (tid,)
            )
        ]
        refs = [
            r["ref_path"]
            for r in conn.execute(
                "SELECT ref_path FROM ticket_references WHERE ticket_id = ? ORDER BY ordinal",
                (tid,),
            )
        ]
        values = {name: row[name] for name in STORED_FIELDS}
        return Ticket(tags=tags, depends_on=deps, references=refs, **values)

    def _write_children(self, conn, ticket: Ticket) -> None:
        """Rewrite the tag, dependency, reference and note rows for one ticket.

        Child rows are derived from the Ticket, never merged: the ticket is the
        authority, and a derived index that disagrees with it is a bug that
        `doctor` reports (see `storage_problems`)."""
        conn.execute("DELETE FROM ticket_tags WHERE ticket_id = ?", (ticket.id,))
        conn.execute("DELETE FROM ticket_deps WHERE ticket_id = ?", (ticket.id,))
        conn.execute("DELETE FROM ticket_references WHERE ticket_id = ?", (ticket.id,))
        conn.execute("DELETE FROM ticket_notes WHERE ticket_id = ?", (ticket.id,))
        conn.executemany(
            "INSERT INTO ticket_tags (ticket_id, tag, ordinal) VALUES (?, ?, ?)",
            [(ticket.id, tag, i) for i, tag in enumerate(ticket.tags or [])],
        )
        conn.executemany(
            "INSERT INTO ticket_deps (ticket_id, dep_id, ordinal) VALUES (?, ?, ?)",
            [(ticket.id, dep, i) for i, dep in enumerate(ticket.depends_on or [])],
        )
        conn.executemany(
            "INSERT INTO ticket_references (ticket_id, ref_path, ordinal) VALUES (?, ?, ?)",
            [(ticket.id, ref, i) for i, ref in enumerate(ticket.references or [])],
        )
        conn.executemany(
            "INSERT INTO ticket_notes (ticket_id, ordinal, note_date, agent, message) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (ticket.id, n.ordinal, n.date, n.agent, n.message)
                for n in parse_notes(ticket.body)
            ],
        )

    def _insert_row(self, conn, ticket: Ticket, bucket: Optional[str]) -> None:
        columns = STORED_FIELDS + ("bucket",)
        values = [getattr(ticket, name) for name in STORED_FIELDS] + [bucket]
        placeholders = ", ".join("?" for _ in columns)
        try:
            conn.execute(
                f"INSERT INTO tickets ({', '.join(columns)}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError:
            raise Conflict(f"ticket {ticket.id} already exists in the SQLite sink")
        self._write_children(conn, ticket)

    # ------------------------------------------------------------------
    # Storage primitives
    # ------------------------------------------------------------------

    def ids(self) -> list:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM tickets ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def exists(self, ticket_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return row is not None

    def read(self, ticket_id: str) -> Ticket:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
            return self._row_to_ticket(conn, row)

    def location(self, ticket_id: str) -> str:
        if not self.exists(ticket_id):
            raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
        return f"sqlite:{self._path}#{ticket_id}"

    def location_map(self, tickets) -> dict:
        """A row's location is derived from its id, so no lookup is needed."""
        return {t.id: f"sqlite:{self._path}#{t.id}" for t in tickets}

    def insert(self, ticket: Ticket) -> Ticket:
        with self._connect(write=True) as conn:
            self._insert_row(conn, ticket, bucket=None)
        return ticket

    def update(self, ticket: Ticket, expect: Optional[Expect] = None) -> Ticket:
        with self._connect(write=True) as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
            if row is None:
                raise TicketNotFound(f"no ticket found matching '{ticket.id}'")
            current = self._row_to_ticket(conn, row)
            # The expectation is checked inside the same transaction that writes,
            # which is what makes this a compare-and-swap rather than a race with
            # a check in front of it.
            enforce_expect(current, expect)

            # A status change returns the ticket to the status workflow, which for
            # a file sink means its file moves into the status folder. Clearing the
            # bucket here is the same behavior expressed as a column.
            bucket = None if ticket.status != current.status else row["bucket"]

            assignments = ", ".join(f"{name} = ?" for name in STORED_FIELDS)
            conn.execute(
                f"UPDATE tickets SET {assignments}, bucket = ? WHERE id = ?",
                [getattr(ticket, name) for name in STORED_FIELDS] + [bucket, ticket.id],
            )
            self._write_children(conn, ticket)
        return ticket

    def remove(self, ticket_id: str) -> None:
        with self._connect(write=True) as conn:
            cur = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
            if cur.rowcount == 0:
                raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
            # Child rows go with it via ON DELETE CASCADE.

    def bucket(self, ticket_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT bucket FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
        return row["bucket"]

    def move_to_bucket(self, ticket_id: str, bucket: Optional[str]) -> Ticket:
        """File a ticket in a bucket, or with None return it to the status
        workflow. Changes no field: filing is not a state change."""
        if bucket is not None:
            parts = [p for p in str(bucket).split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                raise TicketError(f"bucket may not contain '..': '{bucket}'")
            bucket = "/".join(parts)
        with self._connect(write=True) as conn:
            cur = conn.execute("UPDATE tickets SET bucket = ? WHERE id = ?", (bucket, ticket_id))
            if cur.rowcount == 0:
                raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            return self._row_to_ticket(conn, row)

    def notes(self, ticket_id: str) -> list:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone() is None:
                raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
            rows = conn.execute(
                "SELECT ordinal, note_date, agent, message FROM ticket_notes "
                "WHERE ticket_id = ? ORDER BY ordinal",
                (ticket_id,),
            ).fetchall()
        return [
            Note(ordinal=r["ordinal"], date=r["note_date"], agent=r["agent"], message=r["message"])
            for r in rows
        ]

    def query(self, q: TicketQuery) -> list:
        """Structured predicates, ordering and (when no text filter is involved)
        limiting are done by SQL; text matching is applied by the shared reference
        matcher to the rows SQL returns.

        The split is deliberate, not lazy: `LIKE` is not equivalent to the
        reference matcher (see this module's docstring), so the text half cannot
        be pushed down without risking a different answer. The limit has to wait
        for the filter for the same reason -- a SQL LIMIT applied first would
        truncate before rows were rejected."""
        q = q.normalized()
        where = []
        params = []

        def add_in(column, values):
            where.append(f"{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)

        for column, values in (
            ("status", q.status),
            ("type", q.type),
            ("ids", q.ids),
        ):
            if values:
                add_in("id" if column == "ids" else column, values)
        for column, value in (
            ("tier", q.tier),
            ("domain", q.domain),
            ("epic", q.epic),
            ("assignee", q.assignee),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if q.priority is not None:
            where.append("priority = ?")
            params.append(q.priority)
        if not q.buckets:
            where.append("bucket IS NULL")
        elif "*" not in q.buckets:
            add_in("bucket", q.buckets)

        sql = "SELECT * FROM tickets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + ORDER_SQL[q.order]
        if q.text is None and q.limit is not None:
            sql += " LIMIT ?"
            params.append(q.limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            tickets = [self._row_to_ticket(conn, row) for row in rows]

        if q.text is not None:
            # Re-sort through the reference ordering: filtering must not change
            # the order, and asserting it here keeps SQL and Python ordering
            # honest even if one of them is adjusted later.
            tickets = sort_tickets([t for t in tickets if q.text.matches(t)], q.order)
        return tickets if q.limit is None else tickets[: q.limit]

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def storage_problems(self, fix: bool = False) -> list:
        """The database-specific invariants: a stale note index, rows orphaned by
        a hand-edited database, a schema version this build doesn't know, and
        SQLite's own structural check."""
        problems = []

        with self._connect() as conn:
            version = self.schema_version()
            if version != SCHEMA_VERSION:
                problems.append(
                    Problem(
                        "schema_version",
                        f"database schema version {version} does not match this arbite's "
                        f"{SCHEMA_VERSION}",
                        location=str(self._path),
                    )
                )

            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            verdict = str(integrity[0]) if integrity else "unknown"
            if verdict != "ok":
                problems.append(
                    Problem(
                        "database_integrity",
                        f"PRAGMA integrity_check reported: {verdict}",
                        location=str(self._path),
                    )
                )

            for row in conn.execute("SELECT id, body FROM tickets ORDER BY id").fetchall():
                tid = row["id"]
                stored = [
                    Note(
                        ordinal=r["ordinal"],
                        date=r["note_date"],
                        agent=r["agent"],
                        message=r["message"],
                    )
                    for r in conn.execute(
                        "SELECT ordinal, note_date, agent, message FROM ticket_notes "
                        "WHERE ticket_id = ? ORDER BY ordinal",
                        (tid,),
                    ).fetchall()
                ]
                derived = parse_notes(row["body"])
                if stored != derived:
                    if fix:
                        conn.execute("DELETE FROM ticket_notes WHERE ticket_id = ?", (tid,))
                        conn.executemany(
                            "INSERT INTO ticket_notes (ticket_id, ordinal, note_date, agent, "
                            "message) VALUES (?, ?, ?, ?, ?)",
                            [
                                (tid, n.ordinal, n.date, n.agent, n.message)
                                for n in derived
                            ],
                        )
                        conn.commit()
                        problems.append(
                            Problem(
                                "notes_index_drift",
                                f"the note index held {len(stored)} entr(ies) but the body has "
                                f"{len(derived)} -- index rebuilt from the body (the body is "
                                "authoritative)",
                                tid,
                                f"sqlite:{self._path}#{tid}",
                                fixed=True,
                            )
                        )
                    else:
                        problems.append(
                            Problem(
                                "notes_index_drift",
                                f"the note index holds {len(stored)} entr(ies) but the body has "
                                f"{len(derived)} -- the body is authoritative; re-run with "
                                "--fix to rebuild the index",
                                tid,
                                f"sqlite:{self._path}#{tid}",
                            )
                        )

            orphan_notes = conn.execute(
                "SELECT COUNT(*) AS n FROM ticket_notes n "
                "LEFT JOIN tickets t ON t.id = n.ticket_id WHERE t.id IS NULL"
            ).fetchone()["n"]
            orphan_tags = conn.execute(
                "SELECT COUNT(*) AS n FROM ticket_tags g "
                "LEFT JOIN tickets t ON t.id = g.ticket_id WHERE t.id IS NULL"
            ).fetchone()["n"]
            orphan_deps = conn.execute(
                "SELECT COUNT(*) AS n FROM ticket_deps d "
                "LEFT JOIN tickets t ON t.id = d.ticket_id WHERE t.id IS NULL"
            ).fetchone()["n"]
            orphan_refs = conn.execute(
                "SELECT COUNT(*) AS n FROM ticket_references r "
                "LEFT JOIN tickets t ON t.id = r.ticket_id WHERE t.id IS NULL"
            ).fetchone()["n"]
            orphans = orphan_notes + orphan_tags + orphan_deps + orphan_refs
            if orphans:
                if fix:
                    for table in ("ticket_notes", "ticket_tags", "ticket_deps", "ticket_references"):
                        conn.execute(
                            f"DELETE FROM {table} WHERE ticket_id NOT IN (SELECT id FROM tickets)"
                        )
                    conn.commit()
                problems.append(
                    Problem(
                        "orphan_index_rows",
                        f"{orphans} index row(s) point at tickets that no longer exist"
                        + (" -- removed" if fix else " (re-run with --fix to remove them)"),
                        location=str(self._path),
                        fixed=fix,
                    )
                )

        return problems
