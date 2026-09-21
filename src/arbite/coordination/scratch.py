"""Scratch transport: the payload area, the commands that manage it, and how it reports.

Scratch lives in the *project*, not in the sink (`.arbite/scratch/`), because a
payload path outside the project makes every harness prompt for permission and
breaks unattended automation. A scratch file is transport, never a record: it
authorizes nothing, it is not a ticket, it is never claimable and it never appears
in discovery -- which is why this module creates the directory, reports what is in
it, and clears it deliberately rather than treating any of it as content.

Reading the payload a mutation will write (`--input`/`--edits` resolved inside
`.arbite/scratch/`, `-` for stdin) and consuming it on success are here too, because
the mutation is what knows it succeeded. The rest of the payload's lifecycle is
`arbite scratch list|clear`, `--keep`, and the line a mutation that stopped on the
bytes prints about the payload it left standing: a caller that read one version and
finds another has not lost its staged bytes, and the sentence says so, so a
recoverable refusal never forces a model to re-emit a file.

A refusal that stops *before* the bytes are judged -- a token somebody already
spent, an attempt that is no longer current -- leaves the payload alone as well, and
prints nothing about it: "re-apply your change" is only true advice when the change
is still the one the caller meant to make, and the frozen blocks for those refusals
(WR3, WR5) print no payload line.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..errors import CoordinationError, PathRefused
from .results import EMPTY, OperationResult, Outcome, succeeded

#: The payload area's name inside the arbite directory.
SCRATCH_DIRNAME = "scratch"

#: The one payload name that means "read the bytes from stdin".
STDIN_NAME = "-"

DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

#: How a consumed payload is reported: the bytes now live in the receipt, so the
#: staged copy is transport that has done its job.
CONSUMED_NOTE = "payload: {display} consumed and cleared (bytes retained as receipt artifact)"

#: How a payload a mutation left standing is reported (SC2, WR2). The sentence says why
#: keeping it matters: the caller's change still applies, so re-sending the file would be
#: work the refusal just saved.
KEPT_NOTE = "payload: {display} kept, so you can re-apply without re-sending the file"

#: How a payload the caller asked to keep is reported after a *successful* mutation.
KEEP_NOTE = "payload: {display} kept (--keep)"

#: How a stdin payload is reported, in the shape the frozen transcripts use: a write
#: names the file's shape, an edit batch names how many edits it holds.
STDIN_NOTE = "(payload read from stdin: {shape})"
WRITE_SHAPE = "{lines} lines, {size}"
EDITS_SHAPE = "{edits} edits"

#: The payload-name refusal, and the hint that follows it (SC5). It names `scratch list`
#: because that is the command a caller with a wrong path actually wants next.
OUTSIDE_HINT = "next: 'arbite scratch list' to see staged payloads, or pipe the content with '{flag} -'"
OUTSIDE_REFUSAL = (
    "'{flag}' takes a name inside .arbite/scratch/ or '-' for stdin; '{name}' is outside "
    "the project"
)

#: `arbite scratch list`: a count line, then one row per payload with its size, age and the
#: agent the store can name. The attribution is reported, never claimed: arbite did not
#: perform the write that staged the file (`writer_of`).
LIST_HEADER = "{count} in .arbite/scratch/:"
LIST_ROW = "  {name}   {size}  written {written}{agent}"
LIST_AGENT = " (agent {agent})"
LIST_NO_AGENT = " (no attempt recorded)"
NO_PAYLOADS = "no payloads in .arbite/scratch/"

#: `arbite scratch clear`: one name reads as a sentence about that file and hands back the
#: list to run next (`--all` is the tidy-up an interrupted run leaves to a human, and needs
#: no continuation).
CLEARED_ONE = "cleared .arbite/scratch/{name} ({size})"
CLEARED_MANY = "cleared {count} from .arbite/scratch/ ({rows})"
CLEARED_NONE = "cleared 0 files from .arbite/scratch/ (nothing was staged)"
CLEARED_ROW = "{name}, {size}"
REMAINS_HINT = "next: 'arbite scratch list' to see what remains"
STAGED_HINT = "next: 'arbite scratch list' to see what is staged"
NO_SUCH_PAYLOAD = "no payload named '{name}' in .arbite/scratch/"
CLEAR_OUTSIDE = "'scratch clear' takes a name inside .arbite/scratch/; '{name}' is not one"
CLEAR_NEEDS_TARGET = "'scratch clear' needs a payload name, or '--all' to clear the whole area"
CLEAR_NOT_BOTH = "'scratch clear' takes payload names or '--all', not both"
CLEAR_FAILED = "could not clear .arbite/scratch/{name}: {reason}"


def scratch_root(arbite_dir) -> Path:
    """Where this project's scratch payloads live."""
    return Path(arbite_dir) / SCRATCH_DIRNAME


def ensure_scratch_dir(arbite_dir) -> Path:
    """Create the payload area if it does not exist, and return it.

    Idempotent, and safe to call from `arbite init`: the directory being present
    says nothing about whether anything is in it."""
    root = scratch_root(arbite_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def human_size(count: int) -> str:
    """A byte count as text prints it: `512 B`, `4.1 KiB`, `12.4 MiB`.

    Binary units with one decimal from KiB up, because that is what the frozen
    transcripts use (`12.4 KiB` for 12700 bytes, `4.1 KiB` for a single payload of
    a few kilobytes). JSON always carries the exact byte count instead: this is a
    display rule, never the fact."""
    size = int(count)
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024.0
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GiB"


def printable_size(count: int) -> str:
    """A byte count as a *file's* shape prints it: KiB and up, always one decimal.

    The frozen LS2 row prints `0.4 KiB` for a 400-byte file, so a size below one KiB
    is rendered in KiB rather than in bytes -- the row is about a file's shape, and a
    file's shape is lines and KiB. The scratch summary deliberately differs (`400 B`),
    because there the exact number of bytes is the fact being reported."""
    size = int(count)
    if size < 1024:
        return f"{size / 1024:.1f} KiB"
    return human_size(size)


@dataclass(frozen=True)
class ScratchSummary:
    """What is sitting in the payload area: a count and a size, nothing more.

    A leftover payload is not corruption -- it is what an interrupted run leaves
    behind, and it is the only copy of a change the model may need to re-apply --
    so this is reported and never affects an exit code by itself."""

    files: int = 0
    bytes: int = 0

    @property
    def is_empty(self) -> bool:
        return self.files == 0

    def describe(self) -> str:
        """The parenthesised summary the workspace report prints."""
        if self.is_empty:
            return "empty"
        noun = "file" if self.files == 1 else "files"
        return f"{self.files} {noun}, {human_size(self.bytes)}"

    def to_dict(self) -> dict:
        return {"files": self.files, "bytes": self.bytes}


def note_lines(
    summary: ScratchSummary, guidance: bool = True, clear_command=None
) -> list:
    """The `note:` lines `doctor` prints about the payload area.

    A leftover payload is expected after an interrupted run and is the only copy of a change
    somebody may still need, so it is reported and never treated as corruption -- and it
    never changes an exit code by itself. The note states the fact; the sentence about
    clearing payloads is printed only when this arbite actually has the command that clears
    them (`clear_command`, passed by the caller after asking the parser), because naming a
    command that does not exist would be a capability claimed on paper only. The probes that
    remain unlanded -- the receipt and change views -- are why that rule is worth keeping.
    """
    if summary.is_empty:
        return ["note: .arbite/scratch/ is empty"]
    noun = "file" if summary.files == 1 else "files"
    fact = (
        f"note: .arbite/scratch/ holds {summary.files} {noun} "
        f"({human_size(summary.bytes)})"
    )
    if not guidance:
        return [fact]
    if clear_command:
        return [
            f"{fact} -- transport left behind, expected after an",
            f"      interrupted run; clear with 'arbite {clear_command} --all'",
        ]
    return [f"{fact} -- transport left behind, expected after an interrupted run"]


def read_payload(arbite_dir, name, flag: str, stdin=None) -> "Payload":
    """The bytes one mutation will write, read from the scratch area or stdin.

    `flag` is the argument the name arrived through (`--input`, `--edits`), so a
    refusal tells the caller which one to fix. A name is resolved *inside*
    `.arbite/scratch/`: a path outside the project is refused rather than accepted,
    because the documented workflow must never need one -- an out-of-project payload
    makes every harness prompt for permission and breaks unattended automation.
    """
    text = "" if name is None else str(name).strip()
    if not text:
        raise PathRefused(
            f"'{flag}' is required: name a payload in .arbite/scratch/ or use '-' for stdin"
        )
    if text == STDIN_NAME:
        if stdin is None:
            raise PathRefused(
                f"'{flag} -' reads the payload from stdin, and stdin is not connected"
            )
        return Payload(stdin.read())
    if _outside_project(text):
        raise PathRefused(
            OUTSIDE_REFUSAL.format(flag=flag, name=text),
            text_hint=OUTSIDE_HINT.format(flag=flag),
        )
    path = scratch_root(arbite_dir) / text
    if not path.is_file():
        raise PathRefused(
            f"no payload named '{text}' in .arbite/scratch/",
            text_hint=(
                f"next: write the bytes to .arbite/scratch/{text} first, or pipe them "
                f"with '{flag} -'"
            ),
        )
    return Payload(path.read_bytes(), name=text, path=path)


def _outside_project(name: str) -> bool:
    """Whether a payload name points out of the scratch area (or out of the project)."""
    if name.startswith("/") or DRIVE_LETTER.match(name):
        return True
    normalised = posixpath.normpath(name)
    return normalised == ".." or normalised.startswith("../")


@dataclass(frozen=True)
class Payload:
    """Bytes staged for one mutation, and where they came from.

    Transport, not a record: the payload authorises nothing, becomes no event on its
    own, and the only copy that turns into evidence is the one the receipt keeps.
    `path` is None for stdin, which is the payload that has no file to consume -- so
    `from_stdin` is the difference between "the staged copy was cleared" and "there
    was never a staged copy"."""

    data: bytes
    name: Optional[str] = None
    path: Optional[Path] = None

    @property
    def from_stdin(self) -> bool:
        return self.path is None

    @property
    def display(self) -> str:
        """How a report names the payload: its scratch path, or `stdin`."""
        if self.path is None:
            return "stdin"
        return f".arbite/{SCRATCH_DIRNAME}/{self.name}"

    def consume(self) -> bool:
        """Delete the staged copy, now that its bytes live in the receipt.

        Returns whether there was a copy to clear. Best-effort: the mutation has
        already been recorded, and failing here would report a successful write as a
        failure over a leftover of transport."""
        if self.path is None:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def consume_note(self) -> str:
        """The `payload:` line a successful mutation prints, or '' for stdin."""
        if self.path is None:
            return ""
        return CONSUMED_NOTE.format(display=self.display)

    def kept_note(self) -> str:
        """The `payload:` line a mutation that stopped on the bytes prints, or '' for stdin.

        A piped payload has no staged copy to keep, so it has nothing to say about one."""
        if self.path is None:
            return ""
        return KEPT_NOTE.format(display=self.display)

    def keep_note(self) -> str:
        """The `payload:` line a successful `--keep` mutation prints, or '' for stdin."""
        if self.path is None:
            return ""
        return KEEP_NOTE.format(display=self.display)

    def stdin_note(self, shape: str) -> str:
        """The `(payload read from stdin: ...)` line, or '' for a staged file."""
        if self.path is not None:
            return ""
        return STDIN_NOTE.format(shape=shape)


#: The command a scratch report hands back, so the hint text and the `next_actions` it
#: claims to describe cannot name two different commands.
SCRATCH_LIST = "arbite scratch list"


def _count(number: int, noun: str) -> str:
    """`1 file` / `3 files`: one place for the plural, so no report's noun drifts from its
    count."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _written_clock(written: datetime) -> str:
    """A payload's age as the row prints it: local time, which is how a reader places it."""
    return written.astimezone().strftime("%H:%M:%S")


def _written_utc(written: datetime) -> str:
    """The same instant as JSON carries it: UTC RFC 3339 to the second."""
    return written.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PayloadEntry:
    """One staged payload: its name relative to the area, its size and its last-write time.

    A payload has no record of its own -- no arbite command performed the write that staged
    it -- so everything a report says about one is read from the file itself."""

    name: str
    path: Path
    size: int
    written: datetime

    @property
    def display(self) -> str:
        """The path a report names it by, the same shape a payload's `display` uses."""
        return f".arbite/{SCRATCH_DIRNAME}/{self.name}"

    def row(self, agent: Optional[str] = None) -> str:
        """The list row: what it is, how big, how old, and who the store can name."""
        who = LIST_AGENT.format(agent=agent) if agent else LIST_NO_AGENT
        return LIST_ROW.format(
            name=self.name,
            size=human_size(self.size),
            written=_written_clock(self.written),
            agent=who,
        )

    def to_dict(self, agent: Optional[str] = None) -> dict:
        return {
            "name": self.name,
            "path": self.display,
            "bytes": self.size,
            "written": _written_utc(self.written),
            "agent": agent,
        }


def payload_entries(arbite_dir) -> list:
    """Every payload file in the area, in name order, with its size and age.

    Files at any depth, because a caller may group payloads in its own subdirectory, and a
    missing scratch directory is an empty list rather than an error -- the same answer
    `scratch_summary` gives."""
    root = scratch_root(arbite_dir)
    if not root.is_dir():
        return []
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            # A payload somebody removed while this report was being built is not a reason
            # to fail a report about the others.
            continue
        entries.append(
            PayloadEntry(
                name=path.relative_to(root).as_posix(),
                path=path,
                size=stat.st_size,
                written=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return entries


def writer_of(store) -> Optional[str]:
    """The agent a payload row is attributed to, when the store can name one.

    Arbite never performs the write that stages a payload -- a caller's own tool does -- so
    there is no record of authorship to read. What the store does know is which attempt was
    working last, which for a workspace with one agent at a time has exactly one answer; a
    store that names no attempt says so rather than inventing a name."""
    if store is None:
        return None
    attempts = list(store.records("attempt"))
    if not attempts:
        return None
    newest = max(attempts, key=lambda attempt: (attempt.last_activity, attempt.id))
    return newest.worker_id


def scratch_list(arbite_dir, store=None) -> OperationResult:
    """`arbite scratch list`: what is staged, how big, how old, and who was working there.

    Nothing staged is outcome 2 rather than an error -- the answer `file list` gives for an
    empty directory -- and it carries no `next:` line, because there is no command a caller
    should run about an area that is already empty."""
    entries = payload_entries(arbite_dir)
    if not entries:
        return OperationResult(
            Outcome(EMPTY), [NO_PAYLOADS], {"files": [], "count": 0, "bytes": 0}, [], ""
        )
    agent = writer_of(store)
    return succeeded(
        lines=[LIST_HEADER.format(count=_count(len(entries), "file"))]
        + [entry.row(agent) for entry in entries],
        data={
            "files": [entry.to_dict(agent) for entry in entries],
            "count": len(entries),
            "bytes": sum(entry.size for entry in entries),
        },
    )


def scratch_clear(arbite_dir, names=(), all_: bool = False) -> OperationResult:
    """`arbite scratch clear NAME...` and `--all`: delete staged payloads deliberately.

    Clearing is the command the doctor note points at, so this is the one place a payload is
    removed on purpose; nothing else deletes transport. A name that is absent -- or outside
    the area, which would be clearing somebody else's file -- is refused with the list that
    shows what is really staged, and `--all` on an empty area is an honest zero rather than
    an error.

    A named clear reads as a sentence about one file and hands back the list to run next;
    `--all`, and a batch of names, report the count and the files they cleared."""
    entries = payload_entries(arbite_dir)
    if all_:
        return _cleared_report(entries, all_=True)
    wanted = [str(name).strip() for name in names]
    return _cleared_report(_named_payloads(entries, wanted), all_=False)


def _named_payloads(entries, wanted) -> list:
    """The payloads `wanted` names, or the refusal that stops the clear before it starts.

    Names are compared as `arbite scratch list` prints them (relative to the area, POSIX
    separators), so a caller can copy one out of the listing; a name that points out of the
    area is refused before any lookup, because `scratch clear` must never become a way to
    delete a file that is not a payload."""
    known = {entry.name: entry for entry in entries}
    for name in wanted:
        if _outside_project(name):
            raise PathRefused(CLEAR_OUTSIDE.format(name=name), text_hint=STAGED_HINT)
        if posixpath.normpath(name) not in known:
            raise PathRefused(NO_SUCH_PAYLOAD.format(name=name), text_hint=STAGED_HINT)
    named = {posixpath.normpath(name) for name in wanted}
    return [entry for entry in entries if entry.name in named]


def _cleared_report(cleared, all_: bool) -> OperationResult:
    """The report a clear prints, once the named files are gone."""
    removed = _unlink(cleared)
    data = {
        "cleared": [entry.to_dict() for entry in removed],
        "count": len(removed),
        "bytes": sum(entry.size for entry in removed),
    }
    rows = "; ".join(
        CLEARED_ROW.format(name=entry.name, size=human_size(entry.size)) for entry in removed
    )
    if all_:
        # `--all` is the tidy-up an interrupted run leaves to a human: it reports what it
        # removed -- or an honest zero -- and names no next step, because the area is empty.
        if not removed:
            return succeeded(lines=[CLEARED_NONE], data=data)
        return succeeded(
            lines=[CLEARED_MANY.format(count=_count(len(removed), "file"), rows=rows)],
            data=data,
        )
    if len(removed) == 1:
        lines = [CLEARED_ONE.format(name=removed[0].name, size=human_size(removed[0].size))]
    else:
        lines = [CLEARED_MANY.format(count=_count(len(removed), "file"), rows=rows)]
    return succeeded(
        lines=lines, data=data, next_actions=[SCRATCH_LIST], text_hint=REMAINS_HINT
    )


def _unlink(entries) -> list:
    """Delete `entries`, returning the ones that are gone; the first failure stops the clear.

    Continuing after a failure would report a file as cleared in an area this command cannot
    write, so the caller gets that file and the reason instead."""
    removed = []
    for entry in entries:
        try:
            entry.path.unlink()
        except OSError as exc:
            raise CoordinationError(
                CLEAR_FAILED.format(name=entry.name, reason=exc.strerror or exc)
            ) from exc
        removed.append(entry)
    return removed


def scratch_summary(arbite_dir) -> ScratchSummary:
    """Count the payload files under `arbite_dir`, and total their sizes.

    Files only, at any depth: a subdirectory left by a tool is not a payload, and
    counting directories would report a size no payload has. A missing scratch
    directory -- every project before its first payload -- is an empty summary, not
    an error, because `doctor` and `workspace show` must work in a store that has
    never carried one."""
    root = scratch_root(arbite_dir)
    if not root.is_dir():
        return ScratchSummary()
    files = 0
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files += 1
            try:
                total += path.stat().st_size
            except OSError:
                # A payload that vanished between the walk and the stat is not a
                # reason to fail a report about the others.
                continue
    return ScratchSummary(files=files, bytes=total)
