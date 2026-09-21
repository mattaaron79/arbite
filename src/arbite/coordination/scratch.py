"""Scratch transport: the payload area, and how it is reported.

Scratch lives in the *project*, not in the sink (`.arbite/scratch/`), because a
payload path outside the project makes every harness prompt for permission and
breaks unattended automation. A scratch file is transport, never a record: it
authorizes nothing, it is not a ticket, it is never claimable and it never appears
in discovery -- which is why this module only creates the directory and reports
what is in it.

Consuming and clearing a payload (success consumes it, failure keeps it, `--keep`
opts out, `arbite scratch list|clear`) is the scratch slice (tic-95c0); the summary
here is what the workspace report and `doctor` render, so the count and the size a
user sees come from one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The payload area's name inside the arbite directory.
SCRATCH_DIRNAME = "scratch"


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
