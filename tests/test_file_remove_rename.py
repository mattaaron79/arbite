"""Removal, rename and safe parent creation (planning key C08).

Every behavioural test runs against both sinks (the `sink` fixture), so "the file
sink and the SQLite sink behave equivalently" is checked for the remove/rename
surface too. The suite is organised around the four acceptance criteria:

1. rename obtains source/destination ownership and validates both versions or
   destination absence;
2. a conflicting destination or invalid path leaves BOTH paths unchanged;
3. receipts preserve deleted bytes and both rename paths, and an interrupted
   rename recovers honestly;
4. safe missing parents are supported, while unsupported file types, metadata
   operations and recursive deletion fail explicitly.
"""

from __future__ import annotations

import os

import pytest

from arbite import (
    application,
    coordination,
    fileclaims,
    filemutations,
    filereads,
    lifecycle,
    mutation,
)
from arbite.application import Actor
from arbite.errors import (
    ClaimConflict,
    CoordinationNotFound,
    FileBusy,
    StaleRead,
    UnsupportedCoordination,
)
from helpers import make_ticket

ABSENT = coordination.ABSENT


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _project(
    sink,
    arbite_dir,
    *,
    files=(("src/a.py", "alpha\nbeta\ngamma\n"),),
    ticket="tic-a1b2",
    worker="claude.opus.001",
    fault_injector=None,
):
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    for rel, text in files:
        (root / rel).write_text(text)
    sink.create(make_ticket(ticket))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get(ticket), worker_id=worker
    ).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(
        service, claims=claims, reads=reads, fault_injector=fault_injector
    )
    return mutations, reads, claims, attempt, root, service


def _fresh_token(reads, claims, attempt, path):
    """Claim `path` and take the fresh post-claim read that authorizes a mutation."""
    claims.claim(attempt, [path])
    receipt = reads.read(attempt, path)
    assert receipt.write_authorizing is True
    return receipt.read_token


def _other_attempt(sink, root, ticket="tic-z9y8", worker="other.agent.001"):
    """A second, independent active attempt in the same workspace."""
    if not sink.exists(ticket):
        sink.create(make_ticket(ticket))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get(ticket), worker_id=worker
    ).attempt
    return service, fileclaims.FileClaimService(service), attempt


def _intents(service, operation_id):
    with service.store.transaction(write=False) as tx:
        return list(tx.find("operation_intent", operation_id=operation_id))


class FaultOnce:
    """Raise `mutation.FaultInjected` the first time `phase` is reached."""

    def __init__(self, phase):
        self.phase = phase
        self.fired = False

    def __call__(self, phase):
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


class RacingClaimService(fileclaims.FileClaimService):
    """A claims service whose `claim` is immediately followed by an external writer.

    Used to prove a rename destination that appears *after* the ownership check is
    still caught, because the engine re-checks the destination version inside its
    operation lock rather than trusting the pre-claim observation.
    """

    def __init__(self, *args, racer_path, racer_bytes=b"raced\n", **kwargs):
        super().__init__(*args, **kwargs)
        self._racer_relative = racer_path
        self._racer_bytes = racer_bytes

    def claim(self, attempt, requested_paths, **kwargs):
        result = super().claim(attempt, requested_paths, **kwargs)
        for path in requested_paths:
            if str(path) == self._racer_relative:
                absolute = os.path.join(self.root, *self._racer_relative.split("/"))
                with open(absolute, "wb") as handle:
                    handle.write(self._racer_bytes)
        return result


# ---------------------------------------------------------------------------
# acceptance 1 & 2: ownership, versions, refusals leave both paths unchanged
# ---------------------------------------------------------------------------


def test_rename_owns_both_paths_and_records_both(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.rename(attempt, "src/a.py", "src/moved.py", read_token=token)

    assert result.ok and result.applied and result.kind == "rename"
    assert result.paths == ["src/a.py", "src/moved.py"]
    assert not (root / "src" / "a.py").exists()
    assert (root / "src" / "moved.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert result.before["src/a.py"] == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    assert result.after["src/a.py"] == ABSENT
    assert result.after["src/moved.py"] == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    # The moved bytes are durable evidence, and the receipt names both paths.
    assert service.store.read_artifact_bytes(result.after["src/moved.py"]) == (
        b"alpha\nbeta\ngamma\n"
    )
    receipt = service.read_record("operation_receipt", result.operation_id)
    assert receipt.paths == ["src/a.py", "src/moved.py"]
    # BOTH paths are owned by this attempt afterwards.
    assert claims.claim_for("src/a.py").observed_version == ABSENT
    destination_claim = claims.claim_for("src/moved.py")
    assert destination_claim.attempt_id == attempt.id
    assert destination_claim.observed_version == result.after["src/moved.py"]
    assert len(_intents(service, result.operation_id)) == 1


def test_rename_requires_a_source_token_and_leaves_both_paths_unchanged(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    with pytest.raises(StaleRead):
        mutations.rename(attempt, "src/a.py", "src/moved.py")

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert not (root / "src" / "moved.py").exists()


def test_rename_conflicting_destination_is_file_busy_and_changes_nothing(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(
        sink, arbite_dir, files=(("src/a.py", "alpha\n"), ("src/b.py", "beta\n"))
    )
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    _other_service, other_claims, other_attempt = _other_attempt(sink, root)
    other_claims.claim(other_attempt, ["src/b.py"])

    with pytest.raises(FileBusy):
        mutations.rename(attempt, "src/a.py", "src/b.py", read_token=token)

    # BOTH paths are exactly as they were, and the other attempt still owns b.py.
    assert (root / "src" / "a.py").read_bytes() == b"alpha\n"
    assert (root / "src" / "b.py").read_text() == "beta\n"
    assert claims.claim_for("src/b.py").attempt_id == other_attempt.id


def test_rename_onto_an_existing_destination_needs_an_explicit_version(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(
        sink, arbite_dir, files=(("src/a.py", "alpha\n"), ("src/b.py", "beta\n"))
    )
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(UnsupportedCoordination) as caught:
        mutations.rename(attempt, "src/a.py", "src/b.py", read_token=token)

    assert caught.value.details.get("reason") == filemutations.REASON_DESTINATION_EXISTS
    assert (root / "src" / "a.py").read_bytes() == b"alpha\n"
    assert (root / "src" / "b.py").read_bytes() == b"beta\n"
    # The refusal happens before any claim is acquired for the destination.
    assert claims.claim_for("src/b.py") is None


def test_rename_replacing_an_existing_destination_preserves_its_bytes(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(
        sink, arbite_dir, files=(("src/a.py", "alpha\n"), ("src/b.py", "beta\n"))
    )
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.rename(
        attempt,
        "src/a.py",
        "src/b.py",
        read_token=token,
        dest_expected=coordination.digest_of_text("beta\n"),
    )

    assert result.applied
    assert (root / "src" / "b.py").read_bytes() == b"alpha\n"
    assert not (root / "src" / "a.py").exists()
    # The bytes the rename replaced are evidence too, not silently discarded.
    assert service.store.read_artifact_bytes(coordination.digest_of_text("beta\n")) == b"beta\n"
    assert service.store.read_artifact_bytes(coordination.digest_of_text("alpha\n")) == b"alpha\n"


def test_rename_destination_version_mismatch_changes_nothing(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(
        sink, arbite_dir, files=(("src/a.py", "alpha\n"), ("src/b.py", "beta\n"))
    )
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(StaleRead):
        mutations.rename(
            attempt,
            "src/a.py",
            "src/b.py",
            read_token=token,
            dest_expected=coordination.digest_of_text("not beta\n"),
        )

    assert (root / "src" / "a.py").read_bytes() == b"alpha\n"
    assert (root / "src" / "b.py").read_bytes() == b"beta\n"


def test_rename_destination_race_is_caught_inside_the_operation_lock(sink, arbite_dir):
    """A destination that appears after ownership is taken is never overwritten."""
    _mutations, _reads, _claims, attempt, root, service = _project(sink, arbite_dir)
    # Rebuild the surface on a claims service whose `claim` is immediately followed
    # by an external writer, so the destination exists by the time the engine locks.
    racing = RacingClaimService(service, racer_path="src/moved.py", racer_bytes=b"raced\n")
    mutations = filemutations.FileMutationService(service, claims=racing)
    token = _fresh_token(mutations.reads, racing, attempt, "src/a.py")

    with pytest.raises(StaleRead):
        mutations.rename(attempt, "src/a.py", "src/moved.py", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert (root / "src" / "moved.py").read_bytes() == b"raced\n"


def test_rename_refuses_invalid_paths_and_special_targets(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(UnsupportedCoordination):
        mutations.rename(attempt, "src/a.py", "src/a.py", read_token=token)

    (root / "src" / "sub").mkdir()
    with pytest.raises(UnsupportedCoordination) as caught:
        mutations.rename(attempt, "src/a.py", "src/sub", read_token=token)
    assert caught.value.details.get("reason") == filemutations.REASON_DIRECTORY_DESTINATION

    with pytest.raises(UnsupportedCoordination):
        mutations.rename(attempt, "src/a.py", "../escape.py", read_token=token)

    (root / "src" / "link.py").symlink_to(root / "src" / "a.py")
    with pytest.raises(UnsupportedCoordination):
        mutations.rename(attempt, "src/link.py", "src/other.py", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert not (root / "src" / "other.py").exists()


def test_rename_of_a_missing_source_is_not_found(sink, arbite_dir):
    mutations, _reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    with pytest.raises(CoordinationNotFound):
        mutations.rename(attempt, "src/missing.py", "src/moved.py", read_token="unused")


# ---------------------------------------------------------------------------
# removal: evidence, token invalidation, explicit refusals
# ---------------------------------------------------------------------------


def test_remove_preserves_deleted_bytes_and_invalidates_the_token(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.remove(attempt, "src/a.py", read_token=token)

    assert result.ok and result.applied and result.kind == "remove"
    assert not (root / "src" / "a.py").exists()
    assert result.after["src/a.py"] == ABSENT
    assert service.store.read_artifact_bytes(
        coordination.digest_of_text("alpha\nbeta\ngamma\n")
    ) == b"alpha\nbeta\ngamma\n"
    assert claims.claim_for("src/a.py").observed_version == ABSENT
    assert len(_intents(service, result.operation_id)) == 1


def test_remove_requires_a_fresh_read_token(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    with pytest.raises(StaleRead):
        mutations.remove(attempt, "src/a.py")

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_removal_then_recreation_requires_a_new_read(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    mutations.remove(attempt, "src/a.py", read_token=token)

    # The claim records the absence, so recreating needs no token at all ...
    recreated = mutations.write(attempt, "src/a.py", b"reborn\n")
    assert recreated.applied and (root / "src" / "a.py").read_bytes() == b"reborn\n"
    # ... but the pre-removal token is dead: it cannot authorize another write.
    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/a.py", b"again\n", read_token=token)
    assert (root / "src" / "a.py").read_bytes() == b"reborn\n"
    # A fresh post-claim read does authorize it.
    fresh = reads.read(attempt, "src/a.py").read_token
    assert mutations.write(attempt, "src/a.py", b"again\n", read_token=fresh).applied
    assert service.store.read_artifact_bytes(coordination.digest_of_text("reborn\n")) == b"reborn\n"


def test_remove_keeps_binary_bytes_verbatim(sink, arbite_dir):
    payload = bytes(range(256)) * 8
    mutations, reads, claims, attempt, root, service = _project(
        sink, arbite_dir, files=(("src/bin.dat", "\x00placeholder\n"),)
    )
    (root / "src" / "bin.dat").write_bytes(payload)
    # A binary file has no obtainable read token (the read surface refuses it), so
    # the claim's recorded digest is the authorization; no token is passed.
    claims.claim(attempt, ["src/bin.dat"])
    with pytest.raises(UnsupportedCoordination):
        reads.read(attempt, "src/bin.dat")

    result = mutations.remove(attempt, "src/bin.dat")

    assert result.applied
    assert not (root / "src" / "bin.dat").exists()
    assert service.store.read_artifact_bytes(coordination.digest_of_bytes(payload)) == payload
    assert claims.claim_for("src/bin.dat").observed_version == ABSENT


def test_remove_binary_without_ownership_is_still_refused(sink, arbite_dir):
    payload = b"\x00\x01\xff\xfebinary"
    mutations, _reads, _claims, attempt, root, _service = _project(
        sink, arbite_dir, files=(("src/bin.dat", "\x00placeholder\n"),)
    )
    (root / "src" / "bin.dat").write_bytes(payload)

    with pytest.raises(ClaimConflict):
        mutations.remove(attempt, "src/bin.dat")

    assert (root / "src" / "bin.dat").read_bytes() == payload


def test_remove_refuses_a_directory_without_recursive_deletion(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    (root / "src" / "sub").mkdir()
    (root / "src" / "sub" / "inner.py").write_text("inner\n")
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(UnsupportedCoordination) as caught:
        mutations.remove(attempt, "src/sub", read_token=token)

    assert caught.value.details.get("reason") == filemutations.REASON_RECURSIVE_DELETE
    assert (root / "src" / "sub").is_dir()
    assert (root / "src" / "sub" / "inner.py").read_text() == "inner\n"


def test_remove_refuses_special_files_and_hard_links(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    os.mkfifo(root / "src" / "pipe")

    with pytest.raises(UnsupportedCoordination):
        mutations.remove(attempt, "src/pipe", read_token=token)

    os.link(root / "src" / "a.py", root / "src" / "alias.py")
    with pytest.raises(UnsupportedCoordination):
        mutations.remove(attempt, "src/alias.py", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_remove_without_a_claim_is_refused(sink, arbite_dir):
    mutations, reads, _claims, attempt, root, _service = _project(sink, arbite_dir)
    observation = reads.read(attempt, "src/a.py")  # a read, but not a claimed one

    with pytest.raises(ClaimConflict):
        mutations.remove(attempt, "src/a.py", read_token=observation.read_token)

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_remove_of_a_missing_path_is_not_found(sink, arbite_dir):
    mutations, reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    with pytest.raises(CoordinationNotFound):
        mutations.remove(attempt, "src/missing.py", read_token=token)


# ---------------------------------------------------------------------------
# acceptance 3: interrupted rename recovers honestly
# ---------------------------------------------------------------------------


def test_rename_interrupted_between_paths_recovers_through_the_service(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(
        sink,
        arbite_dir,
        fault_injector=FaultOnce(mutation.FAULT_AFTER_DEST_COMMITTED),
    )
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(mutation.FaultInjected):
        mutations.rename(attempt, "src/a.py", "src/moved.py", read_token=token)

    # The honest interrupted state: destination committed, source not yet removed.
    assert (root / "src" / "moved.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert (root / "src" / "a.py").exists()
    assert mutations.engine.pending_intents(), "the intent must survive for recovery"

    # The next relevant operation reconciles it, without a daemon or a shell move.
    recovered = mutations.engine.reconcile()

    assert [report.state for report in recovered] == ["applied"]
    assert not (root / "src" / "a.py").exists()
    assert (root / "src" / "moved.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    receipt = service.read_record("operation_receipt", recovered[0].operation_id)
    assert receipt.result == "ok"
    assert receipt.paths == ["src/a.py", "src/moved.py"]


# ---------------------------------------------------------------------------
# acceptance 4: safe parent creation and explicit unsupported refusals
# ---------------------------------------------------------------------------


def test_create_creates_safe_missing_parents_and_claims_the_path(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)

    result = mutations.write(attempt, "deep/nested/new.txt", b"hello\n")

    assert result.applied
    assert result.created_parents == ["deep", "deep/nested"]
    assert (root / "deep" / "nested" / "new.txt").read_bytes() == b"hello\n"
    claim = claims.claim_for("deep/nested/new.txt")
    assert claim is not None and claim.generation == 1
    assert claim.observed_version == result.after["deep/nested/new.txt"]


def test_rename_creates_safe_missing_destination_parents(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.rename(
        attempt, "src/a.py", "deep/dir/moved.py", read_token=token
    )

    assert result.applied
    assert result.created_parents == ["deep", "deep/dir"]
    assert (root / "deep" / "dir" / "moved.py").read_bytes() == b"alpha\nbeta\ngamma\n"
    assert not (root / "src" / "a.py").exists()
    assert claims.claim_for("deep/dir/moved.py") is not None


def test_parent_creation_refuses_escape_protected_symlink_and_file_components(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)

    with pytest.raises(UnsupportedCoordination):
        mutations.write(attempt, ".arbite/evil.txt", b"x")
    with pytest.raises(UnsupportedCoordination):
        mutations.write(attempt, "../outside.txt", b"x")

    (root / "linkdir").symlink_to(root / "src", target_is_directory=True)
    with pytest.raises(UnsupportedCoordination):
        mutations.write(attempt, "linkdir/new.txt", b"x")

    (root / "plain").write_text("not a directory\n")
    with pytest.raises(UnsupportedCoordination):
        mutations.write(attempt, "plain/child.txt", b"x")

    assert not (root / "src" / "new.txt").exists()
    assert not os.path.exists(root / "plain" / "child.txt")
    assert claims.claim_for("src/new.txt") is None


def test_metadata_operations_are_not_exposed_by_the_file_surface():
    """There is no chmod/chown/touch-style proxy operation: the file surface is
    discovery, reads, ownership and byte mutations only (C08 adds remove/rename)."""
    from arbite import cli

    parser, _subparsers = cli.build_parser()
    for argv in (
        ["file", "chmod", "src/a.py"],
        ["file", "chown", "src/a.py", "root"],
        ["file", "touch", "src/a.py"],
        ["file", "remove", "src", "--recursive"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
