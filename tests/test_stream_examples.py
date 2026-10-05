"""The frozen narration-stream transcripts this slice owns: ST1-ST6.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command, exit
code, stream and rows -- with ids, times and paths normalised on both sides (`examples.py`),
on both sinks: a stream is a project-local file whichever store holds the tickets, so the
same block has to come out of either one. `stream_state.py` builds the worlds the blocks
describe, claiming the ticket through the real command because that is what creates the
attempt every record is attributed to.

Beyond the transcripts, the invariants the ticket names are asserted directly, because one
frozen block cannot state them: a refusal writes nothing at all, a blank stdin line is not a
record, a clear leaves the lock file alone, and a stream file is invisible to the ticket
scan, discovery and claims.
"""

from __future__ import annotations

import json

import examples
import lifecycle_state
import stream_state as state

TICKET = state.TICKET
AGENT = state.AGENT
STREAM = state.STREAM_PATH


def _fence(scenario_id: str, index: int = 0):
    """One fenced transcript of a block (`scenarios` with more than one fence pass an
    index; `examples.scenario_block` reads the first)."""
    blocks = examples.fenced_blocks(scenario_id)
    if index == 0:
        return examples.scenario_block(scenario_id)
    return examples.scenario_from_block(blocks[index], scenario_id)


def _narrated(tmp_path, kind: str, count: int = len(state.LINES)):
    """A claimed ticket with the first `count` of the fixture's lines recorded."""
    project, attempt = state.claimed(tmp_path, kind)
    state.narrate(project, attempt, state.LINES[:count])
    return project


# --- ST1: narrate and read ---------------------------------------------------


def test_ST1_narration_is_one_record_per_line_and_read_back_in_order(tmp_path, kind):
    """The first fence writes a line argument, the second pipes two lines through stdin --
    the two ways a worker narrates -- and the third reads the whole stream back."""
    inline = tmp_path / "inline"
    inline.mkdir()
    project, _ = state.claimed(inline, kind)
    written = examples.assert_scenario(_fence("ST1", 0), project)

    assert written.startswith(f"wrote 1 record to {STREAM}")
    assert len(state.records(project)) == 1

    piped = tmp_path / "piped"
    piped.mkdir()
    project, attempt = state.claimed(piped, kind)
    state.narrate(project, attempt, state.LINES[:1])
    examples.assert_scenario(
        _fence("ST1", 1),
        project,
        stdin="".join(f"{text}\n" for _, text in state.LINES[1:3]),
    )
    assert [record["text"] for record in state.records(project)] == [
        text for _, text in state.LINES[:3]
    ]

    read = tmp_path / "read"
    read.mkdir()
    read = examples.assert_scenario(_fence("ST1", 2), _narrated(read, kind))

    assert f"cursor: 4 (resume with 'arbite stream read {TICKET} --after 4')" in read
    assert read.splitlines()[3].endswith(state.LINES[3][1])


def test_ST1_a_blank_line_is_not_a_record(tmp_path, kind):
    """The stdin fence drops blank and whitespace-only lines: an empty record would spend a
    sequence and say nothing."""
    project, attempt = state.claimed(tmp_path, kind)
    state.narrate(project, attempt, state.LINES[:1])

    examples.run_cli(
        project, "stream", "write", TICKET, "-", stdin="one\n\n   \ntwo\n"
    )

    assert [record["text"] for record in state.records(project)] == [
        state.LINES[0][1],
        "one",
        "two",
    ]


def test_ST1_the_records_name_the_attempt_that_was_working(tmp_path, kind):
    """Attribution is the attempt's, and `--actor` overrides only the name it is filed
    under -- arbite records attribution, never authentication."""
    project, attempt = state.claimed(tmp_path, kind)

    examples.run_cli(project, "stream", "write", TICKET, "mine")
    examples.run_cli(project, "stream", "write", TICKET, "--actor", "claude.opus.002", "theirs")

    first, second = state.records(project)

    assert first["attempt_id"] == attempt
    assert (first["actor"], second["actor"]) == (AGENT, "claude.opus.002")


# --- ST2: polling ------------------------------------------------------------


def test_ST2_after_resumes_and_nothing_new_is_exit_two(tmp_path, kind):
    """One world, two fences: the poll that finds the two new records, and the poll after
    them that finds none and says so with its own exit code."""
    project = _narrated(tmp_path, kind)

    resumed = examples.assert_scenario(_fence("ST2", 0), project)
    nothing = examples.assert_scenario(_fence("ST2", 1), project)

    assert resumed.splitlines()[0].startswith("3 ")
    assert nothing == f"no stream for {TICKET} since seq 4\n"


def test_ST2_the_two_selection_flags_are_alternatives(tmp_path, kind):
    project = _narrated(tmp_path, kind)

    refused = examples.run_cli(project, "stream", "read", TICKET, "--after", "1", "--tail", "2")

    assert refused.returncode == 1
    assert "--after and --tail are alternatives" in refused.stderr


def test_ST2_a_tail_bootstraps_from_the_end(tmp_path, kind):
    project = _narrated(tmp_path, kind)

    tailed = examples.run_cli(project, "stream", "read", TICKET, "--tail", "1")

    assert tailed.stdout.splitlines()[0].startswith("4 ")
    assert tailed.stdout.splitlines()[1] == (
        f"cursor: 4 (resume with 'arbite stream read {TICKET} --after 4')"
    )


# --- ST3: list and path ------------------------------------------------------


def test_ST3_list_and_path_agree_about_what_is_recording(tmp_path, kind):
    project = _narrated(tmp_path, kind)

    listed = examples.assert_scenario(_fence("ST3", 0), project)
    path = examples.assert_scenario(_fence("ST3", 1), project)

    assert listed.splitlines()[1].startswith(f"  {TICKET}   4 record(s)  ")
    assert path.strip() == str(state.stream_file(project))
    assert json.loads(examples.run_cli(project, "stream", "list", "--json").stdout)["count"] == 1


# --- ST4: clear --------------------------------------------------------------


def test_ST4_a_named_clear_removes_the_file_and_leaves_the_lock(tmp_path, kind):
    project = _narrated(tmp_path, kind)

    cleared = examples.assert_scenario(_fence("ST4", 0), project)
    again = examples.assert_scenario(_fence("ST4", 1), project)

    assert cleared.startswith(f"cleared {STREAM}")
    assert not state.stream_file(project).exists()
    assert (project / ".arbite" / "streams" / ".lock").exists()
    assert again == "cleared 0 streams from .arbite/streams/ (nothing was recorded)\n"


def test_ST4_clearing_a_ticket_that_never_narrated_is_refused(tmp_path, kind):
    project, _ = state.claimed(tmp_path, kind)

    refused = examples.run_cli(project, "stream", "clear", "tic-c3d4")

    assert refused.returncode == 1
    assert "no stream for 'tic-c3d4' in .arbite/streams/" in refused.stderr


# --- ST5: refuse narration nobody is working ---------------------------------


def test_ST5_writing_without_an_active_attempt_is_refused_and_writes_nothing(tmp_path, kind):
    project = state.initialise(tmp_path, kind)
    lifecycle_state.claimable(project, TICKET, kind)

    refused = examples.assert_scenario(_fence("ST5"), project)

    assert not state.stream_file(project).exists()
    assert not (project / ".arbite" / "streams" / ".lock").exists()
    assert refused.startswith("error: ")


# --- ST6: the submit soft gate ----------------------------------------------


def test_ST6_submit_notes_the_silence_but_still_submits(tmp_path):
    """The block's own transcript is the file sink's, because the `submitted ... ->`
    location is a property of the store (`<ARBITE>/review/tic-XXXX.md` there,
    `<ARBITE>/arbite.db#tic-XXXX` on the database sink); the note below it is not, and is
    asserted per sink by the test after this one."""
    project = state.initialise(tmp_path, "file")
    lifecycle_state.claimable(project, TICKET, "file")
    examples.run_cli(project, "claim", TICKET, "--agent", AGENT)

    submitted = examples.assert_scenario(_fence("ST6"), project)

    assert "note: attempt" in submitted and "recorded no stream entries" in submitted
    # The gate is soft: the work moved anyway, and the write is what says so.
    assert "submitted" in submitted.splitlines()[0]
    assert json.loads(
        examples.run_cli(project, "show", TICKET, "--json").stdout
    )["status"] == "review"


def test_ST6_a_narrated_attempt_leaves_no_note(tmp_path, kind):
    project, attempt = state.claimed(tmp_path, kind)
    state.narrate(project, attempt, state.LINES[:1])

    submitted = examples.run_cli(project, "submit", TICKET)

    assert submitted.returncode == 0, submitted.stderr
    assert "stream" not in submitted.stdout
