"""Where the narration stream meets the lifecycle: claim, submit, doctor and the report.

The stream is a *suggestion*: `claim` names the file so a worker can narrate, `submit`
says one sentence when the attempt that just ended narrated nothing, `doctor` reports the
area and which in-flight tickets stayed quiet, and `workspace show` reports it as one more
piece of the workspace. Nothing here is ever refused for staying silent -- these tests pin
that too, because a soft gate that quietly became a hard one would be a worse contract
than no gate at all.
"""

from __future__ import annotations

import json

import pytest

import examples
import lifecycle_state

AGENT = "claude.opus.001"
TICKET = "tic-cf9f"

with_review = pytest.mark.parametrize("review", [True, False])


def initialise(tmp_path, kind: str, review: bool = True):
    """An initialised project, with the review chain on or off."""
    project = lifecycle_state.initialise(tmp_path, kind)
    if not review:
        config = project / ".arbite" / "project.yaml"
        config.write_text(config.read_text() + "review: false\n")
    return project


def claimable(project, kind: str, ticket_id: str = TICKET):
    lifecycle_state.claimable(project, ticket_id, kind)
    return ticket_id


def claim(project, ticket_id: str = TICKET, agent: str = AGENT):
    return examples.run_cli(project, "claim", ticket_id, "--agent", agent, "--json")


def write(project, *texts, ticket_id: str = TICKET):
    return examples.run_cli(project, "stream", "write", ticket_id, *texts)


def doctor(project, *args):
    return examples.run_cli(project, "doctor", *args)


# --- claim names the stream -------------------------------------------------


def test_claim_prints_the_stream_file_and_the_command_that_writes_it(tmp_path, kind):
    project = initialise(tmp_path, kind)
    claimable(project, kind)

    proc = examples.run_cli(project, "claim", TICKET, "--agent", AGENT)

    stream = project / ".arbite" / "streams" / f"{TICKET}.jsonl"
    line = f"stream: {stream} (write with 'arbite stream write {TICKET} -')"
    lines = proc.stdout.splitlines()
    assert line in lines
    # Directly before the file hint, so the two things a worker does next -- narrate, then
    # claim the paths -- are the last two lines it reads.
    assert lines[lines.index(line) + 1].startswith("next: ")


def test_claim_json_carries_the_same_facts(tmp_path, kind):
    project = initialise(tmp_path, kind)
    claimable(project, kind)

    payload = json.loads(claim(project).stdout)

    assert payload["stream"] == {
        "path": str(project / ".arbite" / "streams" / f"{TICKET}.jsonl"),
        "write": f"arbite stream write {TICKET} -",
    }


def test_promote_agent_reports_the_stream_too(tmp_path, kind):
    """`promote --agent` claims in the same call, so it has to hand back the same facts."""
    project = initialise(tmp_path, kind)
    examples.run_cli(project, "raw", "feature", "a raw capture")
    raw = json.loads(examples.run_cli(project, "list", "raw", "--json").stdout)[0]["id"]

    proc = examples.run_cli(
        project, "promote", raw, "--title", "classified", "--tier", "medium", "--domain", "io",
        "--agent", AGENT,
    )

    assert f"arbite stream write {raw} -" in proc.stdout


def test_list_next_claim_reports_the_stream_too(tmp_path, kind):
    """The batch dispatch prints a table rather than a claim report, so the stream facts
    reach a dispatcher through `--json` -- and they have to be there, because a claimed
    ticket with no attempt still has somewhere to narrate."""
    project = initialise(tmp_path, kind)
    claimable(project, kind, "tic-c3d4")

    proc = examples.run_cli(project, "list", "next", "--claim", AGENT, "--json")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload[0]["stream"] == {
        "path": str(project / ".arbite" / "streams" / "tic-c3d4.jsonl"),
        "write": "arbite stream write tic-c3d4 -",
    }


# --- submit's soft gate -----------------------------------------------------


@with_review
def test_submit_says_so_when_the_ending_attempt_narrated_nothing(tmp_path, kind, review):
    project = initialise(tmp_path, kind, review)
    claimable(project, kind)
    attempt = json.loads(claim(project).stdout)["attempt"]["id"]

    proc = examples.run_cli(project, "submit", TICKET)

    assert proc.returncode == 0, proc.stderr
    assert (
        f"note: attempt {attempt} recorded no stream entries "
        f"(narrate with 'arbite stream write {TICKET} -')"
    ) in proc.stdout.splitlines()
    # The softest possible gate: the ticket still moved.
    assert proc.stdout.splitlines()[0].startswith("submitted ")


@with_review
def test_submit_stays_quiet_when_the_attempt_narrated(tmp_path, kind, review):
    project = initialise(tmp_path, kind, review)
    claimable(project, kind)
    claim(project)
    write(project, "narrating as I go")

    proc = examples.run_cli(project, "submit", TICKET)

    assert proc.returncode == 0, proc.stderr
    assert "stream" not in proc.stdout
    assert "note:" not in proc.stdout


def test_close_does_not_gate_on_narration(tmp_path, kind):
    """Only `submit` nudges: closing work arbite was never asked to narrate prints no note
    about it."""
    project = initialise(tmp_path, kind)
    claimable(project, kind)
    claim(project)

    proc = examples.run_cli(project, "close", TICKET)

    assert proc.returncode == 0, proc.stderr
    assert "stream" not in proc.stdout


# --- doctor -----------------------------------------------------------------


def test_doctor_names_the_in_flight_ticket_that_is_not_narrating(tmp_path, kind):
    project = initialise(tmp_path, kind)
    claimable(project, kind)
    claim(project)

    proc = doctor(project)

    assert proc.returncode == 0, proc.stderr
    assert f"note: 1 ticket(s) in flight have no stream entries: {TICKET}" in proc.stdout
    # An area with nothing in it says nothing: the quiet case is the normal one.
    assert ".arbite/streams/ holds" not in proc.stdout


def test_the_review_case_counts_the_attempt_that_finished_the_work(tmp_path, kind):
    """A ticket in review has no *active* attempt, so the fact that it stayed quiet has to
    come from the attempt that submitted it -- otherwise the note disappears exactly when a
    reviewer would want it."""
    project = initialise(tmp_path, kind)
    claimable(project, kind)
    claim(project)
    examples.run_cli(project, "submit", TICKET)

    payload = json.loads(doctor(project, "--json").stdout)

    assert payload["streams_missing"] == [TICKET]


def test_doctor_reports_the_area_in_text_and_json(tmp_path, kind):
    project = initialise(tmp_path, kind)
    claimable(project, kind)
    claim(project)
    write(project, "hello")

    text = doctor(project)
    payload = json.loads(doctor(project, "--json").stdout)

    assert "note: .arbite/streams/ holds 1 stream(s) (" in text.stdout
    # The ticket now *is* narrating, so it is not one of the quiet ones.
    assert "in flight" not in text.stdout
    assert payload["streams"]["files"] == 1
    assert payload["streams"]["bytes"] > 0
    assert payload["streams_missing"] == []


def test_doctor_stays_silent_about_narration_in_a_quiet_project(tmp_path, kind):
    """DR1/DR2/DR3's shape: no streams and nothing in flight means no note at all, which is
    what keeps a clean report clean."""
    project = initialise(tmp_path, kind)

    text = doctor(project)
    payload = json.loads(doctor(project, "--json").stdout)

    assert text.returncode == 0, text.stderr
    assert "stream" not in text.stdout
    assert payload["streams"] == {"files": 0, "bytes": 0}
    assert payload["streams_missing"] == []


def test_a_ticket_that_is_only_open_is_not_in_flight(tmp_path, kind):
    """`open` work nobody has claimed has no attempt that could be quiet."""
    project = initialise(tmp_path, kind)
    claimable(project, kind, "tic-a1b2")

    proc = doctor(project)

    assert "in flight" not in proc.stdout
    assert json.loads(doctor(project, "--json").stdout)["streams_missing"] == []


# --- workspace show ---------------------------------------------------------


def test_workspace_show_reports_the_stream_area(tmp_path, kind):
    project = initialise(tmp_path, kind)

    empty = examples.run_cli(project, "workspace", "show")
    payload = json.loads(examples.run_cli(project, "workspace", "show", "--json").stdout)

    assert "streams:   .arbite/streams/  (no streams)" in empty.stdout
    assert payload["streams"] == {"root": ".arbite/streams", "files": 0, "bytes": 0}

    claimable(project, kind)
    claim(project)
    write(project, "hi")

    narrated = examples.run_cli(project, "workspace", "show")
    after = json.loads(examples.run_cli(project, "workspace", "show", "--json").stdout)

    assert "streams:   .arbite/streams/  (1 stream(s), " in narrated.stdout
    assert after["streams"]["files"] == 1
    assert after["streams"]["bytes"] > 0
