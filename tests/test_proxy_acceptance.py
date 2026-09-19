"""End-to-end acceptance for the shared-directory proxy (planning key C12).

This is the integration-acceptance counterpart to the per-slice suites: it proves
the *documented* two-agent workflow really runs, on **both** sinks, and that the
documentation says what the implementation actually does.

Covered here (the C12 acceptance criteria):

1. ``test_agent_guidance_*`` / ``test_readme_*`` -- agents are instructed to use
   arbite for source discovery, reads and mutations, to re-read after claim,
   takeover and ticket boundaries, and the docs state the deliberate deferrals
   (no runner/daemon/watcher/scheduler or automatic stale takeover) and limits
   (test/build output, evidence growth, the optional external enforcement
   boundary) honestly.
2. ``test_documented_recipe_runs`` -- the disposable-workspace recipe in
   ``scripts/proxy_recipe.py`` runs to completion on both sinks.
3. ``test_two_processes_share_a_directory`` -- two independent OS processes do
   independent file work, race for one contested file (exactly one winner, the
   other gets a structured ``file_busy``), are refused on a stale read, and show
   close releasing claims so the other agent can take the file.
4. ``test_takeover_and_external_drift_boundary`` -- an explicit administrative
   takeover revokes the old attempt, and a direct (non-proxy) filesystem write is
   detected and never silently overwritten.
5. ``test_crash_between_apply_and_receipt_recovers`` -- a process dies after the
   filesystem change but before the receipt; nothing runs in the background, the
   pending intent is reported, and the next relevant operation reconciles it
   honestly with the evidence preserved.

Unresolved platform limits (documented, not papered over): arbite v1 refuses
symlink components and special files rather than following them; there is no
recursive deletion and no metadata (chmod/chown) editing; binary files can be
written whole but their content is not served by the versioned read surface (a
version-only read gives them a read token); the local lock is
process-safe but a hostile local filesystem race is out of scope; and there is no
artifact garbage collection, so evidence grows with use. These are stated in
README.md and in the generated agent guide.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import application, docs, filemutations, mutation
from arbite.application import Actor
from arbite.sinks import SinkSpec, build_sink

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
RECIPE = REPO_ROOT / "scripts" / "proxy_recipe.py"

SINKS = ("file", "sqlite")
AGENT_A = "agent.a.001"
AGENT_B = "agent.b.001"
TICKET_ID_RE = re.compile(r"tic-[0-9a-f]{4,}")


def _environment() -> dict:
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    # The sink under test is always stated explicitly, never inherited.
    env.pop("ARBITE_SINK", None)
    return env


class Result:
    def __init__(self, argv, code: int, stdout: bytes, stderr: bytes):
        self.argv = argv
        self.code = code
        self.stdout = stdout.decode("utf-8", "replace")
        self.stderr = stderr.decode("utf-8", "replace")

    def json(self) -> dict:
        return json.loads(self.stdout)


class Cli:
    """Run the real CLI in its own process, against one throwaway workspace."""

    def __init__(self, workspace: Path, sink: str):
        self.workspace = Path(workspace)
        self.sink = sink

    def run(self, *args, expect=0, stdin: bytes | None = None, timeout: int = 180) -> Result:
        argv = [sys.executable, "-m", "arbite.cli", "--sink", self.sink, *args]
        proc = subprocess.run(
            argv,
            cwd=str(self.workspace),
            env=_environment(),
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
        result = Result(argv, proc.returncode, proc.stdout, proc.stderr)
        if expect is not None:
            assert result.code == expect, (
                f"arbite {' '.join(args)} -> exit {result.code}, expected {expect}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def json(self, *args, expect=0, stdin: bytes | None = None) -> dict:
        return self.run(*args, expect=expect, stdin=stdin).json()


def create_ticket(cli: Cli, title: str) -> str:
    output = cli.run(
        "create",
        "--title",
        title,
        "--type",
        "chore",
        "--tier",
        "medium",
        "--domain",
        "io",
    ).stdout
    match = TICKET_ID_RE.search(output)
    assert match, f"no ticket id in create output: {output!r}"
    return match.group(0)


def active_attempt(cli: Cli, ticket: str) -> str:
    """The active attempt id, discovered the way the docs tell an agent to."""
    document = json.loads(cli.run("export", "--scope", "coordination", "--no-artifacts").stdout)
    attempts = [
        attempt
        for attempt in document["records"]["work_attempts"]
        if attempt["ticket_id"] == ticket and attempt["state"] == "active"
    ]
    assert len(attempts) == 1, f"expected one active attempt for {ticket}, got {attempts}"
    return attempts[0]["id"]


def claim_path(cli: Cli, ticket: str, attempt: str, path: str) -> dict:
    return cli.json("file", "claim", path, "--ticket", ticket, "--attempt", attempt, "--json")["data"]


def read_token(cli: Cli, ticket: str, attempt: str, path: str) -> str:
    receipt = cli.json("file", "read", path, "--ticket", ticket, "--attempt", attempt, "--json")["data"]
    assert receipt["write_authorizing"] is True, receipt
    return receipt["read_token"]


# ---------------------------------------------------------------------------
# documentation (C12 acceptance 1 and 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_agent_guidance_mandates_the_proxy(workspace):
    cli = Cli(workspace, "file")
    cli.run("init")
    guide = (workspace / ".arbite" / "REFERENCE.md").read_text()
    for needle in (
        "Shared directory: the file proxy",
        "arbite file claim",
        "stale_read",
        "after every claim, takeover and ticket boundary",
        "no runner, daemon, watcher or scheduler",
        "unattributed drift",
        "garbage-collected",
        "cannot prove who made a direct filesystem change",
    ):
        assert needle in guide, f"generated agent guide is missing: {needle!r}"


def test_installed_instructions_match_the_example_and_mandate_proxy_use():
    # docs.py stores the block verbatim rather than reading AGENTS_EXAMPLE.md at
    # runtime; this is the regression check that the two cannot drift apart.
    assert (REPO_ROOT / "AGENTS_EXAMPLE.md").read_text() == docs.ARBITE_INSTRUCTIONS_BLOCK
    block = docs.ARBITE_INSTRUCTIONS_BLOCK
    for needle in (
        "Shared directory: use arbite for file work",
        "Take a fresh one after every claim and before each mutation",
        "stale_read",
        "no runner, daemon, watcher or scheduler",
        "unattributed drift",
        "never garbage-collected",
        "cannot prove who made a direct filesystem change",
    ):
        assert needle in block, f"the installed instructions block is missing: {needle!r}"


def test_readme_scope_covers_the_implemented_proxy_and_its_limits():
    readme = (REPO_ROOT / "README.md").read_text()
    for needle in (
        "## Shared-directory coordination (the file proxy)",
        "arbite file claim",
        "### The agent mandate",
        "### Owner recipe: two agents in one directory",
        "### No runner, daemon or automatic takeover",
        "### Test and build output",
        "### Evidence growth and retention",
        "### The optional external enforcement boundary",
        "artifact garbage collection",
    ):
        assert needle in readme, f"README is missing: {needle!r}"
    # The old scope exclusion must be gone: the proxy is implemented now.
    assert "Enforcing policy beyond data integrity" not in readme


# ---------------------------------------------------------------------------
# the documented disposable-workspace recipe (C12 acceptance 3 and 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink", SINKS)
def test_documented_recipe_runs(sink):
    proc = subprocess.run(
        [sys.executable, str(RECIPE), "--sink", sink],
        cwd=str(REPO_ROOT),
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"recipe failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert f"RECIPE-OK sink={sink}" in proc.stdout
    for step in (
        "independent file work",
        "contested src/shared.py",
        "stale read token refused",
        "detected (stale_read)",
        "takeover revoked",
        "close released the claims",
    ):
        assert step in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# two independent processes in one shared directory (C12 acceptance 3)
# ---------------------------------------------------------------------------


def _agent_process(
    sink,
    workspace,
    agent,
    ticket,
    attempt,
    own_path,
    own_payload,
    contested_path,
    start,
    done,
    results,
):
    """One agent, in its own OS process, doing the documented workflow.

    Module-level so it is picklable under the ``spawn`` start method.
    """
    report = {"agent": agent, "ticket": ticket, "attempt": attempt, "own_path": own_path}
    try:
        cli = Cli(Path(workspace), sink)
        # Independent work: own file claim + create, then a version-checked write.
        cli.run("file", "claim", own_path, "--ticket", ticket, "--attempt", attempt, "--json")
        cli.run(
            "file", "write", own_path, "--ticket", ticket, "--attempt", attempt, "--input", "-", "--json",
            stdin=own_payload,
        )
        report["independent"] = (Path(workspace) / own_path).read_bytes() == own_payload
        token = cli.json(
            "file", "read", own_path, "--ticket", ticket, "--attempt", attempt, "--json"
        )["data"]["read_token"]
        cli.run(
            "file", "write", own_path, "--ticket", ticket, "--attempt", attempt,
            "--read-token", token, "--input", "-", "--json", stdin=b"second write\n",
        )
        # Stale-read refusal: the consumed token cannot authorize a second change.
        reused = cli.json(
            "file", "write", own_path, "--ticket", ticket, "--attempt", attempt,
            "--read-token", token, "--input", "-", "--json", stdin=b"third write\n", expect=1,
        )
        report["stale_ok"] = (
            reused["code"] == "stale_read"
            and (Path(workspace) / own_path).read_bytes() == b"second write\n"
        )

        # Contested ownership: both processes attempt the claim at the same time.
        start.wait(90)
        proc = cli.run(
            "file", "claim", contested_path, "--ticket", ticket, "--attempt", attempt, "--json",
            expect=None,
        )
        contested = {"returncode": proc.code, "code": None, "holder": None}
        if proc.code == 0:
            contested["code"] = "claimed"
            cli.run(
                "file", "write", contested_path, "--ticket", ticket, "--attempt", attempt,
                "--input", "-", "--json", stdin=b"shared by winner\n",
            )
        else:
            payload = json.loads(proc.stdout)
            contested["code"] = payload["code"]
            contested["holder"] = payload["details"]["holder_ticket"]
        report["contested"] = contested
        done.wait(90)
        results.put(report)
    except Exception as error:  # pragma: no cover - surfaced through the queue
        report["error"] = f"{type(error).__name__}: {error}"
        results.put(report)


@pytest.mark.parametrize("sink", SINKS)
def test_two_processes_share_a_directory(workspace, sink):
    cli = Cli(workspace, sink)
    cli.run("init")
    (workspace / "src").mkdir()
    t_a = create_ticket(cli, "Agent A task")
    t_b = create_ticket(cli, "Agent B task")
    cli.run("claim", t_a, "--agent", AGENT_A)
    cli.run("claim", t_b, "--agent", AGENT_B)
    attempts = {
        t_a: active_attempt(cli, t_a),
        t_b: active_attempt(cli, t_b),
    }
    assert attempts[t_a] != attempts[t_b]

    context = multiprocessing.get_context("spawn")
    start = context.Barrier(2)
    done = context.Barrier(2)
    results = context.Queue()
    agents = (
        (AGENT_A, t_a, "src/alpha.py", b"alpha owned by A\n"),
        (AGENT_B, t_b, "src/beta.py", b"beta owned by B\n"),
    )
    processes = [
        context.Process(
            target=_agent_process,
            args=(
                sink,
                str(workspace),
                agent,
                ticket,
                attempts[ticket],
                own_path,
                payload,
                "src/shared.py",
                start,
                done,
                results,
            ),
        )
        for agent, ticket, own_path, payload in agents
    ]
    for process in processes:
        process.start()
    reports = [results.get(timeout=200) for _ in processes]
    for process in processes:
        process.join(200)
        assert process.exitcode == 0

    errors = [report for report in reports if "error" in report]
    assert not errors, errors
    assert all(report["independent"] for report in reports), reports
    assert all(report["stale_ok"] for report in reports), reports

    winners = [report for report in reports if report["contested"]["returncode"] == 0]
    losers = [report for report in reports if report["contested"]["returncode"] != 0]
    assert len(winners) == 1 and len(losers) == 1, reports
    assert losers[0]["contested"]["code"] == "file_busy", reports
    assert losers[0]["contested"]["holder"] == winners[0]["ticket"], reports
    assert (workspace / "src" / "shared.py").read_bytes() == b"shared by winner\n"

    # Close cleanup: closing the winner's ticket releases its file claims, so the
    # other agent's *existing* attempt can take a file it held.
    winner, loser = winners[0], losers[0]
    cli.run("close", winner["ticket"], "--agent", winner["agent"], "--reason", "done")
    taken = claim_path(cli, loser["ticket"], loser["attempt"], winner["own_path"])
    assert taken["claimed"][0]["generation"] >= 1
    token = read_token(cli, loser["ticket"], loser["attempt"], winner["own_path"])
    cli.run(
        "file", "write", winner["own_path"], "--ticket", loser["ticket"], "--attempt",
        loser["attempt"], "--read-token", token, "--input", "-", "--json",
        stdin=b"taken after close\n",
    )
    assert (workspace / winner["own_path"]).read_bytes() == b"taken after close\n"

    changes = cli.json("changes", loser["ticket"], "--json")["data"]
    assert changes["evidence"]["operation_count"] >= 2, changes["counts"]
    assert cli.json("doctor", "--json")["remaining"] == 0


# ---------------------------------------------------------------------------
# takeover and the external-tool boundary (C12 acceptance 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink", SINKS)
def test_takeover_and_external_drift_boundary(workspace, sink):
    cli = Cli(workspace, sink)
    cli.run("init")
    (workspace / "src").mkdir()
    tid = create_ticket(cli, "Boundary work")
    cli.run("claim", tid, "--agent", AGENT_A)
    attempt = active_attempt(cli, tid)
    claim_path(cli, tid, attempt, "src/a.py")
    cli.run(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--input", "-", "--json", stdin=b"alpha\n",
    )

    # An external (non-proxy) write is not prevented -- arbite has no way to
    # prove who did it -- but it is detected and never silently overwritten.
    token = read_token(cli, tid, attempt, "src/a.py")
    (workspace / "src" / "a.py").write_bytes(b"external tool\n")
    refused = cli.json(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--read-token", token, "--input", "-", "--json", stdin=b"proxy overwrite\n", expect=1,
    )
    assert refused["code"] == "stale_read", refused
    assert (workspace / "src" / "a.py").read_bytes() == b"external tool\n"

    # The held claim's recorded version no longer matches, so a read is
    # deliberately non-writable; release + re-claim is the documented way forward.
    receipt = cli.json(
        "file", "read", "src/a.py", "--ticket", tid, "--attempt", attempt, "--json"
    )["data"]
    assert receipt["write_authorizing"] is False
    assert receipt["non_writable_reason"] == "claim_version_mismatch", receipt
    released = cli.json(
        "file", "release", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--reason", "external drift", "--json",
    )["data"]
    assert released["released"][0]["state"] == "released"
    reclaimed = claim_path(cli, tid, attempt, "src/a.py")
    assert reclaimed["claimed"][0]["generation"] >= 2
    token = read_token(cli, tid, attempt, "src/a.py")
    cli.run(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--read-token", token, "--input", "-", "--json", stdin=b"recovered\n",
    )
    assert (workspace / "src" / "a.py").read_bytes() == b"recovered\n"

    # Explicit administrative takeover: the old attempt is revoked and cannot
    # mutate under its old token; the new attempt must claim and re-read.
    held = read_token(cli, tid, attempt, "src/a.py")
    cli.run("claim", tid, "--agent", AGENT_B, "--force", "--reason", "old worker stalled")
    revoked = cli.json(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--read-token", held, "--input", "-", "--json", stdin=b"stale worker\n", expect=1,
    )
    assert revoked["ok"] is False
    assert (workspace / "src" / "a.py").read_bytes() == b"recovered\n"

    new_attempt = active_attempt(cli, tid)
    assert new_attempt != attempt
    claim_path(cli, tid, new_attempt, "src/a.py")
    token = read_token(cli, tid, new_attempt, "src/a.py")
    cli.run(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", new_attempt,
        "--read-token", token, "--input", "-", "--json", stdin=b"after takeover\n",
    )
    assert (workspace / "src" / "a.py").read_bytes() == b"after takeover\n"
    assert cli.json("doctor", "--json")["remaining"] == 0


# ---------------------------------------------------------------------------
# crash recovery across processes (C12 acceptance 4)
# ---------------------------------------------------------------------------


class _FaultOnce:
    """A fault injector that raises once, at one named boundary."""

    def __init__(self, phase: str):
        self.phase = phase
        self.fired = False

    def __call__(self, phase: str) -> None:
        if phase == self.phase and not self.fired:
            self.fired = True
            raise mutation.FaultInjected(phase)


def _crash_mid_write(kind, arbite_dir, root, attempt_id, token, results):
    """Die after the filesystem change but before the receipt is recorded."""
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        service = application.coordination_service_for(
            sink, root=str(root), actor=Actor("crasher")
        )
        mutations = filemutations.FileMutationService(
            service, fault_injector=_FaultOnce(mutation.FAULT_AFTER_APPLY)
        )
        with service.store.transaction(write=False) as tx:
            attempt = tx.get("work_attempt", attempt_id)
        try:
            mutations.write(attempt, "src/a.py", b"crashed write\n", read_token=token)
        except mutation.FaultInjected:
            results.put("crashed-after-apply")
        else:  # pragma: no cover - the injected fault must fire
            results.put("unexpectedly-completed")
    except Exception as error:  # pragma: no cover - surfaced through the queue
        results.put(f"error:{type(error).__name__}:{error}")


def _reconcile_pending(kind, arbite_dir, root, results):
    """The *next relevant operation*: recovery is invoked, never scheduled."""
    try:
        sink = build_sink(SinkSpec(kind=kind), Path(arbite_dir))
        service = application.coordination_service_for(
            sink, root=str(root), actor=Actor("recoverer")
        )
        reports = filemutations.FileMutationService(service).engine.reconcile()
        results.put([(report.state, report.operation_id, list(report.paths)) for report in reports])
    except Exception as error:  # pragma: no cover - surfaced through the queue
        results.put(f"error:{type(error).__name__}:{error}")


@pytest.mark.parametrize("sink", SINKS)
def test_crash_between_apply_and_receipt_recovers(workspace, sink):
    cli = Cli(workspace, sink)
    cli.run("init")
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("alpha\n")
    tid = create_ticket(cli, "Crash work")
    cli.run("claim", tid, "--agent", AGENT_A)
    attempt = active_attempt(cli, tid)
    claim_path(cli, tid, attempt, "src/a.py")
    token = read_token(cli, tid, attempt, "src/a.py")

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    arbite_dir = workspace / ".arbite"
    child = context.Process(
        target=_crash_mid_write,
        args=(sink, str(arbite_dir), str(workspace), attempt, token, results),
    )
    child.start()
    child.join(180)
    assert child.exitcode == 0
    assert results.get(timeout=60) == "crashed-after-apply"

    # The bytes landed; the receipt did not. Nothing reconciles in the background.
    assert (workspace / "src" / "a.py").read_bytes() == b"crashed write\n"
    doctor = cli.json("doctor", "--json", expect=3)
    assert doctor["remaining"] >= 1
    assert any(problem["kind"] == "coordination_pending_intent" for problem in doctor["problems"]), (
        doctor["problems"]
    )

    # The next relevant operation reconciles honestly and keeps the evidence.
    recover = context.Process(
        target=_reconcile_pending, args=(sink, str(arbite_dir), str(workspace), results)
    )
    recover.start()
    recover.join(180)
    assert recover.exitcode == 0
    reports = results.get(timeout=60)
    assert not isinstance(reports, str), reports
    assert any(report[0] == "applied" for report in reports), reports

    changes = cli.json("changes", tid, "--json")["data"]
    operations = changes["evidence"]["operations"]
    assert any(op["operation_kind"] == "write" for op in operations), operations
    assert all(op["result"] == "ok" for op in operations), operations
    assert cli.json("doctor", "--json")["remaining"] == 0
