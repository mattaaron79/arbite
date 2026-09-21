"""The workspace surface, end to end: `arbite init` records it, `workspace show` reports it.

These run the real CLI in a throwaway project, on both sinks, because the contract
being protected is the one an agent or a shell script sees: argv, exit codes, text
and `--json`. The scenarios themselves (WS1, WS2, DR4) are asserted against the
frozen transcripts in `test_examples.py`; what is checked here is everything those
three transcripts do not cover: the sqlite sink, the JSON shape, idempotence, and
the guard that refuses to build a layout over a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import examples
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store
from arbite.sinks import SinkSpec, build_sink

SINK_KINDS = ("file", "sqlite")


def make_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """A project directory with a committed sink choice, not yet initialised."""
    project = tmp_path / "project"
    project.mkdir()
    arbite_dir = project / ".arbite"
    arbite_dir.mkdir()
    (arbite_dir / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    return project


def init(project: Path, expect: int = 0, *args):
    proc = examples.run_cli(project, "init", *args)
    assert proc.returncode == expect, (
        f"arbite init -> exit {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


@pytest.fixture(params=SINK_KINDS)
def project(request, tmp_path) -> Path:
    """An initialised project, on each sink: `init` is where the workspace is recorded."""
    project = make_project(tmp_path, request.param)
    init(project)
    return project


def test_init_records_the_workspace_binding_and_the_layout(project):
    """`init` is the one command that establishes the binding, so no later command
    has to invent one -- and there is no `bind` command to forget to run."""
    sink_kind = "sqlite" if (project / ".arbite" / "arbite.db").exists() else "file"
    sink = build_sink(SinkSpec(kind=sink_kind), project / ".arbite")
    store = open_coordination_store(sink)

    workspace = store.get_workspace()

    assert workspace is not None
    assert workspace.root == str(project)
    assert workspace.store_kind == sink_kind
    assert workspace.coordination_kind == sink_kind
    assert workspace.id == coordination_records.derived_workspace_id(
        project, sink_kind, sink.root
    )
    assert (project / ".arbite" / "scratch").is_dir()

    if sink_kind == "file":
        for name in ("claims", "events", "receipts", "artifacts", "attempts", "observations"):
            assert (project / ".arbite" / "coordination" / name).is_dir(), name


def test_init_is_idempotent_and_keeps_one_binding(project):
    """Re-running `init` must not stack a second workspace record or lose the first:
    tickets already in the store have to survive it."""
    init(project)
    sink_kind = "sqlite" if (project / ".arbite" / "arbite.db").exists() else "file"
    store = open_coordination_store(build_sink(SinkSpec(kind=sink_kind), project / ".arbite"))

    first = store.get_workspace()
    init(project)
    second = store.get_workspace()

    assert first == second
    assert len(store.records("workspace")) == 1


def test_init_refuses_before_writing_when_the_layout_is_blocked(tmp_path):
    """A guard, checked before anything is written: a file where the coordination
    directory belongs is refused with an instruction, rather than replaced (which
    would destroy it) or built around (which would leave half a store)."""
    project = make_project(tmp_path)
    (project / ".arbite" / "coordination").write_text("not a directory\n", encoding="utf-8")

    proc = init(project, 1)

    assert "exists and is not a directory" in proc.stderr
    assert (project / ".arbite" / "coordination").read_text(encoding="utf-8") == "not a directory\n"
    store = open_coordination_store(build_sink(SinkSpec(kind="file"), project / ".arbite"))
    assert store.get_workspace() is None, "nothing was recorded"


@pytest.mark.parametrize("sink_kind", SINK_KINDS)
def test_workspace_show_reports_the_derived_workspace_on_both_sinks(tmp_path, sink_kind):
    """The workspace is derived from the located `.arbite/` directory *plus the
    resolved sink*, so the report names both and says where the sink came from."""
    project = make_project(tmp_path, sink_kind)
    init(project)

    proc = examples.run_cli(project, "workspace", "show")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()

    assert lines[0].startswith("workspace: ws-")
    assert lines[1] == f"root:      {project}"
    assert lines[2] == f"store:     {sink_kind} (sink: {sink_kind} in .arbite/project.yaml)"
    assert lines[3].endswith("(no active claims, 0 events, 0 receipts)")
    assert lines[4].endswith("(empty)")
    assert "next:" not in proc.stdout


def test_workspace_show_says_nothing_is_happening_rather_than_failing(tmp_path):
    """An empty coordination store is a first-class answer, not an error: exit 0,
    with the store named and zeros reported."""
    project = make_project(tmp_path)
    init(project)

    proc = examples.run_cli(project, "workspace", "show")

    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "no active claims" in proc.stdout


def test_workspace_show_works_before_the_project_is_initialised(tmp_path):
    """A project with no store yet still has a derivable workspace: the report is a
    derivation plus counts, and the counts are zero. Nothing is created by asking."""
    project = make_project(tmp_path)

    proc = examples.run_cli(project, "workspace", "show")

    assert proc.returncode == 0, proc.stderr
    assert not (project / ".arbite" / "coordination").exists()
    assert "no active claims" in proc.stdout


def test_workspace_json_carries_every_fact_the_text_prints(tmp_path):
    """Text is primary and JSON is the branchable form of the same facts: a fact
    that exists only in the text is one a machine consumer cannot use."""
    project = make_project(tmp_path)
    init(project)
    # One of everything, so the counts are not all zero by accident.
    store = open_coordination_store(build_sink(SinkSpec(kind="file"), project / ".arbite"))
    workspace = store.get_workspace()
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
    store.put_record(
        coordination_records.FileClaim(
            id="clm-a1b2",
            workspace_id=workspace.id,
            path="src/arbite/schema.py",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            generation=1,
            acquired=coordination_records.utc_now(),
        )
    )
    store.put_record(
        coordination_records.Event(
            id="evt-0001",
            cursor=1,
            kind="claim.acquired",
            recorded_at=coordination_records.utc_now(),
            category="claim",
        )
    )
    (project / ".arbite" / "scratch" / "payload.py").write_bytes(b"x" * 2048)

    text = examples.run_cli(project, "workspace", "show").stdout
    payload = json.loads(examples.run_cli(project, "workspace", "show", "--json").stdout)

    assert payload["next_actions"] == []
    assert payload["root"] == str(project)
    assert payload["id"] in text
    assert payload["store"] == {
        "kind": "file",
        "root": ".arbite",
        "source": "sink: file in .arbite/project.yaml",
    }
    assert payload["coordination"]["claims_active"] == 1
    assert payload["coordination"]["events"] == 1
    # JSON carries the plain path: the trailing slash the text prints says "this is
    # a directory" and is not part of the path.
    assert payload["coordination"]["root"] == ".arbite/coordination"
    assert payload["scratch"] == {"root": ".arbite/scratch", "files": 1, "bytes": 2048}
    assert "1 active claim, 1 event, 0 receipts" in text
    assert "2.0 KiB" in text
    assert ".arbite/coordination/  (1 active claim" in text


def test_workspace_is_derived_not_bound(tmp_path):
    """There is no bind and no conflict path: the same directory always derives the
    same workspace, and a relocated root derives a different one."""
    project = make_project(tmp_path)
    init(project)

    first = json.loads(examples.run_cli(project, "workspace", "show", "--json").stdout)["id"]
    again = json.loads(examples.run_cli(project, "workspace", "show", "--json").stdout)["id"]
    assert first == again

    moved = tmp_path / "moved"
    project.rename(moved)
    elsewhere = json.loads(examples.run_cli(moved, "workspace", "show", "--json").stdout)

    assert elsewhere["id"] != first
    assert elsewhere["root"] == str(moved)


def test_workspace_show_takes_a_subcommand(tmp_path):
    """`arbite workspace` alone is an argparse error naming the subcommand, not a
    command that silently does something."""
    project = make_project(tmp_path)

    proc = examples.run_cli(project, "workspace")

    assert proc.returncode == 2  # argparse's own usage error, not a silent no-op
    assert "SUBCOMMAND" in proc.stderr


def test_doctor_json_names_the_coordination_backend_on_both_sinks(project):
    """DR4's fact, on either sink: the report says which coordination backend the
    store has, because tickets and claims can live in different places."""
    sink_kind = "sqlite" if (project / ".arbite" / "arbite.db").exists() else "file"

    payload = json.loads(examples.run_cli(project, "doctor", "--json").stdout)

    assert payload["coordination"]["kind"] == sink_kind
    assert payload["coordination"]["claims_active"] == 0
    assert payload["coordination"]["events"] == 0
    assert payload["coordination"]["pending_operations"] == 0
    assert payload["scratch"] == {"files": 0, "bytes": 0}
    if sink_kind == "sqlite":
        assert payload["coordination"]["root"] == ".arbite/arbite.db"
    else:
        assert payload["coordination"]["root"] == ".arbite/coordination"


def test_doctor_still_reports_tickets_and_exits_zero_with_coordination_state(project):
    """Coordination records are ordinary state, not problems: `doctor` must not
    start failing because a claim exists (reporting the findings needs the recovery
    engine, tic-b03b)."""
    created = examples.run_cli(
        project, "create", "--title", "a ticket", "--type", "bug", "--tier", "low",
        "--domain", "io",
    )
    assert created.returncode == 0, created.stderr

    proc = examples.run_cli(project, "doctor")

    assert proc.returncode == 0
    assert "checked 1 tickets: no problems found" in proc.stdout
