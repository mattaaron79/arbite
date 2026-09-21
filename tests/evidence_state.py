"""Projects in the states the receipt and change transcripts describe (EV1, EV6).

Both blocks describe the *same* world one command at a time, so the fixture builds that
world with the real commands rather than by hand-writing receipts: a receipt this module
wrote itself would let a view pass while the engine records something else, which is
exactly the agreement these two commands exist to expose.

The world is one attempt's five operations on three paths:

1. `src/arbite/sinks/base.py` grows by eighteen lines -- WR1's write, which is also EV6's
   receipt (`kind: write`, claim generation 2, `570 -> 588 lines`, `+18 -0`).
2. `src/arbite/coordination/records.py` is created, so the net view has an `A` row that
   names no versions.
3. and 4. `src/arbite/schema.py` is edited and then edited back, so one row covers two
   operations and says `no net change` -- the pair the net view must not hide.
5. a fifth operation on `src/arbite/coordination/paths.py` was staged and never applied,
   which is why the header counts five operations while the rows name four: a receipt a
   recovery pass finalised as `failed` provably changed nothing, so a *net* view has no
   row for it and `--all` is where it is listed.

Claim generations matter to EV6, which prints `claim generation: 2`; the paths are claimed
with the generations the block names, as the other fixtures do, and every mutation itself
goes through the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import claims_state as claims
import discovery_state as discovery
import examples
import writes_state as writes
from arbite.coordination import records as coordination_records

HOLDER = claims.HOLDER
HOLDER_WORKER = claims.HOLDER_WORKER
HOLDER_TICKET = claims.HOLDER_TICKET

BASE_PY = writes.BASE_PY
SCHEMA_PY = writes.SCHEMA_PY
RECORDS_PY = "src/arbite/coordination/records.py"
ABANDONED_PY = "src/arbite/coordination/paths.py"

#: The creation's bytes. One line, so a row that prints `created` is describing something
#: with a shape the receipt view can print rather than an empty file.
CREATED_TEXT = "# the operation record\n"

#: EV1's edit-then-revert pair: the file holds `SCHEMA_ORIGINAL`, an edit to
#: `SCHEMA_EDITED` lands (operation three), and a second edit puts the first text back
#: (operation four). The net is zero and *both* operations are in the log.
SCHEMA_ORIGINAL = "def canonical(project_root):\n"
SCHEMA_EDITED = "def canonical_relative(project_root):\n"

#: The abandoned operation's two versions: what the path holds, and what the killed
#: process meant to write.
ABANDONED_TEXT = "# never written\n"
ABANDONED_TARGET = "# meant to be written\n"

#: The two edits as batches, in the shape `--edits` reads.
EDIT_FORWARD = {"edits": [{"old": "canonical", "new": "canonical_relative"}]}
EDIT_BACK = {"edits": [{"old": "canonical_relative", "new": "canonical"}]}


#: The paths `kinds_project` uses, one per operation kind the engine performs.
KINDS_EDIT_PY = "src/arbite/reads.py"
KINDS_MOVED_PY = "src/arbite/moved.py"
KINDS_REMOVED_PY = "src/arbite/gone.py"
KINDS_RENAMED_PY = "src/arbite/renamed.py"
KINDS_BINARY = "assets/icon.png"

RENAMED_TEXT = "renamed bytes\n"
EDITABLE_TEXT = "one\ntwo\n"
EDITED_TEXT = "one\ntwo\nthree\n"
#: Bytes that are not UTF-8 text: a PNG signature and then filler, so the receipt has to
#: hold a byte payload rather than a text diff it could never reproduce.
BINARY_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(
    (index * 37) % 256 for index in range(256)
)


def store_for(project: Path, sink_kind: str = "file"):
    return discovery.store_for(project, sink_kind)


def kinds_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """One project holding every kind of operation the engine performs.

    EV1's world is about the *net* of a ticket; this one exists for the round trips, which
    have to be checked per kind: a receipt that reproduced a text write but not a removed
    file, a moved file or a byte payload would still satisfy a view that only ever looked at
    an edit. Every operation is performed by the real command, so the receipts are the
    engine's own.
    """
    project = writes.init_project(tmp_path, sink_kind)
    _replace_text(project, sink_kind)
    _edit_text(project, sink_kind)
    _create_binary(project, sink_kind)
    _remove_a_file(project, sink_kind)
    _rename_a_file(project, sink_kind)
    return project


def operations_by_kind(project: Path, sink_kind: str = "file") -> dict:
    """`kind -> [receipt ids]` for the ticket, ready for the round-trip assertions."""
    found = {}
    for receipt in operations(project, sink_kind):
        found.setdefault(receipt.kind, []).append(receipt.id)
    return found


def ev_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """EV1's world: five operations by one attempt, in the order the block lists them."""
    project = writes.init_project(tmp_path, sink_kind)
    _write_base(project, sink_kind)
    _create_records(project, sink_kind)
    _edit_then_revert(project, sink_kind)
    abandon_operation(project, sink_kind)
    return project


def base_operation(project: Path, sink_kind: str = "file") -> str:
    """The id of the write the receipt view is asked about: `base.py`'s, at generation 2."""
    store = store_for(project, sink_kind)
    for receipt in store.receipts():
        if receipt.kind == "write" and receipt.paths == [BASE_PY] and not receipt.is_pending:
            return receipt.id
    raise AssertionError("the fixture performs base.py's write")


def operations(project: Path, sink_kind: str = "file") -> list:
    """Every receipt for the ticket, in the order the log holds them (for the assertions)."""
    store = store_for(project, sink_kind)
    return [receipt for receipt in store.receipts() if receipt.ticket_id == HOLDER_TICKET]


def operation_touching(project: Path, path: str, sink_kind: str = "file") -> str:
    """The id of the one operation that names `path` (the fixtures here perform one each)."""
    matched = [receipt.id for receipt in operations(project, sink_kind) if path in receipt.paths]
    assert len(matched) == 1, f"{path} is named by {len(matched)} operations"
    return matched[0]


def failed_operation(project: Path, sink_kind: str = "file") -> str:
    """The id of the operation a recovery pass finalised as failed."""
    for receipt in operations(project, sink_kind):
        if receipt.result == coordination_records.RECEIPT_FAILED:
            return receipt.id
    raise AssertionError("the fixture abandons one operation")


def receipt(project: Path, operation_id: str, sink_kind: str = "file"):
    return store_for(project, sink_kind).get_record("receipt", operation_id)


# ---------------------------------------------------------------------------
# The five operations
# ---------------------------------------------------------------------------


def _write_base(project: Path, sink_kind: str) -> None:
    """WR1's write, which is also EV6's receipt: 570 lines become 588, `+18 -0`."""
    discovery.written(project, BASE_PY, writes.base_text())
    discovery.put_claim(
        project, BASE_PY, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind
    )
    writes.staged(project, "base.py", writes.base_text(append=writes.APPENDED_LINES))
    token = writes.token_for_write(project, sink_kind)
    claims.run(
        project,
        "file", "write", BASE_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "base.py",
        sink_kind=sink_kind,
    )


def _create_records(project: Path, sink_kind: str) -> None:
    """A creation: the claim is on a path that is not there, and the write probes it itself."""
    writes.put_absent_claim(
        project, RECORDS_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    writes.staged(project, "records.py", CREATED_TEXT)
    claims.run(
        project,
        "file", "write", RECORDS_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--input", "records.py",
        sink_kind=sink_kind,
    )


def _edit_then_revert(project: Path, sink_kind: str) -> None:
    """Two edits whose net is zero: one forward, one back, each under its own read token."""
    discovery.written(project, SCHEMA_PY, SCHEMA_ORIGINAL)
    discovery.put_claim(
        project, SCHEMA_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    for name, batch in (("forward.json", EDIT_FORWARD), ("back.json", EDIT_BACK)):
        token = writes.read_token(project, SCHEMA_PY, HOLDER_TICKET, HOLDER, sink_kind)
        writes.staged(project, name, json.dumps(batch))
        claims.run(
            project,
            "file", "edit", SCHEMA_PY,
            "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
            "--read-token", token, "--edits", name,
            sink_kind=sink_kind,
        )


def abandon_operation(project: Path, sink_kind: str = "file", settle: bool = True) -> str:
    """Stage an operation so that it never applied, and return its id.

    The state a killed process leaves behind is an intent whose bytes are still the version
    it found. Writing that state and then running `recover` is how the fixture gets a
    `failed` receipt without asserting anything about it itself: the judgement ("the bytes
    are the before version, so this never happened") is arbite's, and it is the same one
    `arbite doctor` makes. The evidence is stored exactly as a real operation stores it,
    because the receipt that names it has to be one the receipt view could read.

    `settle=False` leaves it *pending*: an operation nobody has judged yet, which is the
    state the change view has to report rather than net up."""
    store = store_for(project, sink_kind)
    discovery.written(project, ABANDONED_PY, ABANDONED_TEXT)
    discovery.put_claim(
        project, ABANDONED_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    untouched = ABANDONED_TEXT.encode("utf-8")
    intended = ABANDONED_TARGET.encode("utf-8")
    before = coordination_records.digest_bytes(untouched)
    after = coordination_records.digest_bytes(intended)
    taken = {artifact.id for artifact in store.records("artifact")}
    artifacts = []
    for digest, data in ((before, untouched), (after, intended)):
        store.put_artifact_bytes(digest, data)
        artifact = coordination_records.Artifact(
            id=coordination_records.new_id("artifact", taken),
            digest=digest,
            size=len(data),
            created=coordination_records.utc_now(),
        )
        taken.add(artifact.id)
        store.put_record(artifact)
        artifacts.append(artifact.id)

    operation_id = coordination_records.new_id(
        "receipt", {record.id for record in store.records("receipt")}
    )
    store.put_record(
        coordination_records.OperationReceipt(
            id=operation_id,
            kind="write",
            paths=[ABANDONED_PY],
            result=coordination_records.RECEIPT_PENDING,
            recorded_at=coordination_records.utc_now(),
            ticket_id=HOLDER_TICKET,
            attempt_id=HOLDER,
            actor=HOLDER_WORKER,
            before={ABANDONED_PY: before},
            after={ABANDONED_PY: after},
            artifacts=sorted(artifacts),
            claim_generation=1,
        )
    )
    if not settle:
        return operation_id
    settled = store.recover(root=project)
    assert [judged.finalised for judged in settled] == [coordination_records.RECEIPT_FAILED], (
        "the fixture's abandoned operation must be judged as never applied"
    )
    return operation_id


# ---------------------------------------------------------------------------
# One of every kind, for the round trips
# ---------------------------------------------------------------------------


def _replace_text(project: Path, sink_kind: str) -> None:
    """A whole-file write of text: two lines become three."""
    discovery.written(project, KINDS_EDIT_PY, EDITABLE_TEXT)
    discovery.put_claim(
        project, KINDS_EDIT_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    token = writes.read_token(project, KINDS_EDIT_PY, HOLDER_TICKET, HOLDER, sink_kind)
    writes.staged(project, "edit.py", EDITED_TEXT)
    claims.run(
        project,
        "file", "write", KINDS_EDIT_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "edit.py",
        sink_kind=sink_kind,
    )


def _edit_text(project: Path, sink_kind: str) -> None:
    """An exact edit of the same path, so a receipt of kind `edit` exists too."""
    token = writes.read_token(project, KINDS_EDIT_PY, HOLDER_TICKET, HOLDER, sink_kind)
    writes.staged(
        project, "batch.json", json.dumps({"edits": [{"old": "three", "new": "four"}]})
    )
    claims.run(
        project,
        "file", "edit", KINDS_EDIT_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--edits", "batch.json",
        sink_kind=sink_kind,
    )


def _create_binary(project: Path, sink_kind: str) -> None:
    """A creation of bytes that are not UTF-8 text: the case a text diff cannot describe."""
    writes.put_absent_claim(
        project, KINDS_BINARY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    (project / ".arbite" / "scratch").mkdir(parents=True, exist_ok=True)
    (project / ".arbite" / "scratch" / "icon.png").write_bytes(BINARY_BYTES)
    claims.run(
        project,
        "file", "write", KINDS_BINARY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--input", "icon.png",
        sink_kind=sink_kind,
    )


def _remove_a_file(project: Path, sink_kind: str) -> None:
    """A removal: the bytes leave the tree and stay in the receipt."""
    discovery.written(project, KINDS_REMOVED_PY, RENAMED_TEXT)
    discovery.put_claim(
        project, KINDS_REMOVED_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=sink_kind
    )
    token = writes.read_token(project, KINDS_REMOVED_PY, HOLDER_TICKET, HOLDER, sink_kind)
    claims.run(
        project,
        "file", "remove", KINDS_REMOVED_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token,
        sink_kind=sink_kind,
    )


def _rename_a_file(project: Path, sink_kind: str) -> None:
    """A rename: one operation over two paths, both recorded, over a text file."""
    source = KINDS_MOVED_PY
    discovery.written(project, source, RENAMED_TEXT)
    discovery.put_claim(
        project, source, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind
    )
    # The destination is not there, so its claim records `absent` rather than bytes: the
    # rename takes that probe itself, exactly as a creation does.
    writes.put_absent_claim(
        project, KINDS_RENAMED_PY, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind
    )
    token = writes.read_token(project, source, HOLDER_TICKET, HOLDER, sink_kind)
    claims.run(
        project,
        "file", "rename", source, KINDS_RENAMED_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token,
        sink_kind=sink_kind,
    )


# ---------------------------------------------------------------------------
# Reading the views back
# ---------------------------------------------------------------------------


def changes(project: Path, *extra, ticket: str = HOLDER_TICKET, sink_kind: str = "file"):
    """The `arbite changes` report for a ticket, as a process result.

    `ticket` defaults to the ticket the fixture builds; a test that means a *different* one
    (a mistyped id, or a ticket with no operations) says so rather than passing a second
    positional argument, which argparse would read as a usage error."""
    return examples.run_cli(project, "changes", ticket, *extra, sink=sink_kind)


def receipt_report(project: Path, operation_id: str, *extra, sink_kind: str = "file"):
    return examples.run_cli(project, "receipt", operation_id, *extra, sink=sink_kind)
