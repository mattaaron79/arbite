"""End-to-end CLI tests: the command surface itself.

These run the real CLI in a subprocess, in a throwaway project directory, because
the contract being protected here is the one an agent or a shell script sees --
argv, exit codes and `--json` payloads -- rather than any internal function. The
same flow is exercised over both sinks: from the outside, only `arbite sink info`
and the `path` field should reveal which store is in use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


@pytest.fixture
def cli(tmp_project):
    """Run the CLI in a throwaway project and assert its exit code."""

    def run(*args, expect=0, sink=None, env=None):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        # Never inherit a sink from the developer's shell: every test states what
        # it is testing, including "the default is files".
        environment.pop("ARBITE_SINK", None)
        if sink:
            environment["ARBITE_SINK"] = sink
        if env:
            environment.update(env)
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


@pytest.fixture
def project(cli, tmp_project):
    """An initialised project using the default (file) sink."""
    cli("init")
    return tmp_project


def ticket_id(output: str) -> str:
    match = re.search(r"(tic-[0-9a-f]{4})", output)
    assert match, f"no ticket id in output: {output}"
    return match.group(1)


def create(cli, title="A ticket", **flags) -> str:
    args = ["create", "--title", title, "--type", "bug", "--tier", "medium", "--domain", "mesh"]
    for key, value in flags.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    return ticket_id(cli(*args).stdout)


# --- init and the sink surface --------------------------------------------


def test_init_creates_the_file_layout_and_a_folder_aware_guide(cli, tmp_project):
    output = cli("init").stdout
    assert "file sink ready" in output
    for name in ("raw", "open", "in_progress", "blocked", "shelved", "closed", "wishlist", "planning"):
        assert (tmp_project / ".arbite" / name).is_dir(), name
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert "folder is the source of truth" in guide
    assert "## Where tickets live (the sink)" in guide


def test_init_with_the_sqlite_sink_creates_a_database_and_says_so(cli, tmp_project):
    output = cli("init", "--sink", "sqlite").stdout
    assert "sqlite sink ready" in output
    assert (tmp_project / ".arbite" / "arbite.db").is_file()
    assert "sink: sqlite" in output, "the user must be told how to make it the default"
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert "sqlite" in guide
    # The guide must not teach the folder rule to a store that has no folders.
    assert "folder is the source of truth" not in guide
    assert "the folder a ticket sits in mirrors its `status`" not in guide
    assert "`in_progress` with `assignee`" in guide


def test_sink_info_reports_the_active_sink(project, cli):
    info = json.loads(cli("sink", "info", "--json").stdout)
    assert info["kind"] == "file"
    assert info["status_is_location"] is True
    assert info["supports_buckets"] is True
    assert info["ticket_count"] == 0
    text = cli("sink", "info").stdout
    # `sink info` is where a user asks "what storage am I using", so it has to
    # name the alternatives rather than only the active one.
    assert "available sinks: file, sqlite" in text


def test_a_database_nobody_selected_is_called_out(cli, tmp_project):
    """`init --sink sqlite` is a per-command flag, so a project can have a full
    database while `arbite list` reads an empty file store. That failure is
    otherwise completely silent, so the CLI says so."""
    cli("init", "--sink", "sqlite")

    proc = cli("list", expect=2)
    assert "exists but no sink is configured" in proc.stderr
    assert "--sink sqlite" in proc.stderr

    # An explicit choice, by flag or environment, is a decision -- never second-guessed.
    assert "no sink is configured" not in cli("list", "--sink", "sqlite", expect=2).stderr
    assert "no sink is configured" not in cli("list", expect=2, sink="file").stderr
    # And once the config names a sink, there is nothing ambiguous left.
    (tmp_project / "arbite.yaml").write_text("sink: file\n")
    assert "no sink is configured" not in cli("list", expect=2).stderr


def test_migrate_names_the_source_when_it_is_already_the_active_sink(cli, tmp_project):
    cli("init")
    (tmp_project / "arbite.yaml").write_text("sink: sqlite\n")
    cli("sink", "init")
    proc = cli("migrate", "--to", "sqlite", expect=1)
    assert "--from file" in proc.stderr
    # Following that instruction works: the file store is empty here, which is an
    # answer (exit 2), not an error.
    followed = cli("migrate", "--from", "file", "--to", "sqlite", expect=2)
    assert "no tickets found in the file sink" in followed.stdout


def test_sink_can_be_selected_by_flag_before_or_after_the_command(cli, tmp_project):
    cli("init", "--sink", "sqlite")
    (tmp_project / "arbite.yaml").write_text("sink: sqlite\n")
    assert json.loads(cli("sink", "info", "--json").stdout)["kind"] == "sqlite"
    assert json.loads(cli("--sink", "sqlite", "sink", "info", "--json").stdout)["kind"] == "sqlite"
    assert json.loads(cli("sink", "info", "--sink", "sqlite", "--json").stdout)["kind"] == "sqlite"
    # ARBITE_SINK outranks the config file
    assert json.loads(cli("sink", "info", "--json", sink="file").stdout)["kind"] == "file"


def test_a_configured_location_is_honoured(cli, tmp_project):
    (tmp_project / "arbite.yaml").write_text(
        "sink: sqlite\nsinks:\n  sqlite:\n    path: .arbite/custom.sqlite\n"
    )
    cli("init")
    assert (tmp_project / ".arbite" / "custom.sqlite").is_file()
    assert json.loads(cli("sink", "info", "--json").stdout)["root"].endswith("custom.sqlite")


def test_an_unknown_sink_is_rejected(cli, tmp_project):
    cli("sink", "info", "--sink", "nosuch", expect=2)  # argparse rejects it


def test_commands_without_a_store_tell_you_to_init(cli, tmp_project):
    proc = cli("list", expect=1)
    assert "run 'arbite init' first" in proc.stderr


# --- create, read, and the JSON contract ----------------------------------


def test_show_json_exposes_the_frontmatter_contract(project, cli):
    tid = create(cli, "Fix LOD", priority=2, tags="lod,mesh")
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["id"] == tid
    assert payload["priority"] == 2
    assert payload["tags"] == ["lod", "mesh"]
    assert payload["status"] == "open"
    assert "body" in payload and "path" in payload
    assert payload["path"].endswith(f"{tid}.md")


def test_a_wildcard_id_resolves_but_a_mutation_refuses_an_ambiguous_one(project, cli):
    first = create(cli, "one")
    cli("create", "--title", "two", "--type", "bug", "--tier", "low", "--domain", "ui")
    assert json.loads(cli("show", first[-3:], "--json").stdout)["id"] == first
    if len(cli("list", "--json").stdout) > 0:
        cli("show", "tic-", "--json")
    cli("note", "tic-", "system", "ambiguous", expect=1)


def test_create_requires_its_fields_unless_blank(project, cli):
    cli("create", "--title", "no type", expect=1)
    blank = ticket_id(cli("create", "--blank").stdout)
    assert json.loads(cli("show", blank, "--json").stdout)["title"].startswith("TODO:")


# --- triage flow ----------------------------------------------------------


def test_raw_list_fetch_and_classify(project, cli):
    tid = ticket_id(cli("raw", "feature", "add per-mesh LOD").stdout)
    listing = cli("list", "raw").stdout
    assert tid in listing and "add per-mesh LOD" in listing
    fetched = json.loads(cli("fetch", "--json").stdout)
    assert fetched["id"] == tid
    assert "derived_note" in fetched
    assert fetched["epic"] == "classification"

    cli("set", tid, "title", "Add per-mesh LOD", "tier", "high", "domain", "mesh",
        "epic", "mesh-pipeline", "priority", "1", "status", "open")
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "open"
    assert tid in cli("list", "next", "--tier", "high").stdout


def test_fetch_with_an_empty_backlog_exits_two(project, cli):
    assert cli("fetch", "--json", expect=2).stdout.strip() == "null"
    assert "no raw tickets" in cli("fetch", expect=2).stdout
    cli("list", "raw", expect=2)


def test_the_shortcuts_are_the_raw_command(cli, tmp_project):
    cli("init")
    tid = ticket_id(cli("bug", "the thing broke").stdout)
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["type"] == "bug"
    assert payload["status"] == "raw"
    assert "the thing broke" in payload["body"]


# --- lifecycle over both sinks --------------------------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_the_lifecycle_behaves_the_same_in_every_sink(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "Worked ticket", priority=1, sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "in_progress"
    cli("note", tid, "claude.haiku.001", "found it", sink=sink_kind)
    cli("block", tid, "--reason", "waiting on upstream", sink=sink_kind)
    cli("unblock", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    cli("release", tid, "--agent", "claude.haiku.001", "--reason", "wrong tier", sink=sink_kind)
    cli("shelve", tid, "--reason", "later", sink=sink_kind)
    cli("unshelve", tid, "--reason", "back", sink=sink_kind)
    cli("close", tid, sink=sink_kind)
    cli("reopen", tid, sink=sink_kind)
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "open"
    assert "found it" in payload["body"]
    assert [n["kind"] for n in json.loads(cli("doctor", "--json", sink=sink_kind).stdout)["problems"]] == []


def test_list_next_prefers_urgency_and_skips_unmet_dependencies(project, cli):
    blocker = create(cli, "blocker", priority=1)
    dependent = create(cli, "dependent", priority=1)
    cli("depend", dependent, blocker)
    assert ticket_id(cli("list", "next").stdout) == blocker
    cli("close", blocker)
    assert ticket_id(cli("list", "next").stdout) == dependent


def test_list_next_claims_in_one_step_and_reports_a_dry_queue(project, cli):
    first = create(cli, "one", priority=1)
    second = create(cli, "two", priority=2)
    claimed = [t["id"] for t in json.loads(cli("list", "next", "--count", "2", "--claim", "claude.haiku.001", "--json").stdout)]
    assert claimed == [first, second]
    assert json.loads(cli("list", "next", "--claim", "claude.haiku.001", "--json", expect=2).stdout) == []


def test_claim_refuses_another_agents_ticket_unless_forced(project, cli):
    tid = create(cli, "contested")
    cli("claim", tid, "--agent", "claude.haiku.001")
    cli("claim", tid, "--agent", "claude.opus.001", expect=1)
    cli("claim", tid, "--agent", "claude.opus.001", "--force")
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["assignee"] == "claude.opus.001"
    assert "Claim taken over from claude.haiku.001" in payload["body"]


def test_views(project, cli):
    blocker = create(cli, "blocker", priority=3)
    dependent = create(cli, "dependent", priority=1)
    cli("depend", dependent, blocker)
    assert blocker in cli("list", "--topo").stdout
    assert dependent in cli("list", "--tree").stdout
    assert dependent in cli("deps", dependent).stdout
    assert blocker in cli("search", "blocker").stdout
    assert dependent in json.loads(cli("list", "--tic", dependent, "--json").stdout)[0]["id"]


def test_set_validates_and_reports_what_it_changed(project, cli):
    tid = create(cli, "before")
    cli("set", tid, "title", "after", "tags", "a, b", "priority", "4")
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["title"] == "after"
    assert payload["tags"] == ["a", "b"]
    assert payload["priority"] == 4
    cli("set", tid, "tier", "impossible", expect=1)
    cli("set", tid, "nosuchfield", "x", expect=1)
    cli("set", tid, "title", expect=1)  # odd number of arguments


def test_move_files_and_unfiles_a_ticket(project, cli):
    tid = create(cli, "a wish")
    cli("move", tid, "/wishlist")
    assert (project / ".arbite" / "wishlist" / f"{tid}.md").exists()
    cli("doctor")  # a bucketed ticket is legitimate, not a problem
    cli("list", expect=2)  # ...and out of the status listings
    cli("move", tid, "/")
    assert (project / ".arbite" / "open" / f"{tid}.md").exists()
    cli("move", tid, "wishlist", expect=1)  # must be root-relative
    cli("move", tid, "/../escape", expect=1)


def test_delete_needs_force_and_leaves_a_receipt(project, cli):
    tid = create(cli, "doomed")
    cli("delete", tid, expect=1)
    assert (project / ".arbite" / "open" / f"{tid}.md").exists()
    receipt = json.loads(cli("delete", tid, "--force", "--agent", "claude.haiku.001", "--reason", "duplicate", "--json").stdout)
    assert receipt["id"] == tid and receipt["deleted"] is True
    assert "Deleted by claude.haiku.001: duplicate" in receipt["note"]
    assert not (project / ".arbite" / "open" / f"{tid}.md").exists()
    cli("show", tid, expect=1)


# --- doctor ---------------------------------------------------------------


def test_doctor_reports_drift_and_fixes_it(project, cli):
    tid = create(cli, "drifting")
    (project / ".arbite" / "open" / f"{tid}.md").rename(project / ".arbite" / "shelved" / f"{tid}.md")

    report = json.loads(cli("doctor", "--json", expect=3).stdout)
    assert [p["kind"] for p in report["problems"]] == ["status_drift"]
    assert report["remaining"] == 1

    fixed = json.loads(cli("doctor", "--json", "--fix").stdout)
    assert fixed["fixed"] == 1
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "shelved"
    assert json.loads(cli("doctor", "--json").stdout)["problems"] == []


def test_doctor_reports_the_sink_it_checked(project, cli):
    report = json.loads(cli("doctor", "--json").stdout)
    assert report["sink"]["kind"] == "file"
    assert report["tickets_checked"] == 0


# --- migrate --------------------------------------------------------------


def test_migrate_round_trips_file_to_sqlite_and_back_byte_identically(project, cli):
    """The end-to-end proof that two independent sinks agree: read everything from
    one, write it to the other, and compare the text form byte for byte."""
    open_ticket = create(cli, "An open ticket", priority=2, tags="a,b")
    closed = create(cli, "A closed ticket")
    cli("note", open_ticket, "claude.haiku.001", "a note that must survive")
    cli("close", closed)
    wish = create(cli, "A wish")
    cli("move", wish, "/wishlist")

    def snapshot() -> dict:
        return {
            str(path.relative_to(project)): path.read_bytes()
            for path in sorted((project / ".arbite").rglob("*.md"))
            if path.name != "AGENTS.md" and "agents" not in path.parts
        }

    before = snapshot()
    assert len(before) == 3

    dry = cli("migrate", "--to", "sqlite", "--dry-run").stdout
    assert "would migrate 3 ticket(s)" in dry
    assert not (project / ".arbite" / "arbite.db").exists(), "--dry-run must not create the target"

    cli("migrate", "--to", "sqlite")
    rows = json.loads(cli("list", "--json", sink="sqlite").stdout)
    assert len(rows) == 2, "the bucketed ticket is out of the status listings"
    assert len(json.loads(cli("list", "--json", "--tic", "tic-", sink="sqlite").stdout)) == 2
    assert json.loads(cli("show", open_ticket, "--json", sink="sqlite").stdout)["path"].startswith("sqlite:")
    assert "a note that must survive" in json.loads(cli("show", open_ticket, "--json", sink="sqlite").stdout)["body"]

    # Wipe the file store and rebuild it from the database: same bytes, same paths.
    for directory in ("raw", "open", "in_progress", "blocked", "shelved", "closed", "wishlist"):
        shutil.rmtree(project / ".arbite" / directory, ignore_errors=True)
    cli("migrate", "--from", "sqlite", "--to", "file")

    assert snapshot() == before
    assert json.loads(cli("doctor", "--json").stdout)["problems"] == []


def test_migrate_refuses_to_clobber_without_overwrite(project, cli):
    tid = create(cli, "original")
    cli("migrate", "--to", "sqlite")
    cli("set", tid, "title", "changed in the file sink")
    cli("migrate", "--to", "sqlite")
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["title"] == "original"
    cli("migrate", "--to", "sqlite", "--overwrite")
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["title"] == "changed in the file sink"


def test_migrate_from_an_empty_store_exits_two(project, cli):
    cli("migrate", "--to", "sqlite", expect=2)
