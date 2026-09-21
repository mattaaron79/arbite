"""The event stream the EV transcripts describe, built through the store.

Shared by the scenario tests (`test_events_examples.py`) and the command tests
(`test_events_cli.py`) because both describe the same state: 34 events on one
store, whose newest four are exactly the rows EV2/EV3/EV5 print.

Read-category observations sit earlier in the stream, which is what `--include-reads`
exists to include -- and why the *default* view still prints a `read.file` row: a
read that belongs to an operation is file activity, not an observation (see the
category note in `records.EVENT_CATEGORIES`).
"""

from __future__ import annotations

from pathlib import Path

import examples
from arbite.coordination import records
from arbite.coordination.store import open_coordination_store
from arbite.sinks import SinkSpec, build_sink

#: The four newest events, exactly as the transcripts print them. The values the
#: example harness does *not* normalise (subject paths, kinds, actors, outcomes)
#: have to match the document character for character, which is why they are here
#: rather than invented per test.
NEWEST_ROWS = (
    {
        "cursor": 31,
        "kind": "write.file",
        "category": "file",
        "subject": "src/arbite/sinks/base.py",
        "operation": "op-9a14",
        "ticket": "tic-cf9f",
        "attempt": "att-91bd",
        "actor": "claude.opus.001",
        "at": "2026-09-21T13:14:02Z",
        "result": "+18 -0",
    },
    {
        "cursor": 32,
        "kind": "claim.file",
        "category": "claim",
        "subject": "src/arbite/schema.py",
        "operation": "op-3f02",
        "ticket": "tic-cf9f",
        "attempt": "att-91bd",
        "actor": "claude.opus.001",
        "at": "2026-09-21T13:15:11Z",
        "result": "gen 3",
    },
    {
        "cursor": 33,
        "kind": "read.file",
        "category": "file",
        "subject": "src/arbite/graph.py",
        "operation": "op-4a90",
        "ticket": "tic-9b57",
        "attempt": "att-4c81",
        "actor": "claude.sonnet.002",
        "at": "2026-09-21T13:15:40Z",
        "result": "read-only",
    },
    {
        "cursor": 34,
        "kind": "close.ticket",
        "category": "lifecycle",
        "subject": "tic-cf9f",
        "operation": "op-7c11",
        "ticket": "tic-cf9f",
        "attempt": "att-91bd",
        "actor": "claude.opus.001",
        "at": "2026-09-21T13:20:03Z",
        "result": "2 claims released",
    },
)

#: How many events precede those four, and which of them are read observations.
#: The subjects of the earlier events are deliberately no longer than the widest one
#: the transcripts print, because the column width is laid out from the stream.
EARLIER_EVENTS = 30
OBSERVATION_CURSORS = (4, 9, 14, 19, 24, 29)


def build_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project holding the 34 events the EV transcripts describe."""
    project = tmp_path / "project"
    project.mkdir(parents=True)
    arbite_dir = project / ".arbite"
    arbite_dir.mkdir()
    (arbite_dir / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    proc = examples.run_cli(project, "init")
    assert proc.returncode == 0, proc.stderr

    store = open_coordination_store(build_sink(SinkSpec(kind=sink_kind), arbite_dir))
    for cursor in range(1, EARLIER_EVENTS + 1):
        observation = cursor in OBSERVATION_CURSORS
        store.put_record(
            records.Event(
                id=f"evt-{cursor:04x}",
                cursor=cursor,
                kind="read.observed" if observation else "claim.acquired",
                category=records.READ_CATEGORY if observation else "claim",
                recorded_at="2026-09-21T13:00:00Z",
                ticket_id="tic-cf9f",
                attempt_id="att-91bd",
                actor="claude.opus.001",
                payload={
                    "subject": "src/arbite/schema.py",
                    "result": "read-only" if observation else "gen 1",
                },
            )
        )
    for row in NEWEST_ROWS:
        store.put_record(
            records.Event(
                id=f"evt-{row['cursor']:04x}",
                cursor=row["cursor"],
                kind=row["kind"],
                category=row["category"],
                recorded_at=row["at"],
                ticket_id=row["ticket"],
                attempt_id=row["attempt"],
                actor=row["actor"],
                operation_id=row["operation"],
                payload={"subject": row["subject"], "result": row["result"]},
            )
        )
    return project
