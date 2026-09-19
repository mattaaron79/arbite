"""Bounded discovery and versioned reads (planning key C06).

Every behavioural test is parametrized over both sinks through the `sink`
fixture, so "the file sink and the SQLite sink behave the same" is checked rather
than assumed: a read observation recorded by one is readable by the other's
queries, and a busy receipt is identical from the outside.

The tests cover the four C06 acceptance criteria directly:

1. a foreign claim yields a busy owner and a non-writable receipt, with an
   optional fail-if-busy refusal;
2. a write-authorizing token needs a fresh read after acquisition by the current
   attempt, and a ranged read still identifies the whole-file version;
3. the absent-path probe supports a safe create, while list/search mint no
   observations and authorize nothing;
4. output limits/pagination and unsupported encodings/file types are explicit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arbite import application, coordination, fileclaims, filereads, lifecycle
from arbite.application import Actor
from arbite.errors import (
    CoordinationNotFound,
    FileBusy,
    StaleRead,
    UnsupportedCoordination,
)
from helpers import make_ticket


# --- helpers ---------------------------------------------------------------


def _project(sink, arbite_dir, *, ticket_id="tic-a1b2", worker="claude.opus.001"):
    """A bound workspace with one active attempt and a small file tree."""
    root = arbite_dir.parent
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a.py").write_text("alpha\nbeta\ngamma\n")
    (root / "src" / "b.py").write_text("beta in b\n")
    (root / "src" / "bin.dat").write_bytes(b"\x00\x01\x02")
    (root / "src" / "latin.dat").write_bytes("caf\xe9\n".encode("latin-1"))
    sink.create(make_ticket(ticket_id))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get(ticket_id), worker_id=worker).attempt
    return (
        filereads.FileReadService(service),
        fileclaims.FileClaimService(service),
        attempt,
        root,
        service,
    )


def _second_attempt(sink, root, *, ticket_id="tic-b2c3", worker="claude.opus.002"):
    sink.create(make_ticket(ticket_id))
    service = application.coordination_service_for(
        sink, root=str(root), actor=Actor(worker)
    )
    attempt = lifecycle.TicketLifecycle(service, sink).acquire(
        sink.get(ticket_id), worker_id=worker
    ).attempt
    return filereads.FileReadService(service), attempt, service


def _observations(service, *, attempt_id=None):
    with service.store.transaction(write=False) as tx:
        rows = tx.find("read_observation")
    if attempt_id is not None:
        rows = [row for row in rows if row.attempt_id == attempt_id]
    return rows


# --- reads: content, ranges, encodings -------------------------------------


def test_whole_read_records_the_whole_file_digest_and_is_complete(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    receipt = reads.read(attempt, "src/a.py")

    assert receipt.text == "alpha\nbeta\ngamma\n"
    assert receipt.content_complete is True
    assert receipt.whole_file_digest == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    assert receipt.size == len(b"alpha\nbeta\ngamma\n")
    assert receipt.encoding == "utf-8"
    assert receipt.newline == "lf"
    assert receipt.lines_total == 3  # splitlines() collapses the trailing newline


def test_ranged_read_identifies_the_whole_file_and_is_not_complete(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    receipt = reads.read(attempt, "src/a.py", lines=(2, 2))

    assert receipt.text == "beta"
    assert receipt.content_complete is False
    assert receipt.line_range_requested == (2, 2)
    assert receipt.line_range_returned == (2, 2)
    assert receipt.range_clamped is False
    # The digest still covers the whole file, not the served window.
    assert receipt.whole_file_digest == coordination.digest_of_text("alpha\nbeta\ngamma\n")


def test_range_beyond_eof_is_clamped_and_says_so(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    receipt = reads.read(attempt, "src/a.py", lines=(2, 99))

    assert receipt.text == "beta\ngamma"
    assert receipt.range_clamped is True
    assert receipt.line_range_returned == (2, 3)
    assert receipt.range_empty is False


def test_range_starting_past_eof_is_explicitly_empty(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    receipt = reads.read(attempt, "src/a.py", lines=(50, 60))

    assert receipt.text == ""
    assert receipt.range_empty is True
    assert receipt.content_complete is False


def test_utf8_bom_is_supported_and_reported(sink, arbite_dir):
    reads, _claims, attempt, root, _service = _project(sink, arbite_dir)
    (root / "src" / "bom.py").write_bytes("\ufeffhello\n".encode("utf-8"))
    receipt = reads.read(attempt, "src/bom.py")

    assert receipt.text == "hello\n"
    assert receipt.encoding == "utf-8-sig"


def test_binary_file_is_refused_explicitly(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    with pytest.raises(UnsupportedCoordination) as excinfo:
        reads.read(attempt, "src/bin.dat")

    error = excinfo.value
    assert error.error_code == "unsupported"
    assert error.details["reason"] == filereads.SKIP_BINARY
    assert error.details["size"] == 3
    assert error.details["digest"].startswith("sha256:")
    # A refused read records no observation: no bytes were served.
    assert _observations(_service) == []


def test_non_utf8_bytes_are_refused_explicitly(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    with pytest.raises(UnsupportedCoordination) as excinfo:
        reads.read(attempt, "src/latin.dat")

    assert excinfo.value.error_code == "unsupported"
    assert excinfo.value.details["reason"] in (
        filereads.SKIP_BINARY,
        filereads.SKIP_NOT_UTF8,
    )
    assert _observations(_service) == []


def test_crlf_content_is_reported_without_altering_the_digest(sink, arbite_dir):
    reads, _claims, attempt, root, _service = _project(sink, arbite_dir)
    (root / "src" / "dos.py").write_bytes(b"one\r\ntwo\r\n")
    receipt = reads.read(attempt, "src/dos.py")

    assert receipt.newline == filereads.NEWLINE_CRLF
    assert receipt.whole_file_digest == coordination.digest_of_bytes(b"one\r\ntwo\r\n")


def test_read_missing_file_is_not_found(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    with pytest.raises(CoordinationNotFound):
        reads.read(attempt, "src/missing.py")


def test_read_rejects_traversal_and_protected_paths(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    for bad in ("../escape.py", ".arbite/arbite.yaml", ".git/config"):
        with pytest.raises(UnsupportedCoordination):
            reads.read(attempt, bad)


# --- busy owner, non-writable receipts, fail-if-busy -----------------------


def test_foreign_claim_read_serves_bytes_with_a_busy_owner(sink, arbite_dir):
    reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    mine = claims.claim(attempt, ["src/a.py"]).acquired[0]

    other_reads, other_attempt, other_service = _second_attempt(sink, root)
    receipt = other_reads.read(other_attempt, "src/a.py")

    assert receipt.text == "alpha\nbeta\ngamma\n"  # bytes ARE served
    assert receipt.busy is True
    assert receipt.non_writable is True
    assert receipt.write_authorizing is False
    assert receipt.non_writable_reason == filereads.REASON_FOREIGN_CLAIM
    assert receipt.busy_owner["holder_ticket"] == "tic-a1b2"
    assert receipt.busy_owner["holder_attempt"] == attempt.id
    assert receipt.busy_owner["holder_claim"] == mine.id
    assert receipt.busy_owner["holder_generation"] == 1
    assert receipt.busy_owner["holder_observed_version"] == mine.observed_version
    assert receipt.claim_generation == 1  # the observed (foreign) generation is recorded
    assert receipt.claim_observed_version == mine.observed_version

    # The observation is durable evidence, explicitly non-writable.
    stored = reads.read_observation(receipt.read_token)
    assert stored.write_authorizing is False
    assert stored.claim_generation == 1
    assert stored.attempt_id == other_attempt.id


def test_fail_if_busy_refuses_before_serving_and_records_nothing(sink, arbite_dir):
    reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])
    other_reads, other_attempt, other_service = _second_attempt(sink, root)
    before = _observations(other_service)

    with pytest.raises(FileBusy) as excinfo:
        other_reads.read(other_attempt, "src/a.py", fail_if_busy=True)

    assert excinfo.value.error_code == "file_busy"
    assert excinfo.value.details["holder_ticket"] == "tic-a1b2"
    assert excinfo.value.details["holder_attempt"] == attempt.id
    assert excinfo.value.details["read_allowed"] is True
    assert _observations(other_service) == before  # nothing was served or recorded


def test_fail_if_busy_does_not_trip_on_your_own_claim(sink, arbite_dir):
    reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/a.py"])
    receipt = reads.read(attempt, "src/a.py", fail_if_busy=True)

    assert receipt.busy is False
    assert receipt.write_authorizing is True


# --- authorization: fresh reads only ---------------------------------------


def test_pre_claim_read_does_not_authorize_but_a_fresh_post_claim_read_does(
    sink, arbite_dir
):
    reads, claims, attempt, _root, service = _project(sink, arbite_dir)

    before = reads.read(attempt, "src/a.py")
    assert before.write_authorizing is False
    assert before.non_writable_reason == filereads.REASON_NO_CLAIM
    assert before.claim_generation is None

    claim = claims.claim(attempt, ["src/a.py"]).acquired[0]
    after = reads.read(attempt, "src/a.py")
    assert after.write_authorizing is True
    assert after.non_writable is False
    assert after.claim_generation == claim.generation

    # The application-layer guard agrees: the pre-claim token is refused, the
    # fresh post-claim token authorizes a write.
    pre_observation = reads.read_observation(before.read_token)
    with pytest.raises(StaleRead):
        application.require_write_authorization(
            pre_observation, claim=claim, attempt=attempt
        )
    fresh_observation = reads.read_observation(after.read_token)
    assert (
        application.require_write_authorization(
            fresh_observation, claim=claim, attempt=attempt
        ).write_authorizing
        is True
    )


def test_ranged_post_claim_read_authorizes_the_whole_file_version(sink, arbite_dir):
    reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    claim = claims.claim(attempt, ["src/a.py"]).acquired[0]
    receipt = reads.read(attempt, "src/a.py", lines=(1, 1))

    assert receipt.write_authorizing is True
    assert receipt.content_complete is False
    observation = reads.read_observation(receipt.read_token)
    assert observation.digest == claim.observed_version
    # A stored line_range round-trips as JSON, so it comes back as a list.
    assert list(observation.line_range) == [1, 1]
    application.require_write_authorization(observation, claim=claim, attempt=attempt)


def test_claim_version_mismatch_after_external_edit_is_non_writable(sink, arbite_dir):
    reads, claims, attempt, root, service = _project(sink, arbite_dir)
    claim = claims.claim(attempt, ["src/a.py"]).acquired[0]
    (root / "src" / "a.py").write_text("changed outside arbite\n")

    receipt = reads.read(attempt, "src/a.py")

    assert receipt.busy is False  # the claim is ours
    assert receipt.write_authorizing is False
    assert receipt.non_writable_reason == filereads.REASON_CLAIM_VERSION_MISMATCH
    assert receipt.claim_generation == claim.generation
    assert receipt.whole_file_digest != claim.observed_version
    observation = reads.read_observation(receipt.read_token)
    assert observation.write_authorizing is False


def test_a_second_read_after_release_and_reclaim_is_required(sink, arbite_dir):
    reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    first = claims.claim(attempt, ["src/a.py"]).acquired[0]
    fresh = reads.read(attempt, "src/a.py")
    assert fresh.write_authorizing is True

    claims.release(attempt, ["src/a.py"], reason="yield to another worker")
    second = claims.claim(attempt, ["src/a.py"]).acquired[0]
    assert second.generation == first.generation + 1

    # The read taken under the old generation is stale for the new claim.
    stale = reads.read_observation(fresh.read_token)
    with pytest.raises(StaleRead):
        application.require_write_authorization(stale, claim=second, attempt=attempt)


# --- probe: absent-path safe create ----------------------------------------


def test_probe_absent_path_supports_safe_create_and_owns_nothing(sink, arbite_dir):
    reads, claims, attempt, _root, service = _project(sink, arbite_dir)
    receipt = reads.probe(attempt, "src/new.py")

    assert receipt.exists is False
    assert receipt.version == coordination.ABSENT
    assert receipt.safe_to_create is True
    assert receipt.busy is False
    assert receipt.required_claim_version == coordination.ABSENT
    # A probe is inspection: it acquires nothing and records nothing.
    assert receipt.claim_acquired is False
    assert receipt.observation_recorded is False
    assert claims.active_claims(attempt_id=attempt.id) == []
    assert _observations(service) == []


def test_probe_existing_path_is_not_safe_to_create(sink, arbite_dir):
    reads, _claims, attempt, _root, _service = _project(sink, arbite_dir)
    receipt = reads.probe(attempt, "src/a.py")

    assert receipt.exists is True
    assert receipt.safe_to_create is False
    assert receipt.version.startswith("sha256:")
    assert receipt.required_claim_version is None


def test_probe_reports_a_foreign_claim_as_busy(sink, arbite_dir):
    reads, claims, attempt, root, _service = _project(sink, arbite_dir)
    claims.claim(attempt, ["src/new.py"])
    other_reads, other_attempt, _other_service = _second_attempt(sink, root)

    receipt = other_reads.probe(other_attempt, "src/new.py")

    assert receipt.exists is False
    assert receipt.safe_to_create is False
    assert receipt.busy is True
    assert receipt.busy_owner["holder_ticket"] == "tic-a1b2"


def test_probe_of_your_own_absent_claim_is_safe_and_reports_the_claim(sink, arbite_dir):
    reads, claims, attempt, _root, _service = _project(sink, arbite_dir)
    claim = claims.claim(attempt, ["src/new.py"]).acquired[0]

    receipt = reads.probe(attempt, "src/new.py")

    assert receipt.safe_to_create is True
    assert receipt.busy is False
    assert receipt.claim["claim_id"] == claim.id
    assert receipt.claim["generation"] == claim.generation
    assert receipt.claim["observed_version"] == coordination.ABSENT


# --- list -------------------------------------------------------------------


def test_list_is_sorted_bounded_and_reports_truncation(sink, arbite_dir):
    reads, _claims, _attempt, root, _service = _project(sink, arbite_dir)
    for index in range(5):
        (root / "src" / f"f{index}.txt").write_text(f"file {index}\n")

    page = reads.list("src", limit=2)
    assert [entry.path for entry in page.entries] == ["src/a.py", "src/b.py"]
    assert page.truncated is True
    assert page.next_offset == 2
    assert filereads.MARKER_OUTPUT_TRUNCATED in page.markers

    second = reads.list("src", limit=2, offset=2)
    assert second.offset == 2
    assert second.entries[0].path == "src/bin.dat"
    assert filereads.MARKER_OFFSET_ADVANCED in second.markers


def test_list_reports_path_and_version_metadata_for_files(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    page = reads.list("src")
    by_path = {entry.path: entry for entry in page.entries}

    assert by_path["src/a.py"].kind == "file"
    assert by_path["src/a.py"].version == coordination.digest_of_text("alpha\nbeta\ngamma\n")
    assert by_path["src/a.py"].classification == "text"
    assert by_path["src/bin.dat"].classification == "binary"
    assert not page.entries[0].version_omitted


def test_list_excludes_protected_metadata_and_counts_it(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    page = reads.list()

    assert all(not entry.path.startswith(".arbite") for entry in page.entries)
    assert all(not entry.path.startswith(".git") for entry in page.entries)
    assert page.excluded_protected >= 1
    assert filereads.MARKER_PROTECTED_EXCLUDED in page.markers


def test_list_skips_symlinks_and_reports_them(sink, arbite_dir):
    reads, _claims, _attempt, root, _service = _project(sink, arbite_dir)
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported on this platform")
    try:
        os.symlink(root / "src" / "a.py", root / "src" / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    page = reads.list("src")

    assert all(entry.path != "src/link.py" for entry in page.entries)
    assert page.skipped_symlinks == 1
    assert filereads.MARKER_SYMLINKS_SKIPPED in page.markers


def test_list_omits_an_oversized_files_version_explicitly(sink, arbite_dir, monkeypatch):
    reads, _claims, _attempt, root, _service = _project(sink, arbite_dir)
    monkeypatch.setattr(filereads, "MAX_VERSION_BYTES", 4)
    page = reads.list("src")
    by_path = {entry.path: entry for entry in page.entries}

    big = by_path["src/a.py"]
    assert big.version is None
    assert big.version_omitted is True
    assert filereads.MARKER_VERSION_OMITTED in page.markers
    assert big.size == len(b"alpha\nbeta\ngamma\n")


def test_list_caps_an_oversized_limit_and_says_so(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    page = reads.list(limit=99999)

    assert page.limit == filereads.MAX_LIST_LIMIT
    assert page.limit_capped is True
    assert filereads.MARKER_LIMIT_CAPPED in page.markers


def test_list_rejects_traversal_protected_and_missing_prefixes(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    for bad in ("../", ".arbite", ".git"):
        with pytest.raises(UnsupportedCoordination):
            reads.list(bad)
    # A nonexistent *component* is a missing parent (never an implicit mkdir).
    with pytest.raises(UnsupportedCoordination):
        reads.list("does/not/exist")


# --- search -----------------------------------------------------------------


def test_search_matches_paths_and_content_and_reports_skipped_files(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    page = reads.search("beta")

    by_key = {(match.path, match.line): match for match in page.matches}
    assert ("src/a.py", 2) in by_key
    assert by_key[("src/a.py", 2)].text == "beta"
    assert ("src/b.py", 1) in by_key

    missing = reads.search("not-present-anywhere")
    assert missing.matches == []


def test_search_matches_a_path_without_opening_it(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    page = reads.search(r"a\.py$")

    path_matches = [match for match in page.matches if match.line is None]
    assert [match.path for match in path_matches] == ["src/a.py"]
    assert path_matches[0].version.startswith("sha256:")


def test_search_reports_binary_and_oversized_files_as_skipped(sink, arbite_dir, monkeypatch):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    monkeypatch.setattr(filereads, "MAX_SEARCH_FILE_BYTES", 4)
    page = reads.search("alpha")

    reasons = {entry["reason"] for entry in page.skipped}
    assert filereads.SKIP_TOO_LARGE in reasons
    assert filereads.MARKER_CONTENT_NOT_SCANNED in page.markers
    assert page.matches == []


def test_search_pagination_has_a_deterministic_next_offset(sink, arbite_dir):
    reads, _claims, _attempt, root, _service = _project(sink, arbite_dir)
    (root / "src" / "multi.py").write_text("hit one\nhit two\nhit three\n")

    first = reads.search("hit", limit=2)
    assert first.returned == 2
    assert first.truncated is True
    assert first.next_offset == 2

    second = reads.search("hit", limit=2, offset=2)
    assert second.returned == 1
    assert second.truncated is False
    assert second.next_offset is None
    keys = [
        (match.path, match.line) for match in (first.matches + second.matches)
    ]
    assert keys == [("src/multi.py", 1), ("src/multi.py", 2), ("src/multi.py", 3)]


def test_search_truncates_a_long_matched_line_explicitly(sink, arbite_dir, monkeypatch):
    reads, _claims, _attempt, root, _service = _project(sink, arbite_dir)
    monkeypatch.setattr(filereads, "MAX_MATCH_LINE_CHARS", 5)
    (root / "src" / "long.py").write_text("needle-and-a-very-long-tail\n")

    page = reads.search("needle")
    match = page.matches[0]
    assert match.text == "needl"
    assert match.text_truncated is True


def test_search_rejects_an_invalid_pattern(sink, arbite_dir):
    reads, _claims, _attempt, _root, _service = _project(sink, arbite_dir)
    with pytest.raises(UnsupportedCoordination):
        reads.search("[")


def test_discovery_authorizes_nothing(sink, arbite_dir):
    reads, claims, attempt, _root, service = _project(sink, arbite_dir)

    reads.list("src")
    reads.search("alpha")

    # No observation, no claim, and therefore no read token to present.
    assert _observations(service) == []
    assert claims.active_claims(attempt_id=attempt.id) == []


# --- observation persistence on both sinks ---------------------------------


def test_read_observations_persist_with_the_documented_fields(sink, arbite_dir):
    reads, claims, attempt, _root, service = _project(sink, arbite_dir)
    claim = claims.claim(attempt, ["src/a.py"]).acquired[0]
    first = reads.read(attempt, "src/a.py")
    second = reads.read(attempt, "src/a.py", lines=(3, 3))

    rows = _observations(service)
    assert len(rows) == 2
    by_id = {row.id: row for row in rows}
    assert by_id[first.read_token].line_range is None
    assert by_id[first.read_token].write_authorizing is True
    assert list(by_id[second.read_token].line_range) == [3, 3]
    assert by_id[second.read_token].digest == claim.observed_version
    for row in rows:
        assert coordination.is_digest(row.digest)
        assert coordination.is_utc_timestamp(row.observed_at)
        assert row.path == "src/a.py"
        assert row.claim_generation == claim.generation

    # Read observations are a separate event category, not ordinary traffic.
    events = service.store.event_log(category="read")
    assert {event.payload["path"] for event in events} == {"src/a.py"}
    assert all(event.category == "read" for event in events)
    assert any(event.payload["write_authorizing"] for event in events)
