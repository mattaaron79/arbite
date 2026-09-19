"""Fixtures shared by the sink and CLI test suites.

The central fixture is `sink`, parametrized over every implementation: the
conformance suite runs each of its tests against both, which is what makes "any
sink implements the same interface" a checked claim rather than an aspiration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arbite.sinks import SinkSpec, build_sink
from arbite.sinks.file import FileSink
from arbite.sinks.sqlite import SqliteSink
from helpers import make_ticket

SINK_KINDS = ("file", "sqlite")


def make_sink(kind: str, arbite_dir: Path, initialise: bool = True):
    """Build a sink of `kind` anchored at `arbite_dir`, as config resolution
    would, without going through the CLI or a config file."""
    spec = SinkSpec(kind=kind)
    sink = build_sink(spec, arbite_dir)
    if initialise:
        sink.init()
    return sink


@pytest.fixture(params=SINK_KINDS)
def kind(request) -> str:
    """Both sink implementations, so a test written once is checked twice."""
    return request.param


@pytest.fixture
def arbite_dir(tmp_path: Path) -> Path:
    directory = tmp_path / ".arbite"
    directory.mkdir()
    return directory


@pytest.fixture
def sink(kind: str, arbite_dir: Path):
    return make_sink(kind, arbite_dir)


@pytest.fixture
def populated(sink):
    """A store covering every status, a bucket, notes, a dependency and a closed
    ticket -- enough for the structural assertions to have something to bite on."""
    sink.create(
        make_ticket(
            "tic-a1b2",
            title="Fix LOD pop-in",
            status="open",
            type="bug",
            tier="medium",
            domain="mesh",
            epic="mesh-pipeline",
            priority=2,
            tags=["lod", "mesh"],
            created="2026-01-01T00:00:00",
            updated="2026-01-01T00:00:00",
            body="## Description\nLOD pops between levels.\n\n## Notes\n",
        )
    )
    # Every ticket gets a distinct type and a distinctive title/tags, so a filter
    # test that expects exactly one row is testing the filter and not the fixture.
    sink.create(
        make_ticket(
            "tic-b2c3",
            title="Add per-mesh LOD",
            status="open",
            type="feature",
            tier="high",
            domain="mesh",
            epic="mesh-pipeline",
            priority=1,
            tags=["lod"],
            depends_on=["tic-a1b2"],
            created="2026-01-02T00:00:00",
            updated="2026-01-02T00:00:00",
        )
    )
    sink.create(
        make_ticket(
            "tic-c3d4",
            title="Currently being worked",
            status="in_progress",
            type="chore",
            domain="audio_gen",
            assignee="claude.haiku.001",
            priority=3,
            created="2026-01-03T00:00:00",
            updated="2026-01-03T00:00:00",
        )
    )
    sink.create(
        make_ticket(
            "tic-d4e5",
            title="Stalled on upstream",
            status="blocked",
            type="chore",
            blocked_by="waiting on the Blender API fix",
            tier="high",
            domain="io",
            created="2026-01-04T00:00:00",
            updated="2026-01-04T00:00:00",
        )
    )
    sink.create(
        make_ticket(
            "tic-e5f6",
            title="Parked for later",
            status="shelved",
            type="chore",
            tier="low",
            domain="ui",
            created="2026-01-05T00:00:00",
            updated="2026-01-05T00:00:00",
        )
    )
    sink.create(
        make_ticket(
            "tic-f607",
            title="Finished work",
            status="closed",
            type="refactor",
            domain="io",
            closed="2026-02-10T10:00:00",
            created="2026-01-06T00:00:00",
            updated="2026-02-10T10:00:00",
        )
    )
    sink.create(
        make_ticket(
            "tic-0718",
            title="A wish",
            status="raw",
            type="feature",
            created="2026-01-07T00:00:00",
            updated="2026-01-07T00:00:00",
            body="## Description\nfly-through camera preview\n\n## Notes\n",
        )
    )
    sink.add_note("tic-c3d4", "claude.haiku.001", "first progress update")
    sink.add_note("tic-c3d4", "claude.haiku.001", "second progress update")
    sink.move_to_bucket("tic-0718", "wishlist")
    return sink


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """A directory to run the CLI in, with a fresh (uninitialised) arbite dir."""
    project = tmp_path / "project"
    project.mkdir()
    return project
