"""The coordination domain models: ids, timestamps, digests, paths, records.

These tests are deliberately about the *contract* the planning documents state,
not about a storage implementation: an opaque id is opaque, a coordination
timestamp is UTC while a legacy ticket timestamp is not, a digest covers the
whole file even for a ranged read, a path never escapes the workspace root, a
terminal attempt cannot come back, two active claims cannot share a path, and
the JSON error vocabulary is a stable set of strings.

Nothing here touches a sink, which is the point: these records are storage
neutral by construction, so the same tests apply to every sink that later stores
them.
"""

from __future__ import annotations

import pytest

from arbite import coordination as c
from arbite import schema
from arbite.errors import (
    AttemptInactive,
    ClaimConflict,
    ErrorCode,
    InvalidRecord,
    StaleRead,
    StoreBindingConflict,
    UnsupportedCoordination,
)

NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T00:00:05Z"
DIGEST_A = c.digest_of_text("alpha")
DIGEST_B = c.digest_of_text("beta")


# --- fixtures/factories ----------------------------------------------------


def workspace(root="/tmp/ws", **overrides):
    data = dict(
        id=c.new_record_id("workspace"),
        root=root,
        created=NOW,
        updated=NOW,
    )
    data.update(overrides)
    return c.Workspace(**data)


def binding(workspace_id, sink_kind="file", location="/tmp/ws/.arbite", **overrides):
    data = dict(
        id=c.new_record_id("store_binding"),
        workspace_id=workspace_id,
        sink_kind=sink_kind,
        location=location,
        bound_at=NOW,
    )
    data.update(overrides)
    return c.StoreBinding(**data)


def attempt(workspace_id, **overrides):
    data = dict(
        id=c.new_record_id("work_attempt"),
        ticket_id="tic-a1b2",
        worker_id="claude.opus.001",
        workspace_id=workspace_id,
        generation=1,
        started=NOW,
        last_activity=NOW,
    )
    data.update(overrides)
    return c.WorkAttempt(**data)


def claim(workspace_id, attempt_id, path="src/a.py", **overrides):
    data = dict(
        id=c.new_record_id("file_claim"),
        workspace_id=workspace_id,
        path=path,
        ticket_id="tic-a1b2",
        attempt_id=attempt_id,
        generation=1,
        acquired=NOW,
        observed_version=DIGEST_A,
    )
    data.update(overrides)
    return c.FileClaim(**data)


def observation(operation_id="op-unused", path="src/a.py", **overrides):
    data = dict(
        id=c.new_record_id("read_observation"),
        operation_id=operation_id,
        path=path,
        digest=DIGEST_A,
        observed_at=NOW,
    )
    data.update(overrides)
    return c.ReadObservation(**data)


def receipt(attempt_id="att-x", ticket_id="tic-a1b2", kind_="write", **overrides):
    data = dict(
        id=c.new_operation_id(),
        attempt_id=attempt_id,
        ticket_id=ticket_id,
        actor="claude.opus.001",
        kind_=kind_,
        timestamp=NOW,
    )
    data.update(overrides)
    return c.OperationReceipt(**data)


def event(**overrides):
    data = dict(
        id=c.new_record_id("event"),
        kind_="operation_recorded",
        category="operation",
        timestamp=NOW,
    )
    data.update(overrides)
    return c.Event(**data)


# --- opaque ids ------------------------------------------------------------


def test_opaque_ids_are_short_prefixed_and_unique():
    ids = {c.gen_opaque_id("clm") for _ in range(200)}
    assert len(ids) == 200
    for value in ids:
        assert c.OPAQUE_ID_PATTERN.match(value)
        assert c.is_opaque_id(value, "clm")


def test_new_record_id_uses_the_kind_prefix_and_rejects_unknown_kinds():
    assert c.new_record_id("file_claim").startswith("clm-")
    assert c.new_record_id("operation_receipt").startswith("op-")
    assert c.new_operation_id().startswith("op-")
    with pytest.raises(UnsupportedCoordination):
        c.new_record_id("not_a_record")


def test_gen_opaque_id_refuses_a_bad_prefix():
    with pytest.raises(UnsupportedCoordination):
        c.gen_opaque_id("TOOLONG")


def test_is_opaque_id_of_the_wrong_prefix_is_false():
    value = c.gen_opaque_id("att")
    assert c.is_opaque_id(value, "att")
    assert not c.is_opaque_id(value, "clm")
    assert not c.is_opaque_id("tic-a1b2")


# --- timestamps: new records are UTC, legacy tickets are untouched ---------


def test_utc_now_is_a_utc_timestamp():
    value = c.utc_now()
    assert c.is_utc_timestamp(value)
    assert value.endswith("Z")
    assert c.parse_utc(value).tzinfo is not None


def test_parse_utc_rejects_legacy_and_local_forms():
    for bad in ("2026-01-01", "2026-01-01T00:00:00", "not-a-time", None):
        with pytest.raises(InvalidRecord):
            c.parse_utc(bad)


def test_legacy_ticket_timestamps_are_deliberately_different():
    """The compatibility rule, pinned as a test: a coordination validator must
    not accept the legacy ticket form, and the legacy schema must not start
    emitting the coordination form."""
    assert schema.DATE_PATTERN.match("2026-01-01")
    assert schema.DATE_PATTERN.match("2026-01-01T12:34:56")
    assert not schema.DATE_PATTERN.match("2026-01-01T12:34:56Z")
    assert not c.is_utc_timestamp("2026-01-01T12:34:56")
    assert not schema.DATE_PATTERN.match(c.utc_now())
    assert not schema.now().endswith("Z")


# --- digests ---------------------------------------------------------------


def test_digest_functions_and_predicates():
    import hashlib

    assert c.digest_of_bytes(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert c.digest_of_text("é") == "sha256:" + hashlib.sha256("é".encode("utf-8")).hexdigest()
    assert c.is_digest(DIGEST_A)
    assert not c.is_digest("sha256:xyz")
    assert not c.is_digest(c.ABSENT)
    assert c.is_digest_or_absent(c.ABSENT)
    assert c.is_digest_or_absent(DIGEST_A)
    assert not c.is_digest_or_absent("changed")


# --- workspace-relative path policy ----------------------------------------


def test_canonical_relative_path_normalises_separators_and_dots():
    assert c.canonical_relative_path("./src/a.py") == "src/a.py"
    assert c.canonical_relative_path("src\\nested\\a.py") == "src/nested/a.py"
    assert c.canonical_relative_path("src/./a.py") == "src/a.py"


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../escape", "a/../../b", "~/.ssh/id", "C:/x"])
def test_canonical_relative_path_rejects_escapes(bad):
    with pytest.raises(UnsupportedCoordination):
        c.canonical_relative_path(bad)


def test_protected_paths_are_recognised():
    assert c.is_protected_path(".arbite/arbite.db")
    assert c.is_protected_path(".git/config")
    assert not c.is_protected_path("src/.arbite.py")


# --- JSON result/error vocabulary -------------------------------------------


def test_json_result_vocabulary_is_stable():
    ok = c.ok_result({"ticket": "tic-a1b2"})
    assert ok["ok"] is True and ok["code"] is None
    assert ok["schema_version"] == c.CONTRACT_VERSION

    err = c.error_result("stale_read", "moved on", retryable=True)
    assert err["ok"] is False
    assert err["code"] == "stale_read"
    assert err["retryable"] is True
    assert err["bytes_may_have_changed"] is False
    assert set(ok) == set(err)


def test_typed_errors_carry_the_documented_code_and_payload():
    cases = {
        StaleRead("x"): ErrorCode.STALE_READ,
        AttemptInactive("x"): ErrorCode.ATTEMPT_INACTIVE,
        ClaimConflict("x"): ErrorCode.CLAIM_CONFLICT,
        StoreBindingConflict("x"): ErrorCode.STORE_BINDING_CONFLICT,
        InvalidRecord("x"): ErrorCode.INVALID_RECORD,
    }
    for error, code in cases.items():
        assert error.error_code == code
        payload = error.to_result()
        assert payload["ok"] is False
        assert payload["code"] == code
    assert StaleRead("x").retryable is True
    assert InvalidRecord("x").retryable is False


def test_attribution_notice_separates_attribution_from_authentication():
    assert "attribution" in c.ATTRIBUTION_NOTICE.lower()
    assert "authentication" in c.ATTRIBUTION_NOTICE.lower()


# --- records round-trip and validate ---------------------------------------


def test_every_record_round_trips_through_json():
    ws = workspace()
    ws.bind(binding(ws.id))
    att = attempt(ws.id)
    clm = claim(ws.id, att.id, observed_version=c.ABSENT)
    obs = observation(attempt_id=att.id, claim_generation=clm.generation)
    art = c.Artifact(
        id=c.new_record_id("artifact"),
        digest=DIGEST_A,
        size=5,
        created=NOW,
        location=f"artifact:{DIGEST_A}",
    )
    rec = receipt(
        attempt_id=att.id,
        paths=["src/a.py"],
        before={"src/a.py": c.ABSENT},
        after={"src/a.py": DIGEST_A},
        artifact_refs=[art.id],
        claim_generation=clm.generation,
    )
    evt = event(cursor=7, operation_id=rec.id)
    rpt = c.RecoveryReport(
        workspace_id=ws.id,
        operation_id=rec.id,
        state="pending",
        observed_at=NOW,
        paths=["src/a.py"],
    )

    for record in (ws, ws.store_binding, att, clm, obs, art, rec, evt, rpt):
        rebuilt = c.record_from_dict(record.to_dict())
        assert rebuilt == record, record.kind
        assert rebuilt.kind == record.kind
        assert record.validate() == [], (record.kind, record.validate())

    # The operation's own kind is exposed under a name that does not collide
    # with the record tag.
    payload = rec.to_dict()
    assert payload["kind"] == "operation_receipt"
    assert payload["operation_kind"] == "write"
    assert "kind_" not in payload


def test_record_from_dict_rejects_unknown_and_missing_kinds():
    with pytest.raises(InvalidRecord):
        c.record_from_dict({"kind": "no_such_record"})
    with pytest.raises(InvalidRecord):
        c.record_from_dict({})
    with pytest.raises(InvalidRecord):
        c.record_from_dict("not a mapping")


# --- per-record invariants --------------------------------------------------


def test_workspace_binding_accepts_one_authoritative_store():
    ws = workspace()
    first = ws.bind(binding(ws.id), timestamp=NOW)
    assert ws.is_bound and ws.store_binding is first

    same = ws.bind(binding(ws.id), timestamp=LATER)  # identical sink+location
    assert same is first
    assert ws.updated == NOW  # idempotent bind does not touch the workspace

    with pytest.raises(StoreBindingConflict):
        ws.bind(binding(ws.id, sink_kind="sqlite"), timestamp=LATER)

    other = workspace()
    with pytest.raises(StoreBindingConflict):
        other.bind(binding(ws.id))


def test_workspace_with_a_mismatched_nested_binding_is_invalid():
    ws = workspace()
    ws.store_binding = binding(workspace("other").id)
    problems = ws.validate()
    assert any("workspace_id" in p for p in problems)


def test_active_attempt_must_not_have_an_end_and_terminal_must():
    assert attempt("ws-x").validate() == []
    ended_active = attempt("ws-x", ended=NOW)
    assert any("active" in p for p in ended_active.validate())

    released = attempt("ws-x")
    released.release(LATER)
    assert released.validate() == []
    assert released.state == "released" and released.ended == LATER

    terminal_without_end = attempt("ws-x", state="released")
    assert any("ended" in p for p in terminal_without_end.validate())


def test_terminal_attempt_cannot_be_revived():
    att = attempt("ws-x")
    att.release(LATER)
    with pytest.raises(InvalidRecord):
        att.touch(LATER)
    with pytest.raises(InvalidRecord):
        att.finish(LATER)


def test_attempt_generation_must_be_a_positive_integer():
    assert any("generation" in p for p in attempt("ws-x", generation=0).validate())
    assert any("generation" in p for p in attempt("ws-x", generation="1").validate())


def test_released_claim_keeps_its_history_and_a_new_claim_is_a_new_token():
    clm = claim("ws-x", "att-x")
    clm.release(LATER)
    assert clm.validate() == []
    assert clm.state == "released" and clm.released == LATER
    with pytest.raises(InvalidRecord):
        clm.release(LATER)  # never reactivated
    assert any("released" in p for p in claim("ws-x", "att-x", state="released").validate())


def test_claim_path_must_be_canonical_and_in_root():
    assert any("canonical" in p for p in claim("ws-x", "att-x", path="./src/a.py").validate())
    assert any("escapes" in p for p in claim("ws-x", "att-x", path="../a.py").validate())
    assert any("protected" in p for p in claim("ws-x", "att-x", path=".arbite/x").validate())


def test_claim_observed_version_must_be_a_digest_or_absent():
    assert claim("ws-x", "att-x", observed_version=c.ABSENT).validate() == []
    assert any("observed_version" in p for p in claim("ws-x", "att-x", observed_version="?").validate())


def test_read_observation_requires_a_whole_file_digest_and_a_sane_range():
    assert observation(line_range=[1, 3]).validate() == []
    assert any("line_range" in p for p in observation(line_range=[3, 1]).validate())
    assert any("line_range" in p for p in observation(line_range=[0, 3]).validate())
    assert any("digest" in p for p in observation(digest="nope").validate())


def test_read_observation_does_not_authorize_writes_until_marked():
    obs = observation()
    assert obs.write_authorizing is False
    obs.authorize()
    assert obs.write_authorizing is True


def test_artifact_size_and_digest_are_validated():
    good = c.Artifact(id=c.new_record_id("artifact"), digest=DIGEST_A, size=0, created=NOW, location="x")
    assert good.validate() == []
    assert any("size" in p for p in c.Artifact(
        id=c.new_record_id("artifact"), digest=DIGEST_A, size=-1, created=NOW, location="x"
    ).validate())


def test_error_receipt_requires_an_error_code():
    bad = receipt(result="error")
    assert any("error" in p for p in bad.validate())
    good = receipt(result="error", error={"code": "stale_read", "message": "x"})
    assert good.validate() == []


def test_event_vocabulary_and_cursor():
    assert event(cursor=None).validate() == []
    assert event(cursor=0).validate() == []
    assert any("cursor" in p for p in event(cursor=-1).validate())
    assert any("event kind" in p for p in event(kind_="nope").validate())
    assert any("category" in p for p in event(category="nope").validate())


def test_recovery_report_states_are_from_the_vocabulary():
    good = c.RecoveryReport(workspace_id="ws-x", operation_id="op-x", state="drifted", observed_at=NOW)
    assert good.validate() == []
    bad = c.RecoveryReport(workspace_id="ws-x", operation_id="op-x", state="assumed", observed_at=NOW)
    assert any("state" in p for p in bad.validate())


# --- collection invariants --------------------------------------------------


def test_collection_rejects_two_active_attempts_for_one_ticket():
    first = attempt("ws-x", ticket_id="tic-a1b2")
    second = attempt("ws-x", ticket_id="tic-a1b2")
    problems = c.validate_collection([first, second])
    assert any("two active attempts" in p for p in problems)


def test_collection_rejects_two_active_claims_for_one_path():
    first = claim("ws-x", "att-1")
    second = claim("ws-x", "att-2")
    problems = c.validate_collection([first, second])
    assert any("exclusively" in p or "active" in p for p in problems)


def test_collection_rejects_duplicate_ids_and_cursors():
    a = event(cursor=1)
    b = event(cursor=1)
    problems = c.validate_collection([a, b])
    assert any("cursor" in p for p in problems)

    same_id = [claim("ws-x", "att-1", id="clm-0123456789abcdef"),
               claim("ws-y", "att-2", id="clm-0123456789abcdef")]
    assert any("appears 2 times" in p for p in c.validate_collection(same_id))


def test_validate_records_prefixes_the_record_identity():
    problems = c.validate_records([attempt("ws-x", generation=0)])
    assert problems and all("work_attempt" in p for p in problems)


def test_a_clean_collection_validates():
    ws = workspace()
    ws.bind(binding(ws.id))
    att = attempt(ws.id)
    clm = claim(ws.id, att.id)
    assert c.validate_records([ws, att, clm]) == []
