"""The frozen passthrough transcripts this slice owns: PC1, PC5 and PC6.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command,
exit code, stream and lines -- with ids, digests, paths and durations normalised on both
sides (`examples.py`), against a project built by `passthrough_state.py` with the real
CLI. PC6 holds three fences under one heading, so each is read with
`scenario_from_block` and asserted as its own transcript.

**Every block in this module is asserted byte for byte.** C15 reconciled the five blocks
that could not be reproduced as written, and each correction is recorded on the ticket:

- PC6's shell-syntax fence named no shell syntax at all (`sed -i 's/a/b/' src/arbite/cli.py`
  is ordinary argv, while PC1's substitution script has a `|` *inside* an argument and must
  run). The fence now carries the redirection token it is about, and it refuses as the block
  says.
- PC5's event row omitted the actor column PC1 prints for the same ticket and attempt; the
  row shows it now.
- PC2 elided the echoed argv and hand-padded the claim banner; PC4 did the same and also
  folded the banner onto one line and listed its rows schema.py-then-query.py, while paths
  sort lexically and `query.py` comes first. Both blocks now print exactly what the command
  prints, including the two-line banner `file claim` writes (FC1) and the `event:` line.

Beyond the transcripts, the facts a block cannot state are asserted directly: that the
sed really changed the file, that a receipt and both events exist afterwards, that a
refusal appended no event at all, and that the shell block's redirect landed in scratch
without being reported as a managed change.
"""

from __future__ import annotations

from dataclasses import replace

import examples
import passthrough_state as state
from arbite.coordination import passthrough as coordination_passthrough

HOLDER_TICKET = state.HOLDER_TICKET
HOLDER = state.HOLDER

#: PC1 and PC2 run the same substitution: the `O_EXCL` line gains `O_NOFOLLOW`.
SUBSTITUTED_FLAG = "O_EXCL|O_NOFOLLOW"
SED_SCRIPT = f"s/O_EXCL/{SUBSTITUTED_FLAG}/"


def _fence(scenario_id: str, index: int) -> examples.Scenario:
    """One fenced transcript of a block that holds more than one (`examples.py` reads the
    first for `scenario_block`; PC6 holds three)."""
    return examples.scenario_from_block(examples.fenced_blocks(scenario_id)[index], scenario_id)


# --- PC1: a familiar tool, and what arbite recorded about it --------------------


def test_PC1_wrap_a_familiar_tool_in_observed_mode(tmp_path):
    """PC1 byte for byte: the echo line, the duration, the row, the event and the hint.

    The transcript is asserted exactly, and so is what it *stands for*: the file really
    contains the substituted flag afterwards, and the run left a receipt of kind
    `passthrough` plus the `passthrough.changed` and `passthrough.exec` events -- the
    evidence a block of text cannot show."""
    project = state.project(tmp_path)
    output = examples.assert_scenario(examples.scenario_block("PC1"), project)

    assert "O_EXCL|O_NOFOLLOW" in (project / state.FILE_PY).read_text()
    assert "(no exclusivity claimed)" in output

    receipts = state.receipts(project)
    assert [receipt.kind for receipt in receipts] == ["passthrough"]
    assert receipts[0].paths == [state.FILE_PY]
    assert receipts[0].ticket_id == HOLDER_TICKET and receipts[0].attempt_id == HOLDER
    assert receipts[0].actor == state.HOLDER_WORKER
    assert receipts[0].claim_generation == 0, "observed mode holds no claim"
    assert receipts[0].before[state.FILE_PY] != receipts[0].after[state.FILE_PY]

    changed = state.events(project, coordination_passthrough.CHANGED_KIND)
    execs = state.events(project, coordination_passthrough.EXEC_KIND)
    assert [event.operation_id for event in changed] == [receipts[0].id]
    assert len(execs) == 1 and execs[0].payload["tool"] == "sed"
    assert execs[0].payload["exit_code"] == 0
    assert execs[0].payload["mode"] == "observed" and execs[0].payload["exclusive"] is False


def test_PC1_the_receipt_and_change_views_read_the_observed_change(tmp_path):
    """The block's own hint works: `arbite changes` nets the run, `arbite receipt`
    reproduces it.

    The version the sed replaced is a digest with no image behind it -- arbite observed
    the change rather than performing it, so those bytes were gone before it looked --
    and the receipt says exactly that instead of calling it missing evidence."""
    project = state.project(tmp_path)
    examples.run_cli(project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--",
                     "sed", "-i", "s/O_EXCL/O_EXCL|O_NOFOLLOW/", state.FILE_PY)
    operation = state.receipts(project)[0].id

    changes = state.run(project, "changes", HOLDER_TICKET)
    assert state.FILE_PY in changes.stdout and operation in changes.stdout

    receipt = state.run(project, "receipt", operation)
    assert "kind: passthrough" in receipt.stdout
    assert "(no image: observed, not performed by arbite)" in receipt.stdout
    assert "after: sha256:" in receipt.stdout and "(412 lines)" in receipt.stdout


# --- PC5: shell mode, and the limits it states ----------------------------------


def test_PC5_shell_mode_stated_limits(tmp_path):
    """PC5 byte for byte: the `sh -c` echo, the exit line and the event row.

    The exit line carries the redirection note instead of the no-exclusivity note, and the
    event names the `sh` tool with the same actor column PC1 prints. The redirect really
    happened -- the count is in the scratch area -- and it is *not* a change: scratch is
    transport, so the report has no `changed` block at all, which is the second half of the
    block's point."""
    project = state.project(tmp_path)
    output = examples.assert_scenario(_fence("PC5", 0), project)

    assert (project / state.SCRATCH_COUNT).read_text().strip() != ""
    assert "changed 1 path" not in output and "changed 2 paths" not in output
    assert state.receipts(project) == [], "a redirect into scratch is not a managed change"

    execs = state.events(project, coordination_passthrough.EXEC_KIND)
    assert len(execs) == 1
    assert execs[0].payload["tool"] == "sh" and execs[0].payload["shell"] is True
    assert execs[0].actor == state.HOLDER_WORKER
    assert execs[0].payload["changed"] == []


# --- PC6: refusals never run the command ----------------------------------------


def test_PC6_the_missing_tool_is_refused_before_anything_runs(tmp_path):
    """PC6's second fence, byte for byte: 127, `command did not run`, and no event."""
    project = state.project(tmp_path)
    examples.assert_scenario(_fence("PC6", 1), project)

    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.receipts(project) == []


def test_PC6_an_interactive_command_is_refused(tmp_path):
    """PC6's third fence, byte for byte: 126, the refusal and the edit hint.

    Nothing ran: the editor never opened, no event was appended, and no receipt exists --
    which is what "refusals never run the command" means in the records."""
    project = state.project(tmp_path)
    examples.assert_scenario(_fence("PC6", 2), project)

    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.receipts(project) == []


def test_PC6_shell_syntax_is_refused_before_anything_runs(tmp_path):
    """PC6's first fence, byte for byte: the redirection is argv arbite will not interpret.

    The block's `$` line ends in `> /tmp/sed.out`, so the sed is handed a redirect token it
    did not write. The refusal names `'>' style shell syntax`. Nothing ran: the file the sed
    was aimed at is byte-identical, no event was appended and no receipt exists."""
    project = state.project(tmp_path)
    before = (project / state.CLI_PY).read_text()

    examples.assert_scenario(_fence("PC6", 0), project)

    assert (project / state.CLI_PY).read_text() == before
    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.receipts(project) == []


# --- PC2, PC3, PC4: guarded mode (C14) -------------------------------------------


def test_PC2_guarded_mode(tmp_path):
    """PC2: the declared path is claimed, the command runs inside it, it is released.

    Byte for byte, banner and echoed argv included. The block's command really runs, so the
    assertions after it are the half a transcript cannot state: the sed rewrote the line it
    aims at, the receipt records the claim generation the run held -- one past the three
    acquisitions the same attempt made earlier -- and the release revoked *that* claim while
    the attempt's other three stayed exactly as they were.
    """
    project = state.generation_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("PC2"), project)

    assert SUBSTITUTED_FLAG in (project / state.FILE_PY).read_text()
    receipt = state.receipts(project)[0]
    assert receipt.kind == "passthrough" and receipt.paths == [state.FILE_PY]
    assert receipt.claim_generation == len(state.PRIOR_PATHS) + 1
    assert receipt.ticket_id == HOLDER_TICKET and receipt.attempt_id == HOLDER

    assert state.claims_for(project, state.FILE_PY) == [], "the run's own claim was released"
    assert [claim.path for claim in state.active_claims(project)] == sorted(state.PRIOR_PATHS)
    released = state.claim_records(project, state.FILE_PY)[0]
    assert not released.is_active, "the release keeps the record instead of erasing it"
    assert released.release_reason == coordination_passthrough.RELEASE_REASON

    payload = state.events(project, coordination_passthrough.EXEC_KIND)[0].payload
    assert payload["mode"] == "guarded" and payload["exclusive"] is True
    assert payload["claim_paths"] == [state.FILE_PY]
    assert payload["claim_generation"] == len(state.PRIOR_PATHS) + 1
    assert payload["unclaimed_write"] == []


def test_PC3_guarded_mode_a_busy_declared_path(tmp_path):
    """PC3 byte for byte: the refusal, the holder named, and nothing run or claimed.

    The block is a refusal, so the harness reads its body from stderr and asserts stdout is
    empty. The assertions after it are the ones a transcript cannot make: the bytes the sed
    would have written are the fixture's, no execution event was appended, and the refused
    attempt claimed nothing -- the path is still the holder's.
    """
    project = state.busy_project(tmp_path)
    before = (project / state.SCHEMA_PY).read_bytes()
    examples.assert_scenario(examples.scenario_block("PC3"), project)

    assert (project / state.SCHEMA_PY).read_bytes() == before
    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.receipts(project) == []
    assert [claim.attempt_id for claim in state.active_claims(project)] == [HOLDER]


def test_PC4_guarded_mode_a_change_outside_the_claimed_set(tmp_path):
    """PC4: the escape is detected, attributed, and left exactly where the tool put it.

    Byte for byte, including the two-line banner, the echoed argv, the `event:` line and the
    `claims released (...)` line the block had folded away. The rows print in canonical path
    order (query.py first), which is what the display rules produce, and that order is
    asserted directly below. What a transcript cannot show is asserted after it: the escaped
    bytes are still the sed's, the escaping receipt records no claim, and the claim this run
    *did* hold was released anyway, because ownership does not outlive the run that took it.
    """
    project = state.escape_project(tmp_path)
    stdout = examples.assert_scenario(examples.scenario_block("PC4"), project)

    rows = [
        line for line in stdout.splitlines() if line.strip().startswith(("M ", "A ", "D "))
    ]
    assert state.QUERY_PY in rows[0] and state.SCHEMA_PY in rows[1], (
        "rows print in canonical path order, which the block's own order is not"
    )

    schema_text = (project / state.SCHEMA_PY).read_text().splitlines()
    query_text = (project / state.QUERY_PY).read_text().splitlines()
    assert schema_text[state.SCHEMA_LINE - 1] == "y"
    assert query_text[state.QUERY_LINE - 1] == "y", (
        "the escaped write is still the tool's bytes: arbite does not undo a command it "
        "did not perform"
    )

    by_path = {receipt.paths[0]: receipt for receipt in state.receipts(project)}
    assert sorted(by_path) == [state.QUERY_PY, state.SCHEMA_PY]
    assert by_path[state.SCHEMA_PY].claim_generation == 1
    assert by_path[state.QUERY_PY].claim_generation == 0, "nothing was claimed for it"

    execution = state.events(project, coordination_passthrough.EXEC_KIND)[0].payload
    assert execution["unclaimed_write"] == [state.QUERY_PY]
    assert execution["claim_paths"] == [state.SCHEMA_PY]
    assert execution["exclusive"] is True and execution["mode"] == "guarded"

    changed = {
        event.subject: event.payload
        for event in state.events(project, coordination_passthrough.CHANGED_KIND)
    }
    assert changed[state.SCHEMA_PY]["claimed"] is True
    assert changed[state.QUERY_PY]["claimed"] is False

    assert state.claims_for(project, state.SCHEMA_PY) == [], (
        "the run's claim is released even when it caught an escape"
    )


def test_PC4_the_json_splits_the_tools_code_from_arbites(tmp_path):
    """The branchable form of PC4's run: two codes, and the escape in both halves.

    `exit_code` is always the wrapped tool's own -- it exited 0 -- while `arbite_exit_code`
    is what this process returns, 1, because the verification found something the caller has
    to act on. That split is the honest answer to "which of the two am I looking at", and it
    is why guarded output can be branchable at all.
    """
    project = state.escape_project(tmp_path)
    payload = state.guarded_json(
        project,
        "sed", "-i", "s/x/y/", state.SCHEMA_PY, state.QUERY_PY,
        paths=[state.SCHEMA_PY],
        expect=1,
    )

    assert payload["exit_code"] == 0 and payload["arbite_exit_code"] == 1
    assert payload["ran"] is True
    assert payload["mode"] == "guarded" and payload["exclusive"] is True
    assert payload["escaped"] is True and payload["unclaimed_write"] == [state.QUERY_PY]
    assert payload["claims"] == {
        "paths": [state.SCHEMA_PY],
        "generation": 1,
        "released": True,
        "release_reason": coordination_passthrough.RELEASE_REASON,
    }
    assert payload["exclusivity"]["claimed"] == [state.SCHEMA_PY]
    assert payload["exclusivity"]["available"] is True

    changed = {change["path"]: change for change in payload["changed"]}
    assert changed[state.SCHEMA_PY]["claimed"] is True
    assert changed[state.QUERY_PY]["claimed"] is False
    assert changed[state.QUERY_PY]["held_by"] is None
    assert payload["next_actions"] == [
        f"arbite file claim {state.QUERY_PY} --ticket {HOLDER_TICKET} --attempt {HOLDER}",
        f"arbite changes {HOLDER_TICKET}",
    ]
