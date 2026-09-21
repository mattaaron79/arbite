"""The frozen event-stream transcripts this slice owns: EV2, EV3, EV4, EV5, EV7.

Each is asserted against its block in `.arbite/planning/interaction-examples.md` --
command, stdout, stderr and exit code -- with ids, times and paths normalised on
both sides (`examples.py`). The fixture (`event_stream.py`) builds the state the
transcripts describe, so "the transcript passes" means the real command prints
exactly what the document says, on a store holding the events the document counts.
"""

from __future__ import annotations

import json

import pytest

import examples
from event_stream import build_project


@pytest.fixture
def ev_project(tmp_path):
    """EV2/EV3/EV4/EV5's state: 34 events, the newest four being their rows."""
    return build_project(tmp_path)


def test_EV2_tail_the_stream(ev_project):
    """One line per event, carrying kind, subject, operation, ticket/attempt, actor,
    local time and outcome, then the cursor to keep."""
    examples.assert_scenario(examples.scenario_block("EV2"), ev_project)


def test_EV3_resume_from_a_cursor(ev_project):
    examples.assert_scenario(examples.scenario_block("EV3"), ev_project)


def test_EV3_the_poll_shape_is_the_documented_one(ev_project):
    """EV3 documents the JSON an orchestrator polls with in a second block, so the
    shape a caller branches on cannot drift from the prose that promises it.

    The block is marked "abridged" (it shows one of the two events), so the facts it
    states -- the cursor, the next action, and that event's own fields -- are what is
    asserted, not the whole payload."""
    documented = examples.scenario_json_blocks("EV3")
    assert documented, "EV3 must document the poll shape in a second block"

    proc = examples.run_cli(ev_project, "events", "--after", "32", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = examples.normalise_payload(json.loads(proc.stdout), ev_project)
    expected = examples.normalise_payload(documented[0], ev_project)

    assert payload["cursor"] == expected["cursor"]
    assert payload["next_actions"] == expected["next_actions"]
    for event in expected["events"]:
        assert event in payload["events"], event


def test_EV4_nothing_new_since_a_cursor(ev_project):
    """'Nothing happened' is an answer with an exit code, so a poll does not have to
    parse text to know."""
    examples.assert_scenario(examples.scenario_block("EV4"), ev_project)


def test_EV5_reads_are_a_separate_category(ev_project):
    examples.assert_scenario(examples.scenario_block("EV5"), ev_project)


def test_EV7_no_follow(ev_project):
    """A blocking watcher is a sleeping process, so the flag is refused -- and the
    refusal teaches the pattern that works instead."""
    examples.assert_scenario(examples.scenario_block("EV7"), ev_project)
