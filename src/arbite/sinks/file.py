"""The file sink: tickets as markdown files under `.arbite/`, status as folder.

This is where the original design's central mechanic now lives, and lives alone.
A ticket's status decides the folder it sits in, so a state change is a file move
and the frontmatter is rewritten in the same operation -- but nothing above this
module knows that. Commands hand `update()` a Ticket whose status changed and
this module decides what that means on disk.

Three ordering decisions are deliberate, and each buys a specific guarantee:

- **In-place writes** stage a complete temp file in the same directory and
  `os.replace` it into position, so a crash can never leave a half-written
  ticket -- only a visible, recoverable temp file that `doctor` reports.
- **A compared update** (`update(..., expect=...)`, i.e. a claim) reserves the
  destination with `O_CREAT|O_EXCL`. That exclusive create is the mutex: two
  agents that both read the same open ticket and race to claim it both try to
  create `in_progress/<id>.md`, exactly one wins, and the loser gets a Conflict
  instead of silently overwriting the winner's assignee.
- **An unconditional relocation** stages the new content inside the destination
  directory, unlinks the source, then renames it into place. The worst case of a
  crash is a ticket that is briefly invisible but recoverable from its temp file
  -- never two files sharing one id, which would silently corrupt every listing,
  dependency walk and topological order.

Filenames never change on a move, so `git log --follow` on a ticket file traces
its whole lifecycle. That property, and the folder-is-truth invariant, are
features of *this* sink: a database sink has no files to follow.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from ..errors import Conflict, TicketError, TicketNotFound
from ..query import TicketQuery
from ..schema import ID_PATTERN, Ticket, parse_notes, parse_ticket
from .base import Expect, Problem, TicketSink, enforce_expect, filter_tickets

# Status -> folder, for every status except "closed", which archives by month.
FLAT_STATUS_DIRS = ("raw", "open", "in_progress", "blocked", "shelved")
CLOSED_DIR = "closed"

# Directories that are part of the layout but hold no tickets, plus non-status
# buckets `arbite init` creates. A bucket is somewhere a ticket can deliberately
# be filed *instead of* its status folder.
RESERVED_DIRS = ("agents",)
DEFAULT_BUCKETS = ("wishlist", "planning")

# Generated files that merely live under the root: never tickets.
GENERATED_FILES = ("AGENTS.md",)

# Temp files staged during an atomic write/move. Dot-prefixed and not suffixed
# .md, so the ticket scan never mistakes one for a ticket; a leftover is a crash
# artifact, and `doctor` reports it.
TMP_PREFIX = ".arbite-tmp-"


def write_atomic(text: str, path: Path) -> None:
    """Write `text` to `path` atomically via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def write_exclusive(text: str, path: Path, label: str) -> None:
    """Create `path` with `text`, failing if the name is already taken.

    The exclusive create is the compare-and-swap for a claim: whichever agent
    opens the name first owns the ticket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise Conflict(
            f"ticket {label} is already filed at {path} -- another agent claimed or "
            f"moved it first; re-read it with 'arbite show {label}'"
        )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if path.exists():
            path.unlink()
        raise


class FileSink(TicketSink):
    """Tickets as markdown files, with the folder standing in for the status."""

    kind = "file"
    status_is_location = True
    supports_buckets = True

    def __init__(self, root: Path):
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def root(self) -> str:
        return str(self._root)

    def init(self) -> None:
        for status in FLAT_STATUS_DIRS:
            (self._root / status).mkdir(parents=True, exist_ok=True)
        (self._root / CLOSED_DIR).mkdir(parents=True, exist_ok=True)
        for bucket in DEFAULT_BUCKETS:
            (self._root / bucket).mkdir(parents=True, exist_ok=True)
        for reserved in RESERVED_DIRS:
            (self._root / reserved).mkdir(parents=True, exist_ok=True)

    def details(self) -> dict:
        return {
            "status_dirs": list(FLAT_STATUS_DIRS),
            "closed_dir": CLOSED_DIR,
            "default_buckets": list(DEFAULT_BUCKETS),
            "buckets": self.buckets(),
            "tmp_prefix": TMP_PREFIX,
        }

    def buckets(self) -> list:
        """Every bucket currently in use, sorted. A bucket is any directory
        under the root that isn't a status folder -- `wishlist`, `planning`,
        `planning/ideas`."""
        found = set()
        for path in self._iter_files():
            bucket = self._bucket_for(path)
            if bucket:
                found.add(bucket)
        return sorted(found)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def status_dir(self, status: str, closed_date: Optional[str] = None) -> Path:
        """The folder a ticket with this status belongs in. `closed` archives by
        close date so the archive doesn't become one flat directory."""
        if status == CLOSED_DIR:
            month = (closed_date or "")[:7]
            if not month:
                raise TicketError("a closed ticket needs a 'closed' date to archive by month")
            return self._root / CLOSED_DIR / month
        if status not in FLAT_STATUS_DIRS:
            raise TicketError(f"unknown status: {status}")
        return self._root / status

    def _relative_parts(self, path: Path) -> tuple:
        return path.relative_to(self._root).parts

    def _bucket_for(self, path: Path) -> Optional[str]:
        """Where this file is filed: None when the folder already implies a
        status (a status folder or the closed archive), otherwise the
        root-relative bucket ('' meaning the root itself)."""
        parts = self._relative_parts(path)
        if len(parts) == 2 and parts[0] in FLAT_STATUS_DIRS:
            return None
        if parts[0] == CLOSED_DIR:
            return None
        return "/".join(parts[:-1])

    def _expected_status_for(self, path: Path) -> Optional[str]:
        """The status this file's location implies, or None if its location
        implies none (a bucket: legitimate filing, not a state)."""
        parts = self._relative_parts(path)
        if len(parts) == 2 and parts[0] in FLAT_STATUS_DIRS:
            return parts[0]
        if len(parts) == 3 and parts[0] == CLOSED_DIR:
            return CLOSED_DIR
        return None

    def _is_ticket_file(self, path: Path) -> bool:
        """Whether a file under the root must be treated as a ticket.

        Everything in a status folder or the closed archive must be one. Anywhere
        else only a ticket-shaped filename counts, because `planning/` is
        documented to hold non-ticket markdown notes and scanning those as
        tickets would report them as unreadable."""
        try:
            parts = self._relative_parts(path)
        except ValueError:
            return False
        if parts[0] in RESERVED_DIRS or path.name in GENERATED_FILES:
            return False
        if path.name.startswith(TMP_PREFIX):
            return False
        if len(parts) == 2 and parts[0] in FLAT_STATUS_DIRS:
            return True
        if len(parts) == 3 and parts[0] == CLOSED_DIR:
            return True
        return bool(ID_PATTERN.match(path.stem))

    def _iter_files(self):
        for path in sorted(self._root.rglob("*.md")):
            if self._is_ticket_file(path):
                yield path

    def _scan(self) -> list:
        """`[(path, Ticket, error)]` for every ticket-shaped file: unparseable
        files come back as (path, None, message) rather than raising, because
        `doctor` exists precisely to report them."""
        out = []
        for path in self._iter_files():
            try:
                out.append((path, parse_ticket(path.read_text(encoding="utf-8")), None))
            except (TicketError, OSError) as e:
                out.append((path, None, f"{path}: {e}"))
        return out

    def _locate(self, ticket_id: str) -> tuple:
        """The file holding an exact id, as (path, Ticket).

        A duplicated id is refused rather than guessed: writing to the wrong copy
        is worse than failing, which is the same rule `doctor` applies."""
        matches = [(p, t) for p, t, _e in self._scan() if t is not None and t.id == ticket_id]
        if not matches:
            raise TicketNotFound(f"no ticket found matching '{ticket_id}'")
        if len(matches) > 1:
            where = ", ".join(str(p) for p, _t in matches)
            raise Conflict(
                f"{len(matches)} files share id {ticket_id}: {where} -- resolve by hand "
                "('arbite doctor' reports this)"
            )
        return matches[0]

    def _index(self) -> tuple:
        """`(tickets, {id: bucket})` for every readable ticket, including ones
        filed in buckets. Commands see buckets or not according to the query."""
        tickets = []
        buckets = {}
        for path, ticket, _e in self._scan():
            if ticket is None:
                continue
            tickets.append(ticket)
            buckets[ticket.id] = self._bucket_for(path)
        return tickets, buckets

    # ------------------------------------------------------------------
    # Storage primitives
    # ------------------------------------------------------------------

    def ids(self) -> list:
        return sorted({t.id for _p, t, _e in self._scan() if t is not None})

    def read(self, ticket_id: str) -> Ticket:
        return self._locate(ticket_id)[1]

    def location(self, ticket_id: str) -> str:
        return str(self._locate(ticket_id)[0])

    def storage_locations(self, ticket_id: str) -> list:
        return [
            str(p) for p, t, _e in self._scan() if t is not None and t.id == ticket_id
        ]

    def location_map(self, tickets) -> dict:
        """Every ticket's path, from a single walk of the tree."""
        wanted = {t.id for t in tickets}
        found = {}
        for path, ticket, _error in self._scan():
            if ticket is not None and ticket.id in wanted:
                found.setdefault(ticket.id, str(path))
        return found

    def exists(self, ticket_id: str) -> bool:
        return bool([p for p, t, _e in self._scan() if t is not None and t.id == ticket_id])

    def insert(self, ticket: Ticket) -> Ticket:
        dest = self.status_dir(ticket.status, ticket.closed) / f"{ticket.id}.md"
        if dest.exists():
            raise Conflict(f"a ticket file already exists at {dest}")
        write_atomic(ticket.to_markdown(), dest)
        return ticket

    def update(self, ticket: Ticket, expect: Optional[Expect] = None) -> Ticket:
        path, current = self._locate(ticket.id)
        enforce_expect(current, expect)

        # A status change always lands in the status tree, which is what moves a
        # ticket out of a bucket: an unshelved or claimed ticket is back in the
        # workflow. Any other update stays where the ticket already is.
        if ticket.status != current.status:
            dest = self.status_dir(ticket.status, ticket.closed) / path.name
        else:
            dest = path

        if dest == path:
            write_atomic(ticket.to_markdown(), dest)
            return ticket

        self._relocate(path, dest, ticket, exclusive=expect is not None)
        return ticket

    def remove(self, ticket_id: str) -> None:
        path, _ticket = self._locate(ticket_id)
        path.unlink()

    def query(self, q: TicketQuery) -> list:
        tickets, buckets = self._index()
        return filter_tickets(tickets, q, buckets)

    def bucket(self, ticket_id: str) -> Optional[str]:
        return self._bucket_for(self._locate(ticket_id)[0])

    def move_to_bucket(self, ticket_id: str, bucket: Optional[str]) -> Ticket:
        """File a ticket somewhere other than its status folder, or with None
        return it to the folder its status implies.

        Deliberately changes no field: filing is not a state change. The `bucket`
        is a root-relative path ('' is the root), and '..' is refused so a ticket
        can never be moved out of the arbite root."""
        path, ticket = self._locate(ticket_id)
        if bucket is None:
            dest = self.status_dir(ticket.status, ticket.closed) / path.name
        else:
            parts = [p for p in str(bucket).split("/") if p and p != "."]
            if any(p == ".." for p in parts):
                raise TicketError(f"bucket may not contain '..': '{bucket}'")
            dest = self._root.joinpath(*parts) / path.name if parts else self._root / path.name
        if dest == path:
            return ticket
        self._relocate(path, dest, ticket, exclusive=False)
        return ticket

    def notes(self, ticket_id: str) -> list:
        return parse_notes(self.read(ticket_id).body)

    def _relocate(self, source: Path, dest: Path, ticket: Ticket, exclusive: bool) -> None:
        """Move a ticket file, writing its current content to `dest`.

        `exclusive=True` (a compared update, i.e. a claim) reserves the
        destination name first, so exactly one of several racing agents can move
        a ticket into in_progress/. `exclusive=False` stages in the destination
        directory, unlinks the source and renames, so a crash can't leave two
        files with one id."""
        if dest.exists():
            raise Conflict(f"a ticket file already exists at {dest}")
        if exclusive:
            write_exclusive(ticket.to_markdown(), dest, ticket.id)
            if source.exists():
                source.unlink()
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(dest.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(ticket.to_markdown())
                fh.flush()
                os.fsync(fh.fileno())
            if source.exists():
                source.unlink()
            os.replace(tmp, dest)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def storage_problems(self, fix: bool = False) -> list:
        """The file-specific invariants: folder/frontmatter drift, a ticket left
        loose at the root, an archive in the wrong month, unreadable files, and
        temp files stranded by a crash.

        A ticket filed in a bucket is *not* a problem: its location deliberately
        implies no status, so there is nothing to drift from."""
        problems = []

        for path, ticket, error in self._scan():
            if ticket is None:
                problems.append(Problem("unreadable", error, location=str(path)))
                continue

            bucket = self._bucket_for(path)
            expected = self._expected_status_for(path)

            if bucket == "":
                # Lying loose in the arbite root: nothing owns it. Its status is
                # known from the frontmatter, so refiling it is unambiguous.
                detail = f"ticket file sitting in the arbite root: {path}"
                if fix:
                    dest = self.status_dir(ticket.status, ticket.closed) / path.name
                    if not dest.exists():
                        self._relocate(path, dest, ticket, exclusive=False)
                        detail = f"{detail} -- moved to {dest}"
                        problems.append(
                            Problem("stray_file", detail, ticket.id, str(dest), fixed=True)
                        )
                        continue
                problems.append(Problem("stray_file", detail, ticket.id, str(path)))

            if expected is None:
                continue

            # The core invariant: the folder wins, the frontmatter is corrected.
            if ticket.status != expected:
                if fix:
                    stale = ticket.status
                    ticket.status = expected
                    write_atomic(ticket.to_markdown(), path)
                    problems.append(
                        Problem(
                            "status_drift",
                            f"frontmatter said '{stale}' but the file sits in {expected}/ "
                            f"-- corrected to '{expected}' (folder is source of truth)",
                            ticket.id,
                            str(path),
                            fixed=True,
                        )
                    )
                else:
                    problems.append(
                        Problem(
                            "status_drift",
                            f"frontmatter says status '{ticket.status}' but the file sits in "
                            f"{expected}/ -- the folder is source of truth",
                            ticket.id,
                            str(path),
                        )
                    )

            if expected == CLOSED_DIR and ticket.closed:
                month = ticket.closed[:7]
                actual_month = path.parent.name
                if month != actual_month:
                    if fix:
                        dest = self.status_dir(CLOSED_DIR, ticket.closed) / path.name
                        if not dest.exists():
                            self._relocate(path, dest, ticket, exclusive=False)
                            problems.append(
                                Problem(
                                    "wrong_archive_month",
                                    f"closed {ticket.closed} but archived under {actual_month}/ "
                                    f"-- moved to {dest.parent.name}/",
                                    ticket.id,
                                    str(dest),
                                    fixed=True,
                                )
                            )
                            continue
                    problems.append(
                        Problem(
                            "wrong_archive_month",
                            f"closed {ticket.closed} but archived under closed/{actual_month}/ "
                            f"(expected closed/{month}/)",
                            ticket.id,
                            str(path),
                        )
                    )

        # Crash artifacts from an interrupted save or move. The content is intact,
        # so this is a recoverable ticket, not a lost one -- but only if someone
        # looks, which is why it is reported rather than cleaned up silently.
        for tmp in sorted(self._root.rglob(f"{TMP_PREFIX}*")):
            if tmp.is_file():
                problems.append(
                    Problem(
                        "stray_temp_file",
                        f"leftover temp file from an interrupted write: {tmp} "
                        "(inspect it; it holds the full ticket content)",
                        location=str(tmp),
                    )
                )

        return problems
