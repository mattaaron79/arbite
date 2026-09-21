"""The frozen claim, attempt and cascade transcripts this slice owns: CL1-CL7, LC1-LC5.

Each is asserted against its block in `.arbite/planning/interaction-examples.md` --
command, exit code and the stream the transcript belongs to -- with ids, times and
paths normalised on both sides (`examples.py`). `lifecycle_state.py` and
`cascade_state.py` build the state each block starts from, so "the transcript passes"
means the real command prints exactly what the document says about a project in the
state the document describes.

RC1 is not here: its block is two interleaved columns of elided output, so its *target*
("exactly one winner, the loser told why in one line, no partial state") is asserted
with real processes in `tests/test_coordination_lifecycle.py`, where a race can
actually be run. The cascade's own multi-process evidence -- a close racing a write, a
write under a token an ended attempt minted -- lives there too.
"""

from __future__ import annotations

import json
import re

import pytest

import cascade_state as cascade
import examples
import lifecycle_state as state
import writes_state
from arbite.coordination import lifecycle as lifecycle_module
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


# --- CL7, LC1-LC4: the cascade through file ownership ------------------------


@pytest.fixture
def cascade_project(tmp_path):
    """CL7/LC1/LC3's world: `tic-cf9f` held by `att-91bd`, which has written one of its
    two claimed paths five times -- so one path is modified at closure and one is not."""
    return cascade.holder_world(tmp_path)


def test_CL7_forced_takeover(cascade_project):
    """CL7's target: the takeover states the generation it revoked *and* that partial
    bytes remain, releases the paths the old attempt held in one commit with it, and
    leaves `doctor` with no orphaned claim to report -- which is the consequence C04
    flagged and this ticket fixes."""
    before = (cascade_project / cascade.SCHEMA_PY).read_bytes()
    stale = writes_state.read_token(
        cascade_project, cascade.SCHEMA_PY, cascade.HOLDER_TICKET, cascade.HOLDER
    )

    examples.assert_scenario(examples.scenario_block("CL7"), cascade_project)

    store = coordination(cascade_project)
    active = store.active_attempts(cascade.HOLDER_TICKET)
    assert len(active) == 1, "exactly one attempt owns the ticket, and it is the new one"
    assert active[0].worker_id == "claude.haiku.003"
    assert active[0].id != cascade.HOLDER

    revoked = store.get_attempt(cascade.HOLDER)
    assert (revoked.state, revoked.outcome) == ("interrupted", "taken_over")
    assert "user reassigned" in revoked.handoff, "the reason is kept with the attempt"

    released = {claim.path: claim for claim in store.records("claim")}
    assert set(released) == {cascade.SCHEMA_PY, cascade.BASE_PY}
    for claim in released.values():
        assert claim.state == "released" and claim.released is not None
        assert cascade.HOLDER in (claim.release_reason or ""), "the release names the attempt"
    assert store.active_claims() == [], "no path stays reserved by a revoked attempt"

    assert (cascade_project / cascade.SCHEMA_PY).read_bytes() == before, "nothing was undone"

    doctor = examples.run_cli(cascade_project, "doctor", "--json")
    assert doctor.returncode == 0, doctor.stdout
    assert json.loads(doctor.stdout)["problems"] == [], "no orphaned_claim is left behind"

    # The revoked generation cannot write: the token outlives the claim it was taken
    # under, and the attempt it names is no longer current, so no bytes change.
    writes_state.staged(
        cascade_project, cascade.PAYLOAD, cascade.schema_text(cascade.RECEIPTS + 1)
    )
    refused = examples.run_cli(
        cascade_project,
        "file",
        "write",
        cascade.SCHEMA_PY,
        "--ticket",
        cascade.HOLDER_TICKET,
        "--attempt",
        cascade.HOLDER,
        "--read-token",
        stale,
        "--input",
        cascade.PAYLOAD,
    )
    assert refused.returncode == 5, refused.stdout + refused.stderr
    assert "no longer current" in refused.stderr and "no bytes were changed" in refused.stderr
    assert (cascade_project / cascade.SCHEMA_PY).read_bytes() == before


def test_LC1_close_releases_claims(cascade_project):
    """LC1's target: close ends the work with the ticket. Every active claim of the
    attempt is released, the attempt is finished, and the receipt manifest stays -- so an
    observer holding a token from before the close cannot mutate anything afterwards."""
    before = (cascade_project / cascade.SCHEMA_PY).read_bytes()
    stale = writes_state.read_token(
        cascade_project, cascade.SCHEMA_PY, cascade.HOLDER_TICKET, cascade.HOLDER
    )

    examples.assert_scenario(examples.scenario_block("LC1"), cascade_project)

    store = coordination(cascade_project)
    assert store.active_attempts(cascade.HOLDER_TICKET) == []
    ended = store.get_attempt(cascade.HOLDER)
    assert (ended.state, ended.outcome) == ("finished", "closed")
    assert store.active_claims() == []
    released = {claim.path: claim for claim in store.records("claim")}
    assert set(released) == {cascade.SCHEMA_PY, cascade.BASE_PY}
    for claim in released.values():
        assert claim.state == "released" and claim.released is not None

    kinds = [event.kind for event in store.events()]
    assert kinds.count(lifecycle_module.RELEASE_FILE) == 2, "one release event per path"
    assert kinds.count(lifecycle_module.ATTEMPT_ENDED) == 1

    # Evidence is retained, not deleted: the receipts the closure counted are there, and
    # so are the bytes they reference.
    assert cascade.retained_receipts(cascade_project, cascade.HOLDER_TICKET) == cascade.RECEIPTS
    before_versions, after_versions = writes_state.recorded_versions(cascade_project)
    for digest in {**before_versions, **after_versions}.values():
        assert store.get_artifact_bytes(digest) is not None, digest

    assert json.loads(
        examples.run_cli(cascade_project, "show", cascade.HOLDER_TICKET, "--json").stdout
    )["status"] == "closed"

    # The close's guarantee, checked rather than assumed: a write under the attempt's own
    # token is refused, and it changes no bytes (WR5's transcript, after a real cascade).
    writes_state.staged(
        cascade_project, cascade.PAYLOAD, cascade.schema_text(cascade.RECEIPTS + 1)
    )
    refused = examples.run_cli(
        cascade_project,
        "file",
        "write",
        cascade.SCHEMA_PY,
        "--ticket",
        cascade.HOLDER_TICKET,
        "--attempt",
        cascade.HOLDER,
        "--read-token",
        stale,
        "--input",
        cascade.PAYLOAD,
    )
    assert refused.returncode == 5, refused.stdout + refused.stderr
    assert "no longer current" in refused.stderr and "no bytes were changed" in refused.stderr
    assert (cascade_project / cascade.SCHEMA_PY).read_bytes() == before


def test_LC2_block_ends_the_attempt_and_keeps_partial_work(tmp_path):
    """LC2's target: blocking stops the *work*. The attempt is interrupted, its one claim
    is released, and the bytes it was working on stay exactly where they are for the next
    worker -- who is named in the `next:` line, because the ticket keeps its assignee."""
    project = cascade.rival_world(tmp_path)

    examples.assert_scenario(examples.scenario_block("LC2"), project)

    store = coordination(project)
    assert store.active_attempts(cascade.RIVAL_TICKET) == []
    ended = store.get_attempt(cascade.RIVAL)
    assert (ended.state, ended.outcome) == ("interrupted", "blocked")
    assert ended.handoff == "waiting on tic-cf9f to close"
    assert store.active_claims() == []

    ticket = state.sink_for(project).get(cascade.RIVAL_TICKET)
    assert (ticket.status, ticket.assignee) == ("blocked", cascade.RIVAL_WORKER)
    assert (project / cascade.FILE_PY).exists(), "the partial work is left in place"

    doctor = examples.run_cli(project, "doctor", "--json")
    assert doctor.returncode == 0, doctor.stdout
    assert json.loads(doctor.stdout)["problems"] == []


def test_LC3_delete_refused_while_claims_are_live(cascade_project):
    """LC3's target: deleting a ticket that still owns work is refused rather than
    cascading the history away -- and the refusal changes nothing, which is why the hint
    offers the two honest orders (hand the work back first, or close and keep it)."""
    examples.assert_scenario(examples.scenario_block("LC3"), cascade_project)

    store = coordination(cascade_project)
    assert store.active_claims(), "the refusal released nothing"
    assert store.active_attempts(cascade.HOLDER_TICKET), "and ended nothing"
    assert state.sink_for(cascade_project).get(cascade.HOLDER_TICKET).status == "in_progress"

    # The other order the hint offers works: close (which releases the claims), then
    # delete -- so the guard is a guard, not a refusal to ever delete this ticket. The
    # delete removes the ticket, and the coordination history stays where it is: the
    # change history is not cascaded away with the document.
    closed = examples.run_cli(cascade_project, "close", cascade.HOLDER_TICKET)
    assert closed.returncode == 0, closed.stderr
    deleted = examples.run_cli(
        cascade_project, "delete", cascade.HOLDER_TICKET, "--force", "--agent", "claude.haiku.003"
    )
    assert deleted.returncode == 0, deleted.stderr
    kept = coordination(cascade_project)
    assert {claim.state for claim in kept.records("claim")} == {"released"}
    assert cascade.retained_receipts(cascade_project, cascade.HOLDER_TICKET) == cascade.RECEIPTS


def test_LC4_reopen_does_not_resurrect_claims(tmp_path):
    """LC4's target: a reopen revives nothing. The claims the close released stay
    released, the token minted before the close stays dead, and the next claim starts a
    new attempt whose fresh claim is a new generation -- a worker re-reads, never resumes."""
    project = cascade.holder_world(tmp_path)
    stale = writes_state.read_token(project, cascade.SCHEMA_PY, cascade.HOLDER_TICKET, cascade.HOLDER)
    before = (project / cascade.SCHEMA_PY).read_bytes()
    closed = examples.run_cli(project, "close", cascade.HOLDER_TICKET)
    assert closed.returncode == 0, closed.stderr

    examples.assert_scenario(examples.scenario_block("LC4"), project)

    store = coordination(project)
    assert store.active_attempts(cascade.HOLDER_TICKET) == []
    assert store.active_claims() == [], "no claim is resurrected"
    generations = {claim.generation for claim in store.records("claim")}
    assert generations == {1}, "the released claims keep the generation they had"

    # The hint LC4 prints is runnable, and it starts a new attempt ...
    claimed = examples.run_cli(
        project, "claim", cascade.HOLDER_TICKET, "--agent", "claude.haiku.003"
    )
    assert claimed.returncode == 0, claimed.stderr
    attempt = coordination(project).active_attempts(cascade.HOLDER_TICKET)[0]
    assert attempt.id != cascade.HOLDER

    # ... and the path it claims is recorded as *that* attempt's, which is what makes the
    # old token dead rather than merely unused: a token authorises a write only against
    # the claim of the attempt that took it, and this claim now names another one.
    acquired = examples.run_cli(
        project,
        "file",
        "claim",
        cascade.SCHEMA_PY,
        "--ticket",
        cascade.HOLDER_TICKET,
        "--attempt",
        attempt.id,
    )
    assert acquired.returncode == 0, acquired.stderr
    fresh = coordination(project).claims_for_path(cascade.SCHEMA_PY)[0]
    assert fresh.attempt_id == attempt.id
    assert (fresh.state, fresh.released) == ("active", None), "a live claim, not the old record"

    writes_state.staged(project, cascade.PAYLOAD, cascade.schema_text(cascade.RECEIPTS + 1))
    refused = examples.run_cli(
        project,
        "file",
        "write",
        cascade.SCHEMA_PY,
        "--ticket",
        cascade.HOLDER_TICKET,
        "--attempt",
        cascade.HOLDER,
        "--read-token",
        stale,
        "--input",
        cascade.PAYLOAD,
    )
    assert refused.returncode == 5, refused.stdout + refused.stderr
    assert "no bytes were changed" in refused.stderr
    assert (project / cascade.SCHEMA_PY).read_bytes() == before


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
