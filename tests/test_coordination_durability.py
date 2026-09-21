"""Durability, as processes rather than as prose: racing, dying, and resuming.

Every claim this slice makes about concurrency and crash safety is checked here with
real processes and real kills -- `tests/coordination_worker.py` is the other side of
each one -- because a mock cannot be killed between writing a journal and applying
it, and a lock that dies with its holder is only interesting if the holder dies.

What is *not* claimed: protection against the machine losing power mid-rename (the
journal is fsynced, its directory is not) and isolation from a reader that arrives
while a commit is half-applied (a reader sees the outstanding journal reported
instead).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import examples
from arbite.coordination import file_backend, records as coordination_records
from arbite.coordination.file_backend import FileCoordinationStore
from arbite.coordination.store import open_coordination_store
from arbite.errors import Busy, Stale
from arbite.sinks import SinkSpec, build_sink

BACKENDS = ("file", "sqlite")
WORKER = Path(__file__).resolve().parent / "coordination_worker.py"
CRASH_CLAIM = "clm-c0de"
RACE_CLAIM = "clm-abcd"


def make_store(root, kind: str = "file"):
    arbite_dir = root / ".arbite"
    arbite_dir.mkdir(parents=True, exist_ok=True)
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    sink.init()
    store = open_coordination_store(sink)
    store.init()
    return store


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    return make_store(tmp_path, request.param)


def run_worker(project, *args, timeout: float = 60.0):
    """One worker process, run against this checkout exactly as a second agent would."""
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    environment.pop("ARBITE_SINK", None)
    return subprocess.run(
        [sys.executable, str(WORKER), *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def worker_report(project, kind) -> dict:
    proc = run_worker(project, "report", str(project), kind)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- racing for one revision ------------------------------------------------


@pytest.mark.parametrize("kind", BACKENDS)
def test_two_processes_racing_one_revision_leave_one_winner(tmp_path, kind):
    """The acceptance criterion, with real processes, on both backends: workers that
    read the same revision cannot all commit, so neither can lose the other's field.
    Exactly one wins, the store holds the winner's version, and the revision moved
    once."""
    project = tmp_path / "project"
    store = make_store(project, kind)
    seed = run_worker(project, "seed", str(project), kind)
    assert seed.returncode == 0, seed.stderr
    assert store.revision("claim", RACE_CLAIM) == 1

    go = tmp_path / "go"
    racers = []
    for index in range(4):
        ready = tmp_path / f"ready-{index}"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                "race",
                str(project),
                kind,
                str(ready),
                str(go),
                f"v{index}",
            ],
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        racers.append((index, ready, proc))

    deadline = time.monotonic() + 30.0
    for _index, ready, _proc in racers:
        while not ready.exists():
            assert time.monotonic() < deadline, "a worker never read the revision"
            time.sleep(0.005)
    go.write_text("go", encoding="utf-8")

    outcomes = {}
    for index, _ready, proc in racers:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode in (0, 3), err
        outcomes[index] = (proc.returncode, out.strip())

    winners = [index for index, (code, _out) in outcomes.items() if code == 0]
    losers = [index for index, (code, _out) in outcomes.items() if code == 3]

    assert len(winners) == 1, outcomes
    assert len(losers) == 3, outcomes
    winner = winners[0]
    stored = store.get_record("claim", RACE_CLAIM)
    assert stored.observed_version == coordination_records.digest_bytes(f"v{winner}".encode())
    assert store.revision("claim", RACE_CLAIM) == 2
    # A lost race changes nothing at all -- not its claim, and not the revision.
    assert all(f"stale v{index}" in outcomes[index][1] for index in losers)


def test_a_loser_can_re_read_and_commit(tmp_path):
    """Stale is a "re-read, then retry" answer rather than a dead end: after losing,
    a worker that reads the current revision commits fine, and the winner's field is
    still there."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert run_worker(project, "seed", str(project), "file").returncode == 0

    stale_revision = store.revision("claim", RACE_CLAIM)
    with store.transaction() as txn:
        txn.replace_record(
            store.get_record("claim", RACE_CLAIM), expect_revision=stale_revision
        )

    with pytest.raises(Stale):
        with store.transaction() as txn:
            txn.replace_record(
                store.get_record("claim", RACE_CLAIM), expect_revision=stale_revision
            )

    fresh = store.revision("claim", RACE_CLAIM)
    assert fresh == stale_revision + 1
    with store.transaction() as txn:
        txn.replace_record(store.get_record("claim", RACE_CLAIM), expect_revision=fresh)

    assert store.revision("claim", RACE_CLAIM) == fresh + 1


# --- dying at a commit boundary (file backend) ------------------------------


def test_a_process_killed_while_staging_a_commit_is_recovered_by_the_next_write(tmp_path):
    """`commit_staged` is the dangerous window: the intent is on disk and nothing is
    applied. A reader is told (rather than shown a half-applied unit), and the next
    write finishes the commit without deciding anything."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert run_worker(project, "seed", str(project), "file").returncode == 0
    before = worker_report(project, "file")

    crashed = run_worker(project, "crash", str(project), "file", "commit_staged")
    assert crashed.returncode == 9, (crashed.stdout, crashed.stderr)

    staged = worker_report(project, "file")
    assert staged["journal"] is True
    assert staged["events"] == before["events"]
    assert CRASH_CLAIM not in staged["claims"]
    assert "pending_commit" in staged["problems"]

    # The next write replays it: the crashed unit's claim *and* its event appear
    # together, and the durable claim that was already there is untouched.
    started = time.monotonic()
    assert run_worker(project, "write", str(project), "file").returncode == 0
    assert time.monotonic() - started < file_backend.LOCK_TIMEOUT, (
        "the dead process must not have left the store locked"
    )

    after = worker_report(project, "file")
    assert after["journal"] is False
    assert "pending_commit" not in after["problems"]
    assert after["claims"][CRASH_CLAIM]["path"] == "src/arbite/errors.py"
    assert after["claims"][RACE_CLAIM] == before["claims"][RACE_CLAIM]
    assert after["events"] == [1, 2]

    # The crashed commit's cursor was not reused or skipped: the ordinary write that
    # triggered the replay took the next one.
    assert run_worker(project, "append", str(project), "file", "1").stdout.strip() == "3"


def test_a_process_killed_after_applying_recovers_without_duplicating(tmp_path):
    """`commit_applied` is the other window: everything is applied and the journal is
    still there. Replaying it must be a no-op -- no second event, no second revision
    -- because every value in the journal is absolute."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert run_worker(project, "seed", str(project), "file").returncode == 0

    crashed = run_worker(project, "crash", str(project), "file", "commit_applied")
    assert crashed.returncode == 9, (crashed.stdout, crashed.stderr)

    applied = worker_report(project, "file")
    assert applied["journal"] is True
    assert applied["events"] == [1]
    assert applied["claims"][CRASH_CLAIM]["revision"] == 1

    assert run_worker(project, "write", str(project), "file").returncode == 0

    after = worker_report(project, "file")
    assert after["journal"] is False
    assert after["events"] == [1, 2]
    assert after["claims"][CRASH_CLAIM]["revision"] == 1, "a replay must not bump a revision"
    assert store.revision("claim", CRASH_CLAIM) == 1


def test_sqlite_discards_a_transaction_a_dead_process_left_open(tmp_path):
    """The same boundary on the other backend, reached another way: the transaction
    never committed, so the database is unchanged and the cursor was never consumed.
    Nothing recovers it because nothing needs to."""
    project = tmp_path / "project"
    store = make_store(project, "sqlite")

    crashed = run_worker(project, "crash", str(project), "sqlite", "commit_staged")
    assert crashed.returncode == 9, (crashed.stdout, crashed.stderr)

    after = worker_report(project, "sqlite")
    assert after["journal"] is None
    assert after["events"] == []
    assert after["claims"] == {}
    assert after["problems"] == []

    assert run_worker(project, "append", str(project), "sqlite", "1").stdout.strip() == "1"


# --- locks ------------------------------------------------------------------


def test_a_held_store_lock_refuses_promptly_and_writes_nothing(tmp_path, monkeypatch):
    """Contention is answered, not waited out: a bounded wait, `Busy` naming the
    store, and nothing written. The lock itself is another *process's*, so this is
    the real cross-process case."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert run_worker(project, "seed", str(project), "file").returncode == 0
    before = worker_report(project, "file")

    holder = subprocess.Popen(
        [sys.executable, str(WORKER), "lock", str(project), "file", "2"],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        monkeypatch.setattr(file_backend, "LOCK_TIMEOUT", 0.3)
        monkeypatch.setattr(file_backend, "LOCK_POLL_SECONDS", 0.01)

        started = time.monotonic()
        with pytest.raises(Busy) as failure:
            with store.transaction() as txn:
                txn.append_event("write.file", "file")
        elapsed = time.monotonic() - started

        assert failure.value.reason == "store_locked"
        assert "nothing was written" in str(failure.value)
        assert elapsed < 2.0, "a one-shot command must not wait indefinitely"
        assert worker_report(project, "file") == before
    finally:
        holder.communicate(timeout=30)

    # ...and once the holder is gone the lock is free, with no cleanup step.
    assert run_worker(project, "write", str(project), "file").returncode == 0


def test_the_lock_is_released_by_the_kernel_when_its_holder_dies(tmp_path):
    """No staleness heuristic and no lock file to clean up: the file is a rendezvous
    point, and the lock dies with the process that held it."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert isinstance(store, FileCoordinationStore)
    lock_file = store._root / file_backend.LOCK_FILENAME
    assert not lock_file.exists(), "nothing creates the lock before it is needed"

    assert run_worker(project, "crash", str(project), "file", "commit_staged").returncode == 9

    assert lock_file.exists()
    # Holding it is possible right away, which is the observable fact: the kernel
    # dropped the dead process's lock when it exited.
    with store._exclusive():
        pass
    assert run_worker(project, "write", str(project), "file").returncode == 0


# --- resuming across processes ----------------------------------------------


def test_a_cursor_survives_a_restart_and_pages_across_processes(tmp_path):
    """The stream is one stream however many processes write to it: a worker appends,
    another process appends, and a `--after` poll in a third sees each event exactly
    once -- which is the whole point of a store-allocated, monotonic cursor."""
    project = tmp_path / "project"
    store = make_store(project, "file")
    assert run_worker(project, "append", str(project), "file", "2").stdout.strip() == "1 2"
    assert run_worker(project, "append", str(project), "file", "3").stdout.strip() == "3 4 5"

    proc = examples.run_cli(project, "events", "--after", "2", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert [event["cursor"] for event in payload["events"]] == [3, 4, 5]
    assert payload["next_actions"] == ["arbite events --after 5"]

    # A fourth process continues where the third stopped.
    assert run_worker(project, "append", str(project), "file", "1").stdout.strip() == "6"
    assert [event.cursor for event in store.events()] == [1, 2, 3, 4, 5, 6]
    assert open_coordination_store(build_sink(SinkSpec(kind="file"), project / ".arbite")).revision(
        "event", store.events()[0].id
    ) == 1
