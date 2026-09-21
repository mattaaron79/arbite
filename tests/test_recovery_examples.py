"""The frozen transcripts this slice owns: DR1 and DR2 (and DR3, which is the scratch slice's).

Each is asserted against its block in `.arbite/planning/interaction-examples.md` -- command,
exit code, output -- with ids, times and paths normalised on both sides (`examples.py`), on
**both** sinks: the coordination findings are the same facts whichever store holds the records,
which is the property the two backends exist for. `recovery_state.py` builds the damaged store
the transcripts describe.

**Two documented deviations, and only two.** Both are sentences the frozen blocks print that
name a command another slice owns: `arbite scratch clear --all` (tic-95c0) and
`arbite receipt`/`arbite changes` (tic-7c42). Arbite may not name a command it does not have --
that is a capability claimed on paper only -- so the report prints an honest sentence without
the command until it exists, and the test accepts the frozen text the moment it can be printed
(`_either_frozen_or_deferred`). Everything else -- every problem line, the wrapping and its
indentation, the fixed/not-fixed markers, the counts, the exit code -- is asserted exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

import examples
import recovery_state as state
from arbite import cli
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

SINKS = ("file", "sqlite")

#: `(frozen, printed here)`: the sentences that name a command this build does not have yet.
#: The test accepts either form, so nothing has to be remembered when that slice lands.
DEFERRED = (
    (
        "note: .arbite/scratch/ holds 3 files (12.4 KiB) -- transport left behind, expected "
        "after an\n      interrupted run; clear with 'arbite scratch clear --all'",
        "note: .arbite/scratch/ holds 3 files (12.4 KiB) -- transport left behind, expected "
        "after an interrupted run",
    ),
    (
        "inspect 'arbite receipt op-4f19' and 'arbite changes tic-1a75'",
        "inspect the receipt op-4f19 and the ticket's recorded change history",
    ),
)


@pytest.fixture(params=SINKS)
def damaged_project(request, tmp_path):
    return state.damaged(tmp_path, request.param)


def _either_frozen_or_deferred(actual: str, frozen: str, project) -> None:
    """Assert the real output matches the transcript, allowing the documented deviations.

    The frozen text is tried first: once the slice that owns the missing command has landed,
    the report names it and the transcript compares byte for byte again -- the deviations
    disappear by themselves rather than by somebody remembering to delete them."""
    actual = examples.normalise(actual, project).strip("\n")
    if actual == examples.normalise(frozen, project).strip("\n"):
        return
    relaxed = frozen
    for frozen_text, deferred in DEFERRED:
        relaxed = relaxed.replace(frozen_text, deferred)
    assert actual == examples.normalise(relaxed, project).strip("\n"), (
        "DR1/DR2 differ from the frozen block by more than the two documented deviations"
    )


def _only_deferred(scenario):
    """The transcript with the deferred sentences replaced, for a byte-exact comparison of
    everything else (ids, times and paths normalised by the harness, as usual)."""
    text = scenario.stdout
    for frozen_text, deferred in DEFERRED:
        text = text.replace(frozen_text, deferred)
    assert text != scenario.stdout, f"{scenario.id} no longer contains a deferred sentence"
    return replace(scenario, stdout=text)


# --- DR1 ----------------------------------------------------------------------


def test_DR1_problems_found(damaged_project):
    """Three findings, each with the response it needs: a claim nobody can use, an unfinished
    operation whose bytes are neither version, and a claim naming an attempt this store does
    not have. The payload note rides along without changing the exit code."""
    scenario = examples.scenario_block("DR1")
    stdout = examples.assert_scenario(_only_deferred(scenario), damaged_project)

    # ...and the deferred sentence is the only reason it is not the frozen block verbatim.
    _either_frozen_or_deferred(stdout, scenario.stdout, damaged_project)
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
    saying why: which of two versions is "correct" is not a question arbite may answer."""
    scenario = examples.scenario_block("DR2")
    # One run only: `--fix` repairs, so a second invocation is a different state (asserted
    # separately below).
    stdout = examples.assert_scenario(_only_deferred(scenario), damaged_project)

    _either_frozen_or_deferred(stdout, scenario.stdout, damaged_project)
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
    """The rule the two deviations exist for, asserted directly rather than trusted: every
    `arbite <command>` a report names is a command the parser really defines.

    Guidance is only useful if the caller can run it; a hint naming a slice that has not
    landed yet sends an agent to a command that fails with `invalid choice`."""
    for args in (("doctor",), ("doctor", "--fix")):
        proc = examples.run_cli(damaged_project, *args)
        # Quoted, which is how this surface writes a runnable hint: prose that happens to say
        # "what arbite can correct automatically" is not naming a command.
        for named in re.findall(r"'arbite ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", proc.stdout):
            assert cli.knows_command(named.strip()), (
                f"'arbite {named}' is named by {' '.join(args)} but this arbite has no such "
                "command"
            )


def test_the_deferred_sentences_are_absent_because_their_commands_are(damaged_project):
    """The deviation is a *consequence* of the capability probe, not a hard-coded choice: with
    no `arbite scratch clear` and no `arbite receipt`, the report prints the honest shorter
    sentences; the moment those commands exist, the frozen ones come back by themselves."""
    assert not cli.knows_command("scratch clear"), "tic-95c0 has landed: update the deviations"
    assert not cli.knows_command("receipt"), "tic-7c42 has landed: update the deviations"
    assert not cli.knows_command("changes"), "tic-7c42 has landed: update the deviations"

    proc = examples.run_cli(damaged_project, "doctor", "--fix")

    assert "arbite scratch clear" not in proc.stdout
    assert "arbite receipt" not in proc.stdout and "arbite changes" not in proc.stdout


def _sink_kind(project) -> str:
    """Which sink the fixture's project committed to, read from its own config."""
    return (project / ".arbite" / "project.yaml").read_text(encoding="utf-8").split(":", 1)[1].strip()
