"""The `arbite changes` CLI surface (planning key C10).

The command is exercised the way an agent or shell script sees it -- argv, exit
codes and the documented `--json` envelope -- over both sinks. The store is
seeded through the same application layer the CLI uses, so the test measures the
query surface rather than a bespoke fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, fileclaims, filemutations, filereads, lifecycle
from arbite.application import Actor
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

SINK_KINDS = ("file", "sqlite")


@pytest.fixture
def cli(tmp_project):
    """Run the CLI in a throwaway project and assert its exit code."""

    def run(*args, expect=0, sink=None):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        environment.pop("ARBITE_SINK", None)
        if sink:
            environment["ARBITE_SINK"] = sink
        proc = subprocess.run(
            [sys.executable, "-m", "arbite.cli", *args],
            cwd=str(tmp_project),
            env=environment,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == expect, (
            f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        return proc

    return run


def _seed(project: Path, kind: str, worker="claude.opus.001"):
    """Create a ticket, claim it and replace one file through the proxy."""
    arbite_dir = project / ".arbite"
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    sink.init()
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "a.py").write_text("alpha\n")
    sink.create(make_ticket("tic-a1b2"))
    service = application.coordination_service_for(
        sink, root=str(project), actor=Actor(worker)
    )
    ctl = lifecycle.TicketLifecycle(service, sink)
    attempt = ctl.acquire(sink.get("tic-a1b2"), worker_id=worker).attempt
    claims = fileclaims.FileClaimService(service)
    reads = filereads.FileReadService(service, claims=claims)
    mutations = filemutations.FileMutationService(service, claims=claims, reads=reads)
    claims.claim(attempt, ["src/a.py"])
    token = reads.read(attempt, "src/a.py").read_token
    mutations.write(attempt, "src/a.py", b"beta\n", read_token=token)
    return attempt


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_changes_ticket_json_is_the_documented_envelope(cli, tmp_project, kind):
    cli("init", sink=kind)
    attempt = _seed(tmp_project, kind)

    payload = json.loads(cli("changes", "tic-a1b2", "--json", sink=kind).stdout)

    assert payload["ok"] is True
    assert payload["data"]["ticket_id"] == "tic-a1b2"
    assert payload["data"]["scope"] == "ticket"
    operations = payload["data"]["evidence"]["operations"]
    assert [op["operation_kind"] for op in operations] == ["write"]
    net = payload["data"]["evidence"]["net_changes"][0]
    assert net["path"] == "src/a.py"
    assert net["change"] == "modified"
    assert net["after_artifact"]["verifiable"] is True
    # Reads never flood the ordinary view.
    assert payload["data"]["read_observations"] == []


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_changes_attempt_view_and_include_reads(cli, tmp_project, kind):
    cli("init", sink=kind)
    attempt = _seed(tmp_project, kind)

    payload = json.loads(
        cli(
            "changes",
            "tic-a1b2",
            "--attempt",
            attempt.id,
            "--include-reads",
            "--json",
            sink=kind,
        ).stdout
    )

    data = payload["data"]
    assert data["scope"] == "attempt"
    assert data["attempt_id"] == attempt.id
    assert [op["attempt_id"] for op in data["evidence"]["operations"]] == [attempt.id]
    assert data["read_observations"], "reads appear only when explicitly requested"
    assert data["read_observations"][0]["path"] == "src/a.py"


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_changes_malformed_attempt_is_a_structured_error(cli, tmp_project, kind):
    cli("init", sink=kind)
    _seed(tmp_project, kind)

    payload = json.loads(
        cli(
            "changes",
            "tic-a1b2",
            "--attempt",
            "att-nope",
            "--json",
            expect=1,
            sink=kind,
        ).stdout
    )

    assert payload["ok"] is False
    assert payload["code"] == "not_found"
    assert payload["details"]["attempt_id"] == "att-nope"


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_changes_human_output_lists_the_net_change(cli, tmp_project, kind):
    cli("init", sink=kind)
    _seed(tmp_project, kind)

    output = cli("changes", "tic-a1b2", sink=kind).stdout

    assert "ticket changes for tic-a1b2" in output
    assert "modified: src/a.py" in output
    assert "op " in output and " write " in output
