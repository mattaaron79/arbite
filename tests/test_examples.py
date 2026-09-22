"""The frozen transcripts this slice owns: WS1, WS2 and DR4.

Each scenario is asserted against the block in
`.arbite/planning/interaction-examples.md` -- command, stdout, stderr and exit code
-- with ids, times and paths normalised on both sides (`examples.py`). The fixtures
build the state the transcript describes (2 active claims, 31 events, 14 receipts, a
scratch payload, and for DR4 22 tickets and 1 pending operation), so "the transcript
passes" means the real command prints exactly what the document says.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import evidence_state
import examples
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket

# The document's own numbers, stated once here so a fixture cannot quietly drift
# from the transcript it is meant to reproduce.
EVENTS = 31
RECEIPTS = 14
ACTIVE_CLAIMS = 2
DR4_TICKETS = 22
WS1_SCRATCH_BYTES = 4198  # 4.1 KiB
DR4_SCRATCH_SIZES = (4000, 4000, 4700)  # 12.4 KiB in total


def _initialise(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project whose committed choice is `sink_kind`.

    The `sink:` key is written before `init` because the WS transcripts print the
    *source* of the store selection ("sink: file in .arbite/project.yaml"), and a
    project that simply accepts the default has no such key to name."""
    project = tmp_path / "project"
    project.mkdir()
    arbite_dir = project / ".arbite"
    arbite_dir.mkdir()
    (arbite_dir / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    proc = examples.run_cli(project, "init")
    assert proc.returncode == 0, proc.stderr
    return project


def _coordination(project: Path, sink_kind: str = "file"):
    """The coordination store for the project's sink, as the CLI resolves it."""
    sink = build_sink(SinkSpec(kind=sink_kind), project / ".arbite")
    return open_coordination_store(sink)


def _seed_coordination(project: Path, claims: int, pending_receipts: int = 0) -> None:
    """Record the attempts, claims, events and receipts the transcripts count.

    Written through the store rather than by hand-writing files: the layout is the
    backend's business, and a fixture that knew it would pass even if the store
    stopped reading what it writes."""
    store = _coordination(project)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init must record the workspace"

    store.put_record(
        coordination_records.WorkAttempt(
            id="att-91bd",
            ticket_id="tic-cf9f",
            worker_id="claude.opus.001",
            workspace_id=workspace.id,
            generation=1,
            state="active",
            started=coordination_records.utc_now(),
            last_activity=coordination_records.utc_now(),
        )
    )
    for index in range(claims):
        store.put_record(
            coordination_records.FileClaim(
                id=f"clm-9b5{index}",
                workspace_id=workspace.id,
                path=f"src/arbite/sink_{index}.py",
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
                generation=1,
                acquired=coordination_records.utc_now(),
                observed_version=coordination_records.ABSENT,
            )
        )
    for cursor in range(1, EVENTS + 1):
        store.put_record(
            coordination_records.Event(
                id=f"evt-{cursor:04x}",
                cursor=cursor,
                kind="claim.acquired",
                recorded_at=coordination_records.utc_now(),
                category="claim",
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
            )
        )
    for index in range(RECEIPTS):
        digest = "sha256:" + f"{index + 1:064d}"
        store.put_record(
            coordination_records.OperationReceipt(
                id=f"op-4f{index:02x}",
                kind="write",
                paths=["src/arbite/schema.py"],
                result=(
                    coordination_records.RECEIPT_PENDING
                    if index < pending_receipts
                    else coordination_records.RECEIPT_SUCCEEDED
                ),
                recorded_at=coordination_records.utc_now(),
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
                before={"src/arbite/schema.py": coordination_records.ABSENT},
                after={"src/arbite/schema.py": digest},
                claim_generation=1,
            )
        )


def _scratch_payload(project: Path, sizes) -> None:
    root = project / ".arbite" / "scratch"
    root.mkdir(parents=True, exist_ok=True)
    for index, size in enumerate(sizes):
        (root / f"payload-{index}.py").write_bytes(b"x" * size)


def _ws2_state(tmp_path: Path) -> Path:
    """WS2's state without the fixture machinery, for the harness's own tests (a
    fixture cannot be requested from inside a test function)."""
    project = _initialise(tmp_path)
    _seed_coordination(project, claims=0)
    return project


@pytest.fixture
def ws1_project(tmp_path):
    """WS1's state: two active claims, a populated history, one leftover payload."""
    project = _initialise(tmp_path)
    _seed_coordination(project, claims=ACTIVE_CLAIMS)
    _scratch_payload(project, [WS1_SCRATCH_BYTES])
    return project


@pytest.fixture
def ws2_project(tmp_path):
    """WS2's state: the same history with nothing active and no payload."""
    return _ws2_state(tmp_path)


@pytest.fixture
def dr4_project(tmp_path):
    """DR4's state: 22 clean tickets, one pending operation, three payload files.

    One of the 22 is `tic-cf9f`, the ticket the seeded attempt belongs to: `doctor` reads the
    coordination records against the ticket set it just checked, so an attempt naming a ticket
    that store does not hold is a finding (`attempt_without_ticket`) -- and the transcript's
    store is clean. The count the transcript asserts (22) is unchanged."""
    project = _initialise(tmp_path)
    sink = build_sink(SinkSpec(kind="file"), project / ".arbite")
    for index in range(DR4_TICKETS - 1):
        sink.create(make_ticket(f"tic-{index + 0x1000:04x}"))
    sink.create(make_ticket("tic-cf9f"))
    _seed_coordination(project, claims=ACTIVE_CLAIMS, pending_receipts=1)
    _scratch_payload(project, DR4_SCRATCH_SIZES)
    return project


# --- the scenarios ---------------------------------------------------------


def test_ws1_report_the_derived_workspace(ws1_project):
    examples.assert_scenario(examples.scenario_block("WS1"), ws1_project)


def test_ws2_workspace_with_nothing_active(ws2_project):
    examples.assert_scenario(examples.scenario_block("WS2"), ws2_project)


def test_dr4_doctor_names_the_stores_coordination_backend(dr4_project):
    examples.assert_scenario(examples.scenario_block("DR4"), dr4_project)


# --- EV1, EV6: the change receipts and net change views ---------------------


@pytest.fixture
def ev_project(tmp_path, kind):
    """EV1's and EV6's world, on both sinks: five operations by one attempt on three paths."""
    return evidence_state.ev_project(tmp_path, kind)


def test_EV1_net_changes_for_a_ticket(ev_project, kind):
    """The net view, per attempt, with the two operations behind one row named.

    Byte for byte on both sinks: the header, each row's letters, version pair and delta, the
    operation lists in log order, the revert note and the exit code. C15 corrected the
    document's hand-aligned rows (its operation column started at 91, 91 and 89, which no
    padding rule produces); the command pads each column to the widest cell of its group, and
    `test_change_evidence.py` asserts the same rows against the layout rule directly.
    """
    stdout = examples.assert_scenario(
        examples.scenario_block("EV1"), ev_project, sink=kind
    )

    assert "edit-then-revert: both operations remain in the log ('--all')" in stdout


def test_EV6_one_receipt(ev_project, kind):
    """One operation's evidence: `result: ok`, both versions with their shapes, and the image
    the receipt keeps.

    EV6's command names an operation id, which is the one fact a transcript cannot carry --
    the fixture performs the operation, so the harness substitutes its id for the document's
    placeholder exactly as it substitutes ticket ids. Everything else is compared byte for
    byte, on both sinks."""
    operation = evidence_state.base_operation(ev_project, kind)
    scenario = examples.with_token(examples.scenario_block("EV6"), operation)

    examples.assert_scenario(scenario, ev_project, sink=kind)
    assert scenario.exit_code == 0


# --- the harness itself ----------------------------------------------------


def test_the_harness_reads_the_frozen_transcript():
    """The block is read from the document, so the test cannot pass on a copy."""
    scenario = examples.scenario_block("WS1")

    assert scenario.title == "report the derived workspace"
    assert scenario.command == ("workspace", "show")
    assert scenario.exit_code == 0
    assert scenario.is_json is False
    assert "ws-7c41" in scenario.stdout


def test_the_harness_reads_a_json_transcript_as_facts():
    """DR4's block is a JSON document: parsed, not compared character by character."""
    scenario = examples.scenario_block("DR4")

    assert scenario.command == ("doctor", "--json")
    assert scenario.is_json is True
    assert scenario.json_payload["sink"]["kind"] == "file"
    assert scenario.json_payload["coordination"]["claims_active"] == 2


def test_normalisation_substitutes_ids_times_and_paths():
    """Every fact that differs per machine is replaced, and nothing else is."""
    raw = (
        "claimed tic-cf9f for claude.opus.001 at 06:12:41 on 2026-09-21\n"
        "attempt: att-91bd (generation 1), workspace ws-7c41, receipt op-4f19\n"
        "root: /media/matt/m2tb/projects/arbite\n"
        "path: .arbite/coordination/claims\n"
    )

    assert examples.normalise(raw) == (
        "claimed tic-XXXX for claude.opus.001 at HH:MM:SS on YYYY-MM-DD\n"
        "attempt: att-XXXX (generation 1), workspace ws-XXXX, receipt op-XXXX\n"
        "root: <ROOT>\n"
        "path: <ARBITE>/coordination/claims\n"
    )


def test_normalisation_collapses_any_checkouts_arbite_path():
    """A transcript's literal path and a real temporary project both land on the
    same placeholder, which is what makes one frozen block portable."""
    elsewhere = "/tmp/pytest-123/test_ws1_project0/project/.arbite/coordination/"

    assert examples.normalise(elsewhere) == "<ARBITE>/coordination/"
    assert examples.normalise("/elsewhere/.arbite") == "<ARBITE>"


def test_normalisation_keeps_utc_stamps_distinct_from_local_times():
    """A stored UTC timestamp and a printed local time normalise differently, so a
    JSON fact and a text fact cannot be confused for each other."""
    normalised = examples.normalise("started 2026-09-21T13:12:04Z, printed 06:12:04")

    assert normalised == "started YYYY-MM-DDTHH:MM:SSZ, printed HH:MM:SS"


def test_the_harness_rejects_output_that_differs(tmp_path):
    """A transcript that does not match fails, rather than passing quietly: the
    scenarios above are only worth something if this one fails."""
    scenario = examples.scenario_block("WS2")
    wrong = examples.Scenario(
        id="WS2",
        title=scenario.title,
        command=scenario.command,
        exit_code=scenario.exit_code,
        stdout=scenario.stdout.replace("no active claims", "3 active claims"),
    )

    with pytest.raises(AssertionError) as failure:
        examples.assert_scenario(wrong, _ws2_state(tmp_path))

    assert "text differs" in str(failure.value)


def test_the_harness_rejects_a_wrong_exit_code(ws2_project):
    """Exit codes are asserted, not just output."""
    scenario = examples.scenario_block("WS2")
    wrong = examples.Scenario(
        id="WS2",
        title=scenario.title,
        command=scenario.command,
        exit_code=scenario.exit_code + 1,
        stdout=scenario.stdout,
    )

    with pytest.raises(AssertionError) as failure:
        examples.assert_scenario(wrong, ws2_project)

    assert "exited 0, expected 1" in str(failure.value)


def test_a_json_transcript_compares_parsed_facts(dr4_project):
    """Re-indenting a JSON transcript does not change what it asserts."""
    scenario = examples.scenario_block("DR4")
    reformatted = examples.Scenario(
        id=scenario.id,
        title=scenario.title,
        command=scenario.command,
        exit_code=scenario.exit_code,
        stdout="",
        json_payload=json.loads(json.dumps(scenario.json_payload)),
    )

    examples.assert_scenario(reformatted, dr4_project)
