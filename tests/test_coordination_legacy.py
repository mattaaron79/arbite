"""Legacy readability and the regression guard for the C01 change.

The coordination slice adds *new* records and does not touch the ticket store,
so the contract this file protects is that an existing store keeps working
exactly as before: date-only ticket timestamps still parse, a pre-coordination
SQLite database still reads, the file sink's folder layout is unchanged, no
coordination table is required for ticket commands, and the CLI still runs
end-to-end against both sinks.

The old-store fixtures are built with raw SQL and raw files rather than through
the current sink API, because the point is to read bytes written the *old* way.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import coordination, schema
from arbite.query import TicketQuery
from arbite.schema import parse_ticket, validate_ticket
from arbite.sinks.file import FileSink, write_atomic
from arbite.sinks.sqlite import DDL, SCHEMA_VERSION, SqliteSink

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

# A ticket written the way the project wrote them before granular or UTC
# timestamps existed: date-only `created`/`updated`, a plain note line.
LEGACY_TICKET_MARKDOWN = """---
id: tic-a1b2
title: Legacy ticket
status: open
type: bug
tier: medium
domain: mesh
created: 2026-01-01
updated: 2026-01-01
tags: []
depends_on: []
---

## Description
written before timestamps were granular

## Notes
- 2026-01-02 someone: an old note
"""


def run_cli(project, *args, sink=None):
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    if sink:
        environment["ARBITE_SINK"] = sink
    proc = subprocess.run(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(project),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"arbite {' '.join(args)} -> exit {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


# --- legacy ticket parsing -------------------------------------------------


def test_legacy_date_only_ticket_still_parses_and_validates():
    ticket = parse_ticket(LEGACY_TICKET_MARKDOWN)
    # PyYAML hands a bare `2026-01-01` back as a date object; that long-standing
    # behaviour is preserved, so compare the text form rather than forcing one.
    assert str(ticket.created) == "2026-01-01"
    assert str(ticket.updated) == "2026-01-01"
    assert ticket.closed is None
    assert validate_ticket(ticket) == []


def test_rendering_a_legacy_ticket_round_trips_its_date_only_timestamps():
    ticket = parse_ticket(LEGACY_TICKET_MARKDOWN)
    reparsed = parse_ticket(ticket.to_markdown())
    assert str(reparsed.created) == "2026-01-01"
    assert str(reparsed.updated) == "2026-01-01"
    assert reparsed.body == ticket.body


def test_the_legacy_timestamp_vocabulary_is_unmodified():
    assert schema.DATE_PATTERN.match("2026-01-01")
    assert schema.DATE_PATTERN.match("2026-01-01T12:34:56")
    assert not schema.DATE_PATTERN.match("2026-01-01T12:34:56Z")
    assert not schema.now().endswith("Z")
    assert SCHEMA_VERSION == 1


# --- legacy file store -----------------------------------------------------


def test_legacy_file_layout_reads_without_any_coordination_state(arbite_dir):
    sink = FileSink(arbite_dir)
    sink.init()
    write_atomic(LEGACY_TICKET_MARKDOWN, arbite_dir / "open" / "tic-a1b2.md")

    assert sink.ids() == ["tic-a1b2"]
    ticket = sink.get("tic-a1b2")
    assert str(ticket.created) == "2026-01-01"
    assert sink.query(TicketQuery())[0].id == "tic-a1b2"
    # The coordination surface is a capability, not a requirement: reading an old
    # layout works with none of it. Providing the store (C02) must therefore
    # conjure nothing on disk -- the layout is created lazily on first *use*.
    store = sink.coordination()
    assert store is not None
    assert store.contract_version() == coordination.CONTRACT_VERSION
    assert not (arbite_dir / "coordination").exists()


# --- legacy SQLite store ---------------------------------------------------


def build_legacy_sqlite(path: Path) -> None:
    """A database exactly as the pre-coordination code created it: the same DDL
    and schema_version, populated with raw rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(DDL)
        conn.execute(
            "INSERT INTO schema_version (version) "
            "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT INTO tickets (id, title, status, type, tier, domain, body, created, updated) "
            "VALUES ('tic-a1b2', 'Legacy ticket', 'open', 'bug', 'medium', 'mesh', '', "
            "'2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO ticket_tags (ticket_id, tag, ordinal) VALUES ('tic-a1b2', 'legacy', 0)"
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_sqlite_store_reads_and_needs_no_coordination_tables(arbite_dir):
    db_path = arbite_dir / "arbite.db"
    build_legacy_sqlite(db_path)

    sink = SqliteSink(db_path)
    assert sink.schema_version() == SCHEMA_VERSION
    assert sink.ids() == ["tic-a1b2"]
    ticket = sink.get("tic-a1b2")
    assert ticket.created == "2026-01-01"
    assert ticket.tags == ["legacy"]
    # Same rule for SQLite: asking for the store creates no tables. The exact
    # table set below is the assertion that keeps this honest.
    assert sink.coordination().contract_version() == coordination.CONTRACT_VERSION

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    # No coordination state was conjured into the old store.
    assert tables == {"schema_version", "tickets", "ticket_tags", "ticket_deps", "ticket_notes"}


def test_an_old_sqlite_store_accepts_new_tickets_unchanged(arbite_dir):
    db_path = arbite_dir / "arbite.db"
    build_legacy_sqlite(db_path)

    from helpers import make_ticket

    sink = SqliteSink(db_path)
    sink.create(make_ticket("tic-c3d4", title="New work", created="2026-03-03T03:03:03"))
    assert sink.get("tic-c3d4").created == "2026-03-03T03:03:03"
    assert sink.schema_version() == SCHEMA_VERSION


# --- CLI regression --------------------------------------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_existing_cli_commands_still_work_on_both_sinks(tmp_path, sink_kind):
    project = tmp_path / sink_kind
    project.mkdir()

    run_cli(project, "init", sink=sink_kind)
    created = run_cli(
        project,
        "create",
        "--title",
        "Regression ticket",
        "--type",
        "bug",
        "--tier",
        "medium",
        "--domain",
        "mesh",
        sink=sink_kind,
    ).stdout
    assert "tic-" in created

    listing = run_cli(project, "list", "--json", sink=sink_kind).stdout
    payload = json.loads(listing)
    assert isinstance(payload, list) and payload and payload[0]["title"] == "Regression ticket"

    showed = run_cli(project, "show", payload[0]["id"], "--json", sink=sink_kind).stdout
    assert json.loads(showed)["id"] == payload[0]["id"]
