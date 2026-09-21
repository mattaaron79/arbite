"""`arbite events`: the flags, the exit codes, and one answer on both sinks.

The transcripts (EV2-EV5, EV7) pin the exact output; this module pins the behaviour
around them -- that the read filter changes the answer, that a poll can follow the
cursor it is handed, that bad input is refused without changing anything, and that
the file and SQLite stores print the same stream.
"""

from __future__ import annotations

import json

import pytest

import examples
from arbite.coordination.app import DEFAULT_EVENT_TAIL
from event_stream import EARLIER_EVENTS, NEWEST_ROWS, OBSERVATION_CURSORS, build_project

ALL_EVENTS = EARLIER_EVENTS + len(NEWEST_ROWS)
NEWEST_CURSOR = NEWEST_ROWS[-1]["cursor"]


@pytest.fixture
def ev_project(tmp_path):
    return build_project(tmp_path)


def run(project, *args):
    return examples.run_cli(project, "events", *args)


def rows(proc) -> list:
    """The cursor of each printed event row (the report's last line is the cursor)."""
    return [int(line.split()[0]) for line in proc.stdout.splitlines()[:-1]]


# --- what the stream shows --------------------------------------------------


def test_a_bare_command_prints_the_tail_the_refusal_suggests(ev_project):
    """The `--follow` refusal recommends `--tail 20`, so that has to be what a bare
    `arbite events` does -- otherwise the advice and the default disagree."""
    bare = run(ev_project)
    explicit = run(ev_project, "--tail", str(DEFAULT_EVENT_TAIL))

    assert bare.returncode == 0, bare.stderr
    assert bare.stdout == explicit.stdout
    assert f"--tail {DEFAULT_EVENT_TAIL}" in run(ev_project, "--follow").stdout


def test_a_project_with_no_coordination_state_reads_as_empty(tmp_path):
    """A store that was never initialised answers "nothing recorded" rather than
    failing: every project is in that state until its first recorded operation."""
    project = tmp_path / "project"
    (project / ".arbite").mkdir(parents=True)
    (project / ".arbite" / "project.yaml").write_text("sink: file\n", encoding="utf-8")

    proc = examples.run_cli(project, "events")

    assert proc.returncode == 2
    assert proc.stderr == ""
    assert proc.stdout == "no events recorded\ncursor: 0\n"


def test_tail_keeps_the_last_rows_and_names_the_cursor_to_keep(ev_project):
    proc = run(ev_project, "--tail", "3")

    assert rows(proc) == [32, 33, 34]
    assert proc.stdout.endswith("cursor: 34 (resume with 'arbite events --after 34')\n")


def test_after_resumes_from_a_cursor(ev_project):
    proc = run(ev_project, "--after", "32")

    assert rows(proc) == [33, 34]
    assert proc.stdout.endswith("cursor: 34 (resume with 'arbite events --after 34')\n")


def test_after_zero_reads_the_whole_stream_in_cursor_order(ev_project):
    proc = run(ev_project, "--after", "0", "--include-reads")

    assert rows(proc) == list(range(1, ALL_EVENTS + 1))


def test_reads_are_excluded_by_default_and_included_on_request(ev_project):
    """The flag has to change the answer or it is decoration. The fixture's sixth and
    seventh observation cursors sit where they make the difference visible: the
    default view skips cursor 29, the read-inclusive one shows it."""
    default = run(ev_project, "--tail", "6")
    including = run(ev_project, "--tail", "6", "--include-reads")

    assert rows(including) == [29, 30, 31, 32, 33, 34]
    assert rows(default) == [28, 30, 31, 32, 33, 34]


def test_a_served_read_is_part_of_the_job_stream(ev_project):
    """`read.file` is a file *activity*, so it shows in the default view; the `read`
    *category* is the observation stream the flag exposes. The frozen EV2/EV3 rows
    depend on exactly that distinction."""
    default = run(ev_project, "--tail", "4")

    assert 33 in rows(default)
    assert "read.file" in default.stdout


# --- the poll shape ---------------------------------------------------------


def test_json_carries_the_same_facts_as_the_text(ev_project):
    """Text is primary, JSON is the branchable form: every fact on a printed row has
    to appear in the payload of the same command, or one of the two is missing a
    fact the other carries."""
    text = run(ev_project, "--tail", "4")
    payload = json.loads(run(ev_project, "--tail", "4", "--json").stdout)

    assert [event["cursor"] for event in payload["events"]] == [31, 32, 33, 34]
    assert payload["cursor"] == 34
    assert payload["next_actions"] == ["arbite events --after 34"]
    for event in payload["events"]:
        line = next(
            row for row in text.stdout.splitlines() if row.startswith(str(event["cursor"]))
        )
        for value in (event["kind"], event["subject"], event["actor"], event["result"]):
            assert value in line, (value, event, line)
        assert f"{event['ticket']}/{event['attempt']}" in line
        assert event["operation"] in line


def test_an_empty_answer_is_still_an_answer(ev_project):
    """Exit 2 with the cursor it was given, so a loop branches on the code."""
    proc = run(ev_project, "--after", "34", "--json")

    assert proc.returncode == 2
    assert proc.stderr == ""
    payload = json.loads(proc.stdout)
    assert payload["events"] == []
    assert payload["cursor"] == 34
    assert payload["next_actions"] == []


def test_a_poll_follows_the_cursor_it_is_handed_through_every_event_once(ev_project):
    """The loop a watcher would write: keep the cursor, call again, stop when the
    answer is "nothing new". Every event arrives exactly once, in cursor order."""
    seen, cursor, calls = [], 0, 0
    while True:
        proc = run(ev_project, "--after", str(cursor), "--json")
        calls += 1
        assert calls < ALL_EVENTS + 2, "the loop must terminate"
        if proc.returncode == 2:
            break
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        seen.extend(event["cursor"] for event in payload["events"])
        next_cursor = payload["cursor"]
        assert next_cursor > cursor, "a poll must not be handed the cursor it asked from"
        cursor = next_cursor

    # The default view skips the observation stream, so the loop sees the job
    # events only -- each exactly once, in order, ending at the newest one.
    assert seen == sorted(set(seen))
    assert seen[-1] == NEWEST_CURSOR
    assert len(seen) == ALL_EVENTS - len(OBSERVATION_CURSORS)


# --- refusals ---------------------------------------------------------------


def test_follow_is_refused_and_the_refusal_teaches_the_pattern(ev_project):
    proc = run(ev_project, "--follow")

    assert proc.returncode == 1
    assert proc.stderr == ""
    assert "one-shot and never block" in proc.stdout
    assert "--tail" in proc.stdout and "--after <cursor>" in proc.stdout


def test_follow_is_refused_in_json_too(ev_project):
    """The refusal is a result like any other, so `--json` gets the same facts."""
    proc = run(ev_project, "--follow", "--json")

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert "one-shot and never block" in payload["error"]
    assert payload["next_actions"]


def test_tail_and_after_are_alternatives(ev_project):
    proc = run(ev_project, "--after", "10", "--tail", "5")

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "--after and --tail are alternatives" in proc.stderr


def test_a_tail_of_zero_is_refused(ev_project):
    proc = run(ev_project, "--tail", "0")

    assert proc.returncode == 1
    assert "--tail must be at least 1" in proc.stderr


def test_a_negative_cursor_is_refused(ev_project):
    proc = run(ev_project, "--after", "-1")

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.startswith("error: --after is a cursor")


# --- one stream, two sinks --------------------------------------------------


@pytest.mark.parametrize("args", [("--tail", "4"), ("--after", "32"), ("--tail", "2", "--include-reads")])
def test_both_sinks_print_the_same_stream(tmp_path, args):
    """Coordination state lives in a different place per sink, and means the same
    thing: the same events, in the same order, laid out the same way."""
    file_project = build_project(tmp_path / "file-sink", sink_kind="file")
    sqlite_project = build_project(tmp_path / "sqlite-sink", sink_kind="sqlite")

    file_output = run(file_project, *args)
    sqlite_output = run(sqlite_project, *args)

    assert file_output.returncode == sqlite_output.returncode
    assert examples.normalise(file_output.stdout, file_project) == examples.normalise(
        sqlite_output.stdout, sqlite_project
    )
