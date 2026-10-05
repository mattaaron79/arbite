"""`arbite stream`: the five subcommands, their exit codes, and one answer per sink.

The stream area is a file beside the tickets rather than a store, so it is the same
place for both sinks -- which is exactly what these tests assert alongside the command
surface: `write` attributed to the active attempt, `read` with the poll semantics the
event stream has (a tail, `--after`, exit 2 for nothing new), `list`, `path`, and the
deliberate `clear`.
"""

from __future__ import annotations

import json

import pytest

import examples
import lifecycle_state

AGENT = "claude.opus.001"
OTHER = "claude.opus.002"
TICKET = "tic-cf9f"
STREAM = f".arbite/streams/{TICKET}.jsonl"


def claimed_project(tmp_path, kind: str):
    """`(project, attempt id)`: one claimed ticket, so narration has an attempt to
    belong to."""
    project = lifecycle_state.initialise(tmp_path, kind)
    lifecycle_state.claimable(project, TICKET, kind)
    claimed = examples.run_cli(project, "claim", TICKET, "--agent", AGENT, "--json")
    assert claimed.returncode == 0, claimed.stderr
    return project, json.loads(claimed.stdout)["attempt"]["id"]


@pytest.fixture
def claimed(tmp_path, kind):
    """One claimed ticket per test, built once and shared by the two views below."""
    return claimed_project(tmp_path, kind)


@pytest.fixture
def project(claimed):
    return claimed[0]


@pytest.fixture
def attempt(claimed):
    return claimed[1]


def stream_file(project):
    return project / ".arbite" / "streams" / f"{TICKET}.jsonl"


def records(project) -> list:
    """The stream's records, as stored."""
    return [json.loads(line) for line in stream_file(project).read_text().splitlines()]


def run(project, *args, stdin=None):
    return examples.run_cli(project, "stream", *args, stdin=stdin)


# --- write ------------------------------------------------------------------


def test_write_records_one_line_and_reports_its_sequence(project, attempt):
    proc = run(project, "write", TICKET, "thinking", "about", "it", "--json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload == {
        "ticket": TICKET,
        "attempt": attempt,
        "actor": AGENT,
        "kind": "thought",
        "path": STREAM,
        "records": 1,
        "cursor": 1,
        "seqs": [1],
        "next_actions": [],
    }
    # Unquoted words are one line, not three records: a sentence typed without quotes
    # records as the sentence it was meant to be.
    assert [record["text"] for record in records(project)] == ["thinking about it"]


def test_write_reports_the_same_thing_in_text(project):
    proc = run(project, "write", TICKET, "thinking")

    assert proc.stdout == f"wrote 1 record to {STREAM} (seq 1)\n"
    assert proc.stderr == ""


def test_write_from_stdin_records_one_line_per_line_and_drops_blanks(project):
    proc = run(project, "write", TICKET, "-", stdin="first\n\nsecond\n   \nthird\n")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"wrote 3 record(s) to {STREAM} (seq 1..3)\n"
    assert [record["text"] for record in records(project)] == ["first", "second", "third"]
    assert [record["seq"] for record in records(project)] == [1, 2, 3]


def test_write_continues_the_sequence_and_keeps_the_kind(project):
    run(project, "write", TICKET, "one")
    run(project, "write", TICKET, "--kind", "action", "two")
    run(project, "write", TICKET, "--kind", "result", "three")

    stored = records(project)

    assert [record["seq"] for record in stored] == [1, 2, 3]
    assert [record["kind"] for record in stored] == ["thought", "action", "result"]


def test_write_is_attributed_to_the_attempt_worker_unless_actor_says_otherwise(
    project, attempt
):
    """Attribution is recorded, never authenticated: `--actor` is how a worker says a
    line came from somebody else, and the attempt is still the one that was working."""
    run(project, "write", TICKET, "mine")
    run(project, "write", TICKET, "--actor", OTHER, "theirs")

    stored = records(project)

    assert [record["actor"] for record in stored] == [AGENT, OTHER]
    assert {record["attempt_id"] for record in stored} == {attempt}


def test_write_without_an_active_attempt_is_refused_and_records_nothing(tmp_path, kind):
    project = lifecycle_state.initialise(tmp_path, kind)
    lifecycle_state.claimable(project, TICKET, kind)

    proc = run(project, "write", TICKET, "nobody is working")

    assert proc.returncode == 1
    assert "is not being worked (no active attempt)" in proc.stderr
    assert "arbite claim" in proc.stderr
    assert not stream_file(project).exists()


def test_write_with_nothing_to_write_is_refused(project):
    empty_argument = run(project, "write", TICKET)
    empty_stdin = run(project, "write", TICKET, "-", stdin="\n\n")

    for refused in (empty_argument, empty_stdin):
        assert refused.returncode == 1
        assert "nothing to write" in refused.stderr
    assert not stream_file(project).exists()


def test_write_refuses_an_unknown_ticket(project):
    proc = run(project, "write", "tic-zzzz", "hello")

    assert proc.returncode == 1
    assert "no ticket found matching" in proc.stderr


# --- read -------------------------------------------------------------------


def test_a_bare_read_prints_the_tail_and_the_sequence_to_resume_from(project):
    run(project, "write", TICKET, "-", stdin="".join(f"line {n}\n" for n in range(1, 26)))

    proc = run(project, "read", TICKET)

    assert proc.returncode == 0
    rows = proc.stdout.splitlines()
    assert len(rows) == 21
    assert rows[0].startswith("6 ")
    assert rows[-1] == f"cursor: 25 (resume with 'arbite stream read {TICKET} --after 25')"
    assert "line 25" in rows[-2]


def test_after_prints_only_what_is_new(project):
    run(project, "write", TICKET, "-", stdin="one\ntwo\nthree\n")

    proc = run(project, "read", TICKET, "--after", "1")

    assert [line.split()[0] for line in proc.stdout.splitlines()[:-1]] == ["2", "3"]
    assert proc.stdout.endswith("cursor: 3 (resume with 'arbite stream read tic-cf9f --after 3')\n")


def test_tail_bootstraps_from_the_end(project):
    run(project, "write", TICKET, "-", stdin="one\ntwo\nthree\n")

    proc = run(project, "read", TICKET, "--tail", "2")

    assert [line.split()[0] for line in proc.stdout.splitlines()[:-1]] == ["2", "3"]


def test_nothing_new_exits_two_with_the_sequence_it_was_given(project):
    run(project, "write", TICKET, "one")

    proc = run(project, "read", TICKET, "--after", "7")

    assert proc.returncode == 2
    assert proc.stdout == f"no stream for {TICKET} since seq 7\n"
    assert proc.stderr == ""


def test_a_ticket_that_never_narrated_reads_as_empty(project):
    proc = run(project, "read", TICKET)

    assert proc.returncode == 2
    assert proc.stdout == f"no stream for {TICKET}\n"


def test_the_two_selection_flags_are_alternatives(project):
    proc = run(project, "read", TICKET, "--after", "1", "--tail", "2")

    assert proc.returncode == 1
    assert "--after and --tail are alternatives" in proc.stderr


def test_a_tail_of_zero_is_refused(project):
    proc = run(project, "read", TICKET, "--tail", "0")

    assert proc.returncode == 1
    assert "--tail must be at least 1" in proc.stderr


def test_json_carries_the_records_in_the_event_vocabulary(project, attempt):
    run(project, "write", TICKET, "--kind", "action", "did it")

    payload = json.loads(run(project, "read", TICKET, "--json").stdout)

    assert payload["ticket"] == TICKET
    assert payload["cursor"] == 1
    assert payload["next_actions"] == [f"arbite stream read {TICKET} --after 1"]
    assert payload["records"] == [
        {
            "seq": 1,
            "at": payload["records"][0]["at"],
            "ticket": TICKET,
            "attempt": attempt,
            "actor": AGENT,
            "kind": "action",
            "text": "did it",
        }
    ]


def test_a_poll_walks_a_stream_once_through_the_cursor_it_is_handed(project):
    """The loop a dashboard writes: keep the sequence, call again, stop on exit 2."""
    run(project, "write", TICKET, "-", stdin="a\nb\nc\n")
    seen = []
    cursor = 0
    for _ in range(4):
        proc = run(project, "read", TICKET, "--after", str(cursor), "--json")
        if proc.returncode == 2:
            break
        payload = json.loads(proc.stdout)
        seen.extend(record["text"] for record in payload["records"])
        cursor = payload["cursor"]

    assert seen == ["a", "b", "c"]


# --- list and path ----------------------------------------------------------


def test_list_prints_a_row_per_narrating_ticket(project):
    run(project, "write", TICKET, "one")

    proc = run(project, "list", "--json")

    payload = json.loads(proc.stdout)
    assert payload["count"] == 1
    assert payload["streams"][0]["ticket"] == TICKET
    assert payload["streams"][0]["records"] == 1

    text = run(project, "list")
    assert text.stdout.splitlines()[0] == "1 stream(s) in .arbite/streams/:"
    assert text.stdout.splitlines()[1].startswith(f"  {TICKET}   1 record(s)  ")


def test_list_of_a_project_that_never_narrated_exits_two(project):
    proc = run(project, "list")

    assert proc.returncode == 2
    assert proc.stdout == "no streams in .arbite/streams/\n"


def test_path_prints_the_absolute_file_for_a_tail(project):
    run(project, "write", TICKET, "one")

    proc = run(project, "path", TICKET)

    assert proc.returncode == 0
    assert proc.stdout.strip() == str(stream_file(project))
    assert proc.stdout.startswith("/")
    assert json.loads(run(project, "path", TICKET, "--json").stdout)["records"] == 1


def test_path_refuses_an_unknown_ticket(project):
    proc = run(project, "path", "tic-zzzz")

    assert proc.returncode == 1
    assert "no ticket found matching" in proc.stderr


# --- clear ------------------------------------------------------------------


def test_clear_of_one_ticket_removes_the_file_and_keeps_the_lock(project):
    run(project, "write", TICKET, "-", stdin="one\ntwo\n")

    proc = run(project, "clear", TICKET)

    assert proc.returncode == 0
    assert proc.stdout.startswith(f"cleared {STREAM} (2 record(s), ")
    assert f"next: 'arbite stream list' to see what remains" in proc.stdout
    assert not stream_file(project).exists()
    assert (project / ".arbite" / "streams" / ".lock").exists()


def test_clear_refuses_a_ticket_that_never_narrated(project):
    run(project, "write", TICKET, "one")

    proc = run(project, "clear", "tic-c3d4")

    assert proc.returncode == 1
    assert "no stream for 'tic-c3d4' in .arbite/streams/" in proc.stderr
    assert "arbite stream list" in proc.stderr


def test_clear_needs_a_target_or_all_but_not_both(project):
    neither = run(project, "clear")
    both = run(project, "clear", TICKET, "--all")

    assert neither.returncode == 1
    assert "needs a ticket id, or '--all'" in neither.stderr
    assert both.returncode == 1
    assert "takes ticket ids or '--all', not both" in both.stderr


def test_clear_all_reports_what_it_removed_and_zero_is_not_an_error(project):
    run(project, "write", TICKET, "one")

    cleared = run(project, "clear", "--all", "--json")

    assert cleared.returncode == 0
    payload = json.loads(cleared.stdout)
    assert payload["count"] == 1
    assert payload["cleared"][0]["ticket"] == TICKET
    assert payload["bytes"] > 0

    again = run(project, "clear", "--all")

    assert again.returncode == 0
    assert again.stdout == "cleared 0 streams from .arbite/streams/ (nothing was recorded)\n"
    assert (project / ".arbite" / "streams" / ".lock").exists()


# --- the same thing on both sinks -------------------------------------------


def test_the_stream_lands_in_the_same_project_local_area_on_both_sinks(tmp_path, kind):
    """The stream is a file beside the tickets, not a row in the store: switching sinks
    moves the tickets, not the narration."""
    project, _ = claimed_project(tmp_path, kind)

    proc = run(project, "write", TICKET, "hello")

    assert proc.returncode == 0, proc.stderr
    assert stream_file(project).is_file()
    assert stream_file(project).read_text().startswith('{"seq": 1,')
