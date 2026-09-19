"""Automatic change receipts and net change views (planning key C10).

Every behavioural test runs against both sinks (the `sink` fixture), so "the file
sink and the SQLite sink answer `arbite changes` identically" is a checked claim.
The suite covers the four C10 acceptance criteria:

1. a known create/edit/remove/rename/binary sequence is reconstructed exactly
   from the stored artifacts, and an edit-then-revert keeps both operations while
   the net view reports no change;
2. ticket and attempt views are bounded, JSON-shaped and reference *verifiable*
   artifacts;
3. the mechanical evidence alone is a file-change manifest (no LLM summary);
4. external drift is labelled observed/unattributed, never assigned to the agent.
"""

from __future__ import annotations

import pytest

from arbite import (
    application,
    changes,
    coordination,
    fileclaims,
    filemutations,
    filereads,
    lifecycle,
    mutation,
    schema,
)
from arbite.application import Actor
from arbite.errors import CoordinationNotFound, DriftDetected
from helpers import make_ticket

ABSENT = coordination.ABSENT
BINARY = b"\x00\x01\x02\xff\xfe\x00binary\x00"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


class FaultOnce:
    """Raise `mutation.FaultInjected` the first time `phase` is reached."""

    def __init__(self, phase):
        self.phase = phase
        self.fired = False

    def __call__(self, phase):
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


class Project:
    def __init__(self, mutations, reads, claims, attempt, root, service, ctl, ticket):
        self.mutations = mutations
        self.reads = reads
        self.claims = claims
        self.attempt = attempt
        self.root = root
        self.service = service
        self.store = service.store
        self.ctl = ctl
        self.ticket = ticket

    def fresh_token(self, path):
        """Claim `path` for the current attempt and take its authorizing read."""
        return self.fresh_token_for(self.attempt, path)

    def fresh_token_for(self, attempt, path):
        """Claim `path` and take the fresh post-claim read that authorizes a write."""
        self.claims.claim(attempt, [path])
        receipt = self.reads.read(attempt, path)
        assert receipt.write_authorizing is True
        return receipt.read_token

    def fresh_ticket(self):
        """Re-read the ticket, so a note added after construction is visible."""
        return self.ctl.tickets.get(self.ticket.id)

    def view(self, **kwargs):
        ticket = kwargs.pop("ticket", None) or self.fresh_ticket()
        return changes.ChangesQuery(self.store, ticket=ticket).view(self.ticket.id, **kwargs)


def _project(
    sink,
    arbite_dir,
    *,
    files=(("src/a.py", "alpha\nbeta\ngamma\n"), ("src/dead.txt", "gone\n")),
    ticket="tic-a1b2",
    worker="claude.opus.001",
):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    for rel, text in files:
        (root / rel).write_text(text)
    sink.create(make_ticket(ticket))
    service = application.coordination_service_for(sink, root=str(root), actor=Actor(worker))
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get(ticket), worker_id=worker).attempt
    claims = fileclaims.FileClaimService(service)
    reads = fileread_service(service, claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)
    return Project(mutations, reads, claims, attempt, root, service, ctl, sink.get(ticket))


def fileread_service(service, claims):
    return filereads.FileReadService(service, claims=claims)


def _known_sequence(project: Project):
    """create (binary) -> edit -> remove -> rename, in that operation order."""
    project.claims.claim(project.attempt, ["src/created.bin"])
    project.mutations.write(project.attempt, "src/created.bin", BINARY)

    token = project.fresh_token("src/a.py")
    project.mutations.edit(
        project.attempt,
        "src/a.py",
        [filemutations.Edit(old="alpha", new="ALPHA")],
        read_token=token,
    )

    token = project.fresh_token("src/dead.txt")
    project.mutations.remove(project.attempt, "src/dead.txt", read_token=token)

    token = project.fresh_token("src/a.py")
    project.mutations.rename(project.attempt, "src/a.py", "src/moved.py", read_token=token)


# ---------------------------------------------------------------------------
# acceptance 1: exact reconstruction, net diff, edit-then-revert
# ---------------------------------------------------------------------------


def test_known_mutation_sequence_reconstructs_from_stored_artifacts(sink, arbite_dir):
    project = _project(sink, arbite_dir)
    _known_sequence(project)

    view = project.view()

    # Ordered mechanical history: every operation, exactly once, in order.
    assert [op["operation_kind"] for op in view.operations] == [
        "write",
        "edit",
        "remove",
        "rename",
    ]
    assert all(op["result"] == "ok" for op in view.operations)
    # The event cursor each receipt was recorded under is carried, so the order is
    # deterministic rather than timestamp-luck.
    assert all(op["event_cursor"] is not None for op in view.operations)

    # Every digest is a digest (never text) and every non-absent artifact is
    # verifiable by reading its bytes back.
    for op in view.operations:
        for change in op["changes"]:
            for side in ("before_artifact", "after_artifact"):
                status = change[side]
                if status["digest"] != ABSENT:
                    assert status["verifiable"] is True, status
        for ref in op["artifacts"]:
            assert ref["verifiable"] is True, ref

    # Net view: create/edit-away-rename/remove/rename-destination.
    net = {entry["path"]: entry for entry in view.net_changes}
    assert net["src/created.bin"]["change"] == "created"
    assert net["src/dead.txt"]["change"] == "removed"
    assert net["src/a.py"]["change"] == "removed"  # edited, then renamed away
    assert net["src/moved.py"]["change"] == "created"
    assert view.touched_paths == sorted(net)
    assert "output_truncated" not in view.markers

    # Byte-exact reconstruction: the recorded digests name content we can retrieve.
    assert project.store.read_artifact_bytes(net["src/created.bin"]["after"]) == BINARY
    assert (
        project.store.read_artifact_bytes(net["src/moved.py"]["after"])
        == b"ALPHA\nbeta\ngamma\n"
    )
    assert (
        project.store.read_artifact_bytes(net["src/a.py"]["before"])
        == b"alpha\nbeta\ngamma\n"
    )
    assert project.store.read_artifact_bytes(net["src/dead.txt"]["before"]) == b"gone\n"


def test_edit_then_revert_reports_reverted_but_keeps_both_operations(sink, arbite_dir):
    project = _project(sink, arbite_dir, files=(("src/a.py", "alpha\n"),))

    token = project.fresh_token("src/a.py")
    project.mutations.edit(
        project.attempt,
        "src/a.py",
        [filemutations.Edit(old="alpha", new="ALPHA")],
        read_token=token,
    )
    token = project.fresh_token("src/a.py")
    project.mutations.edit(
        project.attempt,
        "src/a.py",
        [filemutations.Edit(old="ALPHA", new="alpha")],
        read_token=token,
    )

    view = project.view()

    # The net view folded first-before to last-after: the bytes are back.
    net = {entry["path"]: entry for entry in view.net_changes}["src/a.py"]
    assert net["before"] == net["after"]
    assert net["changed"] is False
    assert net["reverted"] is True
    assert net["change"] == "reverted"
    assert len(net["operations"]) == 2

    # ...and neither operation was erased from the ordered history.
    assert [op["operation_kind"] for op in view.operations] == ["edit", "edit"]
    assert [op["operation_id"] for op in view.operations] == net["operations"]
    assert view.counts["net_reverted_paths"] == 1


def test_binary_write_is_digest_and_artifact_based_not_textual(sink, arbite_dir):
    project = _project(sink, arbite_dir, files=())
    project.claims.claim(project.attempt, ["src/blob.dat"])
    project.mutations.write(project.attempt, "src/blob.dat", BINARY)

    view = project.view()
    op = view.operations[0]
    change = op["changes"][0]

    assert op["operation_kind"] == "write"
    assert change["before"] == ABSENT
    assert change["after"] == coordination.digest_of_bytes(BINARY)
    assert change["after"].startswith("sha256:")
    assert change["after_artifact"]["verifiable"] is True
    assert project.store.read_artifact_bytes(change["after"]) == BINARY
    # No textual diff is required (or produced) for a binary write.
    assert "diff" not in change
    assert view.net_changes[0]["change"] == "created"


# ---------------------------------------------------------------------------
# acceptance 2/3: bounds, pagination, JSON shape, mechanical manifest
# ---------------------------------------------------------------------------


def test_bounds_paginate_operations_and_mark_truncation(sink, arbite_dir):
    project = _project(sink, arbite_dir, files=())
    for index in range(5):
        path = f"src/f{index}.py"
        project.claims.claim(project.attempt, [path])
        project.mutations.write(project.attempt, path, f"content {index}\n".encode())

    first = project.view(limit=2)
    assert len(first.operations) == 2
    assert first.bounds["operations"]["total"] == 5
    assert first.bounds["operations"]["truncated"] is True
    assert first.bounds["operations"]["next_offset"] == 2
    assert "output_truncated" in first.markers
    # The net fold is over the whole scope, not the returned page.
    assert len(first.net_changes) == 5

    second = project.view(limit=2, offset=2)
    assert len(second.operations) == 2
    assert second.bounds["operations"]["next_offset"] == 4
    assert "offset_advanced" in second.markers

    last = project.view(limit=2, offset=4)
    assert len(last.operations) == 1
    assert last.bounds["operations"]["truncated"] is False
    assert last.bounds["operations"]["next_offset"] is None

    capped = project.view(limit=10**9)
    assert capped.bounds["operations"]["limit_capped"] is True
    assert "limit_capped" in capped.markers
    assert len(capped.operations) == 5

    # The JSON payload is exactly the documented shape.
    payload = first.to_dict()
    assert payload["read_only"] is True
    assert payload["evidence"]["operation_count"] == 2
    assert payload["evidence"]["touched_paths"] == first.touched_paths


def test_mechanical_evidence_is_a_manifest_without_any_summary(sink, arbite_dir):
    project = _project(sink, arbite_dir)
    _known_sequence(project)

    view = project.view()
    payload = view.to_dict()

    # No ticket notes exist, yet the mechanical view is already a manifest.
    assert view.summaries["notes"] == []
    assert view.summaries["label"] == changes.PROSE_LABEL
    assert [op["operation_kind"] for op in payload["evidence"]["operations"]] == [
        "write",
        "edit",
        "remove",
        "rename",
    ]
    assert {entry["change"] for entry in payload["evidence"]["net_changes"]} == {
        "created",
        "removed",
    }

    # An agent-authored note is reported as prose and cannot alter the net view.
    sink.add_note(project.ticket.id, "claude.opus.001", "I changed everything, honest")
    with_note = project.view()
    assert with_note.summaries["notes"][0]["message"] == "I changed everything, honest"
    assert with_note.net_changes == view.net_changes


def test_read_observations_are_a_separate_stream(sink, arbite_dir):
    project = _project(sink, arbite_dir)
    project.claims.claim(project.attempt, ["src/a.py"])
    project.reads.read(project.attempt, "src/a.py")

    ordinary = project.view()
    assert ordinary.read_observations == []
    assert ordinary.counts["total_read_observations"] >= 1
    assert "read_observations_excluded" in ordinary.markers

    with_reads = project.view(include_reads=True)
    assert with_reads.read_observations, "reads must appear when explicitly requested"
    observed = with_reads.read_observations[0]
    assert observed["path"] == "src/a.py"
    assert observed["digest"] == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    assert "read_observations_excluded" not in with_reads.markers


# ---------------------------------------------------------------------------
# attempts across a close/reopen, and unattributed attribution
# ---------------------------------------------------------------------------


def _end_attempt_and_close(project, reason="done"):
    """End the active attempt, then close the ticket -- the C09 close flow."""
    project.ctl.end_attempt(project.attempt, state="finished", reason=reason)
    ticket = project.ctl.tickets.get(project.ticket.id)
    ticket.status = "closed"
    ticket.assignee = None
    ticket.closed = schema.now()
    ticket.updated = ticket.closed
    project.ctl.tickets.update(ticket)


def test_closed_and_reopened_attempts_keep_separate_views(sink, arbite_dir):
    project = _project(sink, arbite_dir, files=(("src/a.py", "alpha\n"),))

    token = project.fresh_token("src/a.py")
    project.mutations.write(project.attempt, "src/a.py", b"first\n", read_token=token)
    first_attempt = project.attempt

    _end_attempt_and_close(project)
    assert first_attempt.state != "active"
    # No claim resurrection: the finished attempt's token is gone.
    assert project.claims.claim_for("src/a.py") is None

    # Reopen (open + unassigned), then acquire a fresh generation.
    ticket = project.ctl.tickets.get(project.ticket.id)
    ticket.status = "open"
    ticket.closed = None
    ticket.assignee = None
    project.ctl.tickets.update(ticket)
    second_attempt = project.ctl.acquire(
        project.ctl.tickets.get(ticket.id), worker_id="claude.opus.002"
    ).attempt
    assert second_attempt.generation == first_attempt.generation + 1

    # Work a second file under the new attempt.
    token = project.fresh_token_for(second_attempt, "src/a.py")
    project.mutations.write(second_attempt, "src/a.py", b"second\n", read_token=token)

    first_view = project.view(attempt_id=first_attempt.id)
    assert [op["operation_kind"] for op in first_view.operations] == ["write"]
    assert first_view.operations[0]["attempt_id"] == first_attempt.id
    # The new attempt's operation is observed but NOT attributed to attempt 1.
    foreign = [u for u in first_view.unattributed if u.get("operation_id")]
    assert foreign and all(u["attribution"] == "observed/unattributed" for u in foreign)
    assert foreign[0]["attempt_id"] == second_attempt.id

    second_view = project.view(attempt_id=second_attempt.id)
    assert [op["operation_kind"] for op in second_view.operations] == ["write"]
    assert second_view.operations[0]["attempt_id"] == second_attempt.id

    # The ticket view is the union of both attempts.
    ticket_view = project.view()
    assert len(ticket_view.operations) == 2
    assert {op["attempt_id"] for op in ticket_view.operations} == {
        first_attempt.id,
        second_attempt.id,
    }


def test_attempt_view_rejects_an_unknown_attempt(sink, arbite_dir):
    project = _project(sink, arbite_dir)
    with pytest.raises(CoordinationNotFound) as excinfo:
        project.view(attempt_id="att-doesnotexist")
    assert excinfo.value.details["attempt_id"] == "att-doesnotexist"


def test_external_drift_is_observed_and_never_attributed_to_the_agent(sink, arbite_dir):
    project = _project(sink, arbite_dir, files=(("src/a.py", "alpha\n"),))
    engine = mutation.MutationEngine(
        project.service, project.claims, fault_injector=FaultOnce(mutation.FAULT_AFTER_APPLY)
    )
    claim = project.claims.claim(project.attempt, ["src/a.py"]).acquired[0]
    operation_id = "op-dddddddddddddddd"

    with pytest.raises(mutation.FaultInjected):
        engine.write(
            project.attempt, "src/a.py", b"proxy\n", claim=claim, operation_id=operation_id
        )
    # An unrestricted external writer (not arbite) changes the bytes.
    (project.root / "src" / "a.py").write_bytes(b"external\n")
    with pytest.raises(DriftDetected):
        mutation.MutationEngine(project.service, project.claims).reconcile()

    view = project.view()
    drift = [entry for entry in view.unattributed if entry["kind"] == "drift"]
    assert drift, "the drift event must be reported"
    assert drift[0]["attribution"] == "observed/unattributed"
    assert drift[0]["operation_id"] == operation_id
    assert drift[0]["paths"] == ["src/a.py"]
    assert "unattributed_observed" in view.markers
    # It is not smuggled into the agent's mechanical evidence.
    assert all(op["operation_id"] != operation_id for op in view.operations)


def test_ticket_view_reports_the_active_attempt(sink, arbite_dir):
    project = _project(sink, arbite_dir)
    view = project.view()
    assert view.attempt_id is None
    assert view.active_attempt_id == project.attempt.id
    assert view.to_dict()["active_attempt_id"] == project.attempt.id

    _end_attempt_and_close(project)
    assert project.view().active_attempt_id is None
