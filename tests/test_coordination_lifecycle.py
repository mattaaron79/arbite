"""The rules behind the claim transcripts: what a refusal leaves, and who wins a race.

The frozen transcripts are asserted in `test_lifecycle_examples.py`. What is here is the
behaviour a transcript cannot show: whether a refused acquisition wrote *anything*,
what a takeover does to the attempt it replaces, that a revoked generation stays
revoked, and -- with real processes -- that two claims for one ticket produce one winner
(RC1's target) and that a claim racing a reopened prerequisite always leaves a record of
the conflict.

Two storage domains and no shared lock is the constraint the race tests are shaped by:
the ticket store's compare-and-swap decides a *claim* race, and the coordination store's
commit decides which of a claim and a reopen saw the other. So every assertion here is
about state that must hold whatever the interleaving -- "exactly one winner", "one
active attempt", "the later commit recorded the other's effect" -- rather than about a
particular schedule.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import examples
import lifecycle_state as state
from arbite.coordination import lifecycle as lifecycle_module
from arbite.coordination.app import CoordinationApp
from arbite.coordination.lifecycle import TicketLifecycle
from arbite.coordination.store import open_coordination_store
from arbite.errors import Busy, StaleGeneration

SINK_KINDS = ("file", "sqlite")
WORKER = Path(__file__).resolve().parent / "coordination_worker.py"
REPO_SRC = Path(__file__).resolve().parents[1] / "src"


def lifecycle_for(project, kind: str = "file"):
    """The application layer's lifecycle operations, as the CLI assembles them."""
    sink = state.sink_for(project, kind)
    app = CoordinationApp.open(sink, project, project / ".arbite", store_source="test")
    return sink, TicketLifecycle(sink, app)


def store_for(project, kind: str = "file"):
    return open_coordination_store(state.sink_for(project, kind))


def events_of(store, kind: str) -> list:
    return [event for event in store.events() if event.kind == kind]


def run_worker(*args, timeout: float = 90.0):
    """One worker process, run against this checkout exactly as a second agent would."""
    environment = dict(os.environ, PYTHONPATH=str(REPO_SRC))
    environment.pop("ARBITE_SINK", None)
    return subprocess.run(
        [sys.executable, str(WORKER), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def claim_crash(project, ticket: str, agent: str, boundary: str, kind: str = "file"):
    """Run the real claim and kill the process at a commit boundary.

    `commit_staged` is after the unit's journal is written and before anything is
    applied; `commit_applied` is after every document is written and before the journal
    is cleared. A process dying there is the only honest way to check that an attempt
    and the events describing it are one unit rather than three writes."""
    return subprocess.run(
        [
            sys.executable, str(WORKER), "claim-crash", str(project), kind,
            ticket, agent, boundary,
        ],
        cwd=str(project),
        env=dict(os.environ, PYTHONPATH=str(REPO_SRC)),
        capture_output=True,
        text=True,
        timeout=90,
    )


def race_claims(project, ticket: str, agents, kind: str = "file"):
    """Run `arbite claim` in several processes at once, released by a starting gun.

    The gun is a file the workers wait for: it is what makes the race a race, because
    four processes that start whenever the parent schedules them may never overlap."""
    gun = project / "go"
    workers = [
        subprocess.Popen(
            [sys.executable, str(WORKER), "claim", str(project), kind, ticket, agent, str(gun)],
            cwd=str(project),
            env=dict(os.environ, PYTHONPATH=str(REPO_SRC)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for agent in agents
    ]
    time.sleep(0.4)  # let every worker reach the gun before firing it
    gun.write_text("go", encoding="utf-8")
    return [worker.communicate(timeout=90) + (worker.returncode,) for worker in workers]


# --- what a claim records ---------------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_claim_records_one_attempt_and_its_events(tmp_path, kind):
    """One operation, one unit: the attempt and the two events that describe it.

    The events are what `arbite events` prints and what a later poll resumes from, so
    they are written with the attempt rather than after it."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)

    proc = examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001", sink=kind)
    assert proc.returncode == 0, proc.stderr

    store = store_for(project, kind)
    active = store.active_attempts("tic-cf9f")
    assert len(active) == 1
    assert (active[0].generation, active[0].state) == (1, "active")
    assert active[0].ended is None and active[0].last_activity == active[0].started
    assert store.info().attempts_active == 1

    kinds = [event.kind for event in store.events()]
    assert kinds == [lifecycle_module.ATTEMPT_STARTED, lifecycle_module.TICKET_CLAIMED]
    cursors = [event.cursor for event in store.events()]
    assert cursors == list(range(1, len(cursors) + 1)), "they committed as one unit"
    assert events_of(store, lifecycle_module.TICKET_CLAIMED)[0].actor == "claude.opus.001"
    assert events_of(store, lifecycle_module.TICKET_CLAIMED)[0].attempt_id == active[0].id


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_refused_claim_writes_nothing_at_all(tmp_path, kind):
    """Readiness, classification and the one-active-attempt rule are guards, not
    filters: a refused acquisition leaves no attempt, no event and no ticket change."""
    project = state.initialise(tmp_path, kind)
    dependent, prerequisite = state.dependent_with_open_prerequisite(project, sink_kind=kind)
    before = store_for(project, kind)
    attempts, events = len(before.records("attempt")), len(before.events())

    proc = examples.run_cli(project, "claim", dependent, "--agent", "claude.opus.001", sink=kind)
    assert proc.returncode == 1
    assert "is not ready" in proc.stderr

    store = store_for(project, kind)
    assert store.active_attempts(dependent) == [], "no attempt for the ticket that lost"
    assert (len(store.records("attempt")), len(store.events())) == (attempts, events)
    ticket = state.sink_for(project, kind).get(dependent)
    assert (ticket.status, ticket.assignee) == ("open", None)

    # The prerequisite's own attempt is the one the fixture's claim made, and it is
    # untouched: a refused claim cannot end somebody else's work either.
    assert len(store.active_attempts(prerequisite)) == 1


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_claim_on_unclassified_work_is_refused(tmp_path, kind):
    """Placeholder classification is a claim guard, not just a queue filter: work
    nobody has described cannot become an attempt."""
    project = state.initialise(tmp_path, kind)
    state.put(
        project,
        "tic-cf9f",
        kind,
        title="bug (raw): Requires Classification",
        status="open",
        tier="TODO: tier",
        domain="TODO: domain",
        epic=state.EPIC,
    )

    proc = examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001", sink=kind)
    assert proc.returncode == 1
    assert "not classified yet" in proc.stderr
    assert "arbite promote tic-cf9f" in proc.stderr, "the refusal names the command that classifies it"
    assert store_for(project, kind).records("attempt") == []


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_an_open_ticket_with_an_active_attempt_is_busy_rather_than_claimable(tmp_path, kind):
    """The stranded state a half-applied acquisition leaves: claimable-looking, but not
    claimable. Outcome 4, nothing written, and `list next` does not offer it."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, "tic-cf9f", kind)
    state.claimable(project, "tic-9b57", kind)
    _, lifecycle = lifecycle_for(project, kind)
    store = store_for(project, kind)
    workspace = store.get_workspace().id
    store.put_record(
        lifecycle_module.WorkAttempt(
            id="att-beef",
            ticket_id="tic-cf9f",
            worker_id="claude.opus.001",
            workspace_id=workspace,
            generation=1,
            state="active",
            started=lifecycle_module.utc_now(),
            last_activity=lifecycle_module.utc_now(),
        )
    )

    direct = examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.haiku.003", sink=kind)
    assert direct.returncode == 4, direct.stderr
    assert "already has an active attempt att-beef" in direct.stderr
    assert "nothing" not in direct.stdout

    offered = examples.run_cli(
        project, "list", "next", "--claim", "claude.sonnet.002", "--json", sink=kind
    )
    assert offered.returncode == 0, offered.stderr
    assert [row["id"] for row in json.loads(offered.stdout)] == ["tic-9b57"]
    assert len(store.active_attempts("tic-cf9f")) == 1, "and nothing was written"


def test_an_attempt_and_its_events_commit_together_or_not_at_all(tmp_path):
    """The unit of work, proved by killing the process that was writing it.

    A claim that dies while its commit is staged leaves no attempt and no event; the
    next write to the store finishes the unit and both appear together, with the
    cursors it had reserved. A claim that dies after the documents were applied leaves
    them there, and the replay must not duplicate them.

    The file backend is the one under test here: it is the backend whose commit is a
    journal rather than a database transaction, so it is the one where "recover" is
    something a later operation has to *do*."""
    for boundary, applied in (("commit_staged", False), ("commit_applied", True)):
        root = tmp_path / boundary
        root.mkdir()
        project = state.initialise(root)
        state.claimable(project)

        proc = claim_crash(project, "tic-cf9f", "claude.opus.001", boundary)
        assert proc.returncode == 9, (proc.returncode, proc.stdout, proc.stderr)

        store = store_for(project)
        assert [problem.kind for problem in store.record_problems()] == ["pending_commit"]
        if not applied:
            assert store.records("attempt") == [], "nothing was applied yet"
            assert store.events() == []

        # The next write to the store finishes the interrupted unit. An event with no
        # other purpose is enough: the replay happens at the head of every write.
        with store.transaction() as txn:
            txn.append_event("recovery.replay_trigger", "recovery", subject="test", result="poke")

        store = store_for(project)
        assert store.record_problems() == [], "the journal is gone and the unit is whole"
        active = store.active_attempts("tic-cf9f")
        assert len(active) == 1, "the attempt is there exactly once"
        kinds = [event.kind for event in store.events()]
        assert kinds.count(lifecycle_module.ATTEMPT_STARTED) == 1
        assert kinds.count(lifecycle_module.TICKET_CLAIMED) == 1
        assert all(event.attempt_id is not None for event in store.events()[:2])


# --- a busy store is a refusal, not a wait ---------------------------------


def test_a_claim_refuses_promptly_when_the_store_holds_the_lock(tmp_path, monkeypatch):
    """A caller that arrives while a commit is in flight gets outcome 4 and a word for
    it, in well under the lock timeout, with nothing written.

    This is the "one-shot commands return promptly" rule at its sharpest point: the
    claim has already exchanged the ticket, so a wait here would be a wait with work
    half done."""
    from arbite.coordination import file_backend

    monkeypatch.setattr(file_backend, "LOCK_TIMEOUT", 0.3)
    project = state.initialise(tmp_path)
    state.claimable(project)
    sink, lifecycle = lifecycle_for(project)
    ticket = sink.get("tic-cf9f")

    holder = store_for(project)
    started = time.monotonic()
    with holder._exclusive():
        with pytest.raises(Busy) as failure:
            lifecycle.claim(ticket, "claude.opus.001")
    elapsed = time.monotonic() - started

    assert failure.value.reason == "store_locked"
    assert elapsed < file_backend.LOCK_TIMEOUT + 1.0, f"refused in {elapsed:.2f}s"
    store = store_for(project)
    assert store.records("attempt") == []
    assert store.events() == []
    # The ticket half of the claim had already landed, which is exactly why the refusal
    # names the store rather than pretending nothing happened: the caller adopts or
    # re-runs, and either way it is told.
    assert sink.get("tic-cf9f").assignee == "claude.opus.001"
    assert "nothing was written" in str(failure.value)


# --- a batch claim is one acquisition per row -------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_batch_claim_records_one_attempt_per_row(tmp_path, kind):
    """`list next --claim --count N` is N acquisitions, not one bulk move: every row
    gets its own attempt, and the payload a dispatcher reads carries it."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, "tic-cf9f", kind)
    state.claimable(project, "tic-9b57", kind)

    proc = examples.run_cli(
        project,
        "list",
        "next",
        "--claim",
        "claude.sonnet.002",
        "--count",
        "3",
        "--tier",
        "high",
        "--json",
        sink=kind,
    )

    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)
    assert sorted(row["id"] for row in rows) == ["tic-9b57", "tic-cf9f"]
    store = store_for(project, kind)
    active = store.active_attempts()
    assert sorted(attempt.ticket_id for attempt in active) == ["tic-9b57", "tic-cf9f"]
    assert all(attempt.generation == 1 for attempt in active)
    for row in rows:
        assert row["attempt"]["id"] in {attempt.id for attempt in active}
        assert row["attempt"]["state"] == "active"
    assert len(events_of(store, lifecycle_module.ATTEMPT_STARTED)) == 2


# --- takeover and generations ----------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_takeover_revokes_the_old_attempt_and_starts_another(tmp_path, kind):
    """`--force --reason` is the administrative override: the previous attempt is
    interrupted with its reason kept, a revocation event names its generation, and the
    new worker gets a new attempt of their own."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)
    assert (
        examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )
    store = store_for(project, kind)
    first = store.active_attempts("tic-cf9f")[0]

    proc = examples.run_cli(
        project,
        "claim",
        "tic-cf9f",
        "--agent",
        "claude.haiku.003",
        "--force",
        "--reason",
        "original worker stopped; user reassigned",
        sink=kind,
    )
    assert proc.returncode == 0, proc.stderr
    assert "revoked: attempt" in proc.stdout and "new attempt:" in proc.stdout

    store = store_for(project, kind)
    revoked = store.get_attempt(first.id)
    assert revoked.state == "interrupted"
    assert revoked.outcome == "taken_over"
    assert revoked.handoff == "original worker stopped; user reassigned"
    assert revoked.ended is not None and revoked.last_activity == revoked.ended

    active = store.active_attempts("tic-cf9f")
    assert len(active) == 1 and active[0].id != first.id
    assert active[0].worker_id == "claude.haiku.003" and active[0].generation == 1
    assert state.sink_for(project, kind).get("tic-cf9f").assignee == "claude.haiku.003"

    revocation = events_of(store, lifecycle_module.ATTEMPT_REVOKED)
    assert len(revocation) == 1
    assert revocation[0].attempt_id == first.id
    assert revocation[0].payload["generation"] == first.generation
    assert revocation[0].payload["reason"] == "original worker stopped; user reassigned"


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_revoked_generation_is_refused_and_a_current_one_is_not(tmp_path, kind):
    """The guard a later file operation presents: an attempt that was revoked, or a
    generation that moved, is outcome 5 -- re-read, do not retry the same token."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)
    sink, lifecycle = lifecycle_for(project, kind)
    assert (
        examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )
    old = store_for(project, kind).active_attempts("tic-cf9f")[0]
    assert (
        examples.run_cli(
            project,
            "claim",
            "tic-cf9f",
            "--agent",
            "claude.haiku.003",
            "--force",
            "--reason",
            "worker gone",
            sink=kind,
        ).returncode
        == 0
    )
    current = store_for(project, kind).active_attempts("tic-cf9f")[0]

    assert lifecycle.require_attempt("tic-cf9f", current.id, current.generation).id == current.id

    with pytest.raises(StaleGeneration) as failure:
        lifecycle.require_attempt("tic-cf9f", old.id)
    assert failure.value.reason == "stale_generation"
    assert "no longer current" in str(failure.value)

    with pytest.raises(StaleGeneration):
        lifecycle.require_attempt("tic-cf9f", current.id, generation=current.generation + 1)
    with pytest.raises(StaleGeneration):
        lifecycle.require_attempt("tic-9b57", current.id)
    with pytest.raises(StaleGeneration):
        lifecycle.require_attempt("tic-cf9f", "att-0000")


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_ticket_whose_attempt_is_recorded_still_reports_its_attempt(tmp_path, kind):
    """`active_attempt` is the answer every caller starts from, and two active
    attempts is reported rather than resolved: picking one would be an ownership
    decision nobody made."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)
    sink, lifecycle = lifecycle_for(project, kind)
    ticket = sink.get("tic-cf9f")
    assert lifecycle.active_attempt("tic-cf9f") is None

    lifecycle.claim(ticket, "claude.opus.001")
    assert lifecycle.active_attempt("tic-cf9f").worker_id == "claude.opus.001"

    store = store_for(project, kind)
    store.put_record(
        lifecycle_module.WorkAttempt(
            id="att-dead",
            ticket_id="tic-cf9f",
            worker_id="claude.haiku.003",
            workspace_id=store.get_workspace().id,
            generation=1,
            state="active",
            started=lifecycle_module.utc_now(),
            last_activity=lifecycle_module.utc_now(),
        )
    )
    with pytest.raises(Exception) as failure:
        lifecycle.active_attempt("tic-cf9f")
    assert "active attempts" in str(failure.value)


# --- adopting legacy work ---------------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_adoption_records_an_attempt_and_refuses_a_second_one(tmp_path, kind):
    """Adoption is explicit and once: the legacy ticket gets an attempt starting now,
    and a ticket that already has one is busy rather than adopted twice."""
    project = state.initialise(tmp_path, kind)
    state.legacy_in_progress(project, sink_kind=kind)

    first = examples.run_cli(
        project, "attempt", "adopt", "tic-e9ed", "--agent", "claude.opus.001", sink=kind
    )
    assert first.returncode == 0, first.stderr
    assert "no prior activity is implied" in first.stdout

    store = store_for(project, kind)
    adopted = store.active_attempts("tic-e9ed")
    assert len(adopted) == 1 and adopted[0].generation == 1
    assert events_of(store, lifecycle_module.ATTEMPT_ADOPTED)
    assert not events_of(store, lifecycle_module.TICKET_CLAIMED), "adoption claims nothing"

    second = examples.run_cli(
        project, "attempt", "adopt", "tic-e9ed", "--agent", "claude.opus.001", sink=kind
    )
    assert second.returncode == 4
    assert adopted[0].id in second.stderr
    assert len(store.active_attempts("tic-e9ed")) == 1


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_adoption_refuses_an_attempt_for_a_different_worker(tmp_path, kind):
    """An adopted attempt has to name the worker the ticket already names: anything
    else is a takeover, and a takeover needs a reason."""
    project = state.initialise(tmp_path, kind)
    state.legacy_in_progress(project, sink_kind=kind)

    proc = examples.run_cli(
        project, "attempt", "adopt", "tic-e9ed", "--agent", "claude.haiku.003", sink=kind
    )

    assert proc.returncode == 1
    assert "assigned to claude.opus.001, not claude.haiku.003" in proc.stderr
    assert "arbite claim tic-e9ed --agent claude.haiku.003 --force --reason" in proc.stderr
    assert store_for(project, kind).records("attempt") == []


# --- ending an attempt through the lifecycle commands -----------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_the_lifecycle_commands_end_the_attempt_they_stop(tmp_path, kind):
    """Release, block, shelve and reopen each end the attempt that was working the
    ticket, with the state that tells the truth about why it stopped, and `unblock`
    starts a fresh one for the worker it resumes."""
    project = state.initialise(tmp_path, kind)
    sink, lifecycle = lifecycle_for(project, kind)

    for ticket_id, command, expected_state in (
        ("tic-cf9f", ("release", "--agent", "claude.opus.001"), "released"),
        ("tic-9b57", ("block", "--reason", "waiting on upstream"), "interrupted"),
        ("tic-f0a1", ("shelve", "--reason", "later"), "released"),
    ):
        state.claimable(project, ticket_id, kind)
        assert (
            examples.run_cli(project, "claim", ticket_id, "--agent", "claude.opus.001", sink=kind)
            .returncode
            == 0
        )
        before = store_for(project, kind).active_attempts(ticket_id)[0]
        proc = examples.run_cli(project, command[0], ticket_id, *command[1:], sink=kind)
        assert proc.returncode == 0, proc.stderr
        assert f"ended attempt {before.id}" in proc.stdout
        assert "partial work is left on disk" in proc.stdout

        ended = store_for(project, kind).get_attempt(before.id)
        assert (ended.state, ended.ended is not None) == (expected_state, True)
        assert store_for(project, kind).active_attempts(ticket_id) == []
        assert events_of(store_for(project, kind), lifecycle_module.ATTEMPT_ENDED)

    # Reopen ends the attempt it interrupts, and `ticket.reopened` records the change.
    state.claimable(project, "tic-b002", kind)
    assert (
        examples.run_cli(project, "claim", "tic-b002", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )
    stopped = store_for(project, kind).active_attempts("tic-b002")[0]
    reopened = examples.run_cli(
        project,
        "reopen",
        "tic-b002",
        "--agent",
        "claude.haiku.003",
        "--reason",
        "review found the lock window unguarded",
        sink=kind,
    )
    assert reopened.returncode == 0, reopened.stderr
    assert store_for(project, kind).get_attempt(stopped.id).outcome == "reopened"
    assert events_of(store_for(project, kind), lifecycle_module.TICKET_REOPENED)

    # Unblock resumes blocked work with a *new* attempt, for the worker it names.
    state.claimable(project, "tic-b003", kind)
    assert (
        examples.run_cli(project, "claim", "tic-b003", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )
    assert (
        examples.run_cli(project, "block", "tic-b003", "--reason", "waiting", sink=kind).returncode
        == 0
    )
    assert (
        examples.run_cli(project, "unblock", "tic-b003", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )
    resumed = store_for(project, kind).active_attempts("tic-b003")
    assert len(resumed) == 1 and resumed[0].generation == 1
    assert store_for(project, kind).info().attempts_active == 1

    # Nothing in any of that leaves an integrity finding: ended attempts are history,
    # not orphans, and the active ones name tickets that exist.
    doctor = examples.run_cli(project, "doctor", "--json", sink=kind)
    assert doctor.returncode == 0, doctor.stdout
    assert json.loads(doctor.stdout)["problems"] == []
    assert store_for(project, kind).record_problems() == []


# --- the generic setters ----------------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_the_generic_setters_route_to_the_lifecycle_or_refuse(tmp_path, kind):
    """`set` may edit a ticket, but not in a way that strands an attempt. Each refusal
    names the command that owns the transition, and everything else still works."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)
    assert (
        examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001", sink=kind)
        .returncode
        == 0
    )

    closed = examples.run_cli(project, "set", "tic-cf9f", "status", "closed", sink=kind)
    assert closed.returncode == 1
    assert "cannot close a ticket that has an active attempt" in closed.stderr
    assert "arbite close tic-cf9f" in closed.stderr

    direct = examples.run_cli(project, "set-status", "tic-cf9f", "closed", sink=kind)
    assert direct.returncode == 1
    assert "arbite close tic-cf9f" in direct.stderr, "both front doors refuse the same way"

    released = examples.run_cli(project, "set-status", "tic-cf9f", "open", sink=kind)
    assert released.returncode == 1
    assert "arbite release tic-cf9f --agent claude.opus.001" in released.stderr

    moved = examples.run_cli(project, "set", "tic-cf9f", "assignee", "claude.haiku.003", sink=kind)
    assert moved.returncode == 1
    assert "arbite claim tic-cf9f --agent claude.haiku.003 --force --reason" in moved.stderr

    cleared = examples.run_cli(project, "set", "tic-cf9f", "assignee", "", sink=kind)
    assert cleared.returncode == 1
    assert "arbite release tic-cf9f --agent claude.opus.001" in cleared.stderr
    assert "--force" not in cleared.stderr, "clearing the assignee is a hand-back, not a takeover"

    # Everything the attempt does not care about is still a plain edit.
    allowed = examples.run_cli(project, "set", "tic-cf9f", "priority", "3", "tags", "io", sink=kind)
    assert allowed.returncode == 0, allowed.stderr
    ticket = state.sink_for(project, kind).get("tic-cf9f")
    assert (ticket.priority, ticket.tags, ticket.status) == (3, ["io"], "in_progress")
    assert len(store_for(project, kind).active_attempts("tic-cf9f")) == 1


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_setter_against_an_unclaimed_ticket_is_untouched_by_the_guards(tmp_path, kind):
    """The guards only apply to a *running* attempt: a ticket nobody holds still
    changes status the way it always did."""
    project = state.initialise(tmp_path, kind)
    state.claimable(project, sink_kind=kind)

    assert (
        examples.run_cli(project, "set", "tic-cf9f", "status", "review", sink=kind).returncode == 0
    )
    assert state.sink_for(project, kind).get("tic-cf9f").status == "review"
    assert store_for(project, kind).records("attempt") == []


# --- promotion is an acquisition path too ----------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_promote_with_an_agent_records_the_attempt(tmp_path, kind):
    """Classifying and claiming in one write creates the attempt as well: otherwise the
    worker the ticket was just handed to would have no attempt to claim files with."""
    project = state.initialise(tmp_path, kind)
    raw = examples.run_cli(project, "raw", "bug", "the thing broke", sink=kind)
    ticket_id = re.search(r"tic-[0-9a-f]{4}", raw.stdout).group(0)

    proc = examples.run_cli(
        project,
        "promote",
        ticket_id,
        "--title",
        "Fix the broken thing",
        "--tier",
        "medium",
        "--domain",
        "io",
        "--agent",
        "claude.haiku.001",
        sink=kind,
    )
    assert proc.returncode == 0, proc.stderr
    assert "attempt: att-" in proc.stdout

    active = store_for(project, kind).active_attempts(ticket_id)
    assert len(active) == 1 and active[0].worker_id == "claude.haiku.001"


# --- RC1: two workers, one ticket, one winner -------------------------------


def test_RC1_two_workers_claiming_one_ticket_produce_one_winner(tmp_path):
    """RC1's target, with four real processes: exactly one winner, the losers told why
    in one line, and no partial state -- one attempt, one claimant, one claim event."""
    project = state.initialise(tmp_path)
    state.claimable(project)
    agents = ["claude.opus.001", "claude.haiku.003", "claude.sonnet.002", "claude.haiku.004"]

    results = race_claims(project, "tic-cf9f", agents)
    codes = sorted(result[2] for result in results)
    assert codes == [0, 1, 1, 1], [result[0] for result in results]

    winners = [agent for agent, result in zip(agents, results) if result[2] == 0]
    assert len(winners) == 1
    for agent, (stdout, stderr, code) in zip(agents, results):
        if code == 0:
            continue
        assert stdout == "", "a refusal prints nothing on stdout"
        if "is not in the expected state" in stderr:
            assert "attempt held by: att-" in stderr, stderr
            assert f"({winners[0]})" in stderr, "the loser is told which attempt holds it"
            assert f"'arbite list next --claim {agent}'" in stderr
        else:
            # A read that lands inside the winner's relocation sees a ticket that is
            # momentarily in neither folder; reads are not isolated (the file sink says
            # so out loud), so that answer is allowed here and nothing was written by
            # this process either way. The invariant the test is about is the winner.
            assert "no ticket found" in stderr, stderr

    store = store_for(project)
    active = store.active_attempts("tic-cf9f")
    assert len(active) == 1 and active[0].worker_id == winners[0]
    assert len(events_of(store, lifecycle_module.ATTEMPT_STARTED)) == 1
    ticket = state.sink_for(project).get("tic-cf9f")
    assert (ticket.status, ticket.assignee) == ("in_progress", winners[0])

    doctor = examples.run_cli(project, "doctor", "--json")
    assert doctor.returncode == 0, doctor.stdout
    assert json.loads(doctor.stdout)["problems"] == []


def test_RC1_the_claimed_ticket_is_never_offered_again(tmp_path):
    """The second worker's `list next --claim` moves on rather than handing out the
    ticket somebody is already working: a ticket with an active attempt is not
    workable work."""
    project = state.initialise(tmp_path)
    state.claimable(project, "tic-cf9f")
    state.claimable(project, "tic-9b57")
    assert (
        examples.run_cli(project, "claim", "tic-cf9f", "--agent", "claude.opus.001").returncode == 0
    )

    offered = examples.run_cli(
        project, "list", "next", "--claim", "claude.sonnet.002", "--count", "5", "--json"
    )

    assert offered.returncode == 0, offered.stderr
    rows = json.loads(offered.stdout)
    assert [row["id"] for row in rows] == ["tic-9b57"]
    assert rows[0]["assignee"] == "claude.sonnet.002"
    assert len(store_for(project).active_attempts("tic-9b57")) == 1


# --- the claim-versus-reopen race -------------------------------------------


def _dependent_project(tmp_path, kind: str = "file"):
    """A closed prerequisite with a dependent that is being worked.

    Returns the project, the prerequisite id and the dependent id. The dependent's
    attempt is real (it is created by the CLI), because that is what the invalidation
    flag is about."""
    project = state.initialise(tmp_path, kind)
    state.put(project, "tic-cf9f", kind, title="prerequisite", status="closed", closed="2026-01-02T00:00:00", **state.epic_ticket())
    state.put(project, "tic-9b57", kind, title="dependent", depends_on=["tic-cf9f"], **state.epic_ticket())
    claimed = examples.run_cli(project, "claim", "tic-9b57", "--agent", "claude.opus.001", sink=kind)
    assert claimed.returncode == 0, claimed.stderr
    return project, "tic-cf9f", "tic-9b57"


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_reopening_a_prerequisite_flags_running_work_without_undoing_it(tmp_path, kind):
    """Order one: the claim landed first, so the reopen is the side that records the
    conflict -- an invalidation event naming the attempt, and the attempt stays exactly
    as it was, because only the worker can decide what to do with bytes on disk."""
    project, prerequisite, dependent = _dependent_project(tmp_path, kind)
    store = store_for(project, kind)
    attempt = store.active_attempts(dependent)[0]

    reopened = examples.run_cli(
        project,
        "reopen",
        prerequisite,
        "--agent",
        "claude.haiku.003",
        "--reason",
        "the lock window was unguarded",
        sink=kind,
    )

    assert reopened.returncode == 0, reopened.stderr
    assert f"invalidated: attempt {attempt.id} on {dependent} depends on {prerequisite}" in reopened.stdout
    store = store_for(project, kind)
    invalidations = events_of(store, lifecycle_module.ATTEMPT_INVALIDATED)
    assert len(invalidations) == 1
    assert (invalidations[0].attempt_id, invalidations[0].payload["dependency"]) == (
        attempt.id,
        prerequisite,
    )
    assert store.active_attempts(dependent)[0].id == attempt.id, "running work was not undone"
    assert store.record_problems() == []


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_claim_after_a_prerequisite_reopened_is_refused(tmp_path, kind):
    """Order two: the reopen landed first, so the claim is the side that records the
    conflict -- refused as not ready, with nothing written."""
    project = state.initialise(tmp_path, kind)
    state.put(project, "tic-cf9f", kind, title="prerequisite", status="closed", closed="2026-01-02T00:00:00", **state.epic_ticket())
    state.put(project, "tic-9b57", kind, title="dependent", depends_on=["tic-cf9f"], **state.epic_ticket())
    assert (
        examples.run_cli(
            project,
            "reopen",
            "tic-cf9f",
            "--agent",
            "claude.haiku.003",
            "--reason",
            "the lock window was unguarded",
            sink=kind,
        ).returncode
        == 0
    )

    proc = examples.run_cli(project, "claim", "tic-9b57", "--agent", "claude.opus.001", sink=kind)

    assert proc.returncode == 1
    assert "depends_on tic-cf9f is open (not closed)" in proc.stderr
    store = store_for(project, kind)
    assert store.active_attempts("tic-9b57") == []
    assert state.sink_for(project, kind).get("tic-9b57").status == "open"


def test_a_claim_racing_a_reopen_always_leaves_a_record(tmp_path):
    """The race itself, with two real processes and nothing orchestrating them.

    Neither store can be locked with the other, so each side verifies *after* it
    commits: the later of the two commits therefore sees the other's effect and records
    it as an invalidation event. Whatever the schedule, one of the two holds -- the
    claim refused as not ready, or the attempt carried an invalidation -- and that is
    what "a defined serial order" means for this race. Which one it is, is deliberately
    not asserted: a particular interleaving is not something a portable test can
    demand. The *deterministic* forms of both orders are the two tests above."""
    project = state.initialise(tmp_path)
    state.put(
        project,
        "tic-cf9f",
        title="prerequisite",
        status="closed",
        closed="2026-01-02T00:00:00",
        **state.epic_ticket(),
    )
    state.put(project, "tic-9b57", title="dependent", depends_on=["tic-cf9f"], **state.epic_ticket())

    gun = project / "go"
    environment = dict(os.environ, PYTHONPATH=str(REPO_SRC))
    claim = subprocess.Popen(
        [
            sys.executable, str(WORKER), "claim", str(project), "file",
            "tic-9b57", "claude.opus.001", str(gun),
        ],
        cwd=str(project),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reopen = subprocess.Popen(
        [
            sys.executable, "-m", "arbite.cli", "reopen", "tic-cf9f",
            "--agent", "claude.haiku.003", "--reason", "unguarded window",
        ],
        cwd=str(project),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.4)  # both commands are live before either is allowed to write
    gun.write_text("go", encoding="utf-8")
    _, claim_stderr, _ = (*claim.communicate(timeout=90), claim.returncode)
    _, _, _ = (*reopen.communicate(timeout=90), reopen.returncode)

    store = store_for(project)
    active = store.active_attempts("tic-9b57")
    invalidations = events_of(store, lifecycle_module.ATTEMPT_INVALIDATED)
    assert len(active) <= 1, "never two attempts for one ticket"
    assert store.record_problems() == []
    assert [event.kind for event in store.events()].count(lifecycle_module.TICKET_REOPENED) <= 1
    if claim.returncode == 0:
        assert len(active) == 1, "a successful claim owns exactly one attempt"
        assert invalidations, (
            "the claim and the reopen both committed, so the attempt has to be flagged "
            "by whichever of them landed second"
        )
        assert invalidations[0].attempt_id == active[0].id
        assert state.sink_for(project).get("tic-9b57").assignee == "claude.opus.001"
    else:
        assert "is not ready" in claim_stderr, claim_stderr
        assert active == [], "a refused claim leaves no attempt"
        assert state.sink_for(project).get("tic-9b57").status == "open"
