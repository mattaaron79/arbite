"""The frozen passthrough transcripts this slice owns: PC1, PC5 and PC6.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command,
exit code, stream and lines -- with ids, digests, paths and durations normalised on both
sides (`examples.py`), against a project built by `passthrough_state.py` with the real
CLI. PC6 holds three fences under one heading, so each is read with
`scenario_from_block` and asserted as its own transcript.

Two of the frozen blocks cannot be reproduced as written, and both cases are stated here
rather than worked around quietly:

- **PC6's shell-syntax fence names no shell syntax.** Its command is
  `sed -i 's/a/b/' src/arbite/cli.py`, which contains nothing a shell would consume, and
  PC1's `sed -i 's/O_EXCL/O_EXCL|O_NOFOLLOW/'` -- which *does* contain a `|`, inside the
  substitution -- must run. No rule can refuse the first and run the second, so the
  fence's `$` line is the bug: what the block asserts is the refusal of an argv that
  carries a redirection, which is what the test feeds it, with that one deviation named
  in the test that makes it (recorded on tic-faae for C15 to fix in the document).
- **PC5's event row omits the actor column PC1's prints.** Both commands name the same
  ticket and attempt, so the row is the same row; the assertion is the block's facts per
  line (each expected line a prefix of an actual one, whitespace collapsed) rather than
  byte-for-byte, and the test says which abridgement it is accepting.

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
    """PC5 asserted per line, because the block's event row omits the actor column.

    What the block does assert is checked in full: the `sh -c` echo line, the exit line
    with the redirection note instead of the no-exclusivity note, the `sh` tool in the
    event, and exit 0. The redirect really happened -- the count is in the scratch area --
    and it is *not* a change: scratch is transport, so the report has no `changed` block
    at all, which is the second half of the block's point."""
    project = state.project(tmp_path)
    output = examples.assert_scenario_abridged(
        _fence("PC5", 0), project
    )

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
    """PC6's first fence: its refusal, for the command the block is about.

    The block's own `$` line carries no shell syntax at all (see this module's
    docstring), so the same argv *with* the redirection the fence names is what is run:
    `sed -i 's/a/b/' src/arbite/cli.py > /tmp/sed.out` as argv is exactly the mistake the
    refusal exists for -- four arguments, one of them a redirection arbite will not
    interpret. The expected text is the block's, unmodified and compared byte for byte."""
    project = state.project(tmp_path)
    before = (project / state.CLI_PY).read_text()
    block = _fence("PC6", 0)
    redirecting = replace(
        block,
        command=tuple([*block.command, ">", "/tmp/arbite-sed-out.txt"]),
    )
    examples.assert_scenario(redirecting, project)

    # Nothing ran: the file the sed was aimed at is byte-identical, no event was appended
    # and no receipt exists.
    assert (project / state.CLI_PY).read_text() == before
    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.receipts(project) == []


def test_PC6_the_frozen_shell_syntax_command_contains_no_shell_syntax(tmp_path):
    """The fence's command, run as written, is not a shell-syntax refusal at all.

    This is the deviation the test above names, asserted rather than described: the
    frozen `$` line executes (sed rewrites the file, exit 0), because every rule that
    would refuse it -- a `>` token, a `|` token, a `$(...)` -- would also refuse PC1's
    substitution script, whose `|` is a character inside an argument. C15 owns the
    document fix; until then the refusal is asserted against the command the block
    describes."""
    project = state.project(tmp_path)
    proc = examples.run_scenario(_fence("PC6", 0), project)
    assert proc.returncode == 0, proc.stderr

    assert state.CLI_DEF_TEXT not in (project / state.CLI_PY).read_text(), (
        "sed ran and rewrote the line the substitution matches"
    )
    assert state.events(project, coordination_passthrough.EXEC_KIND), "the run was recorded"
