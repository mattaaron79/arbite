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

from arbite import config, docs

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
    guide = (tmp_project / ".arbite" / "REFERENCE.md").read_text()
    assert "folder is the source of truth" in guide
    assert "## Where tickets live (the sink)" in guide
    quickstart = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert "A ticket's folder is its status" in quickstart
    assert ".arbite/REFERENCE.md" in quickstart


def test_the_quickstart_stays_short_and_indexes_real_reference_sections(cli, tmp_project):
    """AGENTS.md is read at the start of every task: it has a hard size budget, and
    every section it sends a reader to must exist in REFERENCE.md."""
    cli("init")
    quickstart = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    reference = (tmp_project / ".arbite" / "REFERENCE.md").read_text()
    assert len(quickstart) <= docs.QUICKSTART_MAX_CHARS, len(quickstart)
    for _, heading in docs._REFERENCE_INDEX:
        assert f"## {heading}" in reference, heading
    for needle in ("--version-only", "active_attempt.id", "--edits -", "stale_read", "file_busy"):
        assert needle in quickstart, needle
    assert "arbite export --scope coordination" not in quickstart + reference


def test_init_leaves_agent_docs_alone_without_the_flags(cli, tmp_project):
    """Installing the block is opt-in: a plain `init` must not drop AGENTS.md or
    CLAUDE.md into a project that never asked for them."""
    cli("init")
    assert not (tmp_project / "AGENTS.md").exists()
    assert not (tmp_project / "CLAUDE.md").exists()


def test_init_agents_doc_creates_then_leaves_the_block_alone(cli, tmp_project):
    """`--agents-doc` writes a new AGENTS.md, and re-running `init` is idempotent:
    the BEGIN/END demarkations stop a second block being stacked."""
    output = cli("init", "--agents-doc").stdout
    doc = (tmp_project / "AGENTS.md").read_text()
    assert "created" in output and "AGENTS.md" in output
    assert doc.startswith("<!-- BEGIN ARBITE INSTRUCTIONS -->")
    assert doc.rstrip().endswith("<!-- END ARBITE INSTRUCTIONS -->")
    assert "Arbite Ticketing System" in doc

    again = cli("init", "--agents-doc").stdout
    assert "already contains the arbite instructions block" in again
    assert (tmp_project / "AGENTS.md").read_text() == doc


def test_init_refreshes_an_outdated_block_between_its_markers(cli, tmp_project):
    """Template fixes reach existing projects: only the marked block is replaced,
    and a file whose markers do not pair up is left alone."""
    stale = "<!-- BEGIN ARBITE INSTRUCTIONS -->\nold advice\n<!-- END ARBITE INSTRUCTIONS -->"
    (tmp_project / "CLAUDE.md").write_text("# Mine above\n\n" + stale + "\n\nMine below.\n")
    assert "refreshed the arbite instructions block" in cli("init", "--claude-doc").stdout
    doc = (tmp_project / "CLAUDE.md").read_text()
    assert doc == "# Mine above\n\n" + docs.ARBITE_INSTRUCTIONS_BLOCK + "\n\nMine below.\n"

    broken = "<!-- END ARBITE INSTRUCTIONS -->\nkeep\n<!-- BEGIN ARBITE INSTRUCTIONS -->\n"
    (tmp_project / "AGENTS.md").write_text(broken)
    assert "do not pair up" in cli("init", "--agents-doc").stdout
    assert (tmp_project / "AGENTS.md").read_text() == broken


def test_init_claude_doc_prepends_without_clobbering_the_file(cli, tmp_project):
    """An existing CLAUDE.md keeps its contents; the block is prepended above them,
    and the alias spelling `--claud-doc` is accepted too."""
    original = "# Project notes\n\nKeep me.\n"
    (tmp_project / "CLAUDE.md").write_text(original)
    output = cli("init", "--claude-doc").stdout
    doc = (tmp_project / "CLAUDE.md").read_text()
    assert "prepended" in output
    assert doc.startswith("<!-- BEGIN ARBITE INSTRUCTIONS -->")
    assert doc.endswith(original)
    assert doc.count("<!-- BEGIN ARBITE INSTRUCTIONS -->") == 1

    assert "already contains" in cli("init", "--claud-doc").stdout
    assert (tmp_project / "CLAUDE.md").read_text() == doc


def test_init_with_the_sqlite_sink_creates_a_database_and_says_so(cli, tmp_project):
    output = cli("init", "--sink", "sqlite").stdout
    assert "sqlite sink ready" in output
    assert (tmp_project / ".arbite" / "arbite.db").is_file()
    # The store it created becomes the project default, so nothing has to be
    # configured by hand afterwards.
    assert "set 'sink: sqlite'" in output
    assert (tmp_project / "arbite.yaml").read_text().strip() == "sink: sqlite"
    # Creating a store is still not migrating into it: the database starts empty, and
    # any existing ticket files stay files until `migrate` runs.
    assert cli("list", expect=2).stdout.strip() == "no tickets found"


def test_init_makes_the_store_it_creates_the_default(cli, tmp_project):
    """The point of writing the config: whatever store you set up is the store every
    later command reads, including one an agent runs with no flags at all."""
    cli("init", "--sink", "sqlite")
    tid = ticket_id(
        cli(
            "create", "--title", "in the database", "--type", "bug", "--tier", "low",
            "--domain", "io",
        ).stdout
    )
    assert json.loads(cli("show", tid, "--json").stdout)["path"].startswith("sqlite:")
    assert not (tmp_project / ".arbite" / "open").exists(), "nothing landed in files"


def test_the_default_sink_writes_no_config(cli, tmp_project):
    """A fresh file-based project stays config-free: nothing to explain, nothing to
    keep in sync with the directory that is already there."""
    cli("init")
    assert not (tmp_project / "arbite.yaml").exists()


def test_setting_a_sink_preserves_the_rest_of_the_config(cli, tmp_project):
    """arbite.yaml is also hand-maintained, so only the `sink:` line is touched."""
    (tmp_project / "arbite.yaml").write_text(
        "# hand-maintained\n"
        "agents: [claude.haiku.001]\n"
        "sink: file\n"
        "sinks:\n"
        "  sqlite:\n"
        "    path: .arbite/other.db\n"
    )
    cli("init", "--sink", "sqlite")
    text = (tmp_project / "arbite.yaml").read_text()
    assert "# hand-maintained" in text
    assert "agents: [claude.haiku.001]" in text
    assert "path: .arbite/other.db" in text
    assert text.count("sink: sqlite") == 1, text
    assert "sink: file" not in text, "the old selection is overwritten, not duplicated"


def test_an_environment_choice_is_not_written_to_the_config(cli, tmp_project):
    """ARBITE_SINK is one process's decision; committed config is the project's."""
    output = cli("init", sink="sqlite").stdout
    assert not (tmp_project / "arbite.yaml").exists()
    assert "--sink sqlite" in output


def test_the_guide_names_the_store_a_plain_command_will_read(cli, tmp_project):
    """An agent reads this file and then runs `arbite` with no flags, so it must not
    name a store those commands would not touch -- and it must not stay quiet about
    a store holding tickets that nothing selects."""
    cli("init", "--sink", "sqlite")
    cli(
        "create", "--title", "in the database", "--type", "bug", "--tier", "low",
        "--domain", "io",
    )
    (tmp_project / "arbite.yaml").unlink()  # the selection goes; the tickets stay

    cli("init")  # re-renders the guide from the sink plain commands will read
    assert "second ticket store that nothing selects" in (
        tmp_project / ".arbite" / "AGENTS.md").read_text()
    guide = (tmp_project / ".arbite" / "REFERENCE.md").read_text()
    assert "holds a second ticket store that nothing selects" in guide
    assert "1 ticket(s) in a `sqlite` store" in guide
    assert "also present, but not selected: `sqlite`" in guide
    assert "check its `kind` field" in guide
    # ...and the behaviour prose describes what a plain command really does.
    assert "folder is the source of truth" in guide

    # Point the project at the database and both the warning and the file-shaped
    # prose go away.
    (tmp_project / "arbite.yaml").write_text("sink: sqlite\n")
    cli("init")
    guide = (tmp_project / ".arbite" / "REFERENCE.md").read_text()
    assert "nothing selects" not in guide
    assert "- active sink: `sqlite`" in guide
    assert "folder is the source of truth" not in guide
    assert "Status is a field" in guide


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
    """If a database exists that nothing selects -- hand-created, or left behind by
    someone deleting the config -- a command reading the file store would otherwise
    report "no tickets" with no hint that the backlog is one file away."""
    cli("init", "--sink", "sqlite")
    (tmp_project / "arbite.yaml").unlink()

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


def test_a_request_is_a_raw_change_request(cli, tmp_project):
    """`arbite request` is a first-class raw capture with its own type: a request for a
    change, not necessarily a bug or a new feature, but a tweak or lateral change. It is
    ordinary work once classified, so it opens/claims like any other ticket."""
    cli("init")
    tid = ticket_id(cli("request", "collapse the toolbar when scrolling").stdout)
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["type"] == "request"
    assert payload["status"] == "raw"
    assert payload["epic"] == "classification"
    assert "collapse the toolbar when scrolling" in payload["body"]
    assert "tweak or lateral change" in payload["body"]

    # The long form is the same ticket, and a request is a valid type for `doctor`.
    long_tid = ticket_id(cli("raw", "request", "collapse the toolbar when scrolling").stdout)
    long_payload = json.loads(cli("show", long_tid, "--json").stdout)
    assert long_payload["type"] == "request"
    assert "tweak or lateral change" in long_payload["body"]
    cli("doctor")


# --- lifecycle over both sinks --------------------------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_the_lifecycle_behaves_the_same_in_every_sink(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "Worked ticket", priority=1, sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "in_progress"
    cli("note", tid, "claude.haiku.001", "found it", sink=sink_kind)
    # C03: block now acts on the ticket's live work attempt, so it must be the
    # attempt's own worker doing it (or an explicit --force --reason revocation).
    # The lifecycle command therefore carries the same --agent as the claim.
    cli("block", tid, "--agent", "claude.haiku.001", "--reason", "waiting on upstream", sink=sink_kind)
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
    """C03 supersession: a forced claim of a ticket with a live work attempt is a
    *takeover*, and an administrative takeover requires an explicit reason. The
    old test's bare `--force` is therefore refused; `--force --reason` still
    succeeds and records the takeover on the ticket."""
    tid = create(cli, "contested")
    cli("claim", tid, "--agent", "claude.haiku.001")
    cli("claim", tid, "--agent", "claude.opus.001", expect=1)
    # Forced without a reason: refused (administrative overrides retain history).
    cli("claim", tid, "--agent", "claude.opus.001", "--force", expect=1)
    assert json.loads(cli("show", tid, "--json").stdout)["assignee"] == "claude.haiku.001"
    # Forced with a reason: an explicit takeover that interrupts the old attempt.
    cli("claim", tid, "--agent", "claude.opus.001", "--force", "--reason", "haiku stalled")
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
            if path.name not in ("AGENTS.md", "REFERENCE.md") and "agents" not in path.parts
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
    """A copy that silently replaced a divergent ticket would destroy the only copy
    of that work, so the destination wins nothing by default."""
    tid = create(cli, "original")
    cli("migrate", "--to", "sqlite")
    # After migrating, the database is the active store, so the file-side edit has
    # to name its sink -- and the re-migration has to name its source.
    cli("set", tid, "title", "changed in the file sink", sink="file")
    cli("migrate", "--from", "file", "--to", "sqlite")
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["title"] == "original"
    cli("migrate", "--from", "file", "--to", "sqlite", "--overwrite")
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["title"] == (
        "changed in the file sink"
    )


def test_migrate_from_an_empty_store_exits_two(project, cli):
    cli("migrate", "--to", "sqlite", expect=2)


def test_migrate_prune_retires_the_source_after_a_verified_copy(project, cli):
    """The destructive half of a migration: for retiring a store once its contents
    are known to be in the other one."""
    open_ticket = create(cli, "moving to the database")
    wish = create(cli, "a filed wish")
    cli("move", wish, "/wishlist")
    assert not (project / "arbite.yaml").exists()

    dry = cli("migrate", "--to", "sqlite", "--prune", "--dry-run").stdout
    assert "would migrate 2 ticket(s)" in dry
    assert "would prune 2 ticket(s)" in dry
    assert (project / ".arbite" / "open" / f"{open_ticket}.md").exists(), "dry run destroys nothing"

    out = cli("migrate", "--to", "sqlite", "--prune").stdout
    assert "pruned 2 ticket(s) from the file sink" in out
    assert not (project / ".arbite" / "open" / f"{open_ticket}.md").exists()
    assert not (project / ".arbite" / "wishlist" / f"{wish}.md").exists()
    # The file store is empty now, and the migration made the database the default,
    # so plain commands follow the tickets rather than the store they left.
    cli("list", "--sink", "file", expect=2)
    assert (project / "arbite.yaml").read_text().strip() == "sink: sqlite"
    assert json.loads(cli("show", open_ticket, "--json").stdout)["title"] == (
        "moving to the database"
    )
    assert json.loads(cli("show", wish, "--json").stdout)["id"] == wish
    cli("doctor")


def test_migrate_makes_the_destination_the_default(project, cli):
    tid = create(cli, "moving")
    assert not (project / "arbite.yaml").exists()

    out = cli("migrate", "--to", "sqlite").stdout
    assert "set 'sink: sqlite'" in out
    assert (project / "arbite.yaml").read_text().strip() == "sink: sqlite"
    assert json.loads(cli("show", tid, "--json").stdout)["path"].startswith("sqlite:")

    # ...and a dry run writes nothing: it does not know yet whether you will go
    # through with it.
    (project / "arbite.yaml").unlink()
    cli("migrate", "--from", "sqlite", "--to", "file", "--dry-run")
    assert not (project / "arbite.yaml").exists()


def test_migrate_prune_refuses_when_a_source_copy_is_the_newer_one(project, cli):
    tid = create(cli, "original")
    cli("migrate", "--to", "sqlite")
    cli("set", tid, "title", "newer in the file sink", sink="file")

    proc = cli("migrate", "--from", "file", "--to", "sqlite", "--prune", expect=1)
    assert "refusing to prune" in proc.stderr
    assert "--overwrite" in proc.stderr
    assert (project / ".arbite" / "open" / f"{tid}.md").exists(), "nothing was destroyed"

    # Following the instruction replaces the stale copy and the prune then proceeds.
    cli("migrate", "--from", "file", "--to", "sqlite", "--prune", "--overwrite")
    assert not (project / ".arbite" / "open" / f"{tid}.md").exists()
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["title"] == (
        "newer in the file sink"
    )


def test_migrate_prune_dry_run_reports_what_it_would_refuse(project, cli):
    tid = create(cli, "original")
    cli("migrate", "--to", "sqlite")
    out = cli("migrate", "--from", "file", "--to", "sqlite", "--prune", "--dry-run").stdout
    assert "would NOT prune" in out
    assert "--overwrite" in out
    assert (project / ".arbite" / "open" / f"{tid}.md").exists()


# --- C03: ticket acquisition and lifecycle policy --------------------------


def _legacy_in_progress(cli, sink_kind, assignee="claude.old.001"):
    """An in_progress ticket with *no* attempt record, as a pre-C03 arbite left it.

    Built with `set status`, which is allowed here precisely because there is no
    attempt: ownership-bearing fields are only refused while an attempt is live.
    """
    tid = create(cli, "legacy in progress", priority=1, sink=sink_kind)
    cli("set", tid, "status", "in_progress", "assignee", assignee, sink=sink_kind)
    return tid


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_claim_adopt_takes_over_a_legacy_in_progress_ticket(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = _legacy_in_progress(cli, sink_kind)

    # A normal claim refuses it: adoption is never implicit, and the refusal
    # names the recovery paths (--adopt / --force --reason).
    proc = cli("claim", tid, "--agent", "claude.new.001", sink=sink_kind, expect=1)
    assert "no attempt record" in proc.stderr and "--adopt" in proc.stderr

    cli("claim", tid, "--agent", "claude.new.001", "--adopt", sink=sink_kind)
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "in_progress"
    assert payload["assignee"] == "claude.new.001"

    # Now that an attempt exists, adopting again is refused (use takeover).
    cli("claim", tid, "--agent", "claude.new.001", "--adopt", sink=sink_kind, expect=1)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_ownership_is_enforced_and_force_needs_a_reason(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "owned", priority=1, sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    # Another agent cannot release the ticket...
    proc = cli("release", tid, "--agent", "claude.opus.001", sink=sink_kind, expect=1)
    assert "claude.haiku.001" in proc.stderr
    # ... and --force without a reason is refused (overrides retain history).
    proc = cli("release", tid, "--agent", "claude.opus.001", "--force", sink=sink_kind, expect=1)
    assert "--reason" in proc.stderr
    # With a reason it is an explicit administrative revocation.
    cli(
        "release",
        tid,
        "--agent",
        "claude.opus.001",
        "--force",
        "--reason",
        "owner is gone",
        sink=sink_kind,
    )
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "open"


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_set_cannot_change_status_or_assignee_while_an_attempt_is_active(
    cli, tmp_project, sink_kind
):
    cli("init", sink=sink_kind)
    tid = create(cli, "held", priority=1, sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    proc = cli("set", tid, "status", "closed", sink=sink_kind, expect=1)
    assert "arbite close" in proc.stderr
    proc = cli("set", tid, "assignee", "claude.opus.001", sink=sink_kind, expect=1)
    assert "assignee" in proc.stderr

    # Other field edits stay allowed while the attempt is active...
    cli("set", tid, "title", "renamed while working", sink=sink_kind)
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["title"] == "renamed while working"
    assert payload["status"] == "in_progress"


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_list_next_claim_still_batches_and_reports_a_dry_queue(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    first = create(cli, "one", priority=1, sink=sink_kind)
    second = create(cli, "two", priority=2, sink=sink_kind)
    claimed = [
        t["id"]
        for t in json.loads(
            cli(
                "list", "next", "--count", "2", "--claim", "claude.haiku.001",
                "--json", sink=sink_kind,
            ).stdout
        )
    ]
    assert claimed == [first, second]
    assert json.loads(
        cli("list", "next", "--claim", "claude.haiku.001", "--json", sink=sink_kind, expect=2).stdout
    ) == []


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_claim_refuses_an_unready_ticket(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    blocker = create(cli, "blocker", priority=1, sink=sink_kind)
    dependent = create(cli, "dependent", priority=1, sink=sink_kind)
    cli("depend", dependent, blocker, sink=sink_kind)

    proc = cli("claim", dependent, "--agent", "claude.haiku.001", sink=sink_kind, expect=1)
    assert "unmet dependencies" in proc.stderr
    assert blocker in proc.stderr


def test_claim_refuses_a_raw_placeholder_ticket(project, cli):
    tid = ticket_id(cli("raw", "bug", "something is broken").stdout)
    proc = cli("claim", tid, "--agent", "claude.haiku.001", expect=1)
    assert "not 'open'" in proc.stderr


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_reopen_notes_dependents_that_lost_workability(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    blocker = create(cli, "blocker", priority=1, sink=sink_kind)
    dependent = create(cli, "dependent", priority=1, sink=sink_kind)
    cli("depend", dependent, blocker, sink=sink_kind)
    cli("close", blocker, sink=sink_kind)

    proc = cli("reopen", blocker, sink=sink_kind)
    assert dependent in proc.stderr  # named as no-longer-workable

    # The dependent is untouched by the reopen: no silent status rewrite.
    payload = json.loads(cli("show", dependent, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "open"
    assert payload["assignee"] is None


# --- file ownership surface (shared-directory coordination, C04) -----------


def active_attempt_id(project: Path, sink_kind: str, ticket: str) -> str:
    """The active work attempt recorded for `ticket`, read through the real sink."""
    sink = config.open_sink(sink_kind, project)
    with sink.coordination().transaction(write=False) as tx:
        attempts = [
            attempt
            for attempt in tx.find("work_attempt", ticket_id=ticket)
            if attempt.is_active
        ]
    assert len(attempts) == 1
    return attempts[0].id


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_file_claim_and_release_surface(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "File work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.opus.001", sink=sink_kind)
    attempt = active_attempt_id(tmp_project, sink_kind, tid)

    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\n")

    claimed = json.loads(
        cli(
            "file", "claim", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind,
        ).stdout
    )
    assert claimed["ok"] is True
    assert claimed["data"]["claimed"][0]["path"] == "src/a.py"
    assert claimed["data"]["claimed"][0]["generation"] == 1
    assert claimed["data"]["claimed"][0]["observed_version"].startswith("sha256:")

    # A second attempt cannot take the same path: file_busy, with the holder named.
    other = create(cli, "Other work", sink=sink_kind)
    cli("claim", other, "--agent", "claude.haiku.001", sink=sink_kind)
    other_attempt = active_attempt_id(tmp_project, sink_kind, other)
    busy = json.loads(
        cli(
            "file", "claim", "src/a.py", "--ticket", other, "--attempt", other_attempt,
            "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert busy["ok"] is False
    assert busy["code"] == "file_busy"
    assert busy["details"]["holder_ticket"] == tid
    assert busy["details"]["holder_attempt"] == attempt

    # A rejected alias reports the documented unsupported code, not a traceback.
    bad = json.loads(
        cli(
            "file", "claim", "../escape.py", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert bad["code"] == "unsupported"

    # A release requires an attributable reason (argparse refuses the omission).
    cli(
        "file", "release", "src/a.py", "--ticket", tid, "--attempt", attempt,
        sink=sink_kind, expect=2,
    )
    released = json.loads(
        cli(
            "file", "release", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--reason", "done", "--json", sink=sink_kind,
        ).stdout
    )
    assert released["ok"] is True
    assert released["data"]["released"][0]["state"] == "released"


# --- lifecycle cleanup cascade over the file surface (C09) -----------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_lifecycle_commands_release_file_claims(cli, tmp_project, sink_kind):
    """C09: release/block/shelve/close all release the attempt's file claims.

    Proven end-to-end: a path claimed by the ticket's attempt is `file_busy` for a
    second attempt, and becomes claimable by that second attempt immediately after
    the lifecycle command ends the first attempt.
    """
    cli("init", sink=sink_kind)
    (tmp_project / "src").mkdir()
    commands = (
        ("release", ["--agent", "claude.haiku.001", "--reason", "yielding"]),
        ("block", ["--agent", "claude.haiku.001", "--reason", "upstream"]),
        ("shelve", ["--agent", "claude.haiku.001"]),
        ("close", ["--agent", "claude.haiku.001"]),
    )
    for name, extra in commands:
        path = f"src/{name}.py"
        (tmp_project / path).write_text("alpha\n")
        primary = create(cli, f"primary {name}", sink=sink_kind)
        other = create(cli, f"other {name}", sink=sink_kind)
        cli("claim", primary, "--agent", "claude.haiku.001", sink=sink_kind)
        attempt = active_attempt_id(tmp_project, sink_kind, primary)
        cli("file", "claim", path, "--ticket", primary, "--attempt", attempt, sink=sink_kind)
        cli("claim", other, "--agent", "claude.opus.001", sink=sink_kind)
        other_attempt = active_attempt_id(tmp_project, sink_kind, other)
        # Busy while the first attempt holds it.
        cli(
            "file", "claim", path, "--ticket", other, "--attempt", other_attempt,
            sink=sink_kind, expect=1,
        )
        cli(name, primary, *extra, sink=sink_kind)
        # The lifecycle command released the claim, so the other attempt takes it.
        taken = json.loads(
            cli(
                "file", "claim", path, "--ticket", other, "--attempt", other_attempt,
                "--json", sink=sink_kind,
            ).stdout
        )
        assert taken["ok"] is True
        # A released claim is retained as history, so the reacquisition is a new,
        # strictly higher generation rather than a reuse of the dead token.
        assert taken["data"]["claimed"][0]["generation"] == 2


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_takeover_releases_old_claims_and_refuses_the_stale_worker(cli, tmp_project, sink_kind):
    """C09: takeover revokes the old attempt and its token; a new read is required."""
    cli("init", sink=sink_kind)
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\n")
    tid = create(cli, "contested work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    old = active_attempt_id(tmp_project, sink_kind, tid)
    cli("file", "claim", "src/a.py", "--ticket", tid, "--attempt", old, sink=sink_kind)
    token = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", tid, "--attempt", old, "--json",
            sink=sink_kind,
        ).stdout
    )["data"]["read_token"]

    cli(
        "claim", tid, "--agent", "claude.opus.001", "--force", "--reason",
        "haiku stalled", sink=sink_kind,
    )
    # The stale attempt can no longer mutate under its old token.
    payload = tmp_project / "payload.txt"
    payload.write_text("after\n")
    cli(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", old,
        "--read-token", token, "--input", str(payload), sink=sink_kind, expect=1,
    )
    assert (tmp_project / "src" / "a.py").read_text() == "alpha\n"

    # The new attempt must claim and re-read before it can write.
    new = active_attempt_id(tmp_project, sink_kind, tid)
    assert new != old
    cli("file", "claim", "src/a.py", "--ticket", tid, "--attempt", new, sink=sink_kind)
    cli(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", new,
        "--input", str(payload), sink=sink_kind, expect=1,
    )
    fresh = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", tid, "--attempt", new, "--json",
            sink=sink_kind,
        ).stdout
    )["data"]["read_token"]
    cli(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", new,
        "--read-token", fresh, "--input", str(payload), "--json", sink=sink_kind,
    )
    assert (tmp_project / "src" / "a.py").read_text() == "after\n"


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_unblock_resumes_with_a_fresh_attempt(cli, tmp_project, sink_kind):
    """C09: block ends the attempt and releases claims; unblock starts a new one."""
    cli("init", sink=sink_kind)
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\n")
    tid = create(cli, "interrupted work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    first = active_attempt_id(tmp_project, sink_kind, tid)
    cli("file", "claim", "src/a.py", "--ticket", tid, "--attempt", first, sink=sink_kind)
    cli("block", tid, "--agent", "claude.haiku.001", "--reason", "upstream", sink=sink_kind)

    cli("unblock", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "in_progress"
    resumed = active_attempt_id(tmp_project, sink_kind, tid)
    assert resumed != first
    # The old attempt cannot reclaim; the new attempt can, at generation 1.
    cli(
        "file", "claim", "src/a.py", "--ticket", tid, "--attempt", first,
        sink=sink_kind, expect=1,
    )
    cli("file", "claim", "src/a.py", "--ticket", tid, "--attempt", resumed, sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_delete_refuses_a_ticket_with_lifecycle_history(cli, tmp_project, sink_kind):
    """C09: --force cannot bypass cleanup or silently cascade change history away."""
    cli("init", sink=sink_kind)
    tid = create(cli, "worked", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    active = cli("delete", tid, "--force", "--agent", "claude.haiku.001", sink=sink_kind, expect=1)
    assert "active attempt" in active.stderr

    cli("release", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    history = cli("delete", tid, "--force", "--agent", "claude.haiku.001", sink=sink_kind, expect=1)
    assert "change history" in history.stderr
    # The ticket still exists: the refusal is not a partial delete.
    cli("show", tid, "--json", sink=sink_kind)


# --- discovery and versioned reads (shared-directory coordination, C06) -----


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_file_list_search_read_probe_surface(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "Read work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.opus.001", sink=sink_kind)
    attempt = active_attempt_id(tmp_project, sink_kind, tid)

    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\nbeta\ngamma\n")
    (tmp_project / "src" / "b.py").write_text("beta in b\n")
    (tmp_project / "src" / "bin.dat").write_bytes(b"\x00\x01\x02")

    # list: bounded discovery, protected metadata excluded and reported.
    listed = json.loads(cli("file", "list", "--json", sink=sink_kind).stdout)
    assert listed["ok"] is True
    paths = [entry["path"] for entry in listed["data"]["entries"]]
    assert "src/a.py" in paths
    assert all(not path.startswith(".arbite") for path in paths)
    assert "protected_paths_excluded" in listed["data"]["markers"]

    # search: text/path discovery with a whole-file version on each content hit.
    searched = json.loads(
        cli("file", "search", "beta", "--json", sink=sink_kind).stdout
    )
    hits = {(match["path"], match["line"]) for match in searched["data"]["matches"]}
    assert ("src/a.py", 2) in hits
    assert searched["data"]["matches"][0]["version"].startswith("sha256:")

    # A read before claiming is evidence, not permission.
    pre = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert pre["write_authorizing"] is False
    assert pre["non_writable_reason"] == "no_own_claim"

    cli(
        "file", "claim", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--json", sink=sink_kind,
    )
    fresh = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--lines", "2:2", "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert fresh["write_authorizing"] is True
    assert fresh["text"] == "beta"
    assert fresh["content_complete"] is False
    assert fresh["line_range_returned"] == [2, 2]
    assert fresh["whole_file_digest"].startswith("sha256:")

    # A foreign read serves the bytes but is non-writable and names the holder.
    other = create(cli, "Other read work", sink=sink_kind)
    cli("claim", other, "--agent", "claude.haiku.001", sink=sink_kind)
    other_attempt = active_attempt_id(tmp_project, sink_kind, other)
    foreign = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", other, "--attempt", other_attempt,
            "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert foreign["busy"] is True
    assert foreign["write_authorizing"] is False
    assert foreign["text"] == "alpha\nbeta\ngamma\n"  # bytes ARE served
    assert foreign["busy_owner"]["holder_ticket"] == tid
    assert foreign["busy_owner"]["holder_attempt"] == attempt

    # fail-if-busy refuses with file_busy instead of spending tokens.
    busy = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", other, "--attempt", other_attempt,
            "--fail-if-busy", "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert busy["ok"] is False
    assert busy["code"] == "file_busy"
    assert busy["details"]["holder_attempt"] == attempt

    # An absent-path probe supports a safe create and owns/records nothing.
    probe = json.loads(
        cli(
            "file", "probe", "src/new.py", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert probe["safe_to_create"] is True
    assert probe["version"] == "<absent>"
    assert probe["claim_acquired"] is False
    assert probe["observation_recorded"] is False

    existing = json.loads(
        cli(
            "file", "probe", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert existing["exists"] is True
    assert existing["safe_to_create"] is False

    # Unsupported file types are explicit, never silently truncated.
    binary = json.loads(
        cli(
            "file", "read", "src/bin.dat", "--ticket", tid, "--attempt", attempt,
            "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert binary["ok"] is False
    assert binary["code"] == "unsupported"
    assert binary["details"]["reason"] == "binary"

    # A malformed line range is a usage error, not a different read.
    cli(
        "file", "read", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--lines", "3:1", sink=sink_kind, expect=2,
    )

    # Discovery uses the same canonical path pipeline: traversal is refused.
    escape = json.loads(
        cli("file", "list", "../", "--json", sink=sink_kind, expect=1).stdout
    )
    assert escape["code"] == "unsupported"


# --- version-checked mutations (shared-directory coordination, C07) ---------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_file_write_and_edit_surface(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "Mutation work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.opus.001", sink=sink_kind)
    attempt = active_attempt_id(tmp_project, sink_kind, tid)

    (tmp_project / "src").mkdir()
    target = tmp_project / "src" / "a.py"
    target.write_text("alpha beta gamma\n")
    cli(
        "file", "claim", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--json", sink=sink_kind,
    )

    def fresh_token(path="src/a.py"):
        return json.loads(
            cli(
                "file", "read", path, "--ticket", tid, "--attempt", attempt,
                "--json", sink=sink_kind,
            ).stdout
        )["data"]["read_token"]

    token = fresh_token()
    payload = tmp_project / "payload.txt"
    payload.write_bytes(b"one two one\n")
    wrote = json.loads(
        cli(
            "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--read-token", token, "--input", str(payload), "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert wrote["applied"] is True
    assert wrote["kind"] == "write"
    assert wrote["version"] == wrote["after"]["src/a.py"]
    assert wrote["version"].startswith("sha256:")
    assert wrote["read_token_invalidated"] is True
    assert target.read_bytes() == b"one two one\n"

    # Reusing the consumed token changes no bytes and reports stale_read.
    stale = json.loads(
        cli(
            "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--read-token", token, "--input", str(payload), "--json",
            sink=sink_kind, expect=1,
        ).stdout
    )
    assert stale["ok"] is False
    assert stale["code"] == "stale_read"
    assert target.read_bytes() == b"one two one\n"

    # A replacement with no token at all is refused too.
    cli(
        "file", "write", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--input", str(payload), sink=sink_kind, expect=1,
    )

    # An exact edit batch applies once and returns receipt/version data.
    edits_file = tmp_project / "edits.json"
    edits_file.write_text(json.dumps({"edits": [{"old": "two", "new": "TWO"}]}))
    edited = json.loads(
        cli(
            "file", "edit", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--read-token", fresh_token(), "--edits", str(edits_file), "--json",
            sink=sink_kind,
        ).stdout
    )["data"]
    assert edited["applied"] is True and edited["kind"] == "edit"
    assert target.read_bytes() == b"one TWO one\n"

    # An ambiguous selection refuses the whole batch: no bytes change.
    ambiguous_file = tmp_project / "ambiguous.json"
    ambiguous_file.write_text(json.dumps({"edits": [{"old": "one", "new": "ONE"}]}))
    ambiguous = json.loads(
        cli(
            "file", "edit", "src/a.py", "--ticket", tid, "--attempt", attempt,
            "--read-token", fresh_token(), "--edits", str(ambiguous_file),
            "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert ambiguous["code"] == "edit_selection"
    assert ambiguous["details"]["reason"] == "ambiguous"
    assert target.read_bytes() == b"one TWO one\n"

    # An absent path is created from its absent-path claim with binary bytes and
    # no read token (a read token for an absent path cannot authorize a create).
    cli(
        "file", "claim", "src/new.py", "--ticket", tid, "--attempt", attempt,
        "--json", sink=sink_kind,
    )
    binary_payload = tmp_project / "payload.bin"
    binary_payload.write_bytes(b"\x00\xffbinary")
    created = json.loads(
        cli(
            "file", "write", "src/new.py", "--ticket", tid, "--attempt", attempt,
            "--input", str(binary_payload), "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert created["applied"] is True
    assert created["before"]["src/new.py"] == "<absent>"
    assert (tmp_project / "src" / "new.py").read_bytes() == b"\x00\xffbinary"


def test_file_write_reads_the_payload_from_stdin(cli, tmp_project):
    cli("init")
    tid = create(cli, "stdin work")
    cli("claim", tid, "--agent", "claude.opus.001")
    attempt = active_attempt_id(tmp_project, "file", tid)
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\n")
    cli("file", "claim", "src/a.py", "--ticket", tid, "--attempt", attempt, "--json")
    token = json.loads(
        cli(
            "file", "read", "src/a.py", "--ticket", tid, "--attempt", attempt, "--json"
        ).stdout
    )["data"]["read_token"]

    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    proc = subprocess.run(
        [
            sys.executable, "-m", "arbite.cli", "file", "write", "src/a.py",
            "--ticket", tid, "--attempt", attempt, "--read-token", token,
            "--input", "-", "--json",
        ],
        cwd=str(tmp_project),
        env=environment,
        input="from stdin\n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["data"]["applied"] is True
    assert (tmp_project / "src" / "a.py").read_bytes() == b"from stdin\n"


# --- file removal / rename surface (shared-directory coordination, C08) -----


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_file_remove_and_rename_surface(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "Remove and rename work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.opus.001", sink=sink_kind)
    attempt = active_attempt_id(tmp_project, sink_kind, tid)
    (tmp_project / "src").mkdir()
    (tmp_project / "src" / "a.py").write_text("alpha\n")
    (tmp_project / "src" / "b.py").write_text("beta\n")

    def read_token(path):
        return json.loads(
            cli(
                "file", "read", path, "--ticket", tid, "--attempt", attempt,
                "--json", sink=sink_kind,
            ).stdout
        )["data"]["read_token"]

    # rename owns BOTH paths, safely creates a missing destination parent, and
    # returns both paths and versions.
    cli(
        "file", "claim", "src/a.py", "--ticket", tid, "--attempt", attempt,
        "--json", sink=sink_kind,
    )
    renamed = json.loads(
        cli(
            "file", "rename", "src/a.py", "deep/moved.py", "--ticket", tid,
            "--attempt", attempt, "--read-token", read_token("src/a.py"),
            "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert renamed["applied"] is True
    assert renamed["paths"] == ["src/a.py", "deep/moved.py"]
    assert renamed["source"] == "src/a.py" and renamed["destination"] == "deep/moved.py"
    assert renamed["created_parents"] == ["deep"]
    assert (tmp_project / "deep" / "moved.py").read_text() == "alpha\n"
    assert not (tmp_project / "src" / "a.py").exists()

    # A destination another attempt owns is file_busy: NEITHER path changes.
    other = create(cli, "Other remove work", sink=sink_kind)
    cli("claim", other, "--agent", "claude.haiku.001", sink=sink_kind)
    other_attempt = active_attempt_id(tmp_project, sink_kind, other)
    cli(
        "file", "claim", "src/b.py", "--ticket", other, "--attempt", other_attempt,
        "--json", sink=sink_kind,
    )
    busy = json.loads(
        cli(
            "file", "rename", "deep/moved.py", "src/b.py", "--ticket", tid,
            "--attempt", attempt, "--read-token", read_token("deep/moved.py"),
            "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert busy["ok"] is False and busy["code"] == "file_busy"
    assert busy["details"]["holder_attempt"] == other_attempt
    assert (tmp_project / "deep" / "moved.py").read_text() == "alpha\n"
    assert (tmp_project / "src" / "b.py").read_text() == "beta\n"

    # Removing a directory is refused explicitly: v1 has no recursive deletion.
    (tmp_project / "src" / "sub").mkdir()
    refused = json.loads(
        cli(
            "file", "remove", "src/sub", "--ticket", tid, "--attempt", attempt,
            "--read-token", "unused-token", "--json", sink=sink_kind, expect=1,
        ).stdout
    )
    assert refused["ok"] is False and refused["code"] == "unsupported"
    assert refused["details"]["reason"] == "recursive_delete_unsupported"
    assert (tmp_project / "src" / "sub").is_dir()

    # remove deletes through the journal, preserves the bytes, and reports absence.
    removed = json.loads(
        cli(
            "file", "remove", "deep/moved.py", "--ticket", tid, "--attempt", attempt,
            "--read-token", read_token("deep/moved.py"), "--json", sink=sink_kind,
        ).stdout
    )["data"]
    assert removed["applied"] is True
    assert removed["after"]["deep/moved.py"] == "<absent>"
    assert not (tmp_project / "deep" / "moved.py").exists()

    # --read-token is mandatory for a removal.
    cli(
        "file", "remove", "src/b.py", "--ticket", tid, "--attempt", attempt,
        sink=sink_kind, expect=2,
    )


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_file_surface_has_no_metadata_or_recursive_operations(cli, tmp_project, sink_kind):
    """Metadata edits and recursive deletion are not implemented, and argparse
    refuses them rather than letting a caller believe a recorded operation ran."""
    cli("init", sink=sink_kind)
    cli("file", "chmod", "src/a.py", sink=sink_kind, expect=2)
    cli("file", "chown", "src/a.py", "root", sink=sink_kind, expect=2)
    cli("file", "remove", "src", "--recursive", sink=sink_kind, expect=2)
