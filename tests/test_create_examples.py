"""The frozen ticket-creation transcripts this slice owns: CR1, CR2.

No other family runs `create --json` or `raw --json` / the shortcut commands with
`--json`, so CR covers the whole machine contract for capturing a ticket: the one
document it prints, and the promise that the document is exactly what `show --json`
prints for the same ticket. `examples.py` reads the blocks out of
`.arbite/planning/interaction-examples.md`, and each block is compared as a parsed
JSON document with ids and paths normalised on both sides.

Two fields are facts about the run, not the contract, and the harness cannot pin
them: the `created`/`updated` wall-clock second, and the sqlite sink's location
string (the file block prints a `.arbite/...` path; `path` is a design difference
between the sinks, not a drift). The tests therefore adopt the run's stamps into
the documented document -- after asserting they parse and are equal, which is the
statement "a ticket captured once has never been rewritten" -- and pin the sqlite
`path` to its own exact form instead of the block's file one. Every other field of
the block is asserted, `body` and `description` verbatim.
"""

from __future__ import annotations

import json
from datetime import datetime

import examples

#: The two fields a capture run stamps at its own wall clock, plus the identity
#: and location minted with them: all four differ between the shortcut's capture
#: and the long form's, and nothing else in the document does.
VOLATILE = ("id", "created", "updated", "path")


def _init(tmp_path, kind: str):
    project = tmp_path / "project"
    project.mkdir()
    proc = examples.run_cli(project, "init", sink=kind)
    assert proc.returncode == 0, proc.stderr
    return project


def _captured(project, scenario, kind):
    """Run the block's command and return its parsed document, asserting the
    transcript's exit code and that stdout held nothing but that one document
    (any extra line would make `json.loads` raise)."""
    proc = examples.run_scenario(scenario, project, sink=kind)
    assert proc.returncode == scenario.exit_code, (
        f"scenario {scenario.id} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    assert proc.stderr == "", f"scenario {scenario.id} wrote to stderr: {proc.stderr}"
    return json.loads(proc.stdout)


def _assert_document(project, scenario, payload, kind: str, path_field: str):
    """The frozen block, field by field, against the run's document."""
    assert scenario.is_json, f"scenario {scenario.id}'s transcript is a JSON document"
    created = payload["created"]
    assert payload["updated"] == created, "a captured ticket has never been rewritten"
    datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")

    actual = examples.normalise_payload(payload, project)
    documented = examples.normalise_payload(scenario.json_payload, project)
    documented["created"] = documented["updated"] = created
    if kind == "file":
        assert actual["path"] == documented["path"] == path_field
    else:
        # The sqlite sink names the database and row, not a file: same fact,
        # different location, by design. (The harness's path rule folds the
        # `sqlite:<dir>/` prefix into <ARBITE> like any other directory.)
        assert actual["path"] == "<ARBITE>/arbite.db#tic-XXXX"
        documented["path"] = actual["path"]
    assert actual == documented


# --- CR1: create --json ------------------------------------------------------


def test_CR1_create_json_prints_the_show_document(tmp_path, kind):
    project = _init(tmp_path, kind)
    scenario = examples.scenario_block("CR1")
    payload = _captured(project, scenario, kind)

    _assert_document(project, scenario, payload, kind, "<ARBITE>/open/tic-XXXX.md")

    # The contract the block exists for: the capture document is the show
    # document, compared before normalisation so no placeholder can paper over
    # a drifting field.
    shown = json.loads(examples.run_cli(project, "show", payload["id"], "--json",
                                        sink=kind).stdout)
    assert payload == shown

    # And the text path is untouched: without --json the creation line stands.
    text = examples.run_cli(project, "create", "--title", "plain", "--type", "bug",
                            "--tier", "low", "--domain", "ui", sink=kind)
    assert text.returncode == 0 and text.stdout.startswith("created tic-"), text.stdout


# --- CR2: the shortcuts and raw --json ---------------------------------------


def test_CR2_a_shortcut_json_is_the_long_forms_document(tmp_path, kind):
    project = _init(tmp_path, kind)
    scenario = examples.scenario_block("CR2")
    payload = _captured(project, scenario, kind)

    _assert_document(project, scenario, payload, kind, "<ARBITE>/raw/tic-XXXX.md")

    # A raw capture is still a raw capture: show, the backlog and fetch all see
    # the same ticket the transcript prints.
    assert payload == json.loads(examples.run_cli(project, "show", payload["id"],
                                                  "--json", sink=kind).stdout)
    assert payload["id"] in examples.run_cli(project, "list", "raw", sink=kind).stdout
    assert json.loads(examples.run_cli(project, "fetch", "--json",
                                       sink=kind).stdout)["id"] == payload["id"]

    # The long form's document is the shortcut's, apart from the identity the
    # second run minted for its own ticket.
    twin = json.loads(examples.run_cli(project, "raw", "feature", "add per-mesh LOD",
                                       "--json", sink=kind).stdout)
    stripped = lambda d: {k: v for k, v in d.items() if k not in VOLATILE}
    assert stripped(twin) == stripped(payload)
    assert twin["id"] != payload["id"]
