"""The projects the ST transcripts describe, built through the real commands.

A stream belongs to a *working* ticket, so the world every ST block needs is an
initialised project with one claimed ticket: the attempt is created by `arbite claim`,
because that is the only thing that creates one, and its id is what the records are
attributed to. `tic-cf9f` and `claude.opus.001` are the ids the rest of the example
document already uses for "one claimed ticket", so the narration blocks read as the same
world as the claim blocks beside them.
"""

from __future__ import annotations

import json
from pathlib import Path

import examples
import lifecycle_state
from arbite.coordination import streams

TICKET = "tic-cf9f"
AGENT = "claude.opus.001"
#: The stream file's report-relative path, as the transcripts print it.
STREAM_PATH = f".arbite/streams/{TICKET}.jsonl"

#: One line of narration, three kinds of it, and the shape the ST1 block prints. Kept
#: here because the transcript and the fixture have to say the same words.
LINES = (
    ("thought", "reading the sink's ticket scan"),
    ("thought", "a stream file's stem matches the ticket pattern, so the scan has to skip it"),
    ("thought", "added 'streams' to RESERVED_DIRS"),
    ("result", "arbite list no longer reports a phantom ticket"),
)


def initialise(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project whose committed choice is `sink_kind`."""
    return lifecycle_state.initialise(tmp_path, sink_kind)


def claimed(tmp_path: Path, sink_kind: str = "file"):
    """`(project, attempt id)`: one claimed ticket, so narration has a live attempt.

    The attempt id is read back out of the claim's JSON rather than invented: it is a real
    record's id, which is what a stream record names."""
    project = initialise(tmp_path, sink_kind)
    lifecycle_state.claimable(project, TICKET, sink_kind)
    proc = examples.run_cli(project, "claim", TICKET, "--agent", AGENT, "--json")
    assert proc.returncode == 0, proc.stderr
    return project, json.loads(proc.stdout)["attempt"]["id"]


def narrate(project: Path, attempt: str, lines=LINES) -> None:
    """Record `lines` in order, through the module rather than the CLI.

    A fixture that narrated through `arbite stream write` would be testing the command in
    the middle of setting up a state for it, and ST1's own block is where that command is
    asserted."""
    for kind, text in lines:
        streams.append_records(project / ".arbite", TICKET, attempt, AGENT, kind, [text])


def stream_file(project: Path) -> Path:
    return project / ".arbite" / "streams" / f"{TICKET}.jsonl"


def records(project: Path) -> list:
    return [json.loads(line) for line in stream_file(project).read_text().splitlines()]
