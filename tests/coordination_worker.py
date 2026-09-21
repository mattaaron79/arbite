"""One coordination operation, done by a separate process, for the durability tests.

Those tests are about what *processes* do to a store -- race for one revision, die at
a commit boundary, continue a stream a previous process started -- so the operations
have to happen in real processes: a fixture cannot be killed between writing a
journal and applying it, and a lock that dies with its holder is only interesting if
the holder really dies.

Usage: python3 tests/coordination_worker.py <operation> <project-dir> <sink-kind> [args...]

Exit codes: 0 for the operation (printing what it did), 3 for a lost revision race,
9 for a deliberate death at a commit boundary.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arbite.coordination import records as coordination_records  # noqa: E402
from arbite.coordination.store import open_coordination_store  # noqa: E402
from arbite.errors import Stale  # noqa: E402
from arbite.sinks import SinkSpec, build_sink  # noqa: E402

#: The claim the racing workers all try to update, and the one the crash tests
#: stage: fixed ids so the parent can look for exactly them.
RACE_CLAIM = "clm-abcd"
CRASH_CLAIM = "clm-c0de"
WORKSPACE = "ws-0000"


def open_store(project: str, kind: str):
    arbite_dir = Path(project) / ".arbite"
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    sink.init()
    store = open_coordination_store(sink)
    store.init()
    return store


def seed_claim(store) -> None:
    """The record the racing workers contend over: one durable file claim."""
    store.put_record(
        coordination_records.FileClaim(
            id=RACE_CLAIM,
            workspace_id=WORKSPACE,
            path="src/arbite/schema.py",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            generation=1,
            acquired=coordination_records.utc_now(),
        )
    )


def op_append(store, count: str) -> int:
    """Append `count` events in one commit and print the cursors they were given."""
    appended = []
    with store.transaction() as txn:
        for index in range(int(count)):
            appended.append(
                txn.append_event(
                    "write.file",
                    "file",
                    subject=f"src/arbite/worker_{index}.py",
                    result="gen 1",
                    actor="claude.opus.001",
                )
            )
    print(" ".join(str(event.cursor) for event in appended))
    return 0


def op_seed(store) -> int:
    seed_claim(store)
    print("seeded")
    return 0


def op_race(store, ready: str, go: str, label: str) -> int:
    """Read the claim's revision, wait for the starting gun, then write it back.

    The barrier is what makes the race deterministic: every worker reads the *same*
    revision before any of them commits, so exactly one can win. Without it the
    workers would read whatever the previous one left and all succeed, which would
    test nothing.
    """
    revision = store.revision("claim", RACE_CLAIM)
    Path(ready).write_text(str(revision), encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while not Path(go).exists():
        if time.monotonic() > deadline:
            print("no starting gun", file=sys.stderr)
            return 4
        time.sleep(0.005)

    updated = replace(
        store.get_record("claim", RACE_CLAIM),
        observed_version=coordination_records.digest_bytes(label.encode("utf-8")),
    )
    try:
        with store.transaction() as txn:
            txn.replace_record(updated, expect_revision=revision)
    except Stale:
        print(f"stale {label}")
        return 3
    print(f"applied {label}")
    return 0


def op_crash(store, boundary: str) -> int:
    """Stage a claim and its event, then die at the named commit boundary.

    `commit_staged` is after the journal is written and before anything is applied;
    `commit_applied` is after every document is written and before the journal is
    removed. Both leave a store a later write has to make whole.
    """

    def die_at(name: str) -> None:
        if name == boundary:
            os._exit(9)

    store.crash_hook = die_at
    with store.transaction() as txn:
        txn.put_record(
            coordination_records.FileClaim(
                id=CRASH_CLAIM,
                workspace_id=WORKSPACE,
                path="src/arbite/errors.py",
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
                generation=1,
                acquired=coordination_records.utc_now(),
            )
        )
        txn.append_event(
            "claim.acquired",
            "claim",
            subject="src/arbite/errors.py",
            result="gen 1",
            ticket_id="tic-cf9f",
            attempt_id="att-91bd",
            actor="claude.opus.001",
            operation_id="op-c0de",
        )
    print("committed")
    return 0


def op_write(store) -> int:
    """One ordinary write, which is what a store with a pending commit needs."""
    with store.transaction() as txn:
        txn.append_event("write.file", "file", subject="src/arbite/errors.py", result="gen 2")
    print("written")
    return 0


def op_lock(store, seconds: str) -> int:
    """Hold the store's own commit lock, the way a commit holds it, and sleep.

    Reaching for `_exclusive` is deliberate: the point of the contention test is the
    *real* lock -- the one a commit takes -- rather than a stand-in that could behave
    differently.
    """
    with store._exclusive():
        print("locked", flush=True)
        time.sleep(float(seconds))
    print("released")
    return 0


def op_report(store) -> int:
    """What the store holds, for the parent to assert against."""
    journal_reader = getattr(store, "read_commit_journal", None)
    print(
        json.dumps(
            {
                "events": [event.cursor for event in store.events()],
                "claims": {
                    stored.id: {
                        "path": stored.path,
                        "state": stored.state,
                        "revision": store.revision("claim", stored.id),
                    }
                    for stored in store.records("claim")
                },
                "journal": None if journal_reader is None else journal_reader() is not None,
                "problems": [problem.kind for problem in store.record_problems()],
            },
            sort_keys=True,
        )
    )
    return 0


OPERATIONS = {
    "seed": (op_seed, 0),
    "append": (op_append, 1),
    "race": (op_race, 3),
    "crash": (op_crash, 1),
    "write": (op_write, 0),
    "lock": (op_lock, 1),
    "report": (op_report, 0),
}


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    operation, project, kind, *extra = argv
    handler, arity = OPERATIONS.get(operation, (None, 0))
    if handler is None:
        print(f"unknown operation '{operation}'", file=sys.stderr)
        return 2
    if len(extra) != arity:
        print(f"'{operation}' takes {arity} argument(s)", file=sys.stderr)
        return 2
    return handler(open_store(project, kind), *extra)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
