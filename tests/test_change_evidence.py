"""What the receipt and change views promise, on both sinks.

`test_examples.py` asserts the frozen EV1 and EV6 transcripts. This file asserts the
guarantees those transcripts stand for:

- **A receipt reproduces the versions it records.** Both digests, the shape each version
  has, and the bytes behind them, checked against the artifact store rather than against
  the report's own numbers -- and checked for *every* kind the engine performs, because a
  receipt that reproduced a text write but not a removed file, a moved file or a byte
  payload would still satisfy a view that only ever looked at an edit.
- **The net view never hides a reverted operation.** The row that collapses an edit and its
  undo keeps both operations' ids and says so; `--all` shows each operation as the operation
  it was, and an operation that never applied is counted and listed rather than netted.
- **Evidence is stored once per digest.** An edit-then-revert keeps two versions once each,
  and both receipts name the same artifact for the version they share.
- **A version this proxy will not keep refuses the mutation before any byte changes.** The
  limit is explicit in `coordination.store`, one number for both sinks, and a refused
  operation leaves no bytes, no receipt and -- deliberately -- no partial evidence behind.
- **Nothing is summarised and nothing is uploaded.** The JSON shape is asserted key by key,
  so a field that described a change in prose rather than in recorded bytes would fail here.

Both sinks run every one of them: an evidence view that only worked on the backend whose
content happens to be a file would be two different products.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

import discovery_state as discovery
import evidence_state as state
import examples
import lifecycle_state as lifecycle
import writes_state as writes
from arbite import cli
from arbite.coordination import records as coordination_records
from arbite.coordination import store as coordination_store
from arbite.coordination.app import CoordinationApp
from arbite.coordination.lifecycle import TicketLifecycle
from arbite.coordination.mutations import FileMutations, MutationRequest, PathChange
from arbite.errors import EvidenceRefused

HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET
BASE_PY = state.BASE_PY

#: The second worker whose operations the multi-attempt view adds up, and the file it writes.
LATER_AGENT = "claude.opus.002"
LATER_TEXT = "later bytes\n"
LATER_EDITED = "later bytes\nand one more\n"

#: The keys `arbite receipt --json` prints. Named in one place so a later slice cannot add a
#: "summary" field without this test saying so: the receipt is the recorded bytes, and the
#: only prose in the whole surface is the ticket's own notes, which this command never reads.
RECEIPT_KEYS = {
    "operation",
    "kind",
    "result",
    "stored_result",
    "recorded_at",
    "ticket",
    "attempt",
    "actor",
    "claim_generation",
    "paths",
    "artifact",
    "next_actions",
}
PATH_KEYS = {"path", "before", "after"}
ARTIFACT_KEYS = {"image", "stored", "retained", "verified", "sides", "entries"}
ENTRY_KEYS = {"id", "digest", "size", "sides", "verified"}


def receipt_facts(project, operation_id, sink_kind="file") -> dict:
    proc = state.receipt_report(project, operation_id, "--json", sink_kind=sink_kind)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def changes_facts(project, *extra, sink_kind="file") -> dict:
    proc = state.changes(project, *extra, "--json", sink_kind=sink_kind)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def drop_artifact_content(store, digest: str) -> None:
    """Remove a version's bytes *behind the store's back* -- the drift the receipt view catches.

    Backend-specific on purpose: the check is shared, but losing content is not (the file
    backend's bytes are a file, SQLite's are a row), and a helper that removed them through a
    public API would assert what that API does instead of what the view reports."""
    if store.kind == "file":
        store.artifact_path(digest).unlink()
        return
    connection = sqlite3.connect(store.root)
    try:
        connection.execute("DELETE FROM coordination_artifacts WHERE digest = ?", (digest,))
        connection.commit()
    finally:
        connection.close()


# --- a receipt reproduces what the operation recorded -------------------------


def test_the_receipt_reproduces_both_versions_from_the_stored_bytes(tmp_path, kind):
    """EV6's facts, checked against the store: the digests, the shapes, and the bytes.

    The artifact store is the witness rather than the report: the text prints `570 lines`
    because the content filed under that digest has 570 lines, and the JSON's digests are the
    ones `verify_artifact` just recomputed."""
    project = state.ev_project(tmp_path, kind)
    operation = state.base_operation(project, kind)
    record = state.receipt(project, operation, kind)
    facts = receipt_facts(project, operation, kind)

    assert facts["operation"] == operation
    assert facts["kind"] == "write"
    assert facts["result"] == "ok" and facts["stored_result"] == "succeeded"
    assert facts["ticket"] == HOLDER_TICKET and facts["attempt"] == HOLDER
    assert facts["actor"] == "claude.opus.001" and facts["claim_generation"] == 2

    entry = facts["paths"][0]
    assert entry["path"] == BASE_PY
    assert entry["before"]["digest"] == record.before[BASE_PY]
    assert entry["after"]["digest"] == record.after[BASE_PY]
    assert entry["before"]["lines"] == 570 and entry["after"]["lines"] == 588

    store = state.store_for(project, kind)
    assert store.verify_artifact(record.before[BASE_PY]) == writes.base_text().encode("utf-8")
    assert store.verify_artifact(record.after[BASE_PY]) == writes.base_text(
        append=writes.APPENDED_LINES
    ).encode("utf-8")

    text = state.receipt_report(project, operation, sink_kind=kind).stdout
    assert f"before: {coordination_records.short_digest(record.before[BASE_PY])} (570 lines)" in text
    assert f"after: {coordination_records.short_digest(record.after[BASE_PY])} (588 lines)" in text
    assert "artifact: before image stored and retained" in text


def test_every_kind_of_operation_round_trips_through_its_receipt(tmp_path, kind):
    """Write, edit, creation, removal, rename and a byte payload: both versions each time.

    The rename is what keeps the model honest -- one operation, two paths, and the *same*
    version on either side of it -- and the byte payload is the case a text diff could never
    describe."""
    project = state.kinds_project(tmp_path, kind)
    found = state.operations_by_kind(project, kind)
    assert set(found) == {"write", "edit", "rename", "remove"}

    store = state.store_for(project, kind)
    seen_paths = set()
    for operation_ids in found.values():
        for operation_id in operation_ids:
            record = store.get_record("receipt", operation_id)
            entries = {
                entry["path"]: entry
                for entry in receipt_facts(project, operation_id, kind)["paths"]
            }
            assert set(entries) == set(record.paths)
            for path, entry in entries.items():
                if record.before[path] == coordination_records.ABSENT:
                    assert entry["before"] is None
                else:
                    assert entry["before"]["digest"] == record.before[path]
                    assert entry["before"]["bytes"] == len(store.verify_artifact(record.before[path]))
                seen_paths.add(path)

    assert seen_paths == {
        state.KINDS_EDIT_PY,
        state.KINDS_BINARY,
        state.KINDS_REMOVED_PY,
        state.KINDS_MOVED_PY,
        state.KINDS_RENAMED_PY,
    }

    removed = store.get_record("receipt", found["remove"][0])
    assert removed.after[state.KINDS_REMOVED_PY] == coordination_records.ABSENT
    assert store.verify_artifact(removed.before[state.KINDS_REMOVED_PY]) == (
        state.RENAMED_TEXT.encode("utf-8")
    )

    # The creation of the byte payload is the other `write` of this world.
    binary_id = state.operation_touching(project, state.KINDS_BINARY, kind)
    binary = store.get_record("receipt", binary_id)
    assert binary.before[state.KINDS_BINARY] == coordination_records.ABSENT
    assert store.verify_artifact(binary.after[state.KINDS_BINARY]) == state.BINARY_BYTES
    binary_text = state.receipt_report(project, binary_id, sink_kind=kind).stdout
    assert f"({len(state.BINARY_BYTES)} bytes, binary)" in binary_text

    renamed = store.get_record("receipt", found["rename"][0])
    assert renamed.paths == [state.KINDS_MOVED_PY, state.KINDS_RENAMED_PY]
    moved = renamed.before[state.KINDS_MOVED_PY]
    assert renamed.after[state.KINDS_RENAMED_PY] == moved, "one version, two sides"
    assert renamed.before[state.KINDS_RENAMED_PY] == coordination_records.ABSENT
    assert renamed.after[state.KINDS_MOVED_PY] == coordination_records.ABSENT
    assert store.verify_artifact(moved) == state.RENAMED_TEXT.encode("utf-8")


def test_a_creation_and_a_removal_name_the_side_that_has_an_image(tmp_path, kind):
    """A creation has no before image and a removal leaves after absent, so the evidence line
    names the image the operation did keep -- which is the whole claim it makes."""
    project = state.kinds_project(tmp_path, kind)
    found = state.operations_by_kind(project, kind)

    binary = state.operation_touching(project, state.KINDS_BINARY, kind)
    created = state.receipt_report(project, binary, sink_kind=kind).stdout
    assert "before: absent" in created
    assert "artifact: after image stored and retained" in created

    deleted = state.receipt_report(project, found["remove"][0], sink_kind=kind).stdout
    assert "after: absent" in deleted
    assert "artifact: before image stored and retained" in deleted

    facts = receipt_facts(project, found["remove"][0], kind)
    assert facts["artifact"]["image"] == "before" and facts["artifact"]["sides"] == ["before"]


# --- the net view, and the log behind it --------------------------------------


def test_the_net_view_never_hides_a_reverted_operation(tmp_path, kind):
    """EV1's guarantee, asserted where it can fail: the collapsed row names both operations,
    the note points at the ordered view, and `--all` shows each operation separately."""
    project = state.ev_project(tmp_path, kind)
    net = state.changes(project, sink_kind=kind).stdout
    lines = net.splitlines()

    assert lines[0] == f"{HOLDER_TICKET} · attempt {HOLDER} (claude.opus.001) · 5 operations"

    revert_row = next(line for line in lines if state.SCHEMA_PY in line)
    assert "no net change" in revert_row
    pair = revert_row.split("(")[-1].rstrip(")").split(", ")
    assert len(pair) == 2, "both operations of the pair are named in the row"
    assert "edit-then-revert: both operations remain in the log ('--all')" in net

    ordered = state.changes(project, "--all", sink_kind=kind).stdout
    rows = ordered.splitlines()[1:]
    for operation_id in pair:
        assert any(
            line.startswith("M ") and operation_id in line and state.SCHEMA_PY in line
            for line in rows
        ), f"{operation_id} is not an operation in the ordered log"

    # The net view is honest about *why* two operations are one row, and the JSON keeps the
    # operations and the note rather than only the sentence.
    entry = next(
        one
        for one in changes_facts(project, sink_kind=kind)["attempts"][0]["changes"]
        if one["path"] == state.SCHEMA_PY
    )
    assert entry["net_change"] is False and entry["operations"] == pair
    assert entry["before"]["digest"] == entry["after"]["digest"]
    assert "edit-then-revert" in entry["note"]


def test_an_operation_that_never_applied_is_counted_and_listed_but_never_netted(tmp_path, kind):
    """The fifth operation of EV1's world: a receipt a recovery pass finalised as failed.

    It changed nothing, so a *net* view has no row for it -- and hiding it entirely would be
    the other lie, so the header counts it and the ordered view lists it as the operation it
    is."""
    project = state.ev_project(tmp_path, kind)
    failed = state.failed_operation(project, kind)
    store = state.store_for(project, kind)
    assert store.get_record("receipt", failed).result == coordination_records.RECEIPT_FAILED

    net = state.changes(project, sink_kind=kind).stdout
    assert "5 operations" in net, "the count is the log's length"
    assert failed not in net, "a failed operation contributes no net change"

    ordered = state.changes(project, "--all", sink_kind=kind).stdout
    row = next(line for line in ordered.splitlines() if failed in line)
    assert state.ABANDONED_PY in row and "failed -- never applied" in row

    facts = changes_facts(project, "--all", sink_kind=kind)
    assert facts["attempts"][0]["operations"] == 5
    entry = next(one for one in facts["attempts"][0]["log"] if one["operation"] == failed)
    assert entry["result"] == "failed" and entry["stored_result"] == "failed"
    assert entry["paths"][state.ABANDONED_PY]["after"] != coordination_records.ABSENT


def test_evidence_is_stored_once_per_digest_across_two_operations(tmp_path, kind):
    """An edit-then-revert keeps two versions once each: the second operation's *before* is
    the first one's *before* again, and both receipts name the same artifact for it."""
    project = state.ev_project(tmp_path, kind)
    store = state.store_for(project, kind)
    operands = [
        receipt for receipt in state.operations(project, kind) if state.SCHEMA_PY in receipt.paths
    ]
    assert len(operands) == 2

    original = coordination_records.digest_bytes(state.SCHEMA_ORIGINAL.encode("utf-8"))
    edited = coordination_records.digest_bytes(state.SCHEMA_EDITED.encode("utf-8"))
    forward = next(one for one in operands if one.before[state.SCHEMA_PY] == original)
    back = next(one for one in operands if one.before[state.SCHEMA_PY] == edited)

    assert forward.after[state.SCHEMA_PY] == edited
    assert back.after[state.SCHEMA_PY] == original, "back to the bytes it started from"

    records = [artifact for artifact in store.records("artifact") if artifact.digest == edited]
    assert len(records) == 1, "one version, one artifact record"
    assert store.verify_artifact(edited).decode("utf-8") == state.SCHEMA_EDITED
    assert records[0].id in forward.artifacts and records[0].id in back.artifacts


def test_the_net_view_adds_the_ticket_up_when_more_than_one_attempt_did_work(tmp_path, kind):
    """A reopen starts a new attempt, so a ticket's own net is not any one attempt's: the view
    prints a section per attempt and then the ticket's net across them.

    The second attempt is real -- the work is closed, reopened and claimed again, which is the
    only way arbite starts one -- so the sections are the lifecycle's, not a fixture's."""
    project = state.ev_project(tmp_path, kind)
    later_path = "src/arbite/later.py"
    claimed = None
    for command in (
        ("close", HOLDER_TICKET),
        ("reopen", HOLDER_TICKET, "--reason", "one more file to change"),
        ("claim", HOLDER_TICKET, "--agent", LATER_AGENT),
    ):
        proc = examples.run_cli(project, *command, sink=kind)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        claimed = proc.stdout
    attempt = re.search(r"att-[0-9a-f]{4}", claimed).group(0)

    # The second attempt creates a file, so the ticket's net gains an `A` row that neither
    # attempt's own section can show on its own.
    writes.put_absent_claim(project, later_path, HOLDER_TICKET, attempt, generation=1, sink_kind=kind)
    writes.staged(project, "later.py", LATER_TEXT)
    written = examples.run_cli(
        project,
        "file", "write", later_path,
        "--ticket", HOLDER_TICKET, "--attempt", attempt,
        "--input", "later.py",
        sink=kind,
    )
    assert written.returncode == 0, written.stdout + written.stderr

    text = state.changes(project, sink_kind=kind).stdout
    assert f"{HOLDER_TICKET} · attempt {HOLDER} (claude.opus.001) · 5 operations" in text
    assert f"{HOLDER_TICKET} · attempt {attempt} ({LATER_AGENT}) · 1 operation" in text
    assert f"{HOLDER_TICKET} · ticket net · 6 operations" in text

    facts = changes_facts(project, sink_kind=kind)
    assert [section["attempt"] for section in facts["attempts"]] == [HOLDER, attempt]
    ticket_net = {entry["path"]: entry for entry in facts["ticket_net"]}
    assert set(ticket_net) == {BASE_PY, state.RECORDS_PY, state.SCHEMA_PY, later_path}
    assert ticket_net[later_path]["status"] == "A", "created, across the whole ticket"
    assert ticket_net[state.SCHEMA_PY]["net_change"] is False, "and the revert is still a net of zero"


# --- refusing rather than reporting drift -------------------------------------


def test_a_receipt_whose_bytes_are_gone_is_refused_and_changes_nothing(tmp_path, kind):
    """A digest that cannot be reproduced is not a receipt: the command refuses, names the
    version, and says that nothing was changed. Printing recorded digests as if they were
    checked would be the one thing this command must never do."""
    project = state.ev_project(tmp_path, kind)
    operation = state.base_operation(project, kind)
    record = state.receipt(project, operation, kind)
    store = state.store_for(project, kind)
    drop_artifact_content(store, record.before[BASE_PY])
    before_bytes = (project / BASE_PY).read_bytes()

    proc = state.receipt_report(project, operation, sink_kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert coordination_records.short_digest(record.before[BASE_PY]) in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert "next: 'arbite doctor'" in proc.stderr, "the refusal names the repair"
    assert (project / BASE_PY).read_bytes() == before_bytes, "a report changes no bytes"


def test_a_receipt_that_lost_its_artifact_record_is_refused(tmp_path, kind):
    """The other half: the bytes are there, but the record that indexes them is not. The
    receipt's own evidence list is what is checked, so a missing index entry cannot pass as a
    complete receipt."""
    project = state.ev_project(tmp_path, kind)
    operation = state.base_operation(project, kind)
    record = state.receipt(project, operation, kind)
    state.store_for(project, kind).delete_record("artifact", record.artifacts[0])

    proc = state.receipt_report(project, operation, sink_kind=kind)

    assert proc.returncode == 1
    assert record.artifacts[0] in proc.stderr
    assert "nothing was changed" in proc.stderr


def test_a_receipt_that_is_not_an_operation_says_what_it_is(tmp_path, kind):
    """A read observation shares the `op-` id space with a receipt, so an id that names one is
    answered as what it is -- and an id nothing has names the command that finds one."""
    project = state.ev_project(tmp_path, kind)
    store = state.store_for(project, kind)
    observation = store.records("observation")[0].id

    handle = state.receipt_report(project, observation, sink_kind=kind)
    assert handle.returncode == 1
    assert "is a read observation, not an operation" in handle.stderr
    assert "arbite events --tail 20 --include-reads" in handle.stderr

    missing = state.receipt_report(project, "op-0000", sink_kind=kind)
    assert missing.returncode == 1
    assert "no operation op-0000 in this store" in missing.stderr
    assert "arbite events --tail 20" in missing.stderr


def test_changes_with_nothing_recorded_is_an_answer_not_an_error(tmp_path, kind):
    """A query that matched no operations exits 2 with the command that shows what did happen,
    and a ticket this project does not have is refused instead -- a typo must not read as
    "nothing happened yet"."""
    project = writes.init_project(tmp_path, kind)

    empty = state.changes(project, sink_kind=kind)
    assert empty.returncode == 2
    assert empty.stdout == (
        f"no operations recorded for {HOLDER_TICKET}\n"
        "next: 'arbite events --tail 20' to see what this workspace recorded\n"
    )
    assert json.loads(state.changes(project, "--json", sink_kind=kind).stdout) == {
        "ticket": HOLDER_TICKET,
        "operations": 0,
        "attempts": [],
        "next_actions": ["arbite events --tail 20"],
    }

    unknown = state.changes(project, ticket="tic-zzzz", sink_kind=kind)
    assert unknown.returncode == 1
    assert "no ticket tic-zzzz" in unknown.stderr and "next: 'arbite list'" in unknown.stderr


# --- the size limit -----------------------------------------------------------


def test_a_version_over_the_size_limit_refuses_before_any_byte_changes(tmp_path, kind, monkeypatch):
    """The limit is one rule for both sinks, and a refused operation leaves nothing behind.

    The limit is patched *in process* rather than reached with a 64 MiB payload: what has to
    be proved -- that the engine refuses before it stores a byte of evidence or touches the
    project -- is proved by running the engine itself, and the command surface is covered
    beside this test."""
    project = writes.init_project(tmp_path, kind)
    path = "src/arbite/kept.py"
    original = "untouched\n"
    replacement = "this replacement is one byte too long\n"
    discovery.written(project, path, original)
    discovery.put_claim(project, path, HOLDER_TICKET, HOLDER, generation=1, sink_kind=kind)
    token = writes.read_token(project, path, HOLDER_TICKET, HOLDER, kind)
    monkeypatch.setattr(coordination_store, "MAX_ARTIFACT_BYTES", len(replacement) - 1)

    sink = lifecycle.sink_for(project, kind)
    app = CoordinationApp.open(sink, project, project / ".arbite")
    mutations = FileMutations(sink, TicketLifecycle(sink, app))

    with pytest.raises(EvidenceRefused) as refusal:
        mutations.apply(
            MutationRequest(
                kind="write",
                ticket_id=HOLDER_TICKET,
                attempt_id=HOLDER,
                actor="claude.opus.001",
                changes=(
                    PathChange(
                        path=path,
                        expect=coordination_records.digest_bytes(original.encode("utf-8")),
                        becomes=coordination_records.digest_bytes(replacement.encode("utf-8")),
                        payload=replacement.encode("utf-8"),
                        token=token,
                    ),
                ),
            )
        )

    message = str(refusal.value)
    assert f"over the {len(replacement) - 1}-byte limit" in message
    assert "nothing was changed" in message
    assert list(refusal.value.next_actions) == [coordination_store.SIZE_LIMIT_HINT]

    assert (project / path).read_text(encoding="utf-8") == original, "no bytes changed"
    assert app.store.records("receipt") == [] and app.store.pending_operations() == []
    assert app.store.records("artifact") == [], "and no partial evidence of its own"
    assert app.store.find_record("observation", token).spent_by is None, "the token is unspent"


def test_the_size_limit_refuses_at_the_command_surface_too(tmp_path):
    """The same rule through the real command: a payload over the limit is refused with its
    size, the limit and the repair, and the project keeps the bytes it had.

    The file sink only, because this is the half a subprocess has to run: the engine's
    pre-check is shared and asserted on both sinks above, and both backends' own
    `put_artifact_bytes` refuse in the same words from the same helper."""
    project = writes.init_project(tmp_path, "file")
    discovery.written(project, BASE_PY, writes.base_text())
    discovery.put_claim(project, BASE_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind="file")
    token = writes.read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, "file")
    oversized = b"x" * (coordination_store.MAX_ARTIFACT_BYTES + 1)
    writes.staged(project, "huge.py", oversized)

    proc = examples.run_cli(
        project,
        "file", "write", BASE_PY,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "huge.py",
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{len(oversized)} bytes" in proc.stderr
    assert f"{coordination_store.MAX_ARTIFACT_BYTES}-byte limit" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert "next: keep files larger than the limit outside managed source paths" in proc.stderr
    assert (project / BASE_PY).read_bytes() == writes.base_text().encode("utf-8")
    assert state.store_for(project, "file").records("receipt") == []
    assert (project / ".arbite" / "scratch" / "huge.py").exists(), "the payload is untouched"


# --- the shape of the output --------------------------------------------------


def test_the_receipt_json_carries_exactly_the_recorded_facts(tmp_path, kind):
    """Key by key, so the surface cannot quietly grow a summary: what JSON prints is the
    operation, its attribution, its versions and its evidence."""
    project = state.ev_project(tmp_path, kind)
    facts = receipt_facts(project, state.base_operation(project, kind), kind)

    assert set(facts) == RECEIPT_KEYS
    assert set(facts["paths"][0]) == PATH_KEYS
    assert set(facts["artifact"]) == ARTIFACT_KEYS
    assert facts["artifact"]["image"] == "before"
    assert facts["artifact"]["stored"] and facts["artifact"]["retained"]
    assert facts["artifact"]["verified"] and facts["artifact"]["sides"] == ["after", "before"]
    assert len(facts["artifact"]["entries"]) == 2
    for entry in facts["artifact"]["entries"]:
        assert set(entry) == ENTRY_KEYS and entry["verified"]


def test_a_pending_operation_is_reported_rather_than_netted(tmp_path, kind):
    """An operation nobody has judged may or may not have happened, so both views say so: the
    receipt prints `pending`, and the change view prints a note naming it instead of folding it
    into a summary a reader could not trust."""
    project = state.ev_project(tmp_path, kind)
    pending = state.abandon_operation(project, kind, settle=False)

    changes = state.changes(project, sink_kind=kind)
    assert changes.returncode == 0
    assert f"pending: {pending} staged a write of {state.ABANDONED_PY}" in changes.stdout
    assert f"'arbite receipt {pending}'" in changes.stdout

    facts = receipt_facts(project, pending, kind)
    assert facts["result"] == "pending" and facts["stored_result"] == "pending"
    text = state.receipt_report(project, pending, sink_kind=kind).stdout
    assert "result: pending" in text
    assert "pending: this operation was staged and not finalized" in text

    assert changes_facts(project, sink_kind=kind)["pending"] == [pending]
    assert state.store_for(project, kind).get_record("receipt", pending).is_pending


def test_every_hint_these_views_print_is_a_command_that_exists(tmp_path, kind):
    """The rule the receipt view inherits from the rest of the surface: guidance is only useful
    if it can be run, so every `arbite <command>` in a refusal is checked against the parser
    rather than trusted."""
    project = state.ev_project(tmp_path, kind)
    operation = state.base_operation(project, kind)
    drop_artifact_content(state.store_for(project, kind), state.receipt(project, operation, kind).after[BASE_PY])

    refusals = [
        state.receipt_report(project, operation, sink_kind=kind),
        state.receipt_report(project, "op-0000", sink_kind=kind),
        state.changes(project, ticket="tic-zzzz", sink_kind=kind),
    ]
    for proc in refusals:
        assert proc.returncode == 1
        for quoted in re.findall(r"'arbite ([^']+)'", proc.stderr):
            words = quoted.split()
            assert any(
                cli.knows_command(" ".join(words[:depth]))
                for depth in range(len(words), 0, -1)
            ), f"'arbite {quoted}' is named but no part of it is a command"
    assert cli.knows_command("receipt") and cli.knows_command("changes")


def test_the_views_are_read_only(tmp_path, kind):
    """Neither command writes anything: the records, the events and the artifacts are exactly
    what they were, which is what makes them safe to run while work is in flight."""
    project = state.ev_project(tmp_path, kind)
    store = state.store_for(project, kind)

    def snapshot():
        return (
            [record.to_dict() for record in store.records("receipt")],
            [record.to_dict() for record in store.records("event")],
            [artifact.to_dict() for artifact in store.records("artifact")],
        )

    before = snapshot()
    for receipt in store.records("receipt"):
        state.receipt_report(project, receipt.id, sink_kind=kind)
    state.changes(project, sink_kind=kind)
    state.changes(project, "--all", sink_kind=kind)

    assert snapshot() == before
