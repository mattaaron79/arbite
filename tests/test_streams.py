"""The narration stream's storage contract: the JSONL file, its cursor, and its reports.

These tests call `coordination.streams` directly rather than through the CLI, so the
module's own contract is pinned independently of the command surface and of the
layout work around it. What they are about: one record per line appended under the
area's lock, a monotonic `seq` that survives a half-written last line, "nothing new"
being a distinct exit code, and a clear that never removes the lock file.
"""

from __future__ import annotations

import json

import pytest

from arbite.coordination import streams
from arbite.errors import CoordinationError, PathRefused, TicketError

TICKET = "tic-a1b2"
ATTEMPT = "att-9f3c"
ACTOR = "claude.opus.001"


@pytest.fixture
def area(tmp_path):
    """A project's arbite directory, with no streams area yet."""
    directory = tmp_path / ".arbite"
    directory.mkdir()
    return directory


def _lines(area, ticket: str = TICKET) -> list:
    text = (streams.streams_root(area) / f"{ticket}{streams.STREAM_SUFFIX}").read_text()
    return text.splitlines()


# --- the file and its records ----------------------------------------------


def test_a_record_is_one_line_of_json_with_the_stored_field_order(area):
    seqs = streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["thinking"])

    assert seqs == [1]
    line = json.loads(_lines(area)[0])
    assert list(line) == [
        "seq",
        "recorded_at",
        "ticket_id",
        "attempt_id",
        "actor",
        "kind",
        "text",
    ]
    assert line["ticket_id"] == TICKET
    assert line["attempt_id"] == ATTEMPT
    assert line["actor"] == ACTOR
    assert line["kind"] == "thought"
    assert line["text"] == "thinking"
    assert line["recorded_at"].endswith("Z")


def test_each_text_becomes_its_own_record_and_the_sequence_keeps_rising(area):
    """One call with three lines and a later call continue the same sequence: the
    cursor a poll keeps has to mean one record, whatever batch wrote it."""
    first = streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])
    second = streams.append_records(area, TICKET, ATTEMPT, ACTOR, "action", ["c"])

    assert first == [1, 2]
    assert second == [3]
    assert [record["text"] for record in streams.read_records(area, TICKET)[0]] == ["a", "b", "c"]


def test_a_second_ticket_starts_its_own_sequence(area):
    """`seq` is per stream, not per store: two tickets narrating at once cannot
    starve each other's cursor."""
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])
    other = "tic-c3d4"

    assert streams.append_records(area, other, "att-1ba5", ACTOR, "thought", ["x"]) == [1]


def test_an_unknown_kind_is_refused_before_anything_is_written(area):
    with pytest.raises(CoordinationError):
        streams.append_records(area, TICKET, ATTEMPT, ACTOR, "dialogue", ["a"])

    assert streams.read_records(area, TICKET) == ([], 0)


def test_a_trailing_partial_line_is_skipped_and_does_not_reset_the_sequence(area):
    """A writer that died between the bytes and the newline leaves a half line. It is
    reported as unparseable, and the next append continues rather than colliding with
    the sequence it was about to use."""
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])
    path = streams.stream_path(area, TICKET)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 9, "recorded_at": "2026-10-05T08:30:00')

    records, skipped = streams.read_records(area, TICKET)

    assert skipped == 1
    assert [record["seq"] for record in records] == [1, 2]
    assert streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["c"]) == [3]


def test_a_blank_line_is_not_counted_as_unparseable(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    with open(streams.stream_path(area, TICKET), "a", encoding="utf-8") as handle:
        handle.write("\n")

    assert streams.read_records(area, TICKET)[1] == 0


def test_a_missing_stream_reads_as_empty_rather_than_failing(area):
    assert streams.read_records(area, TICKET) == ([], 0)
    assert streams.record_count(area, ticket_id=TICKET) == 0


def test_the_stream_path_refuses_anything_that_is_not_a_ticket_id(area):
    for bad in ("../tic-a1b2", "tic-a1b2.jsonl", ".lock", "/etc/passwd", "tic-zzzz"):
        with pytest.raises(TicketError):
            streams.stream_path(area, bad)

    assert streams.stream_path(area, TICKET) == (
        streams.streams_root(area) / f"{TICKET}{streams.STREAM_SUFFIX}"
    )


# --- counting ---------------------------------------------------------------


def test_record_count_filters_by_ticket_and_attempt(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])
    streams.append_records(area, TICKET, "att-1ba5", ACTOR, "thought", ["c"])
    streams.append_records(area, "tic-c3d4", ATTEMPT, ACTOR, "thought", ["d"])

    assert streams.record_count(area, ticket_id=TICKET) == 3
    assert streams.record_count(area, ticket_id=TICKET, attempt_id=ATTEMPT) == 2
    assert streams.record_count(area, attempt_id=ATTEMPT) == 3
    assert streams.record_count(area, attempt_id="att-nope") == 0
    assert streams.record_count(area) == 4


# --- the read view ----------------------------------------------------------


def test_a_bare_read_prints_the_last_twenty_records_and_the_cursor(area):
    texts = [f"line {index}" for index in range(25)]
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", texts)

    result = streams.stream_read(area, TICKET)

    assert result.exit_code == 0
    assert len(result.lines) == streams.DEFAULT_STREAM_TAIL + 1
    assert result.lines[0].startswith("6 ")
    assert result.lines[-1] == f"cursor: 25 (resume with 'arbite stream read {TICKET} --after 25')"
    assert result.data["cursor"] == 25


def test_after_prints_only_what_is_new_and_reports_the_same_cursor(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b", "c"])

    result = streams.stream_read(area, TICKET, after=1)

    assert [line.split()[0] for line in result.lines[:-1]] == ["2", "3"]
    assert result.data["cursor"] == 3
    assert result.data["next_actions"] == [f"arbite stream read {TICKET} --after 3"]


def test_tail_prints_the_end_of_what_exists(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b", "c"])

    result = streams.stream_read(area, TICKET, tail=2)

    assert [line.split()[0] for line in result.lines[:-1]] == ["2", "3"]


def test_nothing_new_is_its_own_outcome_and_keeps_the_cursor_asked_for(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])

    result = streams.stream_read(area, TICKET, after=4)

    assert result.exit_code == 2
    assert result.lines == [f"no stream for {TICKET} since seq 4"]
    assert result.data["records"] == []
    assert result.data["cursor"] == 4


def test_a_read_of_a_ticket_that_never_narrated_is_empty_too(area):
    result = streams.stream_read(area, TICKET)

    assert result.exit_code == 2
    assert result.lines == [f"no stream for {TICKET}"]


def test_the_two_selection_flags_are_alternatives(area):
    with pytest.raises(CoordinationError):
        streams.stream_read(area, TICKET, after=1, tail=2)


def test_a_tail_below_one_and_a_negative_after_are_refused(area):
    with pytest.raises(CoordinationError):
        streams.stream_read(area, TICKET, tail=0)
    with pytest.raises(CoordinationError):
        streams.stream_read(area, TICKET, after=-1)


def test_the_json_records_use_the_event_vocabulary(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "action", ["did it"])

    payload = streams.stream_read(area, TICKET).to_json()

    assert payload["ticket"] == TICKET
    assert payload["records"] == [
        {
            "seq": 1,
            "at": payload["records"][0]["at"],
            "ticket": TICKET,
            "attempt": ATTEMPT,
            "actor": ACTOR,
            "kind": "action",
            "text": "did it",
        }
    ]
    assert payload["records"][0]["at"].endswith("Z")


def test_a_row_carries_the_sequence_time_actor_kind_and_text(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "result", ["all green"])

    row = streams.stream_read(area, TICKET).lines[0]

    assert row.split()[0] == "1"
    assert row.split()[2:] == [ACTOR, "result", "all", "green"]
    assert row.startswith("1   ")


# --- write, list, path ------------------------------------------------------


def test_write_reports_one_record_in_the_singular_and_several_by_range(area):
    one = streams.stream_write(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    many = streams.stream_write(area, TICKET, ATTEMPT, ACTOR, "thought", ["b", "c"])

    display = f".arbite/streams/{TICKET}.jsonl"
    assert one.lines == [f"wrote 1 record to {display} (seq 1)"]
    assert many.lines == [f"wrote 2 record(s) to {display} (seq 2..3)"]
    assert many.data == {
        "ticket": TICKET,
        "attempt": ATTEMPT,
        "actor": ACTOR,
        "kind": "thought",
        "path": display,
        "records": 2,
        "cursor": 3,
        "seqs": [2, 3],
    }


def test_write_with_nothing_to_write_is_an_honest_zero(area):
    """The command layer refuses an empty write; the module stays total for a caller that
    reaches it directly, and reports the zero rather than inventing a sequence."""
    result = streams.stream_write(area, TICKET, ATTEMPT, ACTOR, "thought", [])

    assert result.exit_code == 0
    assert result.lines == [f"wrote 0 record(s) to .arbite/streams/{TICKET}.jsonl"]
    assert (result.data["records"], result.data["cursor"], result.data["seqs"]) == (0, 0, [])
    assert streams.read_records(area, TICKET) == ([], 0)


def test_list_prints_a_row_per_ticket_and_exits_two_when_there_are_none(area):
    empty = streams.stream_list(area)

    assert empty.exit_code == 2
    assert empty.lines == ["no streams in .arbite/streams/"]
    assert empty.to_json() == {"streams": [], "count": 0, "next_actions": []}

    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])
    listed = streams.stream_list(area)

    assert listed.lines[0] == "1 stream(s) in .arbite/streams/:"
    assert listed.lines[1].startswith(f"  {TICKET}   2 record(s)  ")
    assert listed.lines[1].endswith(f" by {ACTOR}")
    entry = listed.data["streams"][0]
    assert entry["records"] == 2
    assert entry["cursor"] == 2
    assert entry["attempt"] == ATTEMPT
    assert entry["path"] == f".arbite/streams/{TICKET}.jsonl"
    assert listed.data["count"] == 1


def test_a_stream_file_with_no_records_is_reported_honestly(area):
    streams.ensure_streams_dir(area)
    (streams.streams_root(area) / f"{TICKET}{streams.STREAM_SUFFIX}").write_text("")

    listed = streams.stream_list(area)

    assert listed.lines[1].endswith("(no records yet)")
    assert listed.data["streams"][0]["last_at"] is None


def test_path_prints_the_absolute_file_and_counts_what_is_in_it(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])

    result = streams.stream_path_result(area, TICKET)

    assert result.lines == [str(area / "streams" / f"{TICKET}.jsonl")]
    assert result.to_json()["path"] == result.lines[0]
    assert result.to_json()["records"] == 1


# --- summary, notes and clear ----------------------------------------------


def test_summary_describes_an_empty_area_and_a_populated_one(area):
    assert streams.stream_summary(area).describe() == "no streams"
    assert streams.stream_summary(area).is_empty is True

    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    summary = streams.stream_summary(area)

    assert summary.files == 1
    assert summary.bytes == (area / "streams" / f"{TICKET}.jsonl").stat().st_size
    assert summary.describe() == f"1 stream(s), {streams.human_size(summary.bytes)}"
    assert summary.to_dict() == {"files": 1, "bytes": summary.bytes}


def test_the_lock_file_is_not_a_stream(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])

    assert (streams.streams_root(area) / streams.LOCK_NAME).exists()
    assert [entry.ticket for entry in streams.stream_entries(area)] == [TICKET]
    assert streams.stream_summary(area).files == 1


def test_doctor_notes_say_nothing_when_nothing_is_recorded_and_nothing_is_in_flight(area):
    """The quiet case is the normal one, and DR1/DR2/DR3 keep their frozen shape
    because of it."""
    assert streams.stream_note_lines(streams.StreamSummary(), []) == []


def test_doctor_notes_report_the_area_and_the_tickets_that_stay_quiet(area):
    assert streams.stream_note_lines(streams.StreamSummary(), [TICKET]) == [
        f"note: 1 ticket(s) in flight have no stream entries: {TICKET}"
    ]

    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    summary = streams.stream_summary(area)

    assert streams.stream_note_lines(summary, []) == [
        f"note: .arbite/streams/ holds 1 stream(s) ({streams.human_size(summary.bytes)})"
    ]


def test_clear_needs_a_target_or_all_but_not_both(area):
    with pytest.raises(CoordinationError) as neither:
        streams.stream_clear(area)
    assert "needs a ticket id" in str(neither.value)

    with pytest.raises(CoordinationError) as both:
        streams.stream_clear(area, [TICKET], all_=True)
    assert "not both" in str(both.value)


def test_clear_refuses_a_ticket_that_never_narrated(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])

    with pytest.raises(PathRefused) as refusal:
        streams.stream_clear(area, ["tic-c3d4"])

    assert refusal.value.text_hint == streams.STAGED_HINT
    assert streams.read_records(area, TICKET)[0] != []


def test_clear_of_one_ticket_keeps_the_lock_file_and_names_the_next_step(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a", "b"])

    result = streams.stream_clear(area, [TICKET])

    assert result.lines[0] == (
        f"cleared .arbite/streams/{TICKET}.jsonl (2 record(s), "
        f"{streams.human_size(result.data['bytes'])})"
    )
    assert result.next_actions == [streams.LIST_COMMAND]
    assert not streams.stream_path(area, TICKET).exists()
    assert (streams.streams_root(area) / streams.LOCK_NAME).exists()
    assert streams.stream_entries(area) == []


def test_clear_all_reports_what_it_removed_and_an_honest_zero(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    streams.append_records(area, "tic-c3d4", ATTEMPT, ACTOR, "thought", ["b", "c"])

    cleared = streams.stream_clear(area, all_=True)

    assert cleared.lines[0].startswith("cleared 2 stream(s) from .arbite/streams/ (")
    assert f"{TICKET} (1 record(s)," in cleared.lines[0]
    assert cleared.data["count"] == 2
    assert cleared.data["bytes"] > 0

    again = streams.stream_clear(area, all_=True)

    assert again.lines == ["cleared 0 streams from .arbite/streams/ (nothing was recorded)"]
    assert again.data["count"] == 0
    assert (streams.streams_root(area) / streams.LOCK_NAME).exists()


def test_clear_of_several_named_tickets_reports_them_in_one_sentence(area):
    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])
    streams.append_records(area, "tic-c3d4", ATTEMPT, ACTOR, "thought", ["b"])

    result = streams.stream_clear(area, [TICKET, "tic-c3d4"])

    assert result.lines[0].startswith("cleared 2 stream(s) from .arbite/streams/ (")
    assert result.data["count"] == 2


def test_the_area_is_created_on_demand(area):
    """Appending to a project that never narrated creates the directory, exactly as
    scratch does, so no caller has to ask for it."""
    assert not streams.streams_root(area).exists()

    streams.append_records(area, TICKET, ATTEMPT, ACTOR, "thought", ["a"])

    assert streams.streams_root(area).is_dir()
