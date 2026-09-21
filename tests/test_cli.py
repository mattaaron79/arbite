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

from arbite import cli as arbite_cli
from arbite import docs, schema
from arbite.errors import Conflict
from arbite.sinks import Expect, SinkSpec, build_sink

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


def raw_capture(cli, raw_type="feature", message="add per-mesh LOD", sink=None) -> str:
    """`arbite raw <type> <message>`, returning the id of the capture it minted."""
    return ticket_id(cli("raw", raw_type, message, sink=sink).stdout)


def snapshot_path(project: Path, tid: str) -> Path:
    """Where `arbite promote` freezes a capture: `<arbite_dir>/raw/processed/<id>.raw.md`."""
    return project / ".arbite" / "raw" / "processed" / f"{tid}.raw.md"


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


# --- the generated guide, and the docs it is rendered from -----------------


def _without_generated_stamp(guide: str) -> str:
    """The guide's header carries the date it was rendered, which must not make an
    otherwise-identical regeneration look like a change."""
    return "\n".join(
        line for line in guide.splitlines() if not line.startswith("_Auto-generated by")
    )


def test_the_guide_names_only_the_current_vocabulary_and_commands(cli, tmp_project):
    """The guide is rendered from the installed parsers and the schema constants, so
    it may only ever name what the commands actually accept. This pins that
    mechanically: the status vocabulary (and every member of it) comes from
    `schema.STATUSES`, the command names come from the parser, and the two names the
    docs sweep retired -- `arbite.yaml` as the live config file, and the `planning`
    bucket -- must not reappear in any form."""
    cli("init")
    guide = (tmp_project / ".arbite" / "AGENTS.md").read_text()

    assert " | ".join(schema.STATUSES) in guide
    for status in schema.STATUSES:
        assert status in guide, status

    _, subparsers_by_name = arbite_cli.build_parser()
    for name in subparsers_by_name:
        assert f"`{name}`" in guide, f"command not documented in the guide: {name}"

    # The parts of the current model the sweep was about: the review chain, triage,
    # the reporting commands, the field and the folder layout.
    for fragment in (
        "`submit`", "`accept`", "`promote`", "`set-status`", "`status`", "`progress`",
        "`references`", "review/", "plans/", "raw/processed/", ".arbite/project.yaml",
        "claim",
    ):
        assert fragment in guide, fragment

    assert "arbite.yaml" not in guide
    assert "planning" not in guide


def test_the_guide_is_rewritten_identically_when_nothing_changed(cli, tmp_project):
    """`init` rewrites the guide on every run, so re-running it must be a no-op: a
    committed guide should only ever show up as a diff when something real changed."""
    cli("init")
    first = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    cli("init")
    second = (tmp_project / ".arbite" / "AGENTS.md").read_text()
    assert _without_generated_stamp(second) == _without_generated_stamp(first)


def test_the_installed_instructions_block_matches_the_example():
    """`init --agents-doc`/`--claude-doc` install a block carried in the package, and
    `docs.py` promises `AGENTS_EXAMPLE.md` holds it byte-for-byte. This is the check
    that keeps that promise, and it names the review chain -- so a block that lost
    `submit`/`accept` fails here instead of in a project that never learns them."""
    example = (REPO_ROOT / "AGENTS_EXAMPLE.md").read_text()
    assert example.rstrip("\n") == docs.ARBITE_INSTRUCTIONS_BLOCK.rstrip("\n")
    for fragment in (
        "submit", "review", "accept", "reopen", "promote", "in_progress",
        "plans/", "raw/processed/", ".arbite/project.yaml",
    ):
        assert fragment in docs.ARBITE_INSTRUCTIONS_BLOCK, fragment


def test_the_readme_documents_every_command_and_the_new_model():
    """The README is the long-form half of the same sweep: every command in
    `arbite --help` must be documented here, the status vocabulary must be the
    schema's, and both breaking changes must be called out.

    Unlike the guide, the README deliberately *records* the old `arbite.yaml` in its
    upgrade note, so this checks the live names it carries rather than asserting the
    retired string is absent -- the strict absence guard belongs on the guide."""
    readme = (REPO_ROOT / "README.md").read_text()
    _, subparsers_by_name = arbite_cli.build_parser()
    for name in subparsers_by_name:
        assert f"`{name}`" in readme, f"command not documented in the README: {name}"
    assert " | ".join(schema.STATUSES) in readme
    assert ".arbite/project.yaml" in readme
    assert "## Breaking changes when upgrading" in readme
    assert "`arbite reopen` now requires `--reason`" in readme
    assert "| `in_progress` | `submit` | `review`" in readme


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


def test_review_false_does_not_stop_init_creating_the_review_folder(cli, tmp_project):
    """The `review:` flag gates a future `submit`, not the layout: a project that
    turns review off must not strand tickets already awaiting review, so `init`
    still creates the `review/` status folder."""
    (tmp_project / ".arbite").mkdir()
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: file\nreview: false\n")
    cli("init")
    assert (tmp_project / ".arbite" / "review").is_dir()


def test_a_garbage_review_key_is_a_config_error(cli, tmp_project):
    """A present-but-unusable `review:` is reported the way any malformed config is
    -- naming the file and the key -- rather than defaulting silently to false."""
    (tmp_project / ".arbite").mkdir()
    (tmp_project / ".arbite" / "project.yaml").write_text("sink: file\nreview: maybe\n")

    proc = cli("list", expect=1)
    assert "error:" in proc.stderr
    assert ".arbite/project.yaml" in proc.stderr
    assert "review" in proc.stderr


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


# --- promote: the write half of triage ------------------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_promote_classifies_a_raw_capture_in_place(cli, tmp_project, sink_kind):
    """The happy path: one command turns a raw capture into a fully classified `open`
    ticket at the *same id*, so anything that already referenced the raw request stays
    valid -- `created` and the notes survive. The ticket leaves the triage queue without
    ever having been offered by `list next` wearing a placeholder."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="add per-mesh LOD", sink=sink_kind)
    cli("note", tid, "claude.haiku.001", "saw this while tuning the importer", sink=sink_kind)
    before = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)

    out = cli(
        "promote", tid,
        "--title", "Add per-mesh LOD", "--tier", "high", "--domain", "mesh",
        "--epic", "mesh-pipeline", "--priority", "1", "--tags", "lod,mesh",
        "--description", "Add a per-mesh LOD ladder to the mesh importer.",
        sink=sink_kind,
    ).stdout
    assert tid in out
    assert str(snapshot_path(tmp_project, tid)) in out, "the receipt names the snapshot"

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["id"] == tid
    assert payload["status"] == "open"
    assert payload["title"] == "Add per-mesh LOD"
    assert (payload["tier"], payload["domain"]) == ("high", "mesh")
    assert payload["epic"] == "mesh-pipeline"
    assert payload["priority"] == 1
    assert payload["tags"] == ["lod", "mesh"]
    assert payload["type"] == "feature", "the capture's own type carries over"
    # The same ticket, not a replacement: its id, creation date and notes all survive.
    assert payload["created"] == before["created"]
    assert "saw this while tuning the importer" in payload["body"]
    assert "Original request: add per-mesh LOD" in payload["body"]
    assert "Add a per-mesh LOD ladder to the mesh importer." in payload["body"]

    # Out of the triage queue, into the work queue, and nothing for doctor to report.
    cli("list", "raw", sink=sink_kind, expect=2)
    cli("fetch", sink=sink_kind, expect=2)
    listed = [line for line in cli("list", sink=sink_kind).stdout.splitlines() if tid in line]
    assert len(listed) == 1 and "open" in listed[0]
    cli("doctor", sink=sink_kind)
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "open" / f"{tid}.md").exists()
        assert not (tmp_project / ".arbite" / "raw" / f"{tid}.md").exists()


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_the_promote_snapshot_is_verbatim_and_invisible(cli, tmp_project, sink_kind):
    """`promote` freezes the capture at `raw/processed/<id>.raw.md`, byte-identical to
    the raw ticket's rendered text and taken *before* the rewrite. It is audit history
    rather than a ticket -- it keeps the original `status: raw` frontmatter -- so it must
    stay invisible to `list`, `list raw`, `fetch`, `doctor` and the id index, and `show
    <id>` must show the promoted ticket, not the snapshot."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="add per-mesh LOD", sink=sink_kind)
    original = cli("show", tid, sink=sink_kind).stdout

    cli("promote", tid, "--title", "Add per-mesh LOD", "--tier", "high", "--domain", "mesh",
        sink=sink_kind)

    snapshot = snapshot_path(tmp_project, tid)
    assert snapshot.exists()
    assert snapshot.read_text() == original, "the snapshot is the capture, verbatim"
    assert "status: raw" in snapshot.read_text(), "a snapshot keeps its raw frontmatter"
    assert snapshot.name != f"{tid}.md", "it can never look like a second copy of the ticket"

    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "open"
    cli("list", "raw", sink=sink_kind, expect=2)
    cli("fetch", sink=sink_kind, expect=2)
    listed = [line for line in cli("list", sink=sink_kind).stdout.splitlines() if tid in line]
    assert len(listed) == 1 and "open" in listed[0]
    cli("doctor", sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_promote_with_agent_claims_in_the_same_command(cli, tmp_project, sink_kind):
    """`--agent` is the one-step path `fetch`'s `derived_note` describes: classify and
    claim together, landing at `in_progress` with the assignee rather than open."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, raw_type="bug", message="the thing broke", sink=sink_kind)

    cli("promote", tid, "--title", "Fix the broken thing", "--tier", "medium",
        "--domain", "io", "--agent", "claude.haiku.001", sink=sink_kind)

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "in_progress"
    assert payload["assignee"] == "claude.haiku.001"
    assert payload["epic"] is None, "the triage grouping is cleared either way"
    assert snapshot_path(tmp_project, tid).exists()
    cli("list", "raw", sink=sink_kind, expect=2)
    cli("doctor", sink=sink_kind)
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "in_progress" / f"{tid}.md").exists()


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_promote_routes_a_wish_to_the_wishlist_bucket(cli, tmp_project, sink_kind):
    """The documented wish rule, routed through promote: retyped to `feature`, classified,
    and filed in the wishlist bucket with its status left at `raw` -- so it leaves the
    triage queue (bucketed tickets are out of the status workflow) without becoming work."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, raw_type="wish", message="fly-through camera preview", sink=sink_kind)
    original = cli("show", tid, sink=sink_kind).stdout

    out = cli("promote", tid, "--title", "Camera fly-through preview", "--tier", "low",
              "--domain", "ui", "--description", "Preview a camera fly-through.",
              sink=sink_kind).stdout
    assert "wishlist" in out

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["type"] == "feature", "a wish is reclassified as feature"
    assert payload["status"] == "raw", "a wish is filed, never opened"
    assert payload["title"] == "Camera fly-through preview"
    assert payload["epic"] is None
    assert snapshot_path(tmp_project, tid).read_text() == original
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "wishlist" / f"{tid}.md").exists()

    cli("list", "raw", sink=sink_kind, expect=2)
    cli("fetch", sink=sink_kind, expect=2)
    # A bucketed ticket is out of the status workflow entirely, not just the raw queue.
    cli("list", sink=sink_kind, expect=2)
    cli("doctor", sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_promote_refuses_agent_for_a_wish(cli, tmp_project, sink_kind):
    """Claiming a wishlist item is meaningless, so an explicit `--agent` is refused
    rather than silently ignored -- and the refusal happens before anything is written."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, raw_type="wish", message="a preview mode", sink=sink_kind)

    proc = cli("promote", tid, "--title", "Preview mode", "--tier", "low", "--domain", "ui",
               "--agent", "claude.haiku.001", sink=sink_kind, expect=1)
    assert "--agent" in proc.stderr and "wish" in proc.stderr

    assert not snapshot_path(tmp_project, tid).exists()
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert (payload["status"], payload["type"]) == ("raw", "wish")
    cli("fetch", sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_promote_refuses_placeholders_naming_every_offending_field(cli, tmp_project, sink_kind):
    """A promoted ticket must never reach `list next` with placeholder fields, so the
    required classification is demanded (the same level `arbite create` demands), a value
    that is still a placeholder is refused by name, and a typo'd tier is caught by the
    validator `set` uses -- all before anything is written."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="a thing", sink=sink_kind)
    snapshot = snapshot_path(tmp_project, tid)

    missing = cli("promote", tid, sink=sink_kind, expect=1)
    for flag in ("--title", "--tier", "--domain"):
        assert flag in missing.stderr

    todo = cli("promote", tid, "--title", schema.BLANK_TITLE, "--tier", schema.BLANK_TIER,
               "--domain", schema.BLANK_DOMAIN, sink=sink_kind, expect=1)
    for flag in ("--title", "--tier", "--domain"):
        assert flag in todo.stderr

    # The title `arbite raw` wrote is a placeholder too, however it is spelled.
    raw_title = cli("promote", tid, "--title", schema.RAW_TITLE_FORMAT.format(type="feature"),
                    "--tier", "low", "--domain", "ui", sink=sink_kind, expect=1)
    assert "--title" in raw_title.stderr

    bad_tier = cli("promote", tid, "--title", "A real title", "--tier", "hgih",
                   "--domain", "ui", sink=sink_kind, expect=1)
    assert "invalid tier" in bad_tier.stderr

    # Nothing was written: no snapshot, and the capture is untouched.
    assert not snapshot.exists()
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "raw"
    assert payload["title"] == schema.RAW_TITLE_FORMAT.format(type="feature")
    assert payload["tier"] == schema.BLANK_TIER
    cli("fetch", sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_a_second_promotion_refuses_and_never_rewrites_the_snapshot(cli, tmp_project, sink_kind):
    """A snapshot is frozen, so promoting the same id again errors instead of
    overwriting it -- whether the ticket was opened (status guard) or is a wish that is
    still `raw` (snapshot guard). A classified ticket is refused outright, so promotion
    is never a silent reclassification of work that has moved on."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="a thing", sink=sink_kind)
    cli("promote", tid, "--title", "A thing", "--tier", "medium", "--domain", "mesh",
        sink=sink_kind)
    snapshot = snapshot_path(tmp_project, tid)
    frozen = snapshot.read_bytes()

    again = cli("promote", tid, "--title", "Another thing", "--tier", "low", "--domain", "ui",
                sink=sink_kind, expect=1)
    assert "not a raw capture" in again.stderr
    assert snapshot.read_bytes() == frozen
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert (payload["title"], payload["status"]) == ("A thing", "open")

    # A wish stays `raw`, so here it is the snapshot guard that refuses.
    wid = raw_capture(cli, raw_type="wish", message="a preview", sink=sink_kind)
    cli("promote", wid, "--title", "Preview", "--tier", "low", "--domain", "ui", sink=sink_kind)
    wish_snapshot = snapshot_path(tmp_project, wid)
    frozen_wish = wish_snapshot.read_bytes()
    repromote = cli("promote", wid, "--title", "Preview again", "--tier", "low",
                    "--domain", "ui", sink=sink_kind, expect=1)
    assert str(wish_snapshot) in repromote.stderr
    assert "frozen audit history" in repromote.stderr
    assert wish_snapshot.read_bytes() == frozen_wish

    # And a ticket that was never raw cannot be promoted at all.
    ordinary = create(cli, "Ordinary ticket", sink=sink_kind)
    not_raw = cli("promote", ordinary, "--title", "nope", "--tier", "low", "--domain", "ui",
                  sink=sink_kind, expect=1)
    assert "not a raw capture" in not_raw.stderr
    assert not snapshot_path(tmp_project, ordinary).exists()


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_an_omitted_epic_clears_the_classification_grouping(cli, tmp_project, sink_kind):
    """Raw captures are filed under the `classification` epic *for triage*; a promoted
    ticket must not keep that label, but a real epic set by hand is not a triage grouping
    and survives an omitted `--epic`."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="a thing", sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["epic"] == (
        schema.CLASSIFICATION_EPIC
    )
    cli("promote", tid, "--title", "A thing", "--tier", "medium", "--domain", "mesh",
        sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["epic"] is None

    other = raw_capture(cli, message="another thing", sink=sink_kind)
    cli("set", other, "epic", "mesh-pipeline", sink=sink_kind)
    cli("promote", other, "--title", "Another thing", "--tier", "medium", "--domain", "mesh",
        sink=sink_kind)
    assert json.loads(cli("show", other, "--json", sink=sink_kind).stdout)["epic"] == (
        "mesh-pipeline"
    )


class _StaleExpectSink:
    """A real sink whose `update` is called with a deliberately stale `Expect`.

    The CAS is the sink's own, so the write fails exactly as it would if another agent
    had changed the ticket between the read and the write -- which is the only way to
    test a crash/failure *between* promote's two writes without racing real processes."""

    def __init__(self, sink):
        self._sink = sink

    def __getattr__(self, name):
        return getattr(self._sink, name)

    def update(self, ticket, expect=None):
        return self._sink.update(ticket, expect=Expect(status="closed", assignee="nobody"))


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_the_snapshot_is_written_before_the_ticket_is_mutated(
    cli, tmp_project, monkeypatch, sink_kind
):
    """Snapshot first, then mutate. The mutation is forced to fail (a stale compare-and-swap
    token, as if the ticket changed underneath), and the ordering is observable in what is
    left behind: the frozen capture exists, while the ticket is still a raw, unclassified
    capture in the triage queue."""
    cli("init", sink=sink_kind)
    tid = raw_capture(cli, message="a thing", sink=sink_kind)
    original = cli("show", tid, sink=sink_kind).stdout

    arbite_dir = tmp_project / ".arbite"
    real_sink = build_sink(SinkSpec(kind=sink_kind), arbite_dir)
    monkeypatch.setattr(arbite_cli, "_require_sink", lambda args: _StaleExpectSink(real_sink))
    parser, _ = arbite_cli.build_parser()
    args = parser.parse_args(
        ["promote", tid, "--title", "A thing", "--tier", "medium", "--domain", "mesh"]
    )
    with pytest.raises(Conflict):
        arbite_cli.cmd_promote(args)

    # The snapshot was already on disk when the write was attempted...
    assert snapshot_path(tmp_project, tid).read_text() == original
    # ...and the ticket is untouched: still raw, still unclassified, still in the queue.
    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "raw"
    assert payload["title"] == schema.RAW_TITLE_FORMAT.format(type="feature")
    assert payload["tier"] == schema.BLANK_TIER
    cli("list", "raw", sink=sink_kind)
    cli("fetch", sink=sink_kind)


def test_fetch_and_raw_describe_promote_as_the_write_half(project, cli):
    """The triage guidance is generated from this code, so the drift guard lives here:
    `fetch`'s derived_note names `arbite promote` (and the wishlist routing for a wish),
    which is what makes the read-only queue and the write half a single documented flow."""
    cli("raw", "feature", "a thing")
    note = json.loads(cli("fetch", "--json").stdout)["derived_note"]
    assert "arbite promote" in note

    cli("raw", "wish", "a wishlist item")
    wish_note = json.loads(cli("fetch", "wish", "--json").stdout)["derived_note"]
    assert "arbite promote" in wish_note and "wishlist" in wish_note

    assert "promote" in cli("raw", "--help").stdout + cli("fetch", "--help").stdout


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
    """Losing a race is refused with the holder's attempt named (CL3); `--force` is the
    administrative takeover and therefore takes a reason, because that reason is the
    only record of why the previous worker lost the ticket."""
    tid = create(cli, "contested")
    cli("claim", tid, "--agent", "claude.haiku.001")
    proc = cli("claim", tid, "--agent", "claude.opus.001", expect=1)
    assert tid in proc.stderr and "claude.haiku.001" in proc.stderr
    # --force without a reason is refused before anything is written: an override
    # without a "why" is exactly what the design rule forbids.
    cli("claim", tid, "--agent", "claude.opus.001", "--force", expect=1)
    assert json.loads(cli("show", tid, "--json").stdout)["assignee"] == "claude.haiku.001"
    cli("claim", tid, "--agent", "claude.opus.001", "--force", "--reason", "user reassigned it")
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
    # The refusal is the compare-and-swap text plus the holder's attempt (CL3), which
    # is the fact the old "already assigned to" message was missing.
    proc = cli("claim", tid, "--agent", "claude.opus.001", expect=1)
    assert "is not in the expected state" in proc.stderr
    assert "assignee is claude.haiku.001, expected unassigned" in proc.stderr
    assert "attempt held by: att-" in proc.stderr and "(claude.haiku.001)" in proc.stderr
    assert "next: 'arbite list next --claim claude.opus.001'" in proc.stderr
    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["assignee"] == "claude.haiku.001"
    assert payload["status"] == "in_progress"
    assert in_progress_path.exists()
    assert not open_path.exists()

    # --force takes over: the assignee changes, the status stays in_progress (a
    # takeover is not a re-open), the ticket stays in in_progress/, and the
    # takeover is recorded as a note.
    cli("claim", tid, "--agent", "claude.opus.001", "--force", "--reason", "user reassigned it")
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

    # The referenced plans exist on disk, so the doctor check at the end is about
    # the round trip and not about a dangling reference (which warns, and is
    # reported by `doctor`, precisely so a plan can be written after the ticket).
    plans = project / ".arbite" / "plans"
    for name in ("a.md", "b.md"):
        (plans / name).write_text("a plan\n")

    def snapshot() -> dict:
        return {
            str(path.relative_to(project)): path.read_bytes()
            for path in sorted((project / ".arbite").rglob("*.md"))
            if path.name != "AGENTS.md" and "agents" not in path.parts
        }

    before = snapshot()
    assert len(before) == 5  # three tickets plus the two plans they reference

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

    if sink_kind == "file":
        # The folder is the physical consequence of the status: `review` is a
        # status *location*, so the ticket lands in review/ and `show --json`
        # points at it there.
        review_path = tmp_project / ".arbite" / "review" / f"{tid}.md"
        assert review_path.exists(), "a review ticket lives in review/"
        shown = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
        assert Path(shown["path"]).name == f"{tid}.md"
        assert Path(shown["path"]).parent.name == "review"
        assert Path(shown["path"]).parent.parent.name == ".arbite"

    # `doctor --fix` has nothing to repair: the ticket is already where its
    # status says it belongs, so it leaves the ticket, its location and its
    # status alone on either sink.
    fixed = json.loads(cli("doctor", "--json", "--fix", sink=sink_kind).stdout)
    assert fixed["problems"] == []
    assert fixed["fixed"] == 0
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["status"] == "review"
    if sink_kind == "file":
        assert review_path.exists()


def test_move_against_a_review_ticket_is_a_no_op_not_a_misfiling(project, cli):
    """For a ticket whose status is `review`, the `review/` folder *is* its status
    location, so `move <id> /review` and `move <id> /` both leave it exactly where
    it sits -- neither turns `review` into a bucket and neither makes `doctor`
    complain. A nested `/review/ideas` is a bucket by the `<status>/<bucket>/`
    convention, and `/` returns the ticket to the review/ status folder."""
    tid = create(cli, "awaiting review")
    cli("set", tid, "status", "review")
    review_path = project / ".arbite" / "review" / f"{tid}.md"
    assert review_path.exists()

    cli("move", tid, "/review")  # the status folder, not a bucket
    assert review_path.exists()
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "review"
    cli("doctor")

    cli("move", tid, "/")  # back to where the status says it belongs
    assert review_path.exists()
    cli("doctor")

    cli("move", tid, "/review/ideas")  # a nested folder under a status is a bucket
    nested = project / ".arbite" / "review" / "ideas" / f"{tid}.md"
    assert nested.exists() and not review_path.exists()
    cli("list", expect=2)  # filed away, so out of the status views
    cli("doctor")  # a bucketed ticket is legitimate, not a problem

    cli("move", tid, "/")
    assert review_path.exists() and not nested.exists()
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "review"
    cli("doctor")


def test_review_false_leaves_an_existing_review_ticket_and_folder_alone(cli, tmp_project):
    """Turning the `review:` flag off gates a future `submit` and nothing else: it
    does not remove the `review/` status folder, does not strand a ticket already
    awaiting review, and does not make `doctor` complain. A project that disables
    review must still be able to hold tickets put in review before it was
    disabled."""
    cli("init")
    tid = create(cli, "already awaiting review")
    cli("set", tid, "status", "review")
    review_path = tmp_project / ".arbite" / "review" / f"{tid}.md"
    assert review_path.exists()

    # `init` does not write a config for the default sink, so state the flag
    # explicitly: `review: false` must be in effect for the re-init below.
    config = tmp_project / ".arbite" / "project.yaml"
    config.write_text("sink: file\nreview: false\n")

    cli("init")  # re-init with review disabled
    assert (tmp_project / ".arbite" / "review").is_dir(), "the folder is unconditional"
    assert review_path.exists(), "the review ticket is not stranded"
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "review"
    cli("doctor")
    cli("doctor", "--fix")  # nothing to repair, even with the flag off
    assert review_path.exists()
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "review"


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
    # The plans exist, so this is about rendering and not about a dangling
    # reference (which `doctor` reports, but which does not affect `show`).
    for name in ("a.md", "b.md"):
        (project / ".arbite" / "plans" / name).write_text("a plan\n")
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
    """A reference is only clean while its plan exists on disk -- and it is resolved
    against the arbite directory on *both* sinks, so the same plan satisfies the
    check whether the ticket is a file or a row."""
    cli("init", sink="file")
    create(cli, "referenced", references="plans/a.md,plans/b.md")
    plans = tmp_project / ".arbite" / "plans"
    for name in ("a.md", "b.md"):
        (plans / name).write_text("a plan\n")
    cli("doctor", sink="file")
    cli("migrate", "--to", "sqlite")
    cli("doctor", sink="sqlite")


# --- `arbite ref`: the reference command group ------------------------------


def write_plan(project: Path, ref: str) -> None:
    """Create the plan document a reference points at, so it is not dangling."""
    path = project / ".arbite" / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a plan\n")


def references_of(cli, tid, sink=None) -> list:
    """The ticket's references as `ref list --json` reports them."""
    return json.loads(cli("ref", "list", tid, "--json", sink=sink).stdout)["references"]


def test_ref_add_appends_deduplicated_and_preserving_order(project, cli):
    tid = create(cli, "refs", references="plans/a.md")
    cli("ref", "add", tid, "plans/b.md", "plans/a.md", "plans/c.md")
    # New entries append in the order given; the path that was already there keeps
    # its place rather than being moved to the end.
    assert references_of(cli, tid) == ["plans/a.md", "plans/b.md", "plans/c.md"]


def test_ref_add_several_paths_at_once_works(project, cli):
    tid = create(cli, "refs")
    cli("ref", "add", tid, "plans/a.md", "plans/b.md")
    assert references_of(cli, tid) == ["plans/a.md", "plans/b.md"]


def test_ref_add_an_existing_path_is_a_no_op_success(project, cli):
    tid = create(cli, "refs", references="plans/a.md")
    out = cli("ref", "add", tid, "plans/a.md").stdout
    assert "already references plans/a.md" in out
    assert references_of(cli, tid) == ["plans/a.md"]


def test_ref_rm_removes_and_preserves_the_order_of_the_rest(project, cli):
    tid = create(cli, "refs", references="plans/a.md,plans/b.md,plans/c.md")
    cli("ref", "rm", tid, "plans/b.md")
    assert references_of(cli, tid) == ["plans/a.md", "plans/c.md"]


def test_ref_rm_a_path_it_does_not_reference_is_an_error(project, cli):
    tid = create(cli, "refs", references="plans/a.md")
    proc = cli("ref", "rm", tid, "plans/gone.md", expect=1)
    assert "does not reference" in proc.stderr and "plans/gone.md" in proc.stderr
    assert references_of(cli, tid) == ["plans/a.md"], "the ticket is untouched"


def test_ref_rm_is_atomic_when_one_of_several_paths_is_not_referenced(project, cli):
    """Every path is checked before anything is written, so a multi-path `rm` that
    names one path the ticket does not reference removes none of them -- the
    all-or-nothing rule `set` applies to its property/value pairs."""
    tid = create(cli, "refs", references="plans/a.md,plans/b.md")
    cli("ref", "rm", tid, "plans/a.md", "plans/gone.md", expect=1)
    assert references_of(cli, tid) == ["plans/a.md", "plans/b.md"]


def test_emptying_the_list_by_rm_renders_as_an_absent_field(project, cli):
    """An empty list is exactly an absent field: the `references:` line disappears."""
    tid = create(cli, "refs", references="plans/a.md")
    cli("ref", "rm", tid, "plans/a.md")
    assert "references" not in (project / ".arbite" / "open" / f"{tid}.md").read_text()
    assert json.loads(cli("show", tid, "--json").stdout)["references"] == []
    assert cli("ref", "list", tid).stdout == ""
    assert json.loads(cli("ref", "list", tid, "--json").stdout) == {
        "id": tid,
        "references": [],
    }


def test_ref_list_text_and_json(project, cli):
    tid = create(cli, "refs", references="plans/a.md,plans/b.md")
    assert cli("ref", "list", tid).stdout == "plans/a.md\nplans/b.md\n"
    assert json.loads(cli("ref", "list", tid, "--json").stdout) == {
        "id": tid,
        "references": ["plans/a.md", "plans/b.md"],
    }


def test_ref_paths_are_normalised_and_escapes_refused(project, cli):
    """'/plans/a.md' and 'plans/a.md' are the same reference: the leading '/' is
    stripped before the schema validator (which rejects absolute paths) sees it, so
    `rm` can remove a reference the way `add` wrote it. '..' stays refused."""
    tid = create(cli, "refs")
    cli("ref", "add", tid, "/plans/a.md")
    assert references_of(cli, tid) == ["plans/a.md"]
    cli("ref", "add", tid, "plans/a.md")  # the same reference, so a no-op
    assert references_of(cli, tid) == ["plans/a.md"]
    cli("ref", "rm", tid, "/plans/a.md")  # removable by either spelling
    assert references_of(cli, tid) == []

    added = cli("ref", "add", tid, "plans/../escape.md", expect=1)
    assert "'..'" in added.stderr
    removed = cli("ref", "rm", tid, "/../escape.md", expect=1)
    assert "'..'" in removed.stderr
    assert references_of(cli, tid) == []


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_ref_add_and_rm_round_trip_through_each_sink(cli, tmp_project, sink_kind):
    cli("init", sink=sink_kind)
    tid = create(cli, "refs", sink=sink_kind)
    write_plan(tmp_project, "plans/a.md")

    cli("ref", "add", tid, "/plans/a.md", "plans/b.md", sink=sink_kind)
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["references"] == [
        "plans/a.md",
        "plans/b.md",
    ]
    assert cli("ref", "list", tid, sink=sink_kind).stdout == "plans/a.md\nplans/b.md\n"

    cli("ref", "rm", tid, "plans/b.md", sink=sink_kind)
    assert json.loads(cli("ref", "list", tid, "--json", sink=sink_kind).stdout) == {
        "id": tid,
        "references": ["plans/a.md"],
    }
    # Clean on either sink: the surviving reference resolves to a real file.
    cli("doctor", sink=sink_kind)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_a_reference_survives_a_migration_and_renders_identically(cli, tmp_project, sink_kind):
    other = "sqlite" if sink_kind == "file" else "file"
    cli("init", sink=sink_kind)
    tid = create(cli, "migrating refs", sink=sink_kind)
    write_plan(tmp_project, "plans/a.md")
    cli("ref", "add", tid, "plans/a.md", sink=sink_kind)

    before = cli("show", tid, sink=sink_kind).stdout
    cli("migrate", "--from", sink_kind, "--to", other)
    assert cli("show", tid, sink=other).stdout == before
    assert json.loads(cli("ref", "list", tid, "--json", sink=other).stdout) == {
        "id": tid,
        "references": ["plans/a.md"],
    }
    # A plan is a filesystem document on either sink, so the check is clean from
    # the migrated store too.
    cli("doctor", sink=other)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_a_dangling_reference_warns_on_write_and_is_reported_by_doctor(
    cli, tmp_project, sink_kind
):
    cli("init", sink=sink_kind)
    tid = create(cli, "drafting", sink=sink_kind)

    proc = cli("ref", "add", tid, "plans/missing.md", "plans/also-missing.md", sink=sink_kind)
    assert proc.returncode == 0, "a plan written later is a drafting state, not an error"
    assert proc.stderr.count("warning: reference") == 2
    assert "plans/missing.md" in proc.stderr
    assert json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)["references"] == [
        "plans/missing.md",
        "plans/also-missing.md",
    ]

    report = json.loads(cli("doctor", "--json", sink=sink_kind, expect=3).stdout)
    assert [p["kind"] for p in report["problems"]] == [
        "dangling_reference",
        "dangling_reference",
    ]
    assert report["remaining"] == 2
    assert "plans/missing.md" in report["problems"][0]["detail"]

    # `--fix` repairs neither direction: it neither invents the plan nor deletes the
    # reference, because neither choice is unambiguously the caller's.
    fixed = json.loads(cli("doctor", "--json", "--fix", sink=sink_kind, expect=3).stdout)
    assert fixed["fixed"] == 0
    assert [p["kind"] for p in fixed["problems"]] == [
        "dangling_reference",
        "dangling_reference",
    ]
    assert not (tmp_project / ".arbite" / "plans" / "missing.md").exists()
    assert json.loads(cli("ref", "list", tid, "--json", sink=sink_kind).stdout)["references"] == [
        "plans/missing.md",
        "plans/also-missing.md",
    ]

    # Writing the plan is the ordinary repair: the reference was never corrupt.
    for name in ("missing.md", "also-missing.md"):
        write_plan(tmp_project, f"plans/{name}")
    cli("doctor", sink=sink_kind)


def test_set_references_shares_the_missing_plan_warning(project, cli):
    """`set references` writes the same field through the same check, so it gives
    the same warning -- and still succeeds."""
    tid = create(cli, "a ticket")
    proc = cli("set", tid, "references", "plans/nope.md")
    assert proc.returncode == 0
    assert "warning: reference 'plans/nope.md'" in proc.stderr
    assert references_of(cli, tid) == ["plans/nope.md"]


# --- status: ticket counts by status --------------------------------------


def status_rows(output: str) -> dict:
    """The `arbite status` table as `{name: count}`, in the order printed.

    Parsed from the human output rather than from `--json`: the aligned table *is*
    the human contract, so the status names and their numbers have to be readable
    side by side. The `tickets by status ...` heading has more than two fields, so
    only real table rows match."""
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            rows[parts[0]] = int(parts[1])
    return rows


def seed_a_backlog(cli, sink: str) -> dict:
    """A store covering several statuses with one left empty, plus two tickets
    parked in buckets. Returns `{"counts": {...}, "ids": {...}}`.

    `counts` is keyed by `schema.STATUSES` and is what `arbite status` must
    report. A ticket in a bucket is out of the status workflow, so the promoted
    wish (still `status: raw`) and the hand-filed ticket (still `open`)
    contribute nothing to `raw`/`open` -- which is the whole reason `status` and
    `list --status` agree.

    `review` is never reached by accident: the four lifecycle commands below
    cannot set it (it takes `set <id> status review`), so it stays at zero and
    pins that an empty status is rendered rather than omitted."""
    ids = {
        "open_plain": create(cli, "open, no epic", tier="low", domain="mesh", sink=sink),
        "open_in_epic": create(
            cli, "open, in the epic", tier="medium", domain="mesh", epic="workflow", sink=sink
        ),
        "open_high": create(
            cli, "open, high tier in the epic", tier="high", domain="audio_gen",
            epic="workflow", sink=sink,
        ),
        "claimed": create(
            cli, "claimed", tier="high", domain="mesh", epic="workflow", sink=sink
        ),
        "stalled": create(cli, "stalled", tier="medium", domain="io", sink=sink),
        "parked": create(cli, "parked", tier="low", domain="ui", sink=sink),
        "finished": create(cli, "finished", tier="medium", domain="io", sink=sink),
    }
    cli("claim", ids["claimed"], "--agent", "claude.haiku.001", sink=sink)
    cli("block", ids["stalled"], "--reason", "waiting on upstream", sink=sink)
    cli("shelve", ids["parked"], "--reason", "later", sink=sink)
    cli("close", ids["finished"], sink=sink)

    ids["raw"] = raw_capture(cli, raw_type="feature", message="a raw capture", sink=sink)

    # Two buckets: `promote` files a reclassified wish itself, the other ticket is
    # filed by hand -- the two ways (tic-7e03) a ticket leaves the status workflow.
    ids["bucketed_wish"] = raw_capture(
        cli, raw_type="wish", message="fly-through preview", sink=sink
    )
    cli(
        "promote", ids["bucketed_wish"], "--title", "Camera fly-through preview",
        "--tier", "low", "--domain", "ui",
        "--description", "Preview a camera fly-through.", sink=sink,
    )
    ids["bucketed_plans"] = create(
        cli, "filed in the plans bucket", tier="medium", domain="mesh", sink=sink
    )
    cli("move", ids["bucketed_plans"], "/plans", sink=sink)

    counts = {status: 0 for status in schema.STATUSES}
    counts.update(raw=1, open=3, in_progress=1, blocked=1, shelved=1, closed=1)
    return {"counts": counts, "ids": ids}


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_counts_every_status_in_order_including_zeros(cli, tmp_project, sink_kind):
    """Every status in the vocabulary is rendered, in vocabulary order, with its
    count -- including the ones holding nothing -- and then a total. The
    expectation is keyed by `schema.STATUSES` itself, so a status added to the
    vocabulary must show up here without this test being rewritten."""
    cli("init", sink=sink_kind)
    expected = seed_a_backlog(cli, sink_kind)["counts"]
    cli("doctor", sink=sink_kind)  # the fixture is a legitimate store

    proc = cli("status", sink=sink_kind)
    rows = status_rows(proc.stdout)
    assert list(expected) == list(schema.STATUSES)
    assert list(rows) == list(schema.STATUSES) + ["total"]
    assert rows == {**expected, "total": sum(expected.values())}
    assert rows["review"] == 0, "an empty status is listed, not omitted"

    # An aligned table: every count starts in the same column, and an unnarrowed
    # report is not labelled with filters.
    body = [line for line in proc.stdout.splitlines() if len(line.split()) == 2]
    assert len({len(line) - len(line.split()[1]) for line in body}) == 1, body
    assert "filters:" not in proc.stdout


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_reconciles_with_list_and_sink_info(cli, tmp_project, sink_kind):
    """The three surfaces answer the same question the same way: a count here is
    the number of rows `arbite list --status <status>` shows, and the non-zero
    counts are exactly `sink info`'s per-status counts -- one counting
    implementation serves both, so they cannot drift apart."""
    cli("init", sink=sink_kind)
    expected = seed_a_backlog(cli, sink_kind)["counts"]
    rows = status_rows(cli("status", sink=sink_kind).stdout)

    for status in schema.STATUSES:
        # An empty status is exit 2 with an empty list: an answer, not a failure.
        proc = cli(
            "list", "--status", status, "--json", sink=sink_kind,
            expect=0 if rows[status] else 2,
        )
        assert len(json.loads(proc.stdout)) == rows[status], status

    info = json.loads(cli("sink", "info", "--json", sink=sink_kind).stdout)
    assert info["status_counts"] == {status: n for status, n in expected.items() if n}
    assert info["ticket_count"] == rows["total"] == sum(expected.values())


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_does_not_count_a_bucketed_ticket_under_its_retained_status(
    cli, tmp_project, sink_kind
):
    """A promoted wish keeps `status: raw` and a hand-filed ticket keeps `open`,
    but both are filed in a bucket -- out of the status workflow -- so neither is
    counted. This is what a naive count gets wrong (it would say `raw 2` and
    `open 4`, disagreeing with `arbite list --status raw`)."""
    cli("init", sink=sink_kind)
    seeded = seed_a_backlog(cli, sink_kind)
    rows = status_rows(cli("status", sink=sink_kind).stdout)

    assert (rows["raw"], rows["open"]) == (seeded["counts"]["raw"], seeded["counts"]["open"])
    assert (rows["raw"], rows["open"]) == (1, 3)
    # The triage view agrees with the count, too: the one unpromoted capture is
    # still in the queue and the promoted wish has left it.
    backlog = cli("list", "raw", sink=sink_kind).stdout
    assert seeded["ids"]["raw"] in backlog
    assert seeded["ids"]["bucketed_wish"] not in backlog

    # ...and the same ticket leaves the counts the moment it is filed away.
    cli("move", seeded["ids"]["raw"], "/wishlist", sink=sink_kind)
    after = status_rows(cli("status", sink=sink_kind).stdout)
    assert after["raw"] == 0
    assert after["open"] == rows["open"], "only the filed ticket left the report"
    assert after["total"] == rows["total"] - 1
    assert json.loads(
        cli("list", "--status", "raw", "--json", sink=sink_kind, expect=2).stdout
    ) == []


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_filters_narrow_every_count_and_the_total(cli, tmp_project, sink_kind):
    """`arbite status --epic workflow` answers "how far along is this epic": every
    count narrows to the filter and the total follows, and the applied filters are
    echoed so a narrowed report cannot be mistaken for the whole backlog."""
    cli("init", sink=sink_kind)
    seed_a_backlog(cli, sink_kind)

    proc = cli("status", "--epic", "workflow", sink=sink_kind)
    narrowed = status_rows(proc.stdout)
    expected = {status: 0 for status in schema.STATUSES}
    expected.update(open=2, in_progress=1)  # the epic's two open tickets and its claim
    assert narrowed == {**expected, "total": 3}
    assert "filters: --epic workflow" in proc.stdout

    combined = status_rows(
        cli("status", "--epic", "workflow", "--tier", "high", sink=sink_kind).stdout
    )
    assert combined == {**{s: 0 for s in schema.STATUSES}, "open": 1, "in_progress": 1, "total": 2}

    by_domain = status_rows(cli("status", "--domain", "mesh", sink=sink_kind).stdout)
    # The mesh ticket filed in the plans bucket is not part of the count.
    assert by_domain["total"] == 3

    by_tier = status_rows(cli("status", "--tier", "low", sink=sink_kind).stdout)
    assert {s: n for s, n in by_tier.items() if n} == {"open": 1, "shelved": 1, "total": 2}

    by_assignee = status_rows(
        cli("status", "--assignee", "claude.haiku.001", sink=sink_kind).stdout
    )
    assert {s: n for s, n in by_assignee.items() if n} == {"in_progress": 1, "total": 1}


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
@pytest.mark.parametrize(
    "flags",
    [
        ("--epic", "workflow"),
        ("--domain", "mesh"),
        ("--tier", "high"),
        ("--assignee", "claude.haiku.001"),
        ("--epic", "workflow", "--tier", "high"),
        ("--domain", "mesh", "--epic", "workflow"),
    ],
)
def test_status_filters_agree_with_list_per_status(cli, tmp_project, sink_kind, flags):
    """Each filter, alone and combined, is the same query `arbite list` runs: the
    count for a status equals the rows `arbite list --status <status>` shows under
    the same flags, and the total is the sum of the parts."""
    cli("init", sink=sink_kind)
    seed_a_backlog(cli, sink_kind)
    rows = status_rows(cli("status", *flags, sink=sink_kind).stdout)

    for status in schema.STATUSES:
        proc = cli(
            "list", "--status", status, "--json", *flags, sink=sink_kind,
            expect=0 if rows[status] else 2,
        )
        assert len(json.loads(proc.stdout)) == rows[status], f"{status} {flags}"
    assert rows["total"] == sum(rows[status] for status in schema.STATUSES)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_json_round_trips_in_vocabulary_order(cli, tmp_project, sink_kind):
    """`--json` is a flat mapping of status to count plus `total`, emitted in
    vocabulary order, and it round-trips through json.loads -- filters included,
    because a narrowed report has to be machine-readable too."""
    cli("init", sink=sink_kind)
    expected = seed_a_backlog(cli, sink_kind)["counts"]

    payload = json.loads(cli("status", "--json", sink=sink_kind).stdout)
    assert list(payload) == list(schema.STATUSES) + ["total"]
    assert payload == {**expected, "total": sum(expected.values())}

    narrowed = json.loads(
        cli("status", "--json", "--epic", "workflow", sink=sink_kind).stdout
    )
    assert list(narrowed) == list(schema.STATUSES) + ["total"], "order survives a filter"
    assert (narrowed["open"], narrowed["in_progress"], narrowed["total"]) == (2, 1, 3)
    assert narrowed != payload


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_status_exits_zero_on_an_empty_store_and_a_filter_that_matches_nothing(
    cli, tmp_project, sink_kind
):
    """It is a report, not a query: an all-zero table is a correct answer (exit
    0), not the "2 = nothing matched" signal a listing gives -- and either way
    every status is still listed, with a total."""
    cli("init", sink=sink_kind)

    empty = cli("status", sink=sink_kind, expect=0)
    rows = status_rows(empty.stdout)
    assert list(rows) == list(schema.STATUSES) + ["total"]
    assert all(rows[status] == 0 for status in schema.STATUSES)
    assert rows["total"] == 0
    assert json.loads(cli("status", "--json", sink=sink_kind, expect=0).stdout) == {
        **{status: 0 for status in schema.STATUSES},
        "total": 0,
    }

    seed_a_backlog(cli, sink_kind)
    no_match = cli("status", "--epic", "no-such-epic", sink=sink_kind, expect=0)
    narrowed = status_rows(no_match.stdout)
    assert list(narrowed) == list(schema.STATUSES) + ["total"]
    assert all(narrowed[status] == 0 for status in schema.STATUSES)
    assert narrowed["total"] == 0
    assert "filters: --epic no-such-epic" in no_match.stdout
    assert json.loads(
        cli("status", "--epic", "no-such-epic", "--json", sink=sink_kind, expect=0).stdout
    ) == {**{status: 0 for status in schema.STATUSES}, "total": 0}


def test_status_help_distinguishes_it_from_set_status_and_sink_info(cli, tmp_project):
    """Three surfaces sit next to each other by name -- `arbite status`, `arbite set
    <id> status` and the `--status` filter -- plus `arbite sink info`, which
    answers a related-looking question. Each says what it is not, so no two read
    as duplicates of each other."""
    cli("init")

    def one_line(*args):
        # argparse wraps help to the terminal width, so compare collapsed text.
        return " ".join(cli(*args).stdout.split())

    status_help = one_line("status", "-h")
    assert "not 'arbite set <id> status <value>'" in status_help
    assert "not the '--status' filter on list/search" in status_help
    assert "not 'arbite sink info'" in status_help
    assert "always exits 0" in status_help
    assert "not counted under the status they retain" in status_help

    # ...and the surfaces it could be confused with point back at it.
    assert "arbite status" in one_line("sink", "-h")
    assert "arbite status" in one_line("list", "-h")
    assert "arbite status" in one_line("set", "-h")


# --- set-status: the dedicated status front door --------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_set_status_makes_the_same_change_as_set(cli, tmp_project, sink_kind):
    """`set-status` is additive, so it has to leave the ticket in exactly the state
    `set <id> status <value>` leaves it in -- that is the whole reason the two
    funnel through one shared code path (and why the receipt is the same too)."""
    cli("init", sink=sink_kind)
    direct = create(cli, "via set-status", sink=sink_kind)
    through_set = create(cli, "via set", sink=sink_kind)

    direct_receipt = cli("set-status", direct, "review", sink=sink_kind).stdout.strip()
    set_receipt = cli("set", through_set, "status", "review", sink=sink_kind).stdout.strip()

    assert direct_receipt.startswith(f"set status on {direct} at ")
    assert set_receipt.startswith(f"set status on {through_set} at ")
    for tid in (direct, through_set):
        payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
        assert payload["status"] == "review"
    if sink_kind == "file":
        for tid in (direct, through_set):
            assert (tmp_project / ".arbite" / "review" / f"{tid}.md").exists()


def test_set_status_auto_dates_closed_and_bumps_updated(cli, project):
    """A status change goes through the same path `set status` uses, including the
    `closed` dating that stops a closed ticket being undated."""
    tid = create(cli, "will be closed")
    before = json.loads(cli("show", tid, "--json").stdout)

    cli("set-status", tid, "closed")

    after = json.loads(cli("show", tid, "--json").stdout)
    assert after["status"] == "closed"
    assert after["closed"] == after["updated"]
    assert after["updated"] >= before["updated"]
    assert (project / ".arbite" / "closed").is_dir()


def test_set_status_un_files_on_a_real_change_but_not_a_same_status_call(cli, project):
    """A status *change* lands in the status tree, which is what un-files a ticket
    from a bucket. Asking for the status a ticket already has is a no-op that leaves
    it filed -- and `set status` does exactly the same, so the two doors agree."""
    changed = create(cli, "filed then changed")
    cli("move", changed, "/wishlist")
    cli("set-status", changed, "review")
    assert (project / ".arbite" / "review" / f"{changed}.md").exists()
    assert not (project / ".arbite" / "wishlist" / f"{changed}.md").exists()

    unchanged = create(cli, "filed and left alone")
    cli("move", unchanged, "/wishlist")
    # Reach `review` through `set`, re-file it, then ask for the status it already
    # has through `set-status`: nothing moves, exactly as `set status review` would
    # move nothing if asked again.
    cli("set", unchanged, "status", "review")
    cli("move", unchanged, "/wishlist")
    cli("set-status", unchanged, "review")
    assert (project / ".arbite" / "wishlist" / f"{unchanged}.md").exists()
    assert json.loads(cli("show", unchanged, "--json").stdout)["status"] == "review"


def test_set_status_rejects_an_unknown_status_from_the_vocabulary(cli, project):
    """The choices come from `schema.STATUSES`, so argparse refuses the rest without
    reaching command logic, and the ticket is untouched."""
    tid = create(cli, "stays where it is")

    proc = cli("set-status", tid, "nope", expect=2)

    assert "invalid choice" in proc.stderr
    assert "review" in proc.stderr  # the vocabulary it is choosing from
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "open"


def test_set_status_help_reads_clearly_next_to_set(cli, tmp_project):
    """`arbite set-status` sits next to `arbite set <id> status`, so both name the
    relationship rather than leaving a reader to guess which is which."""
    cli("init")

    def one_line(*args):
        return " ".join(cli(*args).stdout.split())

    set_status_help = one_line("set-status", "-h")
    assert "Additive, not a replacement" in set_status_help
    assert "'arbite set <id> status <value>' keeps working" in set_status_help
    assert "escape hatch" in set_status_help
    assert "no-op" in set_status_help

    # ...and `set` names the relationship from its own side, rather than describing
    # only the mechanics of a status change and leaving the pair unconnected.
    set_help = one_line("set", "-h")
    assert "arbite set-status <id> <status>" in set_help
    assert "same change" in set_help
    assert "escape hatch" in set_help


# --- progress: live epics in dependency order -----------------------------


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_progress_shows_every_ticket_of_a_live_epic(cli, tmp_project, sink_kind):
    """The live ticket is what puts an epic in the report; the closed siblings are
    the context that makes it legible, so they are shown too. An epic with nothing
    live never appears -- that is the difference between this and `arbite status`."""
    cli("init", sink=sink_kind)
    finished = create(cli, "finished sibling", epic="mesh-pipeline", sink=sink_kind)
    cli("close", finished, sink=sink_kind)
    live = create(cli, "the live one", epic="mesh-pipeline", sink=sink_kind)
    dead = create(cli, "all over", epic="dead-epic", sink=sink_kind)
    cli("close", dead, sink=sink_kind)

    output = cli("progress", sink=sink_kind).stdout

    assert "mesh-pipeline" in output
    assert finished in output and live in output
    assert "dead-epic" not in output
    # The per-epic count line, so progress is readable without counting rows.
    assert "1 closed" in output and "1 open" in output


def test_progress_orders_an_epic_topologically(cli, project):
    """Order inside an epic follows `depends_on` -- the same topological ordering
    `list next` uses, not the flat priority sort."""
    cli("init")
    first = create(cli, "the blocker", epic="chain", priority=9)
    second = create(cli, "the blocked", epic="chain", priority=1)
    cli("depend", second, first)

    output = cli("progress", "--epic", "chain").stdout

    assert output.index(first) < output.index(second)


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_progress_groups_epicless_tickets_and_round_trips_json(cli, tmp_project, sink_kind):
    """Live tickets with no epic must not vanish: they get a 'no epic' heading, and
    `--json` emits one object per epic with its counts and its tickets."""
    cli("init", sink=sink_kind)
    lonely = create(cli, "no epic at all", sink=sink_kind)
    grouped = create(cli, "in an epic", epic="workflow", sink=sink_kind)

    payload = json.loads(cli("progress", "--json", sink=sink_kind).stdout)

    headings = [group["epic"] for group in payload]
    assert "workflow" in headings
    # The un-epic'd group carries a null epic rather than a display label, so a
    # consumer can test membership honestly.
    assert None in headings
    ungrouped = next(group for group in payload if group["epic"] is None)
    assert [t["id"] for t in ungrouped["tickets"]] == [lonely]
    assert list(ungrouped["counts"]) == list(schema.STATUSES)
    assert ungrouped["live"] == 1 and ungrouped["total"] == 1

    workflow = next(group for group in payload if group["epic"] == "workflow")
    assert [t["id"] for t in workflow["tickets"]] == [grouped]
    assert workflow["live"] == 1

    assert "no epic" in cli("progress", sink=sink_kind).stdout


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_progress_exits_empty_when_nothing_is_live(cli, tmp_project, sink_kind):
    """An all-closed store has nothing in flight: that is an answer (exit 2), not an
    error, and `--epic` narrows the report rather than forcing an empty one."""
    cli("init", sink=sink_kind)
    tid = create(cli, "all done", epic="workflow", sink=sink_kind)
    cli("close", tid, sink=sink_kind)

    proc = cli("progress", expect=2, sink=sink_kind)
    assert "no live tickets" in proc.stdout

    cli("progress", "--epic", "workflow", expect=2, sink=sink_kind)
    cli("progress", "--epic", "not-an-epic", expect=2, sink=sink_kind)


def test_progress_ignores_a_ticket_filed_in_a_bucket(cli, project):
    """A bucketed ticket is out of the status workflow, so it is not live and cannot
    pull its epic into the report -- the same rule `arbite status` counts by."""
    cli("init")
    tid = create(cli, "filed away", epic="side-epic")
    cli("move", tid, "/wishlist")

    proc = cli("progress", expect=2)

    assert "side-epic" not in proc.stdout
    assert "no live tickets" in proc.stdout


def test_progress_help_distinguishes_it_from_status_and_list_topo(cli, tmp_project):
    cli("init")

    def one_line(*args):
        return " ".join(cli(*args).stdout.split())

    help_text = one_line("progress", "-h")
    assert "not 'arbite status'" in help_text
    assert "not 'arbite list --topo'" in help_text


# --- submit and accept: the review hand-off -------------------------------


def set_review_flag(project: Path, enabled: bool) -> None:
    """Write the committed `review:` answer, the way a project would.

    Only the flag is written: the sink is still chosen per command, exactly as the
    committed config alone would decide it in a real project."""
    (project / ".arbite" / "project.yaml").write_text(
        f"review: {'true' if enabled else 'false'}\n"
    )


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_submit_sends_a_finished_ticket_to_review_keeping_its_assignee(
    cli, tmp_project, sink_kind
):
    """With review on (the default) submit hands the work off: status `review` and the
    ticket still owned by whoever did it, because they are who a reviewer sends it back
    to. It stays `in flight`, so the progress view keeps showing it."""
    cli("init", sink=sink_kind)
    tid = create(cli, "finished work", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    cli(
        "submit", tid, "--agent", "claude.haiku.001",
        "--message", "ready for eyes", sink=sink_kind,
    )

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "review"
    assert payload["assignee"] == "claude.haiku.001"
    assert "Submitted for review: ready for eyes" in cli("show", tid, sink=sink_kind).stdout
    assert tid in cli("progress", sink=sink_kind).stdout
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "review" / f"{tid}.md").exists()


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_submit_closes_the_ticket_when_review_is_disabled(cli, tmp_project, sink_kind):
    """`review: false` is the flag's whole job: the same command closes the ticket
    instead of parking it, dated exactly as `arbite close` dates it -- so the note and
    the receipt say which path was taken."""
    cli("init", sink=sink_kind)
    set_review_flag(tmp_project, False)
    tid = create(cli, "no review here", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    receipt = cli("submit", tid, sink=sink_kind).stdout

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "closed"
    assert payload["closed"] == payload["updated"]
    assert "Submitted; closed (review disabled)." in cli("show", tid, sink=sink_kind).stdout
    assert "closed" in receipt
    if sink_kind == "file":
        assert (tmp_project / ".arbite" / "closed").is_dir()
        assert not (tmp_project / ".arbite" / "review" / f"{tid}.md").exists()


def test_submit_refuses_a_ticket_that_is_already_closed(cli, project):
    """A closed ticket has nothing left to hand off, and the refusal says so rather
    than silently appending another note to it."""
    cli("init")
    tid = create(cli, "done already")
    cli("close", tid)

    proc = cli("submit", tid, expect=1)

    assert f"ticket {tid} is already closed" in proc.stderr
    assert json.loads(cli("show", tid, "--json").stdout)["status"] == "closed"


@pytest.mark.parametrize("sink_kind", ["file", "sqlite"])
def test_accept_closes_a_review_ticket_attributed_to_the_reviewer(cli, tmp_project, sink_kind):
    """Accept is the reviewer's counterpart to submit. The note is attributed to the
    *accepting* agent, not to the ticket's assignee: the record is about who approved
    the work, and the author's own notes stay in place beside it."""
    cli("init", sink=sink_kind)
    tid = create(cli, "to be reviewed", sink=sink_kind)
    cli("claim", tid, "--agent", "claude.haiku.001", sink=sink_kind)
    cli("submit", tid, "--agent", "claude.haiku.001", sink=sink_kind)

    cli("accept", tid, "--agent", "claude.opus.001", "--message", "looks good", sink=sink_kind)

    payload = json.loads(cli("show", tid, "--json", sink=sink_kind).stdout)
    assert payload["status"] == "closed"
    assert payload["closed"] == payload["updated"]
    body = cli("show", tid, sink=sink_kind).stdout
    assert "claude.opus.001: Accepted: looks good" in body
    assert "claude.haiku.001: Submitted for review." in body
    if sink_kind == "file":
        assert not (tmp_project / ".arbite" / "review" / f"{tid}.md").exists()


def test_accept_refuses_a_ticket_that_is_not_in_review(cli, project):
    """Accept is not a general-purpose close: it names the real status instead of
    absorbing the mistake, which is what keeps "accepted" meaning "was reviewed"."""
    cli("init")
    never_submitted = create(cli, "never submitted")
    proc = cli("accept", never_submitted, expect=1)
    assert f"{never_submitted} is not in review (status: open)" in proc.stderr

    already_closed = create(cli, "already closed")
    cli("close", already_closed)
    proc = cli("accept", already_closed, expect=1)
    assert f"{already_closed} is not in review (status: closed)" in proc.stderr


def test_the_review_loop_rejects_with_reopen_and_then_accepts(cli, project):
    """The chain the review status exists for, end to end: claim -> submit -> reopen
    (the rejection path, which must record why) -> work again -> submit -> accept."""
    cli("init")
    tid = create(cli, "round trip")
    cli("claim", tid, "--agent", "claude.haiku.001")
    cli("submit", tid, "--agent", "claude.haiku.001")

    cli("reopen", tid, "--reason", "tests fail on ARM")

    payload = json.loads(cli("show", tid, "--json").stdout)
    assert payload["status"] == "open"
    assert "Reopened: tests fail on ARM." in cli("show", tid).stdout

    cli("claim", tid, "--agent", "claude.haiku.001")
    cli("submit", tid, "--agent", "claude.haiku.001")
    cli("accept", tid, "--agent", "claude.opus.001")

    final = json.loads(cli("show", tid, "--json").stdout)
    assert final["status"] == "closed"
    # The auto-note with no --message is exactly 'Accepted.'
    assert "claude.opus.001: Accepted." in cli("show", tid).stdout
