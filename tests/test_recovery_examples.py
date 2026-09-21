"""The frozen transcripts this slice owns: DR1 and DR2 (and DR3, which is the scratch slice's).

Each is asserted against its block in `.arbite/planning/interaction-examples.md` -- command,
exit code, output -- with ids, times and paths normalised on both sides (`examples.py`), on
**both** sinks: the coordination findings are the same facts whichever store holds the records,
which is the property the two backends exist for. `recovery_state.py` builds the damaged store
the transcripts describe.

**No deviations remain.** Two sentences in these blocks name commands other slices owned, and
`arbite` may not name a command it does not have -- a capability claimed on paper only -- so
each of them printed an honest sentence without the command until that slice landed:

- `arbite scratch clear` arrived with tic-95c0, so the DR1 note prints the frozen guidance
  naming it.
- `arbite receipt` and `arbite changes` arrived with tic-7c42, so the DR2 `--fix` detail prints
  the frozen `inspect 'arbite receipt op-4f19' and 'arbite changes tic-1a75'` sentence.

Both blocks are therefore compared byte for byte, and
`test_the_doctor_report_names_no_command_this_arbite_does_not_have` is what keeps that honest:
a hint that names a command is checked against the parser rather than trusted.
"""

from __future__ import annotations

import json
import re

import pytest

import examples
import recovery_state as state
from arbite import cli
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

SINKS = ("file", "sqlite")

#: DR1's scratch note, frozen, with the guidance tic-95c0's command makes printable.
SCRATCH_NOTE = (
    "note: .arbite/scratch/ holds 3 files (12.4 KiB) -- transport left behind, expected "
    "after an\n      interrupted run; clear with 'arbite scratch clear --all'"
)


@pytest.fixture(params=SINKS)
def damaged_project(request, tmp_path):
    return state.damaged(tmp_path, request.param)


# --- DR1 ----------------------------------------------------------------------


def test_DR1_problems_found(damaged_project):
    """Three findings, each with the response it needs: a claim nobody can use, an unfinished
    operation whose bytes are neither version, and a claim naming an attempt this store does
    not have. The payload note rides along without changing the exit code."""
    scenario = examples.scenario_block("DR1")
    stdout = examples.assert_scenario(scenario, damaged_project)

    # The scratch note is the frozen one, guidance sentence and all, because `arbite scratch
    # clear` exists now (tic-95c0) -- and it is a note, not a problem, so the exit code is
    # the findings' business alone.
    assert examples.normalise(SCRATCH_NOTE, damaged_project) in examples.normalise(
        stdout, damaged_project
    )
    assert scenario.exit_code == 3


def test_DR1_the_three_findings_are_the_three_the_store_holds(damaged_project):
    """The exit code is not the only fact: the findings name the records a repair has to act
    on, in the order a reader acts on them."""
    proc = examples.run_cli(damaged_project, "doctor", "--json")

    payload = json.loads(proc.stdout)
    assert payload["tickets_checked"] == state.TICKETS
    assert [p["kind"] for p in payload["problems"]] == [
        "orphaned_claim",
        "pending_operation",
        "claim_without_attempt",
    ]
    assert [p["fixed"] for p in payload["problems"]] == [False, False, False]
    assert payload["remaining"] == 3 and payload["fixed"] == 0
    assert payload["coordination"]["pending_operations"] == 1
    assert payload["scratch"] == {"files": 3, "bytes": sum(state.SCRATCH_SIZES)}


# --- DR2 ----------------------------------------------------------------------


def test_DR2_fix_repairs_only_the_unambiguous(damaged_project):
    """`--fix` releases the two claims nobody can use and leaves the drift exactly as it was,
    saying why -- and naming the two views a human compares the versions with, which is the
    sentence the receipt slice (tic-7c42) made printable."""
    # One run only: `--fix` repairs, so a second invocation is a different state (asserted
    # separately below).
    scenario = examples.scenario_block("DR2")
    examples.assert_scenario(scenario, damaged_project)

    assert scenario.exit_code == 3


def test_DR2_the_repair_releases_ownership_and_leaves_the_bytes(damaged_project):
    """What the transcript's `fixed` lines mean in the store: both claims are released (the
    records stay as history, with the reason), the attempt that ended is untouched, and the
    drift is still pending with its bytes where they were."""
    project = damaged_project
    examples.run_cli(project, "doctor", "--fix")

    store = open_coordination_store(state.sink_for(project, _sink_kind(project)))
    claims = {claim.path: claim for claim in store.records("claim")}
    assert set(claims) == {state.DRIFT_PATH, state.DANGLING_PATH}
    for claim in claims.values():
        assert claim.state == coordination_records.CLAIM_RELEASED
        assert claim.release_reason.startswith("doctor --fix:")
        assert claim.released is not None
    assert (project / state.DRIFT_PATH).read_bytes() == state.DRIFTED
    assert (project / state.DANGLING_PATH).exists()

    receipts = {receipt.id: receipt for receipt in store.records("receipt")}
    assert receipts[state.OPERATION].is_pending, "drift is never finalised by a repair"
    assert store.active_claims() == []
    assert [problem.kind for problem in store.record_problems()] == ["pending_operation"]

    # The repair is recorded as an event, so the stream explains the released claims.
    released = [event for event in store.events() if event.kind == "release.file"]
    assert len(released) == 2
    assert all("doctor --fix" in event.payload["reason"] for event in released)


def test_DR2_a_second_fix_run_reports_the_drift_and_repairs_nothing_more(damaged_project):
    """Idempotent: the claims are already released, so the second run has only the drift to
    report -- and it reports exactly that, still as a problem (exit 3)."""
    project = damaged_project
    examples.run_cli(project, "doctor", "--fix")

    proc = examples.run_cli(project, "doctor", "--fix")

    assert proc.returncode == 3
    assert "released the claim" not in proc.stdout
    assert proc.stdout.count("problem [") == 1
    assert "pending_operation" in proc.stdout
    assert "checked 22 tickets: 1 problem(s), 0 fixed" in proc.stdout


# --- the boundary of what a report may say ------------------------------------


def test_the_doctor_report_names_no_command_this_arbite_does_not_have(damaged_project):
    """The rule the two deviations existed for, asserted directly rather than trusted: every
    `arbite <command>` a report names is a command the parser really defines.

    Guidance is only useful if the caller can run it; a hint naming a slice that has not
    landed yet sends an agent to a command that fails with `invalid choice`."""
    for args in (("doctor",), ("doctor", "--fix")):
        proc = examples.run_cli(damaged_project, *args)
        # Quoted, which is how this surface writes a runnable hint: prose that happens to say
        # "what arbite can correct automatically" is not naming a command. A hint may carry
        # arguments (`'arbite receipt op-4f19'`), so the check is that some prefix of the
        # quoted words is a command the parser defines.
        for quoted in re.findall(r"'arbite ([^']+)'", proc.stdout):
            words = quoted.split()
            assert any(
                cli.knows_command(" ".join(words[:depth]))
                for depth in range(len(words), 0, -1)
            ), f"'arbite {quoted}' is named by {' '.join(args)} but no part of it is a command"


def test_both_guidance_sentences_print_because_their_commands_exist(damaged_project):
    """A deviation was a *consequence* of the capability probe, not a hard-coded choice.

    `arbite scratch clear` (tic-95c0) and `arbite receipt`/`arbite changes` (tic-7c42) all
    exist, so the notes print the frozen guidance naming them and the two blocks are compared
    byte for byte. This asserts the probe's premise -- the commands really are there -- rather
    than re-reading the transcripts for sentences the blocks already carry."""
    assert cli.knows_command("scratch list") and cli.knows_command("scratch clear")
    assert cli.knows_command("receipt") and cli.knows_command("changes")

    # `doctor` rather than `--fix`: the scratch guidance belongs to the report that is only
    # *judging* the store, while the receipt sentence belongs to the repair run's detail (DR1
    # prints one, DR2 the other).
    judging = examples.run_cli(damaged_project, "doctor")
    repairing = examples.run_cli(damaged_project, "doctor", "--fix")

    assert "clear with 'arbite scratch clear --all'" in judging.stdout
    assert "arbite receipt" not in judging.stdout and "arbite changes" not in judging.stdout
    assert "inspect 'arbite receipt op-4f19' and 'arbite changes tic-1a75'" in repairing.stdout


def _sink_kind(project) -> str:
    """Which sink the fixture's project committed to, read from its own config."""
    return (project / ".arbite" / "project.yaml").read_text(encoding="utf-8").split(":", 1)[1].strip()
