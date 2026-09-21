"""Scratch transport: the payload area, and how it is reported.

Scratch lives in the *project*, not in the sink (`.arbite/scratch/`), because a
payload path outside the project makes every harness prompt for permission and
breaks unattended automation. A scratch file is transport, never a record: it
authorizes nothing, it is not a ticket, it is never claimable and it never appears
in discovery -- which is why this module only creates the directory and reports
what is in it.

Reading the payload a mutation will write (`--input`/`--edits` resolved inside
`.arbite/scratch/`, `-` for stdin) and consuming it on success are here too, because
the mutation is what knows it succeeded. The rest of the scratch slice is
tic-95c0/C09: `arbite scratch list|clear`, `--keep`, and the line a *failed* mutation
prints about the payload it left standing. Until that lands a failed mutation keeps
the payload -- the file is still there for a model to re-apply -- and says nothing
about it, because no frozen transcript of this slice asks for a sentence there.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import PathRefused

#: The payload area's name inside the arbite directory.
SCRATCH_DIRNAME = "scratch"

#: The one payload name that means "read the bytes from stdin".
STDIN_NAME = "-"

DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

#: How a consumed payload is reported: the bytes now live in the receipt, so the
#: staged copy is transport that has done its job.
CONSUMED_NOTE = "payload: {display} consumed and cleared (bytes retained as receipt artifact)"

#: How a stdin payload is reported, in the shape the frozen transcripts use: a write
#: names the file's shape, an edit batch names how many edits it holds.
STDIN_NOTE = "(payload read from stdin: {shape})"
WRITE_SHAPE = "{lines} lines, {size}"
EDITS_SHAPE = "{edits} edits"


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
    them (`clear_command`, which is tic-95c0's), because naming a command that does not
    exist would be a capability claimed on paper only.
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
            f"'{flag}' takes a name inside .arbite/scratch/ or '-' for stdin; "
            f"'{text}' is outside the project",
            text_hint=f"next: pipe the payload with '{flag} -', or stage it in .arbite/scratch/",
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

    def stdin_note(self, shape: str) -> str:
        """The `(payload read from stdin: ...)` line, or '' for a staged file."""
        if self.path is not None:
            return ""
        return STDIN_NOTE.format(shape=shape)


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
