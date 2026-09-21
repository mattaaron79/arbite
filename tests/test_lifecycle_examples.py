"""The frozen claim and attempt transcripts this slice owns: CL1-CL6 and LC5.

Each is asserted against its block in `.arbite/planning/interaction-examples.md` --
command, exit code and the stream the transcript belongs to -- with ids, times and
paths normalised on both sides (`examples.py`). `lifecycle_state.py` builds the state
each block starts from, so "the transcript passes" means the real command prints
exactly what the document says about a project in the state the document describes.

RC1 is not here: its block is two interleaved columns of elided output, so its *target*
("exactly one winner, the loser told why in one line, no partial state") is asserted
with real processes in `tests/test_coordination_lifecycle.py`, where a race can
actually be run.
"""

from __future__ import annotations

import json
import re

import pytest

import examples
import lifecycle_state as state
from arbite.coordination.store import open_coordination_store


def coordination(project, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return open_coordination_store(state.sink_for(project, sink_kind))


@pytest.fixture
def claimable_project(tmp_path):
    """CL1/CL3/LC5's world: `tic-cf9f` classified, ready, and (for CL3/LC5) claimed."""
    project = state.initialise(tmp_path)
    state.claimable(project)
    return project


@pytest.fixture
def claimed_project(claimable_project):
    """CL3/LC5's world: `tic-cf9f` claimed by `claude.opus.001`, with its attempt."""
    state.claimed_by_another(claimable_project)
    return claimable_project


# --- CL1 --------------------------------------------------------------------


def test_CL1_claim_creates_the_attempt(claimable_project):
    """One operation: the ticket exchange, the attempt, and the id a file command
    needs, printed because every later command presents it."""
    examples.assert_scenario(examples.scenario_block("CL1"), claimable_project)

    active = coordination(claimable_project).active_attempts("tic-cf9f")
    assert len(active) == 1, "the claim created exactly one attempt"
    assert active[0].worker_id == "claude.opus.001"
    assert active[0].generation == 1, "a fresh attempt starts at generation 1"
    assert active[0].started == active[0].last_activity, "creation is its first activity"
    assert active[0].ended is None


def test_CL1_the_json_payload_carries_the_attempt(claimable_project):
    """CL1 documents the JSON in a second block, so the fields a caller branches on
    cannot drift from the text that promises them.

    The block is marked "abridged" (it shows the added fields), so the facts it states
    -- the ticket's own fields, the attempt's, and the next action -- are what is
    asserted, not the whole payload."""
    documented = examples.scenario_json_blocks("CL1")
    assert documented, "CL1 must document the added JSON fields in a second block"

    proc = examples.run_cli(
        claimable_project, "claim", "tic-cf9f", "--agent", "claude.opus.001", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    payload = examples.normalise_payload(json.loads(proc.stdout), claimable_project)
    expected = examples.normalise_payload(documented[0], claimable_project)

    for key in ("id", "status", "assignee", "path"):
        assert payload[key] == expected[key], key
    assert set(expected["attempt"]) <= set(payload["attempt"])
    for key, value in expected["attempt"].items():
        assert payload["attempt"][key] == value, key
    assert payload["next_actions"] == expected["next_actions"]


# --- CL2 --------------------------------------------------------------------


def test_CL2_claim_with_an_unmet_dependency(tmp_path):
    """Readiness is checked by the acquisition itself, with the chain named and the
    commands that follow from it."""
    project = state.initialise(tmp_path)
    dependent, prerequisite = state.dependent_with_open_prerequisite(project)

    examples.assert_scenario(examples.scenario_block("CL2"), project)

    store = coordination(project)
    assert store.active_attempts(dependent) == [], "a refused claim creates no attempt"
    ticket = state.sink_for(project).get(dependent)
    assert (ticket.status, ticket.assignee) == ("open", None), "and changes no field"


# --- CL3 --------------------------------------------------------------------


def test_CL3_claim_loses_a_race(claimed_project):
    """The refusal is the compare-and-swap text (the sink's own wording) plus the
    holder's attempt, which is the fact the message used to lack."""
    examples.assert_scenario(examples.scenario_block("CL3"), claimed_project)

    active = coordination(claimed_project).active_attempts("tic-cf9f")
    assert len(active) == 1 and active[0].worker_id == "claude.opus.001"
    ticket = state.sink_for(claimed_project).get("tic-cf9f")
    assert ticket.assignee == "claude.opus.001", "the winner's claim is untouched"


# --- CL4 --------------------------------------------------------------------


def test_CL4_list_next_when_nothing_is_ready(tmp_path):
    """Nothing is ready, and the answer says *why* instead of leaving a caller to
    poll a queue that cannot produce work."""
    project = state.initialise(tmp_path)
    state.blocked_epic(project)

    examples.assert_scenario(examples.scenario_block("CL4"), project)


# --- CL5 --------------------------------------------------------------------


def test_CL5_batch_claim_partially_satisfied(tmp_path):
    """The batch note still holds, each claimed row has its own attempt, and the one
    commentary line goes to stderr so a reader of the table is not disturbed."""
    project = state.initialise(tmp_path)
    first, second = state.two_workable_in_order(project)

    examples.assert_scenario(examples.scenario_block("CL5"), project)

    active = coordination(project).active_attempts()
    assert sorted(attempt.ticket_id for attempt in active) == sorted([first, second])
    assert all(attempt.worker_id == "claude.sonnet.002" for attempt in active)


# --- CL6 --------------------------------------------------------------------


def test_CL6_adopt_legacy_in_progress_work(tmp_path):
    """A legacy ticket gets an attempt now, and the receipt refuses to imply anything
    about the activity that happened before."""
    project = state.initialise(tmp_path)
    state.legacy_in_progress(project)

    examples.assert_scenario(examples.scenario_block("CL6"), project)

    active = coordination(project).active_attempts("tic-e9ed")
    assert len(active) == 1 and active[0].worker_id == "claude.opus.001"
    assert active[0].generation == 1


# --- LC5 --------------------------------------------------------------------


def test_LC5_setters_cannot_bypass_the_lifecycle(claimed_project):
    """`set status` cannot close claimed work: the attempt and its claims have to end
    with the ticket, and only `arbite close` does that."""
    examples.assert_scenario(examples.scenario_block("LC5"), claimed_project)

    ticket = state.sink_for(claimed_project).get("tic-cf9f")
    assert ticket.status == "in_progress", "the refusal changed nothing"
    assert coordination(claimed_project).active_attempts("tic-cf9f"), "and ended nothing"


# --- the harness itself -----------------------------------------------------


def test_the_harness_reads_a_refusal_as_stderr():
    """A block that starts with an outcome label is what the CLI prints on stderr, so
    the harness compares it there and requires stdout to be empty."""
    for scenario_id in ("CL2", "CL3", "LC5"):
        assert examples.scenario_block(scenario_id).on_stderr, scenario_id
    assert not examples.scenario_block("CL1").on_stderr


def test_the_harness_reads_an_annotated_note_as_stderr():
    """CL5 says "note on stderr": the note leaves the body and is asserted against the
    stream it names, and the exit code still comes from the same line."""
    scenario = examples.scenario_block("CL5")

    assert scenario.exit_code == 0
    assert scenario.stderr.startswith("note: asked for 3 ticket(s), claimed 2")
    assert "note:" not in scenario.stdout
    assert re.search(r"in_progress\s+1\s+high\s+io\s+" + state.EPIC, scenario.stdout)
