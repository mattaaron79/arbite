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


def run_cli(cwd: Path, *args):
    """Run the CLI from an explicit working directory.

    The `cli` fixture always runs in the project root; the tests that care about
    resolution *below* the root -- "the config is inside .arbite/, which is what
    gets located first" -- need to start somewhere else."""
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    proc = subprocess.run(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"arbite {' '.join(args)} in {cwd} -> exit {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


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
    for name in ("raw", "open", "in_progress", "review", "blocked", "shelved", "closed",
                 "wishlist", "plans"):
        assert (tmp_project / ".arbite" / name).is_dir(), name
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert "folder is the source of truth" in guide
    assert "## Where tickets live (the sink)" in guide


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
    assert (tmp_project / ".arbite" / "project.yaml").read_text().strip() == "sink: sqlite"
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
    assert not (tmp_project / ".arbite" / "project.yaml").exists()


def test_setting_a_sink_preserves_the_rest_of_the_config(cli, tmp_project):
    """The config is also hand-maintained, so only the `sink:` line is touched."""
    (tmp_project / ".arbite").mkdir()
    (tmp_project / ".arbite" / "project.yaml").write_text(
        "# hand-maintained\n"
        "agents: [claude.haiku.001]\n"
        "sink: file\n"
        "sinks:\n"
        "  sqlite:\n"
        "    path: .arbite/other.db\n"
    )
    cli("init", "--sink", "sqlite")
    text = (tmp_project / ".arbite" / "project.yaml").read_text()
    assert "# hand-maintained" in text
    assert "agents: [claude.haiku.001]" in text
    assert "path: .arbite/other.db" in text
    assert text.count("sink: sqlite") == 1, text
    assert "sink: file" not in text, "the old selection is overwritten, not duplicated"


def test_an_environment_choice_is_not_written_to_the_config(cli, tmp_project):
    """ARBITE_SINK is one process's decision; committed config is the project's."""
    output = cli("init", sink="sqlite").stdout
    assert not (tmp_project / ".arbite" / "project.yaml").exists()
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
    (tmp_project / ".arbite" / "project.yaml").unlink()  # the selection goes; the tickets stay

    cli("init")  # re-renders the guide from the sink plain commands will read
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert "holds a second ticket store that nothing selects" in guide
    assert "1 ticket(s) in a `sqlite` store" in guide
    assert "also present, but not selected: `sqlite`" in guide
    assert "check its `kind` field" in guide
    # ...and the behaviour prose describes what a plain command really does.
    assert "folder is the source of truth" in guide

    # Point the project at the database and both the warning and the file-shaped
    # prose go away.
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: sqlite\n")
    cli("init")
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()
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
    (tmp_project / ".arbite" / "project.yaml").unlink()

    proc = cli("list", expect=2)
    assert "exists but no sink is configured" in proc.stderr
    assert "--sink sqlite" in proc.stderr

    # An explicit choice, by flag or environment, is a decision -- never second-guessed.
    assert "no sink is configured" not in cli("list", "--sink", "sqlite", expect=2).stderr
    assert "no sink is configured" not in cli("list", expect=2, sink="file").stderr
    # And once the config names a sink, there is nothing ambiguous left.
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: file\n")
    assert "no sink is configured" not in cli("list", expect=2).stderr


def test_migrate_names_the_source_when_it_is_already_the_active_sink(cli, tmp_project):
    cli("init")
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: sqlite\n")
    cli("sink", "init")
    proc = cli("migrate", "--to", "sqlite", expect=1)
    assert "--from file" in proc.stderr
    # Following that instruction works: the file store is empty here, which is an
    # answer (exit 2), not an error.
    followed = cli("migrate", "--from", "file", "--to", "sqlite", expect=2)
    assert "no tickets found in the file sink" in followed.stdout


def test_sink_can_be_selected_by_flag_before_or_after_the_command(cli, tmp_project):
    cli("init", "--sink", "sqlite")
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: sqlite\n")
    assert json.loads(cli("sink", "info", "--json").stdout)["kind"] == "sqlite"
    assert json.loads(cli("--sink", "sqlite", "sink", "info", "--json").stdout)["kind"] == "sqlite"
    assert json.loads(cli("sink", "info", "--sink", "sqlite", "--json").stdout)["kind"] == "sqlite"
    # ARBITE_SINK outranks the config file
    assert json.loads(cli("sink", "info", "--json", sink="file").stdout)["kind"] == "file"


def test_a_configured_location_is_honoured(cli, tmp_project):
    (tmp_project / ".arbite").mkdir()
    (tmp_project / ".arbite" / "project.yaml").write_text(
        "sink: sqlite\nsinks:\n  sqlite:\n    path: .arbite/custom.sqlite\n"
    )
    cli("init")
    assert (tmp_project / ".arbite" / "custom.sqlite").is_file()
    assert json.loads(cli("sink", "info", "--json").stdout)["root"].endswith("custom.sqlite")


def test_the_config_resolves_from_any_subdirectory(cli, tmp_project):
    """The config lives *inside* the directory it is found by: resolution locates
    `.arbite/` first and only then reads `project.yaml` in it, so the sink, the
    agents list and per-sink locations all resolve from below the project root."""
    database = tmp_project / ".arbite" / "custom.sqlite"
    (tmp_project / ".arbite").mkdir()
    (tmp_project / ".arbite" / "project.yaml").write_text(
        "sink: sqlite\n"
        "agents: [claude.haiku.001]\n"
        "sinks:\n"
        "  sqlite:\n"
        f"    path: {database}\n"
    )
    cli("init")
    assert database.is_file()
    assert not (tmp_project / ".arbite" / "arbite.db").exists(), "the location was read"
    assert (tmp_project / ".arbite" / "agents" / "claude.haiku.001.md").is_file()

    subdir = tmp_project / "nested" / "deeper"
    subdir.mkdir(parents=True)
    info = json.loads(run_cli(subdir, "sink", "info", "--json").stdout)
    assert info["kind"] == "sqlite", "the configured sink is found from a subdirectory"
    assert info["root"].endswith("custom.sqlite"), "so is its configured location"


def test_the_old_root_level_config_is_ignored(cli, tmp_project):
    """The hard cut: a repo-root `arbite.yaml` or `.arbite.yaml` is not consulted at
    all -- no fallback, no deprecation warning, no migration -- so a project that only
    has one behaves exactly as if it had no config."""
    (tmp_project / "arbite.yaml").write_text("sink: sqlite\n")
    (tmp_project / ".arbite.yaml").write_text("sink: sqlite\n")

    cli("init")

    # The default (file) sink was used, so those files changed nothing...
    assert (tmp_project / ".arbite" / "open").is_dir()
    assert not (tmp_project / ".arbite" / "arbite.db").exists()
    assert not (tmp_project / ".arbite" / "project.yaml").exists()
    # ...and they were left exactly where they were, untouched and unmigrated.
    assert (tmp_project / "arbite.yaml").read_text() == "sink: sqlite\n"
    assert (tmp_project / ".arbite.yaml").read_text() == "sink: sqlite\n"


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


def test_fetch_never_re_serves_a_snapshot_of_a_promoted_request(project, cli):
    """`raw/processed/<id>.raw.md` is the audit copy of a request that has already
    been promoted, kept verbatim -- raw frontmatter and all. It is skipped by path,
    so `fetch` never hands the same request back for a second classification."""
    tid = ticket_id(cli("raw", "feature", "a thing").stdout)
    original = (project / ".arbite" / "raw" / f"{tid}.md").read_text()

    cli("set", tid, "title", "a thing", "tier", "medium", "domain", "mesh", "status", "open")
    snapshot = project / ".arbite" / "raw" / "processed" / f"{tid}.raw.md"
    snapshot.write_text(original)
    assert snapshot.exists()

    # The live ticket is the only one: the snapshot is not a second copy of it.
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "open"
    assert len([line for line in cli("list").stdout.splitlines() if tid in line]) == 1
    # Nothing is left to classify, so the triage queue is empty (exit code 2)...
    cli("list", "raw", expect=2)
    cli("fetch", expect=2)
    assert cli("fetch", "--json", expect=2).stdout.strip() == "null"
    # ...and the snapshot is not a stray, misfiled or duplicated file either.
    cli("doctor")


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
    cli("block", tid, "--reason", "waiting on upstream", sink=sink_kind)
    cli("unblock", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    cli("release", tid, "--agent", "claude.haiku.001", "--reason", "wrong tier", sink=sink_kind)
    cli("shelve", tid, "--reason", "later", sink=sink_kind)
    cli("unshelve", tid, "--reason", "back", sink=sink_kind)
    cli("close", tid, sink=sink_kind)
    cli("reopen", tid, "--agent", "claude.haiku.001", "--reason", "not done after all",
        sink=sink_kind)
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "open"
    assert "found it" in payload["body"]
    assert "Reopened: not done after all." in payload["body"]
    assert [n["kind"] for n in json.loads(cli("doctor", "--json", sink=sink_kind).stdout)["problems"]] == []


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_reopen_requires_a_reason_and_records_it(cli, tmp_project, sink_kind):
    """`reopen` is the rejection path out of review, so the reason is mandatory:
    argparse refuses a bare `reopen` (exit 2) before any command logic runs, leaving
    the ticket exactly where it was, and the reason given becomes the automatic note
    `Reopened: <reason>.` -- with `closed`/`blocked_by` cleared and the ticket moved
    back out of the `closed/YYYY-MM` archive."""
    cli("init", sink=sink_kind)
    tid = create(cli, "Worked ticket", priority=1, sink=sink_kind)
    cli("block", tid, "--reason", "waiting on upstream", sink=sink_kind)
    cli("close", tid, sink=sink_kind)
    archive = tmp_project / ".arbite" / "closed"
    if sink_kind == "file":
        assert list(archive.glob(f"*/{tid}.md")), "close archives by close month"

    # A bare reopen is refused by argparse, so the ticket is untouched.
    proc = cli("reopen", tid, sink=sink_kind, expect=2)
    assert "--reason" in proc.stderr and "required" in proc.stderr
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "closed"
    assert payload["closed"]
    assert payload["blocked_by"] == "waiting on upstream"
    if sink_kind == "file":
        assert list(archive.glob(f"*/{tid}.md")), "it is still archived"

    # Given the reason it reopens: open again, fields cleared, reason recorded.
    cli("reopen", tid, "--agent", "claude.opus.001", "--reason", "tests fail on ARM",
        sink=sink_kind)
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "open"
    assert payload["closed"] is None
    assert payload["blocked_by"] is None
    assert "Reopened: tests fail on ARM." in payload["body"]
    assert "claude.opus.001" in payload["body"]
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "open" / f"{tid}.md").exists()
        assert not list(archive.glob(f"*/{tid}.md")), "it left the closed archive"
    cli("doctor", sink=sink_kind)

    # Already open: still an error -- and the reason is still demanded first, by
    # argparse, before the command gets to look at the ticket.
    proc = cli("reopen", tid, sink=sink_kind, expect=2)
    assert "--reason" in proc.stderr
    proc = cli("reopen", tid, "--reason", "reopen it again", sink=sink_kind, expect=1)
    assert "already open" in proc.stderr
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "open"


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
    assert payload["status"] == "in_progress"  # a takeover does not re-open the ticket
    assert "Claim taken over from claude.haiku.001" in payload["body"]


def test_claim_sets_in_progress_and_files_the_ticket_under_in_progress(project, cli):
    """`claim` is the entry point of the review workflow (claim -> in_progress ->
    submit -> review -> accept), so that transition is a contract rather than an
    incidental side effect: claiming sets `status: in_progress` and the assignee,
    and the file sink files the ticket under `in_progress/` -- the caller never
    needs a separate `set status` after claiming. Pinned here head-on for the file
    sink, including the `--force` takeover and the refused race. The sink-agnostic
    half (status and assignee, both sinks) is in
    `test_the_lifecycle_behaves_the_same_in_every_sink`; the compare-and-swap
    primitive in the conformance suite."""
    tid = create(cli, "Worked ticket")
    open_path = project / ".arbite" / "open" / f"{tid}.md"
    in_progress_path = project / ".arbite" / "in_progress" / f"{tid}.md"
    assert open_path.exists()

    cli("claim", tid, "--agent", "claude.haiku.001")
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["status"] == "in_progress"
    assert payload["assignee"] == "claude.haiku.001"
    assert in_progress_path.exists(), "the claim moved the ticket into in_progress/"
    assert not open_path.exists(), "it did not leave a copy behind in open/"
    cli("doctor")  # one ticket, one location: a move, not a duplicate

    # A claim that loses the race is refused, and the winner's ticket is untouched.
    proc = cli("claim", tid, "--agent", "claude.opus.001", expect=1)
    assert "already assigned to claude.haiku.001" in proc.stderr
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["assignee"] == "claude.haiku.001"
    assert payload["status"] == "in_progress"
    assert in_progress_path.exists()
    assert not open_path.exists()

    # --force takes over: the assignee changes, the status stays in_progress (a
    # takeover is not a re-open), the ticket stays in in_progress/, and the
    # takeover is recorded as a note.
    cli("claim", tid, "--agent", "claude.opus.001", "--force")
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["assignee"] == "claude.opus.001"
    assert payload["status"] == "in_progress"
    assert "Claim taken over from claude.haiku.001" in payload["body"]
    assert in_progress_path.exists()
    assert not open_path.exists()
    cli("doctor")


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


def test_init_creates_the_plans_bucket_and_move_files_there(project, cli):
    """`plans` is the default bucket (hard rename, no alias): `init` creates it and
    `arbite move <id> /plans` files a ticket inside it without changing a field."""
    assert (project / ".arbite" / "plans").is_dir()
    assert not (project / ".arbite" / "planning").exists()
    tid = create(cli, "a plan")
    cli("move", tid, "/plans")
    assert (project / ".arbite" / "plans" / f"{tid}.md").exists()
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "open"
    cli("list", expect=2)  # bucketed, so out of the status listings
    cli("move", tid, "/")
    assert (project / ".arbite" / "open" / f"{tid}.md").exists()


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
    one, write it to the other, and compare the text form byte for byte. The open
    ticket carries references so the field's storage on either side is part of the
    byte-for-byte comparison."""
    open_ticket = create(
        cli, "An open ticket", priority=2, tags="a,b", references="plans/a.md,plans/b.md"
    )
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
    assert json.loads(cli("show", open_ticket, "--json", sink="sqlite").stdout)["references"] == [
        "plans/a.md",
        "plans/b.md",
    ]

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
    assert not (project / ".arbite" / "project.yaml").exists()

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
    assert (project / ".arbite" / "project.yaml").read_text().strip() == "sink: sqlite"
    assert json.loads(cli("show", open_ticket, "--json").stdout)["title"] == (
        "moving to the database"
    )
    assert json.loads(cli("show", wish, "--json").stdout)["id"] == wish
    cli("doctor")


def test_migrate_makes_the_destination_the_default(project, cli):
    tid = create(cli, "moving")
    assert not (project / ".arbite" / "project.yaml").exists()

    out = cli("migrate", "--to", "sqlite").stdout
    assert "set 'sink: sqlite'" in out
    assert (project / ".arbite" / "project.yaml").read_text().strip() == "sink: sqlite"
    assert json.loads(cli("show", tid, "--json").stdout)["path"].startswith("sqlite:")

    # ...and a dry run writes nothing: it does not know yet whether you will go
    # through with it.
    (project / ".arbite" / "project.yaml").unlink()
    cli("migrate", "--from", "sqlite", "--to", "file", "--dry-run")
    assert not (project / ".arbite" / "project.yaml").exists()


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


# --- the status vocabulary --------------------------------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_set_status_review_is_a_first_class_state(cli, tmp_project, sink_kind):
    """No command *sets* `review` by default, so generic `set <id> status review`
    is how it is exercised. From there it must round-trip through the sink,
    validate under `doctor`, and be selectable with `list --status review` -- and
    be treated as not-yet-workable, so `list next` never offers it. The same flow
    must hold on both sinks."""
    cli("init", sink=sink_kind)
    tid = ticket_id(
        cli(
            "create", "--title", "finished, awaiting review", "--type", "bug",
            "--tier", "medium", "--domain", "mesh", sink=sink_kind,
        ).stdout
    )
    cli("set", tid, "status", "review", sink=sink_kind)

    listed = json.loads(cli("list", "--status", "review", "--json", sink=sink_kind).stdout)
    assert [row["id"] for row in listed] == [tid]
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "review"
    cli("doctor", sink=sink_kind)  # exit 0: a review ticket is not a problem
    # `review` is deliberately not workable, so the next-work queue is empty.
    assert json.loads(cli("list", "next", "--json", sink=sink_kind, expect=2).stdout) == []


# --- the references field ---------------------------------------------------


def test_create_sets_references_and_show_reports_them(project, cli):
    tid = create(cli, "referenced", references="plans/review-workflow.md")
    assert json.loads(cli("show", tid, "--json").stdout)["references"] == [
        "plans/review-workflow.md"
    ]


def test_create_rejects_an_invalid_reference(project, cli):
    proc = cli(
        "create", "--title", "bad", "--type", "bug", "--tier", "medium", "--domain", "mesh",
        "--references", "/etc/passwd", expect=1,
    )
    assert "references" in proc.stderr


def test_set_references_works_validates_and_can_be_cleared(project, cli):
    tid = create(cli, "a ticket")
    cli("set", tid, "references", "plans/x.md,plans/y.md")
    assert json.loads(cli("show", tid, "--json").stdout)["references"] == [
        "plans/x.md",
        "plans/y.md",
    ]
    proc = cli("set", tid, "references", "plans/../escape.md", expect=1)
    assert "references" in proc.stderr
    cli("set", tid, "references", "")
    assert json.loads(cli("show", tid, "--json").stdout)["references"] == []


def test_a_ticket_without_references_has_no_references_line(project, cli):
    tid = create(cli, "plain")
    path = project / ".arbite" / "open" / f"{tid}.md"
    assert "references" not in path.read_text()
    assert json.loads(cli("show", tid, "--json").stdout)["references"] == []
    cli("doctor")  # exit 0: no stray field, nothing to report


def test_show_is_identical_from_the_file_and_sqlite_sinks(project, cli):
    """`arbite show` renders the same text whichever sink holds the ticket, and
    references survive a file -> sqlite migration order-preserved."""
    tid = create(cli, "with references", references="plans/b.md,plans/a.md")
    from_file = cli("show", tid, sink="file").stdout
    assert "references:\n- plans/b.md\n- plans/a.md\n" in from_file

    cli("migrate", "--to", "sqlite")
    from_sqlite = cli("show", tid, sink="sqlite").stdout
    assert from_sqlite == from_file
    assert json.loads(cli("show", tid, "--json", sink="sqlite").stdout)["references"] == [
        "plans/b.md",
        "plans/a.md",
    ]
    cli("doctor", sink="sqlite")  # exit 0


def test_doctor_is_clean_with_a_referenced_ticket_on_both_sinks(cli, tmp_project):
    cli("init", sink="file")
    create(cli, "referenced", references="plans/a.md,plans/b.md")
    cli("doctor", sink="file")
    cli("migrate", "--to", "sqlite")
    cli("doctor", sink="sqlite")
