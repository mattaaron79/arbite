"""Projects in the states the scratch transcripts describe: a payload area with something in it.

The SC and DR3 blocks are about *transport*, so the fixture's job is the payload area itself --
how big the staged file is, which agent the store can name, and which file the mutation would
read. Everything a mutation must present (the claim, the read token) is produced by the real
commands in `writes_state`, because a fixture that minted its own token could pass while the
write surface disagreed.

The agent a list row prints is the store's most recently active attempt. Arbite never performs
the write that stages a payload -- a caller's own tool does -- so that is the only attribution
there is, which is why SC1's world holds exactly one attempt and the sentence is unambiguous.

The two fences SC1 and SC4 each contain describe one narrative but two different worlds: SC1's
list shows a `4.1 KiB` payload while its write turns a 570-line file into a 588-line one, so
`writes_state.write_project` (the real 570-line file and its claim) builds the second half and
`staged_payload` builds the first.
"""

from __future__ import annotations

from pathlib import Path

import claims_state as claims
import discovery_state as discovery
import examples
import lifecycle_state as state
import writes_state as writes
from helpers import make_ticket

#: SC1's payload: 4198 bytes, which the row prints as `4.1 KiB`.
BASE_PAYLOAD_BYTES = 4198
BASE_PAYLOAD_LINES = 40

#: SC3's payload: 84 lines of 28 bytes, which the stdin notice prints as `2.3 KiB`.
STDIN_LINES = 84
STDIN_LINE_BYTES = 28

#: SC3's target: a path that does not exist, so the piped bytes create it.
CREATED_PATH = claims.RECORDS_PY

#: The agent SC1's row names, and the one attempt the fixture records.
AGENT = claims.HOLDER_WORKER

#: DR3's store: 22 clean tickets and nothing staged.
DR3_TICKETS = 22


def staged_payload(project: Path, name: str = "base.py", size: int = BASE_PAYLOAD_BYTES) -> Path:
    """Stage one payload of exactly `size` bytes, as a caller's own tool would."""
    return discovery.written(
        project,
        f".arbite/scratch/{name}",
        discovery.exact_size(name, BASE_PAYLOAD_LINES, size),
    )


def stdin_payload() -> str:
    """SC3's piped file: exactly `STDIN_LINES` lines of exactly `STDIN_LINE_BYTES` bytes."""
    return "".join(
        f"# records.py {index:03d}".ljust(STDIN_LINE_BYTES - 1) + "\n"
        for index in range(1, STDIN_LINES + 1)
    )


def transport(tmp_path: Path, sink_kind: str = "file") -> Path:
    """SC1's list world: one ticket in progress for `claude.opus.001`, one staged payload.

    One attempt only, so the agent the row names is the store's answer rather than a
    tie-break between two: the attribution `writer_of` reports is "who was working last"."""
    project = state.initialise(tmp_path, sink_kind)
    state.put(
        project,
        claims.HOLDER_TICKET,
        sink_kind,
        title=state.C03_TITLE,
        status="in_progress",
        assignee=AGENT,
        priority=1,
        **state.epic_ticket(),
    )
    claims.put_attempt(project, claims.HOLDER, claims.HOLDER_TICKET, AGENT, sink_kind)
    staged_payload(project)
    return project


def consumed(tmp_path: Path, sink_kind: str = "file") -> Path:
    """SC1's write world: `base.py` held at generation 2, read, with its replacement staged."""
    return writes.write_project(tmp_path, sink_kind)


def refused(tmp_path: Path, sink_kind: str = "file") -> Path:
    """SC2's world: the rival holds `base.py`, read it, and somebody rewrote the file since.

    The staged payload is still there and still the change the reader meant to make, which is
    what makes the refusal's keep-the-payload sentence true rather than reassuring."""
    project = writes.init_project(tmp_path, sink_kind)
    discovery.written(project, writes.BASE_PY, writes.base_text())
    discovery.put_claim(
        project,
        writes.BASE_PY,
        claims.RIVAL_TICKET,
        claims.RIVAL,
        generation=2,
        sink_kind=sink_kind,
    )
    writes.staged(project, "base.py", writes.base_text(append=writes.APPENDED_LINES))
    return project


def rival_token(project: Path, sink_kind: str = "file") -> str:
    """The token SC2's write presents: the rival's read, taken before the file moved."""
    return writes.read_token(project, writes.BASE_PY, claims.RIVAL_TICKET, claims.RIVAL, sink_kind)


def creation(tmp_path: Path, sink_kind: str = "file") -> Path:
    """SC3's world: nobody has created `records.py` yet, and the claim is at generation 3."""
    project = writes.init_project(tmp_path, sink_kind)
    writes.put_absent_claim(
        project, CREATED_PATH, claims.HOLDER_TICKET, claims.HOLDER, generation=3, sink_kind=sink_kind
    )
    return project


def clearable(tmp_path: Path, sink_kind: str = "file") -> Path:
    """SC4's world: one 4.1 KiB payload staged for `base.py`."""
    project = state.initialise(tmp_path, sink_kind)
    staged_payload(project)
    return project


def doctor_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """DR3's world: 22 clean tickets, no coordination findings, and an empty payload area."""
    project = state.initialise(tmp_path, sink_kind)
    sink = state.sink_for(project, sink_kind)
    for index in range(DR3_TICKETS):
        sink.create(make_ticket(f"tic-{index + 0x1000:04x}"))
    return project


def payload_path(project: Path, name: str = "base.py") -> Path:
    """The staged payload on disk, for the assertions a report cannot make."""
    return project / ".arbite" / "scratch" / name


def receipt_count(project: Path, sink_kind: str = "file") -> int:
    """How many receipts the store holds: a refusal must leave this at zero."""
    return len(list(discovery.store_for(project, sink_kind).records("receipt")))


def claimed_paths(project: Path, sink_kind: str = "file") -> set:
    """Every path the store currently claims, active or released."""
    return {claim.path for claim in discovery.store_for(project, sink_kind).records("claim")}


def initialise_without_attempts(tmp_path: Path, sink_kind: str = "file") -> Path:
    """A project whose store names no attempt at all, with a payload staged in it.

    The row has to say something honest here rather than invent a name: attribution is
    reported, and a store with nobody in it has nobody to report."""
    project = state.initialise(tmp_path, sink_kind)
    staged_payload(project)
    return project


def run(project: Path, *args, sink_kind: str = "file", stdin=None):
    """Run one command against this checkout, as a user would."""
    return examples.run_cli(project, *args, sink=sink_kind, stdin=stdin)
