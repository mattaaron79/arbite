"""The coordination records: their shape, their invariants and their serialisation.

These are the fields every later slice reads, so the checks here are the ones the
handoff states: opaque ids, UTC times, one active attempt per ticket's worth of
generation data, a claim path that is canonical and relative, a read observation
that cannot authorise a write it did not earn, and an explicit marker for "the path
did not exist". Legacy tickets and their timestamps are covered too: this module
must not have changed what the ticket schema reads.
"""

from __future__ import annotations

import pytest

from arbite import schema
from arbite.coordination import records as coordination_records
from arbite.errors import RecordError


def make_attempt(**overrides):
    values = dict(
        id="att-91bd",
        ticket_id="tic-cf9f",
        worker_id="claude.opus.001",
        workspace_id="ws-7c41",
        generation=1,
        state="active",
        started="2026-09-21T13:12:04Z",
        last_activity="2026-09-21T13:14:10Z",
    )
    values.update(overrides)
    return coordination_records.WorkAttempt(**values)


def make_claim(**overrides):
    values = dict(
        id="clm-a1b2",
        workspace_id="ws-7c41",
        path="src/arbite/schema.py",
        ticket_id="tic-cf9f",
        attempt_id="att-91bd",
        generation=1,
        acquired="2026-09-21T13:12:41Z",
    )
    values.update(overrides)
    return coordination_records.FileClaim(**values)


def make_receipt(**overrides):
    digest = "sha256:" + "a" * 64
    values = dict(
        id="op-4f19",
        kind="write",
        paths=["src/arbite/sinks/base.py"],
        result="succeeded",
        recorded_at="2026-09-21T13:15:00Z",
        ticket_id="tic-cf9f",
        attempt_id="att-91bd",
        actor="claude.opus.001",
        before={"src/arbite/sinks/base.py": "sha256:" + "1" * 64},
        after={"src/arbite/sinks/base.py": "sha256:" + "2" * 64},
        claim_generation=1,
        **{"artifacts": []},
    )
    values.update(overrides)
    return coordination_records.OperationReceipt(**values)


def test_every_record_type_round_trips_through_its_stored_form():
    """One serialisation for both backends: what `to_dict` writes, `parse_record`
    reads back into an equal record, discriminator and revision included."""
    records = [
        coordination_records.Workspace(
            id="ws-7c41",
            root="/media/matt/m2tb/projects/arbite",
            store_kind="file",
            store_root="/media/matt/m2tb/projects/arbite/.arbite",
            coordination_kind="file",
            coordination_root="/media/matt/m2tb/projects/arbite/.arbite/coordination",
        ),
        make_attempt(),
        make_claim(),
        coordination_records.ReadObservation(
            id="op-1001",
            path="src/arbite/schema.py",
            digest="sha256:" + "3" * 64,
            observed_at="2026-09-21T13:13:00Z",
            attempt_id="att-91bd",
            actor="claude.opus.001",
            claim_generation=1,
            line_start=10,
            line_end=20,
        ),
        make_receipt(),
        coordination_records.Artifact(
            id="art-0f0f",
            digest="sha256:" + "4" * 64,
            size=1204,
            created="2026-09-21T13:15:01Z",
            media_type="text/plain; charset=utf-8",
            operation_id="op-4f19",
        ),
        coordination_records.Event(
            id="evt-2a2a",
            cursor=7,
            kind="claim.acquired",
            recorded_at="2026-09-21T13:12:41Z",
            category="claim",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            actor="claude.opus.001",
            operation_id="op-4f19",
            payload={"generation": 1},
        ),
    ]

    for record in records:
        stored = record.to_dict()
        assert stored["record"] == record.RECORD_TYPE
        assert stored["schema_revision"] == coordination_records.COORDINATION_SCHEMA_REVISION
        assert coordination_records.parse_record(stored) == record


def test_an_unknown_record_type_is_refused_by_name():
    with pytest.raises(RecordError) as failure:
        coordination_records.parse_record({"record": "reservation", "schema_revision": 1})

    assert "unknown coordination record type 'reservation'" in str(failure.value)


def test_a_newer_schema_revision_is_refused_rather_than_half_read():
    """A record written by a newer arbite must fail loudly: reading its fields with
    this version's meaning is how a claim gets misread."""
    stored = make_claim().to_dict()
    stored["schema_revision"] = coordination_records.COORDINATION_SCHEMA_REVISION + 1

    with pytest.raises(RecordError) as failure:
        coordination_records.parse_record(stored)

    assert "schema revision" in str(failure.value)


def test_a_record_without_its_discriminator_is_refused():
    stored = make_claim().to_dict()
    del stored["record"]

    with pytest.raises(RecordError) as failure:
        coordination_records.parse_record(stored)

    assert "must carry its 'record' discriminator" in str(failure.value)


def test_an_unknown_field_is_reported_not_ignored():
    """Silently dropping a field is how two versions drift apart over time."""
    stored = make_claim().to_dict()
    stored["secret_sauce"] = "extra"

    with pytest.raises(RecordError) as failure:
        coordination_records.parse_record(stored)

    assert "unknown field(s): secret_sauce" in str(failure.value)


def test_ids_are_opaque_and_prefixed_per_record_type():
    existing = set()
    for record_type, prefix in sorted(coordination_records.ID_PREFIXES.items()):
        minted = coordination_records.new_id(record_type, existing)
        assert minted.startswith(f"{prefix}-")
        assert coordination_records.ID_PATTERN.match(minted)
        existing.add(minted)

    with pytest.raises(RecordError):
        coordination_records.new_id("reservation")


def test_ids_avoid_the_ones_already_taken():
    """The caller's retry loop is what settles a race, so a taken id must not be
    handed out again."""
    taken = set()
    for _ in range(50):
        minted = coordination_records.new_id("attempt", taken)
        assert minted not in taken
        taken.add(minted)


def test_timestamps_are_utc_to_the_second_and_parse_back():
    now = coordination_records.utc_now()

    assert coordination_records.is_utc_timestamp(now)
    assert coordination_records.parse_utc(now).tzinfo is not None
    assert coordination_records.parse_utc(now.replace("Z", "+00:00")) == coordination_records.parse_utc(now)


def test_a_legacy_local_timestamp_is_readable_but_a_new_one_must_be_utc():
    """The reader tolerates the shapes that already exist; what arbite *writes* is
    canonical. Refusing to read a record would hide evidence, and writing a local
    time would make two stores disagree about when something happened."""
    assert coordination_records.parse_utc("2026-09-21T13:12:04").hour == 13

    with pytest.raises(RecordError):
        make_attempt(started="2026-09-21 13:12:04")


def test_legacy_tickets_and_timestamps_still_read():
    """The coordination records introduced a stricter time format for *new* records;
    the ticket schema's own, older format is deliberately untouched, so a store
    written before this slice reads, validates and renders exactly as it did."""
    assert schema.DATE_PATTERN.match("2026-09-21")
    assert schema.DATE_PATTERN.match("2026-09-21T13:12:04")

    legacy = schema.Ticket(
        id="tic-a1b2",
        title="A legacy ticket",
        status="closed",
        type="bug",
        tier="medium",
        domain="io",
        created="2026-01-01",
        updated="2026-01-02T09:30:00",
        closed="2026-02-01",
        body="## Description\nold\n\n## Notes\n",
    )
    text = legacy.to_markdown()

    # A date-like string is quoted on the way out, so it reads back as text rather
    # than as a YAML date, and the round trip is byte-identical.
    assert "created: '2026-01-01'" in text

    parsed = schema.parse_ticket(text)
    assert schema.validate_ticket(parsed) == []
    assert parsed.created == "2026-01-01"
    assert parsed.closed == "2026-02-01"
    assert parsed.to_markdown() == text

    # A hand-written, historical ticket with a bare date is still readable: YAML
    # hands back a date, `validate_ticket` accepts its rendered form, and nothing
    # in this slice rewrites it.
    hand_written = schema.parse_ticket(
        "---\n"
        "id: tic-b2c3\n"
        "title: Written by hand\n"
        "status: closed\n"
        "type: bug\n"
        "tier: medium\n"
        "domain: io\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "closed: 2026-01-05\n"
        "---\n\n## Description\nold\n\n## Notes\n"
    )
    assert str(hand_written.created) == "2026-01-01"
    assert schema.validate_ticket(hand_written) == []


def test_a_finished_attempt_must_record_when_it_ended():
    with pytest.raises(RecordError) as failure:
        make_attempt(state="released")

    assert "no 'ended' time" in str(failure.value)
    assert make_attempt(state="released", ended="2026-09-21T13:30:00Z").state == "released"


def test_an_active_attempt_may_not_record_an_end():
    with pytest.raises(RecordError):
        make_attempt(ended="2026-09-21T13:30:00Z")


def test_a_claim_path_must_be_a_canonical_project_relative_path():
    """Aliases, escapes and absolute paths are refused at the record level, so no
    backend can store a claim no later check could resolve."""
    for bad in ("/etc/passwd", "../outside.py", "src/../../escape.py", "src\\windows.py", "C:/x.py", ""):
        with pytest.raises(RecordError):
            make_claim(path=bad)

    assert make_claim(path="src/arbite/schema.py").path == "src/arbite/schema.py"


def test_a_released_claim_keeps_its_history_but_is_not_active():
    released = make_claim(state="released", released="2026-09-21T13:40:00Z")

    assert released.is_active is False
    assert released.held_by("att-91bd") is False
    assert make_claim().held_by("att-91bd") is True
    assert make_claim().held_by("att-91bd", generation=2) is False
    assert make_claim().held_by("att-other") is False

    with pytest.raises(RecordError):
        make_claim(state="released")


def test_a_receipt_records_absence_explicitly_rather_than_as_an_empty_digest():
    created = make_receipt(
        kind="create",
        before={"src/arbite/sinks/base.py": coordination_records.ABSENT},
        after={"src/arbite/sinks/base.py": "sha256:" + "9" * 64},
    )
    assert created.before["src/arbite/sinks/base.py"] == "absent"

    with pytest.raises(RecordError):
        make_receipt(before={"src/arbite/sinks/base.py": ""})


def test_a_receipt_describes_the_same_paths_before_and_after():
    """A create records 'absent' and the new digest for one path; a receipt whose
    two halves describe different sets of paths is evidence of nothing."""
    created = make_receipt(
        kind="create",
        paths=["src/arbite/sinks/base.py", "src/arbite/sinks/other.py"],
        before={
            "src/arbite/sinks/base.py": coordination_records.ABSENT,
            "src/arbite/sinks/other.py": coordination_records.ABSENT,
        },
        after={
            "src/arbite/sinks/base.py": "sha256:" + "2" * 64,
            "src/arbite/sinks/other.py": "sha256:" + "3" * 64,
        },
    )
    assert set(created.before) == set(created.after)

    with pytest.raises(RecordError) as failure:
        make_receipt(
            paths=["src/arbite/sinks/base.py", "src/arbite/sinks/other.py"],
            before={"src/arbite/sinks/base.py": coordination_records.ABSENT},
            after={"src/arbite/sinks/other.py": "sha256:" + "2" * 64},
        )

    assert "same paths" in str(failure.value)


def test_a_receipt_names_only_paths_it_lists():
    with pytest.raises(RecordError):
        make_receipt(after={"src/arbite/other.py": "sha256:" + "2" * 64})


def test_a_pending_receipt_is_the_unfinished_operation_marker():
    pending = make_receipt(result=coordination_records.RECEIPT_PENDING)

    assert pending.is_pending is True
    assert make_receipt().is_pending is False

    with pytest.raises(RecordError):
        make_receipt(result="nearly")


def test_passthrough_may_name_no_path_but_other_receipts_must():
    """A read-only tool's execution is a receipt with nothing in it to attribute a
    file to; a write that names no path would be evidence of nothing."""
    empty = make_receipt(kind="passthrough", paths=[], before={}, after={})
    assert empty.paths == []

    with pytest.raises(RecordError):
        make_receipt(paths=[], before={}, after={})


def test_a_read_observation_never_authorises_a_write_it_did_not_earn():
    """The whole write contract hinges on this: a pre-claim read, a foreign read and
    a read of another path all fail to authorise a mutation."""
    claim = make_claim()
    holder = coordination_records.ReadObservation(
        id="op-1001",
        path="src/arbite/schema.py",
        digest="sha256:" + "3" * 64,
        observed_at="2026-09-21T13:13:00Z",
        attempt_id="att-91bd",
        claim_generation=1,
    )
    assert holder.authorizes_write(claim) is True

    # A read taken before the claim was acquired.
    before_claim = coordination_records.ReadObservation(
        id="op-1002",
        path="src/arbite/schema.py",
        digest="sha256:" + "3" * 64,
        observed_at="2026-09-21T13:11:00Z",
        attempt_id="att-91bd",
        claim_generation=0,
    )
    assert before_claim.authorizes_write(claim) is False

    # A read by another attempt (a foreign read), and a read of another path.
    other = coordination_records.ReadObservation(
        id="op-1003",
        path="src/arbite/schema.py",
        digest="sha256:" + "3" * 64,
        observed_at="2026-09-21T13:13:00Z",
        attempt_id="att-ffff",
        claim_generation=1,
    )
    assert other.authorizes_write(claim) is False
    assert holder.authorizes_write(make_claim(path="src/arbite/sinks/base.py")) is False
    # Nothing at all: an unclaimed path authorises nothing.
    assert holder.authorizes_write(None) is False


def test_an_absent_probe_authorises_creation_and_never_a_rewrite():
    """The absent marker is the one thing that can justify a create, and it must
    never justify a write over bytes that arrived (or existed) in the meantime."""
    probe = coordination_records.ReadObservation(
        id="op-1004",
        path="src/new_file.py",
        digest=coordination_records.ABSENT,
        observed_at="2026-09-21T13:13:00Z",
        attempt_id="att-91bd",
        claim_generation=1,
    )
    claim = make_claim(path="src/new_file.py")

    assert probe.authorizes_creation(claim) is True
    assert probe.authorizes_write(claim) is False
    # A probe taken before the claim still authorises nothing, either way.
    unclaimed_probe = coordination_records.ReadObservation(
        id="op-1005",
        path="src/new_file.py",
        digest=coordination_records.ABSENT,
        observed_at="2026-09-21T13:11:00Z",
        attempt_id="att-91bd",
        claim_generation=0,
    )
    assert unclaimed_probe.authorizes_creation(claim) is False
    # ...and an observation of existing bytes never authorises a creation.
    existing = coordination_records.ReadObservation(
        id="op-1006",
        path="src/new_file.py",
        digest="sha256:" + "3" * 64,
        observed_at="2026-09-21T13:13:00Z",
        attempt_id="att-91bd",
        claim_generation=1,
    )
    assert existing.authorizes_creation(claim) is False


def test_a_read_range_is_ordered_and_does_not_change_the_digest():
    ranged = coordination_records.ReadObservation(
        id="op-1005",
        path="src/arbite/schema.py",
        digest="sha256:" + "3" * 64,
        observed_at="2026-09-21T13:13:00Z",
        line_start=10,
        line_end=20,
    )

    assert ranged.is_ranged is True
    assert ranged.digest == "sha256:" + "3" * 64

    with pytest.raises(RecordError):
        coordination_records.ReadObservation(
            id="op-1006",
            path="src/arbite/schema.py",
            digest="sha256:" + "3" * 64,
            observed_at="2026-09-21T13:13:00Z",
            line_start=20,
            line_end=10,
        )


def test_artifacts_and_observations_are_addressed_by_digest_and_range():
    artifact = coordination_records.Artifact(
        id="art-0f0f", digest="sha256:" + "4" * 64, size=0, created="2026-09-21T13:15:01Z"
    )
    assert artifact.size == 0

    with pytest.raises(RecordError):
        coordination_records.Artifact(id="art-0f0f", digest="4" * 64, size=1, created="2026-09-21T13:15:01Z")


def test_events_are_versioned_and_categorised():
    event = coordination_records.Event(
        id="evt-2a2a",
        cursor=1,
        kind="file.read",
        recorded_at="2026-09-21T13:16:00Z",
        category=coordination_records.READ_CATEGORY,
    )
    assert event.category == "read"
    assert event.payload == {}

    with pytest.raises(RecordError):
        coordination_records.Event(
            id="evt-2a2a",
            cursor=1,
            kind="file.read",
            recorded_at="2026-09-21T13:16:00Z",
            category="gossip",
        )
    with pytest.raises(RecordError):
        coordination_records.Event(
            id="evt-2a2a", cursor=0, kind="x", recorded_at="2026-09-21T13:16:00Z", category="file"
        )


def test_a_workspace_id_is_derived_from_the_root_and_the_store():
    """Derived, never bound: the same facts give the same id, and a relocated root
    or a repointed store is honestly a different workspace."""
    first = coordination_records.derived_workspace_id("/a/project", "file", "/a/project/.arbite")
    assert first == coordination_records.derived_workspace_id("/a/project", "file", "/a/project/.arbite")
    assert coordination_records.ID_PATTERN.match(first)

    assert first != coordination_records.derived_workspace_id("/a/moved", "file", "/a/moved/.arbite")
    assert first != coordination_records.derived_workspace_id("/a/project", "sqlite", "/a/project/.arbite/arbite.db")


def test_a_workspace_root_must_be_absolute():
    with pytest.raises(RecordError):
        coordination_records.Workspace(
            id="ws-7c41",
            root="relative/path",
            store_kind="file",
            store_root="/a/.arbite",
            coordination_kind="file",
            coordination_root="/a/.arbite/coordination",
        )


def test_digest_helpers_agree_with_the_display_rule():
    digest = coordination_records.digest_bytes(b"hello")

    assert coordination_records.DIGEST_PATTERN.match(digest)
    assert coordination_records.short_digest(digest) == digest[: len("sha256:") + 12]
    assert coordination_records.short_digest(coordination_records.ABSENT) == "absent"
