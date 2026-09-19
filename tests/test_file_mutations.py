"""Version-checked whole-file writes and exact targeted edits (planning key C07).

Every behavioural test runs against both sinks (the `sink` fixture), so the claim
that "the file sink and the SQLite sink behave equivalently" is checked here for
the mutation surface too. The suite is organised around the four acceptance
criteria: token reuse/drift, creation/binary/permissions, exact-edit selection
failures, and token invalidation with returned receipt/version data.
"""

from __future__ import annotations

import multiprocessing
import os
import stat
from pathlib import Path

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
    EditSelectionError,
    StaleRead,
    UnsupportedCoordination,
)
from arbite.sinks import SinkSpec, build_sink
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
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)
    return mutations, reads, claims, attempt, root, service


def _fresh_token(reads, claims, attempt, path):
    """Claim `path` and take the fresh post-claim read that authorizes a write."""
    claims.claim(attempt, [path])
    receipt = reads.read(attempt, path)
    assert receipt.write_authorizing is True
    return receipt.read_token


def _intents(service, operation_id):
    with service.store.transaction(write=False) as tx:
        return list(tx.find("operation_intent", operation_id=operation_id))


# ---------------------------------------------------------------------------
# acceptance 1: one token, competing/drifted operations
# ---------------------------------------------------------------------------


def test_write_requires_a_token_for_an_existing_file(sink, arbite_dir):
    mutations, _reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/a.py", b"no token\n")

    assert (arbite_dir.parent / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_write_with_a_fresh_token_replaces_and_returns_a_receipt(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.write(attempt, "src/a.py", b"new\n", read_token=token)

    assert result.ok and result.applied and result.kind == "write"
    assert result.before["src/a.py"] == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    assert result.after["src/a.py"] == coordination.digest_of_text("new\n")
    assert (root / "src" / "a.py").read_bytes() == b"new\n"
    # The receipt and the content-addressed evidence are durable.
    receipt = service.read_record("operation_receipt", result.operation_id)
    assert receipt is not None and receipt.result == "ok"
    assert service.store.read_artifact_bytes(result.after["src/a.py"]) == b"new\n"
    # The old token cannot be used again.
    assert claims.claim_for("src/a.py").observed_version == result.after["src/a.py"]
    assert len(_intents(service, result.operation_id)) == 1


def test_one_token_cannot_authorize_two_writes(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    first = mutations.write(attempt, "src/a.py", b"first\n", read_token=token)
    assert first.applied

    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/a.py", b"second\n", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"first\n"


def test_outside_write_drift_refuses_and_changes_nothing(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    (root / "src" / "a.py").write_bytes(b"external\n")

    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/a.py", b"proxy\n", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"external\n"
    assert mutations.engine.pending_intents() == []
    assert service.read_record("operation_receipt", "op-does-not-exist") is None


def test_edit_drift_since_read_refuses_and_changes_nothing(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    (root / "src" / "a.py").write_bytes(b"external\n")

    with pytest.raises(StaleRead):
        mutations.edit(
            attempt,
            "src/a.py",
            [filemutations.Edit(old="alpha", new="ALPHA")],
            read_token=token,
        )

    assert (root / "src" / "a.py").read_bytes() == b"external\n"


# ---------------------------------------------------------------------------
# acceptance 2: creation, binary payloads, permissions
# ---------------------------------------------------------------------------


def test_create_requires_an_absent_path_claim(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/new.py"])

    result = mutations.write(attempt, "src/new.py", b"created\n")

    assert result.applied
    assert result.before["src/new.py"] == ABSENT
    assert result.after["src/new.py"] == coordination.digest_of_text("created\n")
    assert (root / "src" / "new.py").read_bytes() == b"created\n"


def test_create_without_a_claim_is_refused(sink, arbite_dir):
    mutations, _reads, _claims, attempt, root, _service = _project(sink, arbite_dir)

    with pytest.raises(ClaimConflict):
        mutations.write(attempt, "src/unclaimed.py", b"nope\n")

    assert not (root / "src" / "unclaimed.py").exists()


def test_create_rejects_a_read_token(sink, arbite_dir):
    mutations, reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    claims.claim(attempt, ["src/new.py"])

    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/new.py", b"nope\n", read_token=token)


def test_create_refused_if_the_path_appeared_since_the_claim(sink, arbite_dir):
    mutations, _reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/new.py"])
    (root / "src" / "new.py").write_bytes(b"someone else\n")

    with pytest.raises(StaleRead):
        mutations.write(attempt, "src/new.py", b"mine\n")

    assert (root / "src" / "new.py").read_bytes() == b"someone else\n"


def test_binary_whole_file_write_round_trips_without_textual_processing(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    payload = b"\x00\x01\xff\xfebinary\r\nbytes"

    result = mutations.write(attempt, "src/a.py", payload, read_token=token)

    assert result.applied
    assert (root / "src" / "a.py").read_bytes() == payload
    assert result.after["src/a.py"] == coordination.digest_of_bytes(payload)
    # A binary file is a valid whole-file write target but not a text read target.
    with pytest.raises(UnsupportedCoordination):
        reads.read(attempt, "src/a.py")


def test_large_whole_file_write_round_trips(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    payload = (b"0123456789abcdef" * 20000) + b"\n"

    result = mutations.write(attempt, "src/a.py", payload, read_token=token)

    assert result.applied
    assert (root / "src" / "a.py").read_bytes() == payload


def test_write_preserves_existing_permissions(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    target = root / "src" / "a.py"
    os.chmod(target, 0o640)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    mutations.write(attempt, "src/a.py", b"mode kept\n", read_token=token)

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


# ---------------------------------------------------------------------------
# acceptance 3: exact edit batches, whole-batch failure
# ---------------------------------------------------------------------------


def test_edit_applies_an_exact_batch_once_and_invalidates_the_token(sink, arbite_dir):
    mutations, reads, claims, attempt, root, service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.edit(
        attempt,
        "src/a.py",
        [
            filemutations.Edit(old="alpha", new="ALPHA"),
            filemutations.Edit(old="gamma", new="GAMMA"),
        ],
        read_token=token,
    )

    assert result.applied and result.kind == "edit"
    assert (root / "src" / "a.py").read_bytes() == b"ALPHA\nbeta\nGAMMA\n"
    assert result.after["src/a.py"] == coordination.digest_of_text("ALPHA\nbeta\nGAMMA\n")
    assert service.read_record("operation_receipt", result.operation_id).result == "ok"

    with pytest.raises(StaleRead):
        mutations.edit(
            attempt,
            "src/a.py",
            [filemutations.Edit(old="beta", new="BETA")],
            read_token=token,
        )


def test_edit_ambiguity_absent_and_overlap_each_reject_the_whole_batch(sink, arbite_dir):
    files = (("src/a.py", "abcabc\n"), ("src/b.py", "alpha beta\n"))
    mutations, reads, claims, attempt, root, _service = _project(
        sink, arbite_dir, files=files
    )

    # Ambiguous: 'unique' (the default) with two matches.
    token = _fresh_token(reads, claims, attempt, "src/a.py")
    with pytest.raises(EditSelectionError) as ambiguous:
        mutations.edit(
            attempt,
            "src/a.py",
            [filemutations.Edit(old="abc", new="X")],
            read_token=token,
        )
    assert ambiguous.value.details["reason"] == filemutations.REASON_EDIT_AMBIGUOUS
    assert (root / "src" / "a.py").read_bytes() == b"abcabc\n"

    # Absent: no match at all.
    with pytest.raises(EditSelectionError) as absent:
        mutations.edit(
            attempt,
            "src/a.py",
            [filemutations.Edit(old="zzz", new="X")],
            read_token=token,
        )
    assert absent.value.details["reason"] == filemutations.REASON_EDIT_ABSENT

    # Overlapping selections are refused as a batch, even though each matches once.
    token2 = _fresh_token(reads, claims, attempt, "src/b.py")
    with pytest.raises(EditSelectionError) as overlap:
        mutations.edit(
            attempt,
            "src/b.py",
            [
                filemutations.Edit(old="alpha", new="X"),
                filemutations.Edit(old="lpha ", new="Y"),
            ],
            read_token=token2,
        )
    assert overlap.value.details["reason"] == filemutations.REASON_EDIT_OVERLAPPING
    assert (root / "src" / "b.py").read_bytes() == b"alpha beta\n"


def test_edit_whole_batch_fails_when_any_selection_is_invalid(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(EditSelectionError):
        mutations.edit(
            attempt,
            "src/a.py",
            [
                filemutations.Edit(old="alpha", new="ALPHA"),  # valid
                filemutations.Edit(old="no-such-text", new="X"),  # absent
            ],
            read_token=token,
        )

    # No partial application: the valid edit did not land either.
    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_edit_occurrence_rules():
    text = b"x x x\n"
    first = filemutations.apply_edits(
        text, [filemutations.Edit(old="x", new="y", occurrence="first")]
    )
    last = filemutations.apply_edits(
        text, [filemutations.Edit(old="x", new="y", occurrence="last")]
    )
    second = filemutations.apply_edits(
        text, [filemutations.Edit(old="x", new="y", occurrence="nth", index=2)]
    )
    every = filemutations.apply_edits(
        text, [filemutations.Edit(old="x", new="z", occurrence="all")]
    )
    assert first == b"y x x\n"
    assert last == b"x x y\n"
    assert second == b"x y x\n"
    assert every == b"z z z\n"


def test_edit_requires_a_token_and_an_existing_file(sink, arbite_dir):
    mutations, reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])

    with pytest.raises(StaleRead):
        mutations.edit(
            attempt, "src/a.py", [filemutations.Edit(old="alpha", new="x")]
        )

    claims.claim(attempt, ["src/new.py"])
    token = reads.read(attempt, "src/a.py").read_token
    with pytest.raises(CoordinationNotFound):
        mutations.edit(
            attempt, "src/new.py", [filemutations.Edit(old="x", new="y")], read_token=token
        )


def test_edit_is_not_fuzzy(sink, arbite_dir):
    """Whitespace/case differences do not match: the selection must be exact."""
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    with pytest.raises(EditSelectionError):
        mutations.edit(
            attempt,
            "src/a.py",
            [filemutations.Edit(old="Alpha", new="A")],
            read_token=token,
        )

    assert (root / "src" / "a.py").read_bytes() == b"alpha\nbeta\ngamma\n"


# ---------------------------------------------------------------------------
# acceptance 4: untouched bytes / newline conventions
# ---------------------------------------------------------------------------


def test_edit_preserves_crlf_and_untouched_unicode_bytes(sink, arbite_dir):
    original = "héllo \U0001f680 first\r\nsecond line\r\nthird \u00fc\r\n".encode("utf-8")
    files = (("src/a.py", original.decode("utf-8")),)
    mutations, reads, claims, attempt, root, _service = _project(
        sink, arbite_dir, files=files
    )
    path = root / "src" / "a.py"
    path.write_bytes(original)  # exact CRLF bytes (write_text would be fine too)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    result = mutations.edit(
        attempt,
        "src/a.py",
        [filemutations.Edit(old="second line\n", new="SECOND line\n")],
        read_token=token,
    )

    expected = "héllo \U0001f680 first\r\nSECOND line\r\nthird \u00fc\r\n".encode("utf-8")
    assert result.applied
    assert path.read_bytes() == expected
    # Every untouched region is byte-identical, including its CRLF.
    assert path.read_bytes().startswith("héllo \U0001f680 first\r\n".encode("utf-8"))
    assert path.read_bytes().endswith("third \u00fc\r\n".encode("utf-8"))


def test_write_preserves_crlf_payload_bytes(sink, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    mutations.write(attempt, "src/a.py", b"a\r\nb\r\n", read_token=token)

    assert (root / "src" / "a.py").read_bytes() == b"a\r\nb\r\n"


# ---------------------------------------------------------------------------
# the edit payload grammar (parse-level, sink-independent)
# ---------------------------------------------------------------------------


def test_parse_edits_accepts_list_and_object_and_rejects_bad_shapes():
    listed = filemutations.parse_edits([{"old": "a", "new": "b"}])
    assert listed == [filemutations.Edit(old="a", new="b", occurrence="unique", index=None)]

    wrapped = filemutations.parse_edits_json('{"edits": [{"old": "a", "new": "b", "occurrence": "nth", "index": 2}]}')
    assert wrapped[0].occurrence == "nth" and wrapped[0].index == 2

    with pytest.raises(UnsupportedCoordination):
        filemutations.parse_edits([])
    with pytest.raises(UnsupportedCoordination):
        filemutations.parse_edits([{"old": "a"}])
    with pytest.raises(UnsupportedCoordination):
        filemutations.parse_edits([{"old": "a", "new": "b", "occurrence": "sometimes"}])
    with pytest.raises(UnsupportedCoordination):
        filemutations.parse_edits([{"old": "a", "new": "b", "occurrence": "nth"}])
    with pytest.raises(UnsupportedCoordination):
        filemutations.parse_edits_json("{not json}")


def test_apply_edits_rejects_binary_and_utf16_input():
    with pytest.raises(UnsupportedCoordination):
        filemutations.apply_edits(b"\x00\x01\x02", [filemutations.Edit(old="a", new="b")])
    with pytest.raises(UnsupportedCoordination):
        filemutations.apply_edits(
            "hi\n".encode("utf-16"), [filemutations.Edit(old="hi", new="ho")]
        )


# ---------------------------------------------------------------------------
# one token across processes: exactly one winner (both sinks)
# ---------------------------------------------------------------------------


def _race_write(kind, arbite_dir, root, attempt_id, token, path, content, results):
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        service = application.coordination_service_for(
            sink, root=str(root), actor=Actor("racer")
        )
        with service.store.transaction(write=False) as tx:
            attempt = tx.get("work_attempt", attempt_id)
        mutations = filemutations.FileMutationService(service)
        result = mutations.write(attempt, path, content, read_token=token)
        results.put("ok" if result.applied else "replay")
    except StaleRead:
        results.put("stale_read")
    except Exception as error:  # pragma: no cover - surfaced through the queue
        results.put(f"error:{type(error).__name__}:{error}")


def test_two_processes_one_token_cannot_both_succeed(sink, kind, arbite_dir):
    mutations, reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    token = _fresh_token(reads, claims, attempt, "src/a.py")

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    contents = [b"winner-one\n", b"winner-two\n"]
    processes = [
        context.Process(
            target=_race_write,
            args=(
                kind,
                str(arbite_dir),
                str(root),
                attempt.id,
                token,
                "src/a.py",
                content,
                results,
            ),
        )
        for content in contents
    ]
    for process in processes:
        process.start()
    outcomes = sorted(results.get(timeout=60) for _ in processes)
    for process in processes:
        process.join(60)
        assert process.exitcode == 0

    assert outcomes == ["ok", "stale_read"], outcomes
    final = (root / "src" / "a.py").read_bytes()
    assert final in contents
    assert claims.claim_for("src/a.py").observed_version == coordination.digest_of_bytes(final)
