"""Projects in the states the cascade transcripts describe: CL7 and LC1-LC4.

The blocks are about what *ending* work does to ownership, so every fact they assert is
produced by a real command rather than written into the store: the attempts and tickets
come from the claim fixtures (`claims.holder_project`, seeded with the document's own ids
-- `tic-cf9f`/`att-91bd` and `tic-9b57`/`att-4c81`), the paths are acquired with a real
`arbite file claim`, the receipts LC1 counts are real `arbite file write` operations, and
the closed ticket LC4 starts from is closed by the real `arbite close` the LC1 test
asserts. A fixture that wrote those records itself could pass while the commands
disagreed with it.

One world serves four blocks, because the document uses one: `tic-cf9f` in progress for
`att-91bd`, holding `src/arbite/schema.py` and `src/arbite/sinks/base.py` with the first
written five times, is LC1's closing world, LC3's refused deletion and CL7's takeover.
`rival_world` is LC2's (`tic-9b57` for `att-4c81`, holding `sinks/file.py`) and
`closed_world` is LC4's (the same ticket after the close).
"""

from __future__ import annotations

from pathlib import Path

import claims_state as claims
import writes_state

HOLDER_TICKET = claims.HOLDER_TICKET
RIVAL_TICKET = claims.RIVAL_TICKET
HOLDER = claims.HOLDER
RIVAL = claims.RIVAL
HOLDER_WORKER = claims.HOLDER_WORKER
RIVAL_WORKER = claims.RIVAL_WORKER

SCHEMA_PY = claims.SCHEMA_PY
BASE_PY = claims.BASE_PY
FILE_PY = claims.FILE_PY

#: How many lines `schema.py` starts with (the shape `claims.holder_project` writes).
SCHEMA_LINES = 40
#: LC1's manifest: five operations recorded against the ticket it closes.
RECEIPTS = 5
#: The payload name the five writes send, inside `.arbite/scratch/`.
PAYLOAD = "schema.py"


def schema_text(rounds: int) -> str:
    """`schema.py` after `rounds` writes: the same file, three lines longer each time.

    Nothing in the transcripts depends on the bytes, but they have to *change* for the
    close to report the path as last written by its ticket, so every write appends."""
    last = SCHEMA_LINES + rounds * 3
    return "".join(f"# schema.py line {index}\n" for index in range(1, last + 1))


def store_for(project: Path, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return claims.coordination(project, sink_kind)


def holder_world(tmp_path: Path, sink_kind: str = "file") -> Path:
    """CL7/LC1/LC3's world: two paths held by `att-91bd`, one of them written five times.

    One acquisition claims both paths, which is what makes the ticket hold *two* file
    claims at one generation; the five writes are the operations LC1's manifest counts and
    the reason `schema.py` is modified when the cascade releases it while `base.py` is
    not."""
    project = claims.holder_project(tmp_path, sink_kind)
    claims.run(
        project,
        "file",
        "claim",
        SCHEMA_PY,
        BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=sink_kind,
    )
    for rounds in range(1, RECEIPTS + 1):
        writes_state.staged(project, PAYLOAD, schema_text(rounds))
        token = writes_state.read_token(project, SCHEMA_PY, HOLDER_TICKET, HOLDER, sink_kind)
        claims.run(
            project,
            "file",
            "write",
            SCHEMA_PY,
            "--ticket",
            HOLDER_TICKET,
            "--attempt",
            HOLDER,
            "--read-token",
            token,
            "--input",
            PAYLOAD,
            sink_kind=sink_kind,
        )
    return project


def rival_world(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LC2's world: `tic-9b57` in progress for `att-4c81`, holding one path.

    The holder is `claude.sonnet.002`, which is the worker LC2's `next:` line names: the
    command it suggests has to name the attempt's own worker, because that is who the
    ticket is still assigned to."""
    project = claims.holder_project(tmp_path, sink_kind)
    claims.run(
        project,
        "file",
        "claim",
        FILE_PY,
        "--ticket",
        RIVAL_TICKET,
        "--attempt",
        RIVAL,
        sink_kind=sink_kind,
    )
    return project


def closed_world(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LC4's starting point: LC1's world, closed by the real command.

    The close is the cascade itself, so this starting point is only reachable through the
    behaviour under test -- which is the point: LC4 asserts that reopening what a close
    left behind revives nothing."""
    project = holder_world(tmp_path, sink_kind)
    claims.run(project, "close", HOLDER_TICKET, sink_kind=sink_kind)
    return project


def claim_states(project: Path, sink_kind: str = "file") -> dict:
    """Every claim record's state, by path: `active` still reserves the path."""
    return {
        claim.path: claim.state for claim in store_for(project, sink_kind).records("claim")
    }


def retained_receipts(project: Path, ticket_id: str, sink_kind: str = "file") -> int:
    """The finalized receipts recorded for a ticket, read back from the store.

    The same number the close reports as its manifest, so the line and the records cannot
    drift apart."""
    return sum(
        1
        for receipt in store_for(project, sink_kind).receipts()
        if receipt.ticket_id == ticket_id and not receipt.is_pending
    )
