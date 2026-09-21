"""The file-operation intent journal: the operation, its boundaries, and its recovery.

The transcripts for this slice are the doctor ones (DR1, DR2, in
`test_recovery_examples.py`). What is here is the behaviour a transcript cannot show:
that a retried operation id does not duplicate its effects, that a refused mutation
changes no byte, and -- with real processes killed at each boundary of the protocol --
that an interrupted write either completed exactly once or changed nothing, and that a
human is then told which of the two it was.

Two storage domains again: the coordination store records the intent, and the project
tree holds the bytes, so every claim here is checked against *both* ("the receipt says
this" and "the file holds that"). The SQLite backend cannot store artifact content yet
(tic-7c42 decides how content lives in a database), so a mutation is refused there before
any byte changes; that refusal is asserted, rather than the write it will allow later.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import examples
import lifecycle_state as state
from arbite.coordination import records as coordination_records
from arbite.coordination import recovery
from arbite.coordination.app import CoordinationApp
from arbite.coordination.claims import FileClaims
from arbite.coordination.lifecycle import TicketLifecycle
from arbite.coordination.mutations import (
    BOUNDARIES,
    FileMutations,
    MutationRequest,
    PathChange,
)
from arbite.coordination.paths import probe
from arbite.coordination.store import open_coordination_store
from arbite.errors import Busy, CoordinationError, Stale
from arbite.sinks import SinkSpec, build_sink

TICKET = "tic-cf9f"
AGENT = "deepseek.code.006"
WORKER = Path(__file__).resolve().parent / "mutation_worker.py"
REPO_SRC = Path(__file__).resolve().parents[1] / "src"

#: The engine performs writes and edits (tic-60c7) and create/remove/rename (tic-74e2);
#: every kind it knows is exercised here, because the protocol is per kind even when the
#: syscall is one.
PATH = "src/hello.py"
RENAMED = "src/greeting.py"
BEFORE = b"one\n"
AFTER = b"two\n"
SINKS = ("file", "sqlite")


@dataclass
class Scene:
    """A project mid-operation: a claimed ticket, a claimed path, and its read token."""

    project: Path
    kind: str
    sink: object
    app: CoordinationApp
    lifecycle: TicketLifecycle
    claims: FileClaims
    mutations: FileMutations

    @property
    def store(self):
        return self.app.store

    @property
    def ticket(self):
        return self.sink.get(TICKET)

    @property
    def attempt(self):
        return self.store.active_attempts(TICKET)[0]

    # -- the project's bytes -------------------------------------------------

    def path(self, relative: str) -> Path:
        return self.project / relative

    def write(self, relative: str, data: bytes) -> None:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def bytes(self, relative: str):
        target = self.path(relative)
        return target.read_bytes() if target.is_file() else None

    def version(self, relative: str) -> str:
        return probe(self.project, relative).digest

    def stages(self, relative: str = PATH) -> list:
        return sorted(
            one.name for one in self.path(relative).parent.iterdir() if one.name.endswith(".arbite-stage")
        )

    # -- the token a write presents ------------------------------------------

    def token(self, relative: str) -> str:
        """Mint the read observation a write presents, as `file read` will (tic-1c4f)."""
        claim = self.store.claims_for_path(relative)[0]
        token = coordination_records.new_id(
            "observation", {one.id for one in self.store.records("observation")}
        )
        self.store.put_record(
            coordination_records.ReadObservation(
                id=token,
                path=relative,
                digest=self.version(relative),
                observed_at=coordination_records.utc_now(),
                attempt_id=self.attempt.id,
                claim_generation=claim.generation,
            )
        )
        return token

    def change(self, relative: str, content: bytes, token: str, expect=None) -> PathChange:
        return PathChange(
            path=relative,
            expect=self.version(relative) if expect is None else expect,
            becomes=coordination_records.digest_bytes(content),
            payload=content,
            token=token,
        )

    def request(self, *changes: PathChange, kind: str = "write", operation_id=None) -> MutationRequest:
        return MutationRequest(
            kind=kind,
            ticket_id=TICKET,
            attempt_id=self.attempt.id,
            actor=AGENT,
            changes=changes,
            operation_id=operation_id,
        )

    def replace(
        self, relative: str, content: bytes, kind: str = "write", operation_id=None, expect=None
    ):
        """A whole-file replacement, which is what a write or an edit is."""
        return self.request(
            self.change(relative, content, self.token(relative), expect=expect),
            kind=kind,
            operation_id=operation_id,
        )

    def remove(self, relative: str, kind: str = "remove", operation_id=None):
        return self.request(
            PathChange(
                path=relative,
                expect=self.version(relative),
                becomes=coordination_records.ABSENT,
                token=self.token(relative),
            ),
            kind=kind,
            operation_id=operation_id,
        )

    def rename(self, source: str, dest: str, operation_id=None):
        return self.request(
            PathChange(
                path=source,
                expect=self.version(source),
                becomes=coordination_records.ABSENT,
                token=self.token(source),
            ),
            PathChange(
                path=dest,
                expect=self.version(dest),
                becomes=self.version(source),
                token=self.token(dest),
            ),
            kind="rename",
            operation_id=operation_id,
        )


def scene_for(
    tmp_path: Path, sink_kind: str = "file", content: bytes = BEFORE, paths=(PATH,)
) -> Scene:
    """A project with `TICKET` claimed by AGENT, `paths` claimed, and content on disk.

    The path set is claimed in **one** acquisition, which is what a rename needs: its two
    paths share one generation, and an operation whose paths are at different generations is
    refused (the second path has changed hands since the token was taken)."""
    project = state.initialise(tmp_path, sink_kind)
    state.claimable(project, TICKET, sink_kind)
    claimed = examples.run_cli(project, "claim", TICKET, "--agent", AGENT)
    assert claimed.returncode == 0, claimed.stderr

    sink = build_sink(SinkSpec(kind=sink_kind), project / ".arbite")
    app = CoordinationApp.open(sink, project, project / ".arbite", store_source="test")
    lifecycle = TicketLifecycle(sink, app)
    claims = FileClaims(sink, lifecycle)
    scene = Scene(project, sink_kind, sink, app, lifecycle, claims, FileMutations(sink, lifecycle))
    if content is not None:
        scene.write(PATH, content)
    result = claims.claim(TICKET, scene.attempt.id, list(paths))
    assert result.exit_code == 0, result.to_text()
    return scene


@pytest.fixture
def file_scene(tmp_path):
    return scene_for(tmp_path, "file")


@pytest.fixture
def sqlite_scene(tmp_path):
    return scene_for(tmp_path, "sqlite")


# --- the protocol, in one process -------------------------------------------


def test_a_write_applies_and_records_both_versions(file_scene):
    """The whole protocol for one replacement: the bytes change, the receipt says so, and
    both versions are kept as evidence addressed by their digests."""
    scene = file_scene
    outcome = scene.mutations.apply(scene.replace(PATH, AFTER))

    assert outcome.applied is True
    assert outcome.receipt.result == coordination_records.RECEIPT_SUCCEEDED
    assert scene.bytes(PATH) == AFTER
    assert outcome.receipt.before[PATH] == coordination_records.digest_bytes(BEFORE)
    assert outcome.receipt.after[PATH] == coordination_records.digest_bytes(AFTER)
    assert scene.stages() == [], "the staged copy goes once the target holds the bytes"
    assert scene.store.pending_operations() == []

    archived = {artifact.digest for artifact in scene.store.records("artifact")}
    assert archived == {outcome.receipt.before[PATH], outcome.receipt.after[PATH]}
    assert scene.store.get_artifact_bytes(outcome.receipt.before[PATH]) == BEFORE

    events = [
        (event.kind, event.result)
        for event in scene.store.events()
        if event.operation_id == outcome.operation_id
    ]
    assert events == [("write.intent", "staged"), ("write.file", outcome.receipt.after[PATH])]
    assert scene.store.events()[-1].ticket_id == TICKET


def pending_receipt(scene, *, before=BEFORE, after=AFTER, operation_id="op-c0de", path=PATH):
    """A staged operation with no finalised outcome: what a killed process leaves behind.

    Written through the store rather than through the engine, because the point is a state no
    live engine call produces -- the receipt exists and the bytes it describes may be anything
    at all."""
    receipt = coordination_records.OperationReceipt(
        id=operation_id,
        kind="write",
        paths=[path],
        result=coordination_records.RECEIPT_PENDING,
        recorded_at=coordination_records.utc_now(),
        ticket_id=TICKET,
        attempt_id=scene.store.active_attempts(TICKET)[0].id,
        actor=AGENT,
        before={path: coordination_records.digest_bytes(before)},
        after={path: coordination_records.digest_bytes(after)},
        claim_generation=scene.store.claims_for_path(path)[0].generation,
    )
    scene.store.put_record(receipt)
    return receipt


@pytest.mark.parametrize("kind", SINKS)
def test_recover_reconciles_a_pending_operation_on_both_sinks(tmp_path, kind):
    """`store.recover()` implemented for both backends, and meaning the same thing on each:
    the judgement is about the bytes under the workspace root, not about which store holds the
    record, so an operation that never applied is finalised as failed either way -- and the
    bytes are untouched by the reconciliation."""
    scene = scene_for(tmp_path, kind, content=BEFORE)
    receipt = pending_receipt(scene)

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.NOT_APPLIED
    assert outcome.finalised == coordination_records.RECEIPT_FAILED
    assert scene.store.get_record("receipt", receipt.id).result == (
        coordination_records.RECEIPT_FAILED
    )
    assert scene.bytes(PATH) == BEFORE
    assert scene.store.record_problems([TICKET]) == []


@pytest.mark.parametrize("kind", SINKS)
def test_drift_is_reported_on_both_sinks_and_archived_where_the_backend_can(tmp_path, kind):
    """The same finding on both sinks, with the one difference the backends honestly have:
    the file backend archives the bytes it found as an artifact, while SQLite -- which does not
    store artifact content yet (tic-7c42) -- leaves them in place, which is where they were and
    where the finding tells a human to look."""
    scene = scene_for(tmp_path, kind, content=b"a third version\n")
    receipt = pending_receipt(scene)
    drift_version = scene.version(PATH)

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.DRIFT
    assert scene.store.get_record("receipt", receipt.id).is_pending
    assert scene.bytes(PATH) == b"a third version\n"
    assert [problem.kind for problem in scene.store.record_problems([TICKET])] == [
        "pending_operation"
    ]
    if kind == "file":
        assert outcome.preserved == drift_version
        assert scene.store.get_artifact_bytes(drift_version) == b"a third version\n"
    else:
        assert outcome.preserved is None
        assert scene.store.records("artifact") == []


def test_an_operation_arbite_will_not_read_is_unjudgeable_not_drift(tmp_path):
    """A path arbite refuses to look through -- here a symlink standing where the recorded file
    was -- is *unjudgeable*, not drift: arbite did not read those bytes, so it may not claim
    they are something else. Nothing is changed, the receipt stays pending, and the finding
    says which it is."""
    scene = scene_for(tmp_path, "file")
    receipt = pending_receipt(scene)
    real = scene.path("src/real.py")
    real.write_bytes(b"elsewhere\n")
    scene.path(PATH).unlink()
    scene.path(PATH).symlink_to(real.name)

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.UNKNOWN
    assert outcome.observed[PATH] is None
    assert scene.store.get_record("receipt", receipt.id).is_pending
    assert scene.path(PATH).is_symlink(), "nothing touched the link"
    problems = scene.store.record_problems([TICKET])
    assert [problem.kind for problem in problems] == ["pending_operation"]
    assert "could not judge it" in problems[0].detail


def test_a_retried_operation_id_is_deduplicated(file_scene):
    """A retry that presents the id of an operation already finalised changes nothing:
    not the bytes, not the receipt, not the stream. This is what makes a retry safe."""
    scene = file_scene
    first = scene.mutations.apply(scene.replace(PATH, AFTER))
    events = len(scene.store.events())

    second = scene.mutations.apply(
        scene.request(
            scene.change(PATH, b"three\n", scene.token(PATH), expect=scene.version(PATH)),
            operation_id=first.operation_id,
        )
    )

    assert second.deduplicated is True
    assert second.applied is False
    assert second.operation_id == first.operation_id
    assert scene.bytes(PATH) == AFTER
    assert len(scene.store.events()) == events


def test_a_retried_pending_operation_completes_rather_than_applying_twice(file_scene):
    """The crash case a retry can meet: the bytes are already the recorded after version
    and the receipt was never finalised, so the retry *completes* the operation instead of
    writing the file a second time."""
    scene = file_scene
    pending_receipt(scene, before=BEFORE, after=AFTER)
    scene.write(PATH, AFTER)  # the replacement happened; only the receipt did not

    outcome = scene.mutations.apply(scene.replace(PATH, AFTER, operation_id="op-c0de"))

    assert outcome.recovered is True
    assert outcome.applied is False
    assert scene.bytes(PATH) == AFTER
    assert scene.store.get_record("receipt", "op-c0de").result == (
        coordination_records.RECEIPT_SUCCEEDED
    )


def test_a_token_authorises_only_the_version_it_observed(file_scene):
    """A token names one version: it cannot be presented for a version the caller did not
    actually read, however live the claim is. (That one token then cannot authorise a
    *second* write is tic-60c7's rule -- consuming a token is the read surface's business,
    tic-1c4f -- and what is asserted here is the half this engine owns.)"""
    scene = file_scene
    token = scene.token(PATH)  # observes BEFORE

    with pytest.raises(Stale) as failure:
        scene.mutations.apply(
            scene.request(
                scene.change(
                    PATH, AFTER, token, expect=coordination_records.digest_bytes(AFTER)
                )
            )
        )

    assert "observed" in str(failure.value)
    assert "no bytes were changed" in str(failure.value)
    assert scene.bytes(PATH) == BEFORE

    # ...and the same token, presented for the version it did observe, is authorised.
    outcome = scene.mutations.apply(scene.request(scene.change(PATH, AFTER, token)))
    assert outcome.applied is True
    assert scene.bytes(PATH) == AFTER


def test_bytes_that_moved_since_the_read_are_refused(file_scene):
    """The whole-file digest is re-checked at use time, so a change made between the read
    and the write is outcome 5 rather than an overwrite (the external-writer case the plan
    reports instead of pretending to prevent).

    The wording is the frozen WR2/SC2 block's -- both digests, and when the file moved --
    and the reason is `stale_version`, because a moved file and a spent token are the same
    exit code and different repairs (tic-60c7 owns both sentences; this test asserted the
    older, vaguer wording until the transcript fixed it)."""
    scene = file_scene
    request = scene.replace(PATH, AFTER)
    scene.write(PATH, b"somebody else\n")  # a direct write, attributed to no ticket

    with pytest.raises(Stale) as failure:
        scene.mutations.apply(request)

    message = str(failure.value)
    assert failure.value.reason == "stale_version"
    assert f"you read {coordination_records.short_digest(request.changes[0].expect)}" in message
    assert f"but the file is now {coordination_records.short_digest(coordination_records.digest_bytes(b'somebody else\n'))}" in message
    assert "no bytes were changed" in message
    assert scene.bytes(PATH) == b"somebody else\n"


def test_a_path_another_attempt_holds_is_busy(file_scene):
    """Ownership is checked at use time as well as at claim time: a path that changed hands
    since the read is outcome 4, and nothing is written."""
    scene = file_scene
    request = scene.replace(PATH, AFTER)
    claim = scene.store.claims_for_path(PATH)[0]
    scene.store.put_record(
        coordination_records.FileClaim(
            id=claim.id,
            workspace_id=claim.workspace_id,
            path=PATH,
            ticket_id=TICKET,
            attempt_id="att-0000",
            generation=claim.generation + 1,
            acquired=coordination_records.utc_now(),
            observed_version=claim.observed_version,
        )
    )

    with pytest.raises(Busy) as failure:
        scene.mutations.apply(request)

    assert failure.value.reason == "file_busy"
    assert scene.bytes(PATH) == BEFORE


def test_RC2_a_close_racing_a_write_changes_nothing(file_scene):
    """RC2's target, at the engine the write command will call.

    The transcript's command (`arbite file write`) is tic-60c7's, so what is asserted here
    is its substance: a mutation attempted under a revoked attempt generation is refused as
    stale in the same words, the caller is told no bytes were changed, and the file still
    holds the version it held before. Either the write completes first and the close records
    it, or the close wins and no observer mutates under the old token -- never both.

    The attempt is read *before* the close since tic-e9ed: closing now ends the attempt
    (that is the cascade), so it is no longer there to read afterwards, and the refusal it
    produces names the attempt the caller presented rather than one that is still active."""
    scene = file_scene
    request = scene.replace(PATH, AFTER)
    attempt = scene.attempt
    closed = examples.run_cli(scene.project, "close", TICKET)
    assert closed.returncode == 0, closed.stderr

    with pytest.raises(Stale) as failure:
        scene.mutations.apply(request)

    message = str(failure.value)
    # The reason is narrower than "stale_read" since tic-60c7: a ticket that moved on is
    # the same outcome (5, nothing changed) with a different repair -- reopen or stop --
    # so it carries its own key and its own sentence rather than the re-read hint.
    assert failure.value.reason == "attempt_not_current"
    assert f"attempt {attempt.id} generation {attempt.generation} is no longer " in message
    assert "current" in message
    assert f"({TICKET} closed" in message
    assert "no bytes were changed" in message
    assert scene.bytes(PATH) == BEFORE
    assert scene.store.active_attempts(TICKET) == [], "the close ended the attempt"
    assert scene.store.active_claims() == [], "and released the paths it held"


def test_a_remove_and_a_rename_round_trip_through_the_receipt(tmp_path):
    """The other two kinds the engine performs: both record both paths (a rename records
    where the bytes came from and where they went), and both leave the tree in the state
    their receipt describes."""
    scene = scene_for(tmp_path, "file", paths=(PATH, RENAMED))
    moved = scene.mutations.apply(scene.rename(PATH, RENAMED))

    assert scene.bytes(PATH) is None
    assert scene.bytes(RENAMED) == BEFORE
    assert moved.receipt.paths == [PATH, RENAMED]
    assert moved.receipt.before[PATH] == coordination_records.digest_bytes(BEFORE)
    assert moved.receipt.after[RENAMED] == coordination_records.digest_bytes(BEFORE)
    assert moved.receipt.before[RENAMED] == coordination_records.ABSENT
    assert moved.receipt.after[PATH] == coordination_records.ABSENT

    removed = scene.mutations.apply(scene.remove(RENAMED))
    assert scene.bytes(RENAMED) is None
    assert removed.receipt.after[RENAMED] == coordination_records.ABSENT


def test_a_backend_that_cannot_store_evidence_refuses_before_anything_changes(
    sqlite_scene,
):
    """SQLite cannot store artifact content yet (that is tic-7c42's decision), and the
    engine refuses rather than recording a mutation without evidence: the refusal names the
    owning ticket, and the project is untouched -- no bytes, no staged copy, no pending
    receipt, no event."""
    scene = sqlite_scene
    events = len(scene.store.events())

    with pytest.raises(CoordinationError) as failure:
        scene.mutations.apply(scene.replace(PATH, AFTER))

    assert "tic-7c42" in str(failure.value)
    assert "nothing was changed" in str(failure.value)
    assert scene.bytes(PATH) == BEFORE
    assert scene.stages() == []
    assert scene.store.pending_operations() == []
    assert scene.store.records("receipt") == []
    assert len(scene.store.events()) == events


# --- real processes, killed at each boundary --------------------------------


def run_worker(project, *args, timeout: float = 90.0):
    environment = dict(os.environ, PYTHONPATH=str(REPO_SRC))
    environment.pop("ARBITE_SINK", None)
    return subprocess.run(
        [sys.executable, str(WORKER), *args], env=environment, capture_output=True, text=True,
        timeout=timeout,
    )


def worker_write(scene, path, content, boundary):
    return run_worker(
        scene.project, "write", str(scene.project), scene.kind, TICKET, path, content, boundary
    )


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_RC_a_kill_at_every_boundary_recovers_exactly_once(tmp_path, boundary):
    """Every boundary of the protocol, crossed by a process that really dies.

    Before the replacement: the bytes are the recorded before version, so the operation did
    not happen -- after recovery the receipt is failed, nothing was changed, and the staged
    copy is gone. At and after the replacement: the bytes are the after version, so the
    operation *did* happen -- after recovery the receipt is succeeded and the bytes are
    exactly what it says, once. Either way a human reading the store and the file sees the
    same story.
    """
    scene = scene_for(tmp_path, "file")

    crashed = worker_write(scene, PATH, "two\n", boundary)
    assert crashed.returncode == 9, (crashed.returncode, crashed.stdout, crashed.stderr)

    before_replacement = boundary in ("intent_persisted", "staged", "before_replace")
    assert scene.bytes(PATH) == (BEFORE if before_replacement else AFTER)

    pending = scene.store.pending_operations()
    assert len(pending) == 1, "the intent survives the crash, finalised or not"
    staged_here = boundary in ("staged", "before_replace")
    assert (scene.stages() != []) is staged_here

    outcomes = scene.store.recover()
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.verdict == (
        recovery.NOT_APPLIED if before_replacement else recovery.COMPLETED
    )
    assert outcome.stage_removed is staged_here, (
        "a staged copy is discarded where there is one; the other boundaries leave none"
    )

    receipt = scene.store.get_record("receipt", pending[0].id)
    assert receipt.result == (
        coordination_records.RECEIPT_FAILED if before_replacement
        else coordination_records.RECEIPT_SUCCEEDED
    )
    assert scene.bytes(PATH) == (BEFORE if before_replacement else AFTER)
    assert scene.stages() == [], "a leftover stage file is not evidence of anything finished"
    assert scene.store.pending_operations() == []
    assert scene.store.record_problems([TICKET]) == []
    assert [event.kind for event in scene.store.events()].count("recover.finalized") == 1

    # ...and running the recovery again is a no-op rather than a second finalisation.
    assert scene.store.recover() == []


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_retry_after_a_kill_completes_the_operation_exactly_once(tmp_path, boundary):
    """The other half of the acceptance: after a kill, the caller's retry ends in exactly
    one applied operation. Where nothing had been applied the retry writes the bytes and
    finalises the receipt; where the bytes were already there the retry *completes* the
    operation instead of writing them twice -- the file, the receipt and the stream all say
    one operation happened."""
    scene = scene_for(tmp_path, "file")
    crashed = worker_write(scene, PATH, "two\n", boundary)
    assert crashed.returncode == 9, (crashed.returncode, crashed.stdout, crashed.stderr)
    operation_id = scene.store.pending_operations()[0].id

    retried = run_worker(
        scene.project,
        "retry",
        str(scene.project),
        scene.kind,
        TICKET,
        PATH,
        "two\n",
        scene.token(PATH),
        operation_id,
    )

    assert retried.returncode == 0, (retried.stdout, retried.stderr)
    printed = json.loads(retried.stdout)
    assert printed["operation"] == operation_id
    assert scene.bytes(PATH) == AFTER
    receipt = scene.store.get_record("receipt", operation_id)
    assert receipt.result == coordination_records.RECEIPT_SUCCEEDED
    assert scene.store.pending_operations() == []
    assert scene.stages() == []
    # Exactly one event says this operation happened: the applied one where the retry had to
    # write the bytes, the recovery one where a previous attempt had already written them.
    ended = [
        event
        for event in scene.store.events()
        if event.operation_id == operation_id
        and event.kind in ("write.file", "recover.finalized")
    ]
    assert len(ended) == 1, [event.kind for event in ended]


def test_a_replacement_followed_by_a_store_failure_is_recovered_as_succeeded(tmp_path):
    """The nastiest window, named by the ticket: the file was replaced and the process died
    before the sink recorded it. The bytes and the receipt are two storage domains, so they
    cannot commit together -- which is exactly why the receipt carries both versions and the
    recovery can tell the difference between "it happened" and "it did not"."""
    scene = scene_for(tmp_path, "file")
    crashed = worker_write(scene, PATH, "two\n", "before_finalize")
    assert crashed.returncode == 9, crashed.stderr

    receipt = scene.store.pending_operations()[0]
    assert receipt.result == coordination_records.RECEIPT_PENDING
    assert scene.bytes(PATH) == AFTER, "the replacement is what the crash left behind"

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.COMPLETED
    assert outcome.finalised == coordination_records.RECEIPT_SUCCEEDED
    assert scene.store.get_record("receipt", receipt.id).result == (
        coordination_records.RECEIPT_SUCCEEDED
    )
    assert scene.bytes(PATH) == AFTER


def test_bytes_matching_neither_version_are_drift_and_are_left_alone(tmp_path):
    """The third answer, and the one that must never be guessed at: a third version on disk.
    The bytes are not touched, the receipt stays pending, the finding prints all three
    versions (doctor is where comparing them is the point), and the observed bytes are kept
    as evidence on a backend that can store content."""
    scene = scene_for(tmp_path, "file")
    crashed = worker_write(scene, PATH, "two\n", "before_finalize")
    assert crashed.returncode == 9, crashed.stderr
    receipt = scene.store.pending_operations()[0]
    scene.write(PATH, b"a third version\n")
    drift_version = scene.version(PATH)

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.DRIFT
    assert outcome.finalised is None
    assert outcome.preserved == drift_version
    assert scene.bytes(PATH) == b"a third version\n", "arbite does not overwrite drift"
    assert scene.store.get_record("receipt", receipt.id).result == (
        coordination_records.RECEIPT_PENDING
    )
    assert scene.store.get_artifact_bytes(drift_version) == b"a third version\n"

    kinds = [problem.kind for problem in scene.store.record_problems([TICKET])]
    assert kinds == ["pending_operation"], "drift is the finding doctor exists for"
    detail = scene.store.record_problems([TICKET])[0].detail
    assert drift_version in detail
    assert receipt.before[PATH] in detail and receipt.after[PATH] in detail

    # A second reconciliation neither overwrites anything nor invents a second story.
    scene.store.recover()
    assert scene.bytes(PATH) == b"a third version\n"
    assert scene.store.record_problems([TICKET])[0].kind == "pending_operation"

    # ...and a retry of the operation refuses rather than writing over it.
    with pytest.raises(CoordinationError) as failure:
        scene.mutations.apply(scene.replace(PATH, b"four\n", operation_id=receipt.id))
    assert "was NOT re-applied" in str(failure.value)


def test_a_rename_killed_before_the_replace_moves_nothing(tmp_path):
    """A rename has two paths and one syscall, so a kill before that syscall is the "did
    not happen" answer for *both* paths -- including the destination the operation was going
    to overwrite, which is still exactly as it was."""
    scene = scene_for(tmp_path, "file", paths=(PATH, RENAMED))
    scene.write(RENAMED, b"in the way\n")
    crashed = run_worker(
        scene.project, "rename", str(scene.project), scene.kind, TICKET, PATH, RENAMED,
        "before_replace",
    )
    assert crashed.returncode == 9, (crashed.returncode, crashed.stdout, crashed.stderr)
    receipt = scene.store.pending_operations()[0]
    assert receipt.kind == "rename" and receipt.paths == [PATH, RENAMED]

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.NOT_APPLIED
    assert scene.bytes(PATH) == BEFORE, "an interrupted rename does not move anything by itself"
    assert scene.bytes(RENAMED) == b"in the way\n", "and it does not overwrite the destination"
    assert scene.store.get_record("receipt", receipt.id).result == (
        coordination_records.RECEIPT_FAILED
    )


def test_a_rename_killed_after_the_replace_is_complete(tmp_path):
    """The same rename after its one syscall: the source is gone, the destination holds the
    bytes, and both of those are the recorded after versions -- so the recovery finalises it
    as succeeded without moving anything again."""
    scene = scene_for(tmp_path, "file", paths=(PATH, RENAMED))
    crashed = run_worker(
        scene.project, "rename", str(scene.project), scene.kind, TICKET, PATH, RENAMED,
        "replaced",
    )
    assert crashed.returncode == 9, (crashed.returncode, crashed.stdout, crashed.stderr)

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.COMPLETED
    assert scene.bytes(PATH) is None
    assert scene.bytes(RENAMED) == BEFORE
    assert scene.store.pending_operations() == []


def test_a_rename_interrupted_between_its_paths_is_reported_not_completed(tmp_path):
    """The ticket names this one: a rename interrupted between its source and its
    destination, with somebody else's bytes now at one of the two paths. The recorded
    versions describe neither path, so this is drift: nothing is moved, nothing is
    overwritten, and both paths are left exactly where they are."""
    scene = scene_for(tmp_path, "file", paths=(PATH, RENAMED))
    scene.write(RENAMED, b"in the way\n")
    crashed = run_worker(
        scene.project, "rename", str(scene.project), scene.kind, TICKET, PATH, RENAMED,
        "before_replace",
    )
    assert crashed.returncode == 9, (crashed.returncode, crashed.stdout, crashed.stderr)
    receipt = scene.store.pending_operations()[0]
    scene.write(RENAMED, b"somebody else got here\n")

    outcome = scene.store.recover()[0]

    assert outcome.verdict == recovery.DRIFT
    assert outcome.reason.startswith("some of this operation's paths")
    assert scene.bytes(PATH) == BEFORE
    assert scene.bytes(RENAMED) == b"somebody else got here\n"
    assert scene.store.get_record("receipt", receipt.id).is_pending
    assert [problem.kind for problem in scene.store.record_problems([TICKET])] == [
        "pending_operation"
    ]


def test_the_next_operation_reconciles_what_a_dead_run_left_behind(tmp_path):
    """Reconciliation is not only a doctor action: the next file operation does it first, so
    a plain write is enough to make the store whole again -- and the operation it is about
    still happens."""
    scene = scene_for(tmp_path, "file")
    crashed = worker_write(scene, PATH, "two\n", "before_finalize")
    assert crashed.returncode == 9, crashed.stderr
    abandoned = scene.store.pending_operations()[0]

    outcome = scene.mutations.apply(
        scene.replace(
            PATH, b"three\n", expect=coordination_records.digest_bytes(AFTER)
        )
    )

    assert outcome.applied is True
    assert scene.bytes(PATH) == b"three\n"
    assert scene.store.get_record("receipt", abandoned.id).result == (
        coordination_records.RECEIPT_SUCCEEDED
    ), "the interrupted operation was finished (its bytes were already there)"
    assert scene.store.pending_operations() == []


def test_a_kill_at_the_intent_boundary_leaves_no_bytes_and_no_stage(tmp_path):
    """The earliest boundary, on its own: the intent is durable before anything else, so a
    crash there leaves a receipt and *only* a receipt -- which is what lets the recovery
    distinguish "nothing was staged" from "staged and discarded"."""
    scene = scene_for(tmp_path, "file")
    crashed = worker_write(scene, PATH, "two\n", "intent_persisted")
    assert crashed.returncode == 9, crashed.stderr

    assert scene.bytes(PATH) == BEFORE
    assert scene.stages() == []
    assert len(scene.store.pending_operations()) == 1
    assert scene.store.records("artifact"), "the evidence is written before the bytes change"
