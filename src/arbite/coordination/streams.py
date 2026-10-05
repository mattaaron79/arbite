"""Narration streams: the per-ticket log a worker writes while it works.

A stream is free-form prose about one ticket -- "reading the sink's scan", "the
spec says four hex characters" -- and it is deliberately *not* either of the two
logs arbite already has. `arbite note <id> <agent> "..."` appends a milestone to the
ticket body, which means a ticket rewrite per line; `arbite events` carries
structured *coordination* facts (claims, reads, writes) as one record per event in
the store. Neither is a live tail a dashboard can poll while an agent thinks, which
is what this module exists for.

The area lives in the *project* rather than in the sink (`.arbite/streams/`,
gitignored runtime state), for the same reason scratch does: the state is local
observation, not the git-tracked development record. One file per ticket,
`<ticket_id>.jsonl`, one JSON object per line, append-only, written only here --
`arbite stream write` is the single writer, so the line format is a private
contract rather than a schema other components may extend.

Records carry `seq`, `recorded_at`, `ticket_id`, `attempt_id`, `actor`, `kind` and
`text`, and the read view renames the three record-shaped names to the ones the
event view prints (`at`, `ticket`, `attempt`) so a consumer polling a stream and a
consumer polling `arbite events` read the same vocabulary. `seq` is the cursor:
monotonic *within one ticket's file* -- not per store, because a stream is not a
coordination object and nothing global orders it -- which is what makes
`arbite stream read <id> --after <seq>` resumable, and "nothing new" exit 2 rather
than an error, exactly as the event stream's `--after` works.

Adoption is suggested rather than forced. `arbite claim` surfaces the file and the
command that writes to it, `arbite submit` says so with a `note:` when the ending
attempt wrote nothing, and `arbite doctor` reports the area -- but nothing refuses
because a worker stayed quiet, because narration is a courtesy to the human
watching, never a precondition of the work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import schema
from ..errors import CoordinationError, PathRefused, TicketError
from .locking import StoreLock
from .records import parse_utc, utc_now
from .results import EMPTY, OK, OperationResult, Outcome, succeeded
from .scratch import human_size

#: The narration area's name inside the arbite directory.
STREAMS_DIRNAME = "streams"

#: Every stream file's suffix. Chosen so a stream can never be read as a ticket:
#: the file sink's ticket scan matches `.md` names.
STREAM_SUFFIX = ".jsonl"

#: The lock every append takes, beside the streams it guards. It is never a stream
#: and never removed by `arbite stream clear`.
LOCK_NAME = ".lock"

#: The kinds a record can have, and the one a caller that does not say gets.
#: Deliberately three: what was being thought, what was done, what came back.
STREAM_KINDS = ("thought", "action", "result")
DEFAULT_KIND = "thought"

#: How many records a bare `arbite stream read` prints. The same default the event
#: stream uses, so the two poll surfaces have one number to learn.
DEFAULT_STREAM_TAIL = 20

#: The stored record's keys, in the order one JSONL line carries them.
SEQ_KEY = "seq"
RECORDED_KEY = "recorded_at"
TICKET_KEY = "ticket_id"
ATTEMPT_KEY = "attempt_id"
ACTOR_KEY = "actor"
KIND_KEY = "kind"
TEXT_KEY = "text"

#: The read row's columns: the sequence, the local time, the actor, the kind, and
#: the text last (it is the only unbounded one).
SEQ_WIDTH = 4
TIME_WIDTH = 10
ACTOR_WIDTH = 18
KIND_WIDTH = 8

#: The `arbite stream list` header and row, and the empty-area answer. A stream
#: file with no records at all can only be one a caller created by hand, so the row
#: says that rather than inventing an author or a time.
LIST_HEADER = "{count} in .arbite/streams/:"
LIST_ROW = "  {ticket}   {records} record(s)  {size}  last {last} by {actor}"
LIST_EMPTY_FILE = "  {ticket}   {records} record(s)  {size}  (no records yet)"
NO_STREAMS = "no streams in .arbite/streams/"

#: `arbite stream clear`: one ticket reads as a sentence about that file and hands
#: back the list to run next; `--all` reports the count and reports no next step.
CLEARED_ONE = "cleared .arbite/streams/{ticket}.jsonl ({records} record(s), {size})"
CLEARED_MANY = "cleared {count} stream(s) from .arbite/streams/ ({rows})"
CLEARED_NONE = "cleared 0 streams from .arbite/streams/ (nothing was recorded)"
CLEARED_ROW = "{ticket} ({records} record(s), {size})"
CLEAR_FAILED = "could not clear .arbite/streams/{ticket}.jsonl: {reason}"
NO_SUCH_STREAM = "no stream for '{ticket}' in .arbite/streams/"
CLEAR_NEEDS_TARGET = "'stream clear' needs a ticket id, or '--all' to clear every stream"
CLEAR_NOT_BOTH = "'stream clear' takes ticket ids or '--all', not both"
REMAINS_HINT = "next: 'arbite stream list' to see what remains"
STAGED_HINT = "next: 'arbite stream list' to see what is recorded"

#: The refusals a malformed target gets before any file is touched.
BAD_TICKET = "'{ticket}' is not a ticket id (they look like 'tic-a1b2')"

#: The command a stream report hands back, so the hint text and the `next_actions`
#: it claims to describe cannot name two different commands.
LIST_COMMAND = "arbite stream list"

#: How an acquisition surfaces a ticket's stream: the file to tail, and the command a
#: worker pipes its own narration through. One constant, because `claim` and
#: `promote --agent` both print it and JSON carries the same two facts.
HINT_COMMAND = "arbite stream write {ticket} -"
HINT_LINE = "stream: {path} (write with '{write}')"


def streams_root(arbite_dir) -> Path:
    """Where this project's narration streams live."""
    return Path(arbite_dir) / STREAMS_DIRNAME


def ensure_streams_dir(arbite_dir) -> Path:
    """Create the narration area if it does not exist, and return it.

    Idempotent, and called by every append rather than by a command: the file sink's
    layout creates the directory at `init`, and a project on another sink makes it on
    the first record. The directory being present says nothing about whether anything
    was recorded."""
    root = streams_root(arbite_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def stream_path(arbite_dir, ticket_id) -> Path:
    """The file holding one ticket's stream.

    The ticket id is validated against the ticket vocabulary first, so this can
    never be talked into naming a file outside the area: a caller that passes a path
    gets a refusal rather than a traversal, and the root is never derived from the
    id."""
    text = str(ticket_id)
    if not schema.ID_PATTERN.match(text):
        raise TicketError(BAD_TICKET.format(ticket=text))
    return streams_root(arbite_dir) / f"{text}{STREAM_SUFFIX}"


def _display_path(ticket_id: str) -> str:
    """How the reports name one stream: the project-relative path."""
    return f".arbite/{STREAMS_DIRNAME}/{ticket_id}{STREAM_SUFFIX}"


def _parse_line(raw: str) -> Optional[dict]:
    """One stored line as a record, or None when it is blank or unparseable.

    A half-written last line -- a writer that died between the bytes and the newline
    -- is skipped rather than fatal: the stream is a log, and a log that refuses to
    be read is worse than one that reports what it has."""
    text = raw.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def read_records(arbite_dir, ticket_id) -> tuple:
    """`([record, ...], skipped)`: one ticket's records in file order, and how many
    lines could not be parsed.

    A missing file is `([], 0)` rather than an error -- every project before its
    first record -- so a reader never has to distinguish "no stream" from "an empty
    one"."""
    path = stream_path(arbite_dir, ticket_id)
    if not path.is_file():
        return [], 0
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return [], 0
    records = []
    skipped = 0
    for line in raw.splitlines():
        record = _parse_line(line)
        if record is None:
            if line.strip():
                skipped += 1
            continue
        records.append(record)
    return records, skipped


def _stream_files(arbite_dir) -> list:
    """Every stream file in the area, in name order; the lock file is not a stream."""
    root = streams_root(arbite_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"*{STREAM_SUFFIX}") if path.is_file())


def record_count(arbite_dir, ticket_id=None, attempt_id=None) -> int:
    """How many records the filters select.

    One ticket's file when `ticket_id` is given (the common case: the gate on
    `arbite submit` asks about the attempt that just ended), every stream otherwise
    -- which is what `arbite doctor` needs to answer "did this attempt narrate
    anything" without the ticket at hand."""
    if ticket_id is not None:
        records = read_records(arbite_dir, ticket_id)[0]
    else:
        records = []
        for path in _stream_files(arbite_dir):
            records.extend(read_records(arbite_dir, path.stem)[0])
    if attempt_id is None:
        return len(records)
    return sum(1 for record in records if record.get(ATTEMPT_KEY) == attempt_id)


def append_records(arbite_dir, ticket_id, attempt_id, actor, kind, texts) -> list:
    """Append one record per text, and return the sequences written.

    Under the area's lock, because two attempts on two tickets (or two workers on
    one) share the directory: the lock is what makes "read the last sequence, write
    the next one" atomic. A caller that cannot take it is refused with `Busy`
    (exit 4) and writes nothing, rather than interleaving a half-written file.

    Every call re-reads the file's last sequence instead of trusting a counter
    anybody could hold, so a stream a human edited by hand continues rather than
    colliding."""
    if not texts:
        return []
    if kind not in STREAM_KINDS:
        raise CoordinationError(
            f"unknown stream kind '{kind}' (known: {', '.join(STREAM_KINDS)})"
        )
    root = ensure_streams_dir(arbite_dir)
    path = stream_path(arbite_dir, ticket_id)
    lock = StoreLock(root / LOCK_NAME, describe=str(arbite_dir))
    written = []
    with lock.hold():
        existing = [record for record in read_records(arbite_dir, ticket_id)[0]]
        next_seq = (existing[-1].get(SEQ_KEY) or 0) + 1 if existing else 1
        with open(path, "a", encoding="utf-8") as handle:
            for text in texts:
                record = {
                    SEQ_KEY: next_seq,
                    RECORDED_KEY: utc_now(),
                    TICKET_KEY: str(ticket_id),
                    ATTEMPT_KEY: None if attempt_id is None else str(attempt_id),
                    ACTOR_KEY: None if actor is None else str(actor),
                    KIND_KEY: kind,
                    TEXT_KEY: str(text),
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written.append(next_seq)
                next_seq += 1
            handle.flush()
    return written


def stream_hint(arbite_dir, ticket_id) -> dict:
    """Where a ticket's stream is, and the command that writes to it.

    The two facts an acquisition hands a worker, as a value rather than as printed
    text: the path is absolute because it is a file to tail, and `arbite stream write
    <id> -` is the command that reads a worker's own output. `HINT_LINE` is how those
    facts read on a report, so the text and the JSON cannot come to mean two things."""
    return {
        "path": str(stream_path(arbite_dir, ticket_id)),
        "write": HINT_COMMAND.format(ticket=str(ticket_id)),
    }


def _local_clock(recorded_at) -> str:
    """A stored timestamp as the row prints it: local time, which is how a reader
    places it (the same rule `arbite events` follows)."""
    return parse_utc(recorded_at).astimezone().strftime("%H:%M:%S")


def stream_entry_view(record: dict) -> dict:
    """One stored record as the read view: the event vocabulary's field names.

    Only the three record-shaped names change (`recorded_at`, `ticket_id`,
    `attempt_id`); `seq` stays, because it is the value `--after` takes."""
    return {
        SEQ_KEY: record.get(SEQ_KEY),
        "at": record.get(RECORDED_KEY),
        "ticket": record.get(TICKET_KEY),
        "attempt": record.get(ATTEMPT_KEY),
        ACTOR_KEY: record.get(ACTOR_KEY),
        KIND_KEY: record.get(KIND_KEY),
        TEXT_KEY: record.get(TEXT_KEY),
    }


def _stream_row(record: dict) -> str:
    """One record as a line: sequence, local time, actor, kind, then the text."""
    return (
        f"{record.get(SEQ_KEY):<{SEQ_WIDTH}}"
        f"{_local_clock(record.get(RECORDED_KEY)):<{TIME_WIDTH}}"
        f"{record.get(ACTOR_KEY) or '':<{ACTOR_WIDTH}}"
        f"{record.get(KIND_KEY) or '':<{KIND_WIDTH}}"
        f"{record.get(TEXT_KEY) or ''}"
    ).rstrip()


@dataclass(frozen=True)
class StreamEntry:
    """One ticket's stream: what it holds, how big it is, and where it left off.

    A stream has no record of its own in the coordination store -- it is a file
    beside the tickets, not a coordination object -- so everything a report says
    about one is read from the file itself."""

    ticket: str
    path: Path
    records: int
    bytes: int
    cursor: Optional[int] = None
    last_at: Optional[str] = None
    last_actor: Optional[str] = None
    attempt: Optional[str] = None

    @property
    def display(self) -> str:
        """The path a report names it by."""
        return _display_path(self.ticket)

    def row(self) -> str:
        """A `arbite stream list` row, or the honest note that it is empty."""
        if self.last_at is None:
            return LIST_EMPTY_FILE.format(
                ticket=self.ticket, records=self.records, size=human_size(self.bytes)
            )
        return LIST_ROW.format(
            ticket=self.ticket,
            records=self.records,
            size=human_size(self.bytes),
            last=_local_clock(self.last_at),
            actor=self.last_actor or "(unattributed)",
        )

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "path": self.display,
            "records": self.records,
            "bytes": self.bytes,
            "cursor": self.cursor,
            "last_at": self.last_at,
            "last_actor": self.last_actor,
            "attempt": self.attempt,
        }


def stream_entries(arbite_dir) -> list:
    """Every ticket's stream in the area, in name order.

    One row per file, with the count, the size and the last record's sequence, time,
    actor and attempt: the shape a listing needs, so a reader can tell a busy
    ticket's stream from an idle one without reading either."""
    entries = []
    for path in _stream_files(arbite_dir):
        try:
            size = path.stat().st_size
        except OSError:
            # A stream somebody removed while this listing was being built is not a
            # reason to fail a report about the others.
            continue
        records, _ = read_records(arbite_dir, path.stem)
        last = records[-1] if records else {}
        entries.append(
            StreamEntry(
                ticket=path.stem,
                path=path,
                records=len(records),
                bytes=size,
                cursor=last.get(SEQ_KEY),
                last_at=last.get(RECORDED_KEY),
                last_actor=last.get(ACTOR_KEY),
                attempt=last.get(ATTEMPT_KEY),
            )
        )
    return entries


@dataclass(frozen=True)
class StreamSummary:
    """What the narration area holds: a count and a size.

    A quiet stream area is the *normal* state -- narration is suggested, not
    required -- so unlike scratch's summary this one is never a leftover worth
    raising; it is reported so a reader can see the area is there and empty."""

    files: int = 0
    bytes: int = 0

    @property
    def is_empty(self) -> bool:
        return self.files == 0

    def describe(self) -> str:
        """The parenthesised summary `arbite workspace show` prints."""
        if self.is_empty:
            return "no streams"
        return f"{self.files} stream(s), {human_size(self.bytes)}"

    def to_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes}


def stream_summary(arbite_dir) -> StreamSummary:
    """Count the stream files under `arbite_dir`, and total their sizes.

    A missing area -- every project before its first record -- is an empty summary
    rather than an error, so `doctor` and `workspace show` work in a store that has
    never carried one."""
    files = 0
    total = 0
    for path in _stream_files(arbite_dir):
        files += 1
        try:
            total += path.stat().st_size
        except OSError:
            # A stream that vanished between the walk and the stat is not a reason to
            # fail a report about the others.
            continue
    return StreamSummary(files=files, bytes=total)


def stream_note_lines(summary: StreamSummary, missing) -> list:
    """The `note:` lines `doctor` prints about narration.

    An empty area with nothing in flight says nothing at all -- the quiet case is
    the normal one, and a report that announced "0 streams" on every clean project
    would teach a reader to ignore it. What is worth saying is the two facts a human
    watching a run actually wants: the area is being used, and these in-flight
    tickets are not narrating."""
    lines = []
    if not summary.is_empty:
        lines.append(
            f"note: .arbite/streams/ holds {summary.files} stream(s) "
            f"({human_size(summary.bytes)})"
        )
    quiet = list(missing)
    if quiet:
        lines.append(
            f"note: {len(quiet)} ticket(s) in flight have no stream entries: "
            f"{', '.join(quiet)}"
        )
    return lines


def stream_write(arbite_dir, ticket_id, attempt_id, actor, kind, texts) -> OperationResult:
    """`arbite stream write`: record one line per text, and report where they landed.

    The command a worker runs as it works. It reports the sequences written so a
    caller can resume a poll from exactly those, and it names the file rather than
    only the ticket, because the file is what a dashboard tails."""
    texts = list(texts)
    seqs = append_records(arbite_dir, ticket_id, attempt_id, actor, kind, texts)
    display = _display_path(ticket_id)
    if not seqs:
        # A caller that asked for nothing gets an honest zero: `append_records` is a
        # no-op for it, and the command layer is where "nothing to write" is refused.
        lines = [f"wrote 0 record(s) to {display}"]
    elif len(seqs) == 1:
        lines = [f"wrote 1 record to {display} (seq {seqs[0]})"]
    else:
        lines = [f"wrote {len(seqs)} record(s) to {display} (seq {seqs[0]}..{seqs[-1]})"]
    return succeeded(
        lines=lines,
        data={
            "ticket": str(ticket_id),
            "attempt": None if attempt_id is None else str(attempt_id),
            "actor": None if actor is None else str(actor),
            "kind": kind,
            "path": display,
            "records": len(seqs),
            "cursor": seqs[-1] if seqs else 0,
            "seqs": seqs,
        },
    )


def stream_read(arbite_dir, ticket_id, after=None, tail=None) -> OperationResult:
    """`arbite stream read`: one line per record, and the sequence to resume from.

    The selection rules are the event stream's: `--tail N` prints the last N of what
    is there, `--after SEQ` prints everything after that sequence, a bare read prints
    the last `DEFAULT_STREAM_TAIL`, and the two flags are alternatives. "Nothing new"
    is an answer with its own exit code (2) rather than a failure, so a polling loop
    branches on the code instead of matching prose."""
    if after is not None and tail is not None:
        raise CoordinationError(
            "--after and --tail are alternatives: resume with '--after <seq>', "
            "or bootstrap with '--tail <count>'"
        )
    if tail is not None and tail < 1:
        raise CoordinationError(f"--tail must be at least 1, got {tail}")
    if after is not None and after < 0:
        raise CoordinationError(f"--after is a sequence number, so it is 0 or more, got {after}")

    records = read_records(arbite_dir, ticket_id)[0]
    if tail is not None:
        window = records[-tail:]
    elif after is not None:
        window = [record for record in records if (record.get(SEQ_KEY) or 0) > after]
    else:
        window = records[-DEFAULT_STREAM_TAIL:]

    cursor = window[-1].get(SEQ_KEY) if window else (after or 0)
    resume = f"arbite stream read {ticket_id} --after {cursor}"
    data = {
        "ticket": str(ticket_id),
        "records": [stream_entry_view(record) for record in window],
        "cursor": cursor,
        "next_actions": [resume] if window else [],
    }
    if not window:
        message = (
            f"no stream for {ticket_id} since seq {after}"
            if after is not None
            else f"no stream for {ticket_id}"
        )
        return OperationResult(Outcome(EMPTY), [message], data, [])
    return OperationResult(
        Outcome(OK),
        [_stream_row(record) for record in window]
        + [f"cursor: {cursor} (resume with '{resume}')"],
        data,
        [],
    )


def stream_list(arbite_dir) -> OperationResult:
    """`arbite stream list`: which tickets are narrating, and how far each one got.

    Nothing recorded is outcome 2 rather than an error -- the answer `file list`
    gives for an empty directory -- and it names no next step, because there is
    nothing to run about an area that is already empty."""
    entries = stream_entries(arbite_dir)
    if not entries:
        return OperationResult(
            Outcome(EMPTY), [NO_STREAMS], {"streams": [], "count": 0}, []
        )
    return succeeded(
        lines=[LIST_HEADER.format(count=f"{len(entries)} stream(s)")]
        + [entry.row() for entry in entries],
        data={"streams": [entry.to_dict() for entry in entries], "count": len(entries)},
    )


def stream_path_result(arbite_dir, ticket_id) -> OperationResult:
    """`arbite stream path`: where one ticket's stream is, so a tail can follow it.

    The one report that prints an **absolute** path: a file to `tail -f` or open is
    named for the shell that will use it, not for a reader of the report."""
    path = stream_path(arbite_dir, ticket_id)
    records, _ = read_records(arbite_dir, ticket_id)
    return succeeded(
        lines=[str(path)],
        data={"ticket": str(ticket_id), "path": str(path), "records": len(records)},
    )


def stream_clear(arbite_dir, ticket_ids=(), all_: bool = False) -> OperationResult:
    """`arbite stream clear TICKET...` and `--all`: delete streams deliberately.

    The documentation calls this the prune, so it is the only thing that removes a
    stream; nothing else deletes narration. A ticket id with no stream is refused
    with the listing that shows what is recorded, and `--all` on an empty area is an
    honest zero rather than an error.

    The area's lock file is never a stream and therefore never cleared: it is a
    rendezvous point, and a stream area missing it would make the next append create
    it again anyway."""
    wanted = [str(ticket_id).strip() for ticket_id in ticket_ids]
    if all_ and wanted:
        raise CoordinationError(CLEAR_NOT_BOTH, text_hint=STAGED_HINT)
    if not all_ and not wanted:
        raise CoordinationError(CLEAR_NEEDS_TARGET, text_hint=STAGED_HINT)
    entries = stream_entries(arbite_dir)
    if all_:
        return _cleared_report(entries, all_=True)
    return _cleared_report(_named_streams(entries, wanted), all_=False)


def _named_streams(entries, wanted) -> list:
    """The streams `wanted` names, or the refusal that stops the clear before it starts.

    Ids are matched against the files that are really there, so a malformed id -- or
    one with a separator in it -- is simply not found and nothing is ever built from
    the caller's text."""
    known = {entry.ticket: entry for entry in entries}
    for ticket in wanted:
        if ticket not in known:
            raise PathRefused(NO_SUCH_STREAM.format(ticket=ticket), text_hint=STAGED_HINT)
    named = set(wanted)
    return [entry for entry in entries if entry.ticket in named]


def _cleared_report(cleared, all_: bool) -> OperationResult:
    """The report a clear prints, once the files are gone."""
    removed = _unlink(cleared)
    data = {
        "cleared": [entry.to_dict() for entry in removed],
        "count": len(removed),
        "bytes": sum(entry.bytes for entry in removed),
    }
    rows = "; ".join(
        CLEARED_ROW.format(
            ticket=entry.ticket, records=entry.records, size=human_size(entry.bytes)
        )
        for entry in removed
    )
    if all_:
        # `--all` is the tidy-up a human runs: it reports what it removed -- or an
        # honest zero -- and names no next step, because the area is now empty.
        if not removed:
            return succeeded(lines=[CLEARED_NONE], data=data)
        return succeeded(
            lines=[CLEARED_MANY.format(count=len(removed), rows=rows)],
            data=data,
        )
    if len(removed) == 1:
        lines = [
            CLEARED_ONE.format(
                ticket=removed[0].ticket,
                records=removed[0].records,
                size=human_size(removed[0].bytes),
            )
        ]
    else:
        lines = [CLEARED_MANY.format(count=len(removed), rows=rows)]
    return succeeded(lines=lines, data=data, next_actions=[LIST_COMMAND], text_hint=REMAINS_HINT)


def _unlink(entries) -> list:
    """Delete `entries`, returning the ones that are gone; the first failure stops
    the clear.

    Continuing after a failure would report a file as cleared in an area this
    command cannot write, so the caller gets that file and the reason instead."""
    removed = []
    for entry in entries:
        try:
            entry.path.unlink()
        except OSError as exc:
            raise CoordinationError(
                CLEAR_FAILED.format(ticket=entry.ticket, reason=exc.strerror or exc)
            ) from exc
        removed.append(entry)
    return removed
