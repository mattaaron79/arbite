"""What the write and edit commands promise, on both sinks and under real processes.

`test_write_examples.py` asserts the frozen transcripts. This file asserts the guarantees
those transcripts stand for, and it asserts them the only way a guarantee about *bytes* can
be asserted: by reading the bytes, before and after, on the file system and in the evidence
the receipt holds.

- **One token authorises one mutation.** The spend is committed with the receipt, so it is
  checked here both sequentially and with two `arbite file write` processes racing on one
  token: exactly one wins, the other is stale, and the file holds one copy of the payload.
- **A stale token changes no bytes.** Every refusal path (moved bytes, spent token, revoked
  generation, closed ticket, foreign claim, no claim, generated output, a batch that does not
  apply) is checked against the file's bytes and the receipt count, not just its exit code.
- **An edit batch is all or nothing.** A batch that fails anywhere leaves the file
  byte-for-byte identical and no receipt behind, and produces the same result on the file
  sink (a journal and a process lock) and SQLite (one transaction).
- **Creation, binary and permissions.** A write to an absent path needs a probe token that
  found it absent; bytes round-trip through the artifacts exactly; a 0600 file stays 0600.

Both sinks are exercised by the `kind` fixture (see `conftest.py`), because a mutation that
only worked on one of them would be two different products.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

import examples
import writes_state as state
from arbite.coordination import edits as edit_batches
from arbite.coordination import records as coordination_records
from arbite.errors import EditRefused, PathRefused

BASE_PY = state.BASE_PY
HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET
RIVAL = state.RIVAL

#: The refusal the SQLite coordination backend gives a mutation: artifact content has no home
#: there yet, and `tic-7c42`/C11 decides how one stores it. Named once, because two tests talk
#: about it and a second copy of the sentence would be a second thing to keep true.
EVIDENCE_UNAVAILABLE = "does not store artifact content"



@pytest.fixture
def project_kind() -> str:
    """The sink a *successful* mutation runs on: the file sink, which stores artifact content.

    The SQLite coordination backend has no artifact store yet -- how content lives in a
    database is tic-7c42's decision -- so a mutation there is refused *before* any byte
    changes. That refusal is asserted on its own below rather than skipped, and every test
    that does not depend on a recorded mutation still runs on both sinks through `kind`."""
    return "file"


def write(project, path, token, payload="base.py", kind="file", *extra):
    return examples.run_cli(
        project, "file", "write", path, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", payload, *extra, sink=kind,
    )


def edit(project, path, token, payload="edits.json", kind="file", *extra):
    return examples.run_cli(
        project, "file", "edit", path, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--edits", payload, *extra, sink=kind,
    )


# --- the happy path, on both sinks -------------------------------------------


def test_a_write_replaces_the_bytes_and_records_the_evidence(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """The bytes on disk, the bytes in the receipt, and the digest the report printed agree.

    "The evidence holds what happened" is the claim every later view (`arbite receipt`,
    `arbite changes`) will stand on, so it is asserted against the artifact store rather than
    against the report's own numbers."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    payload = state.base_text(append=state.APPENDED_LINES).encode("utf-8")

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (project / BASE_PY).read_bytes() == payload
    before, after = state.recorded_versions(project, kind)
    assert state.artifact_bytes(project, before[BASE_PY], kind) == state.base_text().encode("utf-8")
    assert state.artifact_bytes(project, after[BASE_PY], kind) == payload
    assert after[BASE_PY] == coordination_records.digest_bytes(payload)


def test_an_edit_writes_once_and_leaves_every_other_byte_alone(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """Untouched bytes are untouched: only the batch's selections differ between versions.

    The fixture's file is deliberately awkward -- CRLF line endings, trailing spaces and a
    final line with no newline -- so "the edit preserved the rest" is a real assertion rather
    than a claim about a file that had nothing to preserve."""
    project = state.init_project(tmp_path, kind)
    original = (
        "from __future__ import annotations\r\n"
        "import os\r\n"
        "\r\n"
        "def schema_1(value):\r\n"
        "    return value   \r\n"
        "\r\n"
        "# trailing line with no newline"
    )
    (project / "src" / "arbite").mkdir(parents=True, exist_ok=True)
    (project / "src" / "arbite" / "schema.py").write_bytes(original.encode("utf-8"))
    state.discovery.put_claim(
        project, "src/arbite/schema.py", HOLDER_TICKET, HOLDER, generation=1, sink_kind=kind
    )
    token = state.read_token(project, "src/arbite/schema.py", HOLDER_TICKET, HOLDER, kind)
    state.staged(
        project,
        "edits.json",
        json.dumps({"edits": [{"old": "import os\r\n", "new": "import os\r\nimport re\r\n"}]}),
    )

    proc = edit(project, "src/arbite/schema.py", token, kind=kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    written = (project / "src" / "arbite" / "schema.py").read_bytes()
    assert written == original.replace("import os\r\n", "import os\r\nimport re\r\n", 1).encode()
    assert written.endswith(b"# trailing line with no newline")
    assert written.count(b"\r\n") == original.count("\r\n") + 1, "CRLF survived"
    assert b"    return value   \r\n" in written, "trailing spaces outside the edit survived"


# --- one token, one mutation -------------------------------------------------


def test_a_spent_token_is_refused_and_the_store_says_which_operation_spent_it(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """The token's record names the receipt, and the token cannot authorise a second change."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    assert write(project, BASE_PY, token, kind=kind).returncode == 0
    spender = state.spent_by(project, token, kind)
    observation = state.store_for(project, kind).get_record("observation", token)
    written = (project / BASE_PY).read_bytes()
    state.staged(project, "base.py", state.base_text(append=state.APPENDED_LINES))

    replay = write(project, BASE_PY, token, kind=kind)

    assert spender == state.newest_receipt(project, kind).id, "the token names its spender"
    assert observation.is_spent and observation.spent_by == spender
    assert observation.authorizes_write(None) is False, "a spent token authorises nothing"
    assert replay.returncode == 5
    assert f"already spent by {spender}" in replay.stderr
    assert (project / BASE_PY).read_bytes() == written, "the replay changed no bytes"
    assert state.receipt_count(project, kind) == 1, "one mutation, one receipt"


def test_two_processes_with_one_token_race_and_exactly_one_wins(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """The guarantee under real concurrency, not just in sequence.

    Two `arbite file write` processes are started with the same read token, each with its own
    copy of the payload (a successful write consumes the payload it read, so one shared file
    would let the winner's consumption decide the loser's outcome -- a race in the fixture
    rather than in the rule this test is about). Whichever the store serialises first spends
    the token; the other must be refused with exit 5 -- there is no interleaving in which both
    replace the file, and the file holds exactly one copy of the payload afterwards (whichever
    process won)."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    payload = state.base_text(append=state.APPENDED_LINES).encode("utf-8")
    state.staged(project, "second.py", state.base_text(append=state.APPENDED_LINES))
    environment = dict(os.environ, PYTHONPATH=str(examples.SRC_DIR))
    environment.pop("ARBITE_SINK", None)

    racers = [
        subprocess.Popen(
            [
                sys.executable, "-m", "arbite.cli", "file", "write", BASE_PY,
                "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
                "--read-token", token, "--input", name,
            ],
            cwd=str(project), env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for name in ("base.py", "second.py")
    ]
    outcomes = [racer.communicate() for racer in racers]
    codes = sorted(racer.returncode for racer in racers)

    assert codes == [0, 5], [outcome[1] for outcome in outcomes]
    assert (project / BASE_PY).read_bytes() == payload
    assert state.receipt_count(project, kind) == 1, "one replacement, one receipt"
    assert state.spent_by(project, token, kind) is not None


# --- refusals that must change no bytes --------------------------------------


def test_a_moved_file_refuses_and_changes_nothing(tmp_path, kind):
    """The external-writer case: outcome 5, both versions named, and the other writer's bytes
    left exactly as they are (arbite reports drift, it does not undo somebody else's work)."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    state.external_edit(project)
    other = (project / BASE_PY).read_bytes()

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert (project / BASE_PY).read_bytes() == other
    assert state.receipt_count(project, kind) == 0
    assert state.spent_by(project, token, kind) is None


def test_a_closed_ticket_refuses_the_write_and_keeps_the_bytes(tmp_path, kind):
    """RC2 at the command: the close wins, no observer mutates under the old token."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    before = (project / BASE_PY).read_bytes()
    assert examples.run_cli(project, "close", HOLDER_TICKET, sink=kind).returncode == 0

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "is no longer current" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before
    assert state.receipt_count(project, kind) == 0


def test_a_path_another_attempt_holds_is_busy(tmp_path, kind):
    """Outcome 4, not 5: the bytes are not the caller's to plan against, so re-reading is the
    wrong advice -- the answer names the holder and nothing is written."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    state.discovery.put_claim(
        project, BASE_PY, state.RIVAL_TICKET, RIVAL, generation=1, sink_kind=kind
    )
    before = (project / BASE_PY).read_bytes()

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "held by" in proc.stderr and "no bytes were changed" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before


def test_an_unclaimed_path_is_an_error_naming_the_claim_to_take(tmp_path, kind):
    """A read authorises nothing: the refusal is exit 1 and hands back the claim command."""
    project = state.unclaimed_project(tmp_path, kind)
    token = state.token_for_unclaimed(project, kind)

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "(no_claim)" in proc.stderr and "a read does not authorize a write" in proc.stderr
    assert f"arbite file claim {BASE_PY} --ticket {HOLDER_TICKET} --attempt {HOLDER}" in proc.stderr
    assert state.receipt_count(project, kind) == 0


def test_generated_output_is_refused_by_policy(tmp_path, kind):
    """BY2 as a rule rather than as one path: a compiled suffix and a nested build directory
    are refused the same way, and the refusal happens before the payload is even read."""
    project = state.generated_project(tmp_path, kind)
    token = state.token_for_generated(project, kind)
    (project / "src" / "arbite" / "sinks" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (project / "src" / "arbite" / "sinks" / "__pycache__" / "base.cpython-314.pyc").write_bytes(b"\x00\x01")
    (project / "src" / "arbite" / "sinks" / "old.pyc").write_bytes(b"\x00\x01")
    state.staged(project, "payload.bin", b"new bytes")

    for path in (
        ".pytest_cache/v/cache/lastfailed",
        "src/arbite/sinks/__pycache__/base.cpython-314.pyc",
        "src/arbite/sinks/old.pyc",
        "src/arbite/build/thing.py",
    ):
        proc = write(project, path, token, payload="payload.bin", kind=kind)
        assert proc.returncode == 1, f"{path}: {proc.stdout}{proc.stderr}"
        assert "excluded by policy (generated or build output)" in proc.stderr

    assert state.receipt_count(project, kind) == 0


def test_the_policy_exclusion_rule_is_the_one_documented():
    """The exclusions are a fixed, readable list -- not `.gitignore`, not a guess."""
    from arbite.coordination.paths import policy_exclusion

    assert policy_exclusion("src/arbite/cli.py") is None
    assert policy_exclusion("build.py") is None, "a file named like a directory is not one"
    assert policy_exclusion("src/arbite/cli.py") is None
    assert policy_exclusion("dist/bundle.js") == "dist"
    assert policy_exclusion("node_modules/left-pad/index.js") == "node_modules"
    assert policy_exclusion("pkg.egg-info/PKG-INFO") == "pkg.egg-info"
    assert policy_exclusion("src/m.pyc") == "m.pyc"


# --- creation, binary and permissions ----------------------------------------


def test_a_write_creates_a_path_a_probe_found_absent(tmp_path):
    """Creation is the same operation with `absent` as the version it replaced.

    A read token cannot authorise it -- `file read` refuses a path that is not there, because
    there are no bytes to serve -- so the command records the probe itself: an observation of
    the absent path, taken under the claim the caller holds, which is what the write spends.
    Asserted on the file sink, which is the one that can record evidence (see the SQLite test
    at the end of this file)."""
    kind = "file"
    project = state.init_project(tmp_path, kind)
    state.put_absent_claim(project, "src/arbite/created.py", HOLDER_TICKET, HOLDER, sink_kind=kind)
    state.staged(project, "created.py", "created by tic-60c7\n")

    proc = examples.run_cli(
        project, "file", "write", "src/arbite/created.py", "--ticket", HOLDER_TICKET,
        "--attempt", HOLDER, "--input", "created.py", "--json", sink=kind,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    written = json.loads(proc.stdout)
    assert written["created"] is True and written["before"] is None
    assert written["after"]["lines"] == 1 and written["after"]["bytes"] == 20
    assert (project / "src/arbite/created.py").read_text() == "created by tic-60c7\n"
    before, after = state.recorded_versions(project, kind)
    assert before["src/arbite/created.py"] == coordination_records.ABSENT
    assert state.artifact_bytes(project, after["src/arbite/created.py"], kind) == b"created by tic-60c7\n"
    observation = state.store_for(project, kind).get_record("observation", written["token"]["id"])
    assert observation.digest == coordination_records.ABSENT, "the probe recorded absence"
    assert observation.claim_generation == 1 and observation.attempt_id == HOLDER
    assert observation.spent_by == written["receipt"], "the creation spent its own probe"


def test_creating_a_path_nobody_claimed_is_refused(tmp_path, kind):
    """The probe is taken under a claim, so a creation without one is the same refusal a
    write over existing bytes gets: the path is not the caller's."""
    project = state.init_project(tmp_path, kind)
    state.discovery.written(project, "src/arbite/existing.py", "# here, so the directory is\n")
    state.staged(project, "created.py", "mine\n")

    proc = examples.run_cli(
        project, "file", "write", "src/arbite/created.py", "--ticket", HOLDER_TICKET,
        "--attempt", HOLDER, "--input", "created.py", sink=kind,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "(no_claim)" in proc.stderr
    assert not (project / "src/arbite/created.py").exists()
    assert state.store_for(project, kind).records("observation") == [], "no probe was recorded"


def test_a_probe_cannot_overwrite_bytes_that_arrived_after_it(tmp_path, kind):
    """The overwrite this proxy exists to prevent: bytes arrived between the probe and the
    write, so the creation is refused rather than replacing them.

    The probe is written through the store because that is what a previous attempt's probe
    *is* -- a record of an absent path -- and the case under test is presenting it after
    somebody else has written the file."""
    project = state.init_project(tmp_path, kind)
    state.put_absent_claim(project, "src/arbite/raced.py", HOLDER_TICKET, HOLDER, sink_kind=kind)
    token = "op-beef"
    state.store_for(project, kind).put_record(
        coordination_records.ReadObservation(
            id=token,
            path="src/arbite/raced.py",
            digest=coordination_records.ABSENT,
            observed_at=coordination_records.utc_now(),
            attempt_id=HOLDER,
            claim_generation=1,
        )
    )
    state.staged(project, "raced.py", "mine\n")
    state.discovery.written(project, "src/arbite/raced.py", "somebody else's bytes\n")

    proc = write(project, "src/arbite/raced.py", token, payload="raced.py", kind=kind)

    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "was absent when you read it" in proc.stderr
    assert (project / "src/arbite/raced.py").read_text() == "somebody else's bytes\n"
    assert state.receipt_count(project, kind) == 0
    assert state.spent_by(project, token, kind) is None


def test_binary_bytes_round_trip_through_the_evidence(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """A byte payload is evidence like any other: the artifacts hold exactly the two versions,
    and the report says the receipt is not a text diff."""
    project = state.binary_project(tmp_path, kind)
    token = state.token_for_binary(project, kind)

    proc = examples.run_cli(
        project, "file", "write", state.ICON_PNG, "--ticket", state.RIVAL_TICKET,
        "--attempt", RIVAL, "--read-token", token, "--input", "icon.png", "--json", sink=kind,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["binary"] is True and payload["created"] is False
    assert payload["after"]["bytes"] == state.ICON_WRITTEN_BYTES
    assert payload["payload"] == {
        "name": "icon.png",
        "source": ".arbite/scratch/icon.png",
        "consumed": True,
    }
    before, after = state.recorded_versions(project, kind)
    assert state.artifact_bytes(project, before[state.ICON_PNG], kind) == state.icon_bytes(
        state.ICON_BYTES
    )
    assert state.artifact_bytes(project, after[state.ICON_PNG], kind) == state.icon_bytes(
        state.ICON_WRITTEN_BYTES
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are the promise here")
def test_a_write_preserves_the_files_permissions(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """`os.replace` would hand a 0600 file the umask's default; the replacement carries the
    mode instead, because a content change must not change who can read the file."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    target = project / BASE_PY
    os.chmod(target, 0o600)

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are the promise here")
def test_an_edit_preserves_the_files_permissions(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    target = project / BASE_PY
    os.chmod(target, 0o640)

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


# --- the edit batch rules ----------------------------------------------------


def test_an_edit_can_select_by_line_instead_of_occurrence(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """`line` and `occurrence` are two ways to say one place; both resolve to the same edit."""
    project = state.ambiguous_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    state.staged(
        project,
        "edits.json",
        json.dumps({"edits": [{"old": state.AMBIGUOUS_TEXT, "new": "Raises Conflict or Stale", "line": 366}]}),
    )

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "replace at line 366" in proc.stdout
    written = (project / BASE_PY).read_text()
    assert written == state.base_text().replace(
        "Raises Conflict, not StaleRead", "Raises Conflict or Stale, not StaleRead", 1
    ), "only the occurrence on line 366 changed"


def test_an_overlapping_batch_changes_nothing(tmp_path, kind):
    """Two selections that intersect have no defined result, so neither is applied."""
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    before = (project / BASE_PY).read_bytes()
    state.staged(
        project,
        "edits.json",
        json.dumps(
            {
                "edits": [
                    {"old": "The one write path", "new": "The single write path"},
                    {"old": "one write path for every kind", "new": "one entry point"},
                ]
            }
        ),
    )

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "overlaps edit 1" in proc.stderr and "no bytes were changed" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before
    assert state.receipt_count(project, kind) == 0


def test_an_absent_selection_changes_nothing(tmp_path, kind):
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    before = (project / BASE_PY).read_bytes()
    state.staged(
        project,
        "edits.json",
        json.dumps({"edits": [{"old": "text that is not in this file", "new": "anything"}]}),
    )

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "does not occur in" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before
    assert state.receipt_count(project, kind) == 0
    assert not list((project / "src" / "arbite" / "sinks").glob("*.partial")), "no staging left"


def test_a_batch_that_fails_after_a_valid_edit_still_changes_nothing(tmp_path, kind):
    """The "no partial write" guarantee, at the file: the first edit applies to nothing at all
    once the second cannot select a place."""
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    before = (project / BASE_PY).read_bytes()
    state.staged(
        project,
        "edits.json",
        json.dumps(
            {
                "edits": [
                    {"old": "The one write path", "new": "The single write path"},
                    {"old": "Raises Conflict", "new": "Raises Conflict or StaleRead"},
                ]
            }
        ),
    )

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "occurs 3 times" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before, "the valid first edit was not applied"
    assert state.receipt_count(project, kind) == 0
    assert state.spent_by(project, token, kind) is None


@pytest.mark.parametrize(
    "document",
    [
        "not json at all",
        json.dumps({"edits": []}),
        json.dumps({"edits": [{"old": "x"}]}),
        json.dumps({"edits": [{"old": "", "new": "x"}]}),
        json.dumps({"edits": [{"old": "x", "new": "y", "occurrence": 0}]}),
        json.dumps({"edits": [{"old": "x", "new": "y", "occurrence": 1, "line": 2}]}),
        json.dumps({"edits": [{"old": "x", "new": "y", "sort": "line"}]}),
    ],
)
def test_a_malformed_batch_is_refused_without_touching_the_file(tmp_path, kind, document):
    """Every shape a payload can get wrong is refused as bad input, before the store is
    consulted, and the file is byte-for-byte what it was."""
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)
    before = (project / BASE_PY).read_bytes()
    state.staged(project, "edits.json", document)

    proc = edit(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "error:" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before
    assert state.receipt_count(project, kind) == 0


def test_the_batch_rules_are_usable_without_a_store():
    """The batch layer is text in, text out: the rules are asserted directly, so a failure
    there is not misread as a store failure."""
    text = "alpha\nbeta\ngamma\nbeta\n"
    batch = edit_batches.parse_batch(
        json.dumps({"edits": [{"old": "beta", "new": "delta", "occurrence": 2}]}).encode()
    )
    applied = edit_batches.apply_batch(text, batch, "x.py", "arbite file read x.py")
    assert applied.text == "alpha\nbeta\ngamma\ndelta\n"
    assert applied.applied[0].line == 4

    with pytest.raises(EditRefused) as ambiguous:
        edit_batches.apply_batch(
            text, edit_batches.parse_batch(b'{"edits": [{"old": "beta", "new": "delta"}]}'), "x.py", "read"
        )
    assert "occurs 2 times (lines 2, 4)" in str(ambiguous.value)
    assert "no bytes were changed" in str(ambiguous.value)


def test_an_edit_needs_a_text_file_and_an_existing_path(tmp_path):
    """The two shapes an edit cannot express: bytes with no lines, and a path that is not
    there (which is what `file write` is for)."""
    kind = "file"
    project = state.binary_project(tmp_path, kind)
    token = state.token_for_binary(project, kind)
    state.staged(project, "edits.json", json.dumps({"edits": [{"old": "x", "new": "y"}]}))

    binary = edit(project, state.ICON_PNG, token, kind=kind)
    assert binary.returncode == 1, binary.stdout + binary.stderr
    assert "not UTF-8 text" in binary.stderr

    second = tmp_path / "second"
    second.mkdir()
    project = state.init_project(second, kind)
    state.put_absent_claim(project, "src/arbite/missing.py", HOLDER_TICKET, HOLDER, sink_kind=kind)
    state.staged(project, "edits.json", json.dumps({"edits": [{"old": "x", "new": "y"}]}))
    token = "op-0000"

    missing = edit(project, "src/arbite/missing.py", token, kind=kind)
    assert missing.returncode == 1, missing.stdout + missing.stderr
    assert "no such path" in missing.stderr
    assert not (project / "src/arbite/missing.py").exists()


# --- the command's own input --------------------------------------------------


def test_a_payload_from_outside_the_project_is_refused(tmp_path, kind):
    """SC5's rule, at the command that reads payloads: only the scratch area and stdin."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    outside = tmp_path / "outside.py"
    outside.write_text("elsewhere\n")

    proc = write(project, BASE_PY, token, payload=str(outside), kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "is outside the project" in proc.stderr
    assert "no bytes were changed" not in proc.stderr or state.receipt_count(project, kind) == 0
    assert (project / BASE_PY).read_bytes() == state.base_text().encode("utf-8")


def test_an_escaping_payload_name_is_refused(tmp_path, kind):
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)

    proc = write(project, BASE_PY, token, payload="../base.py", kind=kind)

    assert proc.returncode == 1
    assert "is outside the project" in proc.stderr


def test_a_change_to_existing_bytes_without_a_token_is_refused(tmp_path, kind):
    """The read half of the authorisation: a claim says whose the path is, and only a read
    taken under it says the bytes may change. The refusal is exit 1 and names the read to
    run -- a caller cannot act on a stale outcome it never had a token for."""
    project = state.write_project(tmp_path, kind)
    missing_write = examples.run_cli(
        project, "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--input", "base.py", sink=kind,
    )
    assert missing_write.returncode == 1, missing_write.stdout + missing_write.stderr
    assert "--read-token is required" in missing_write.stderr
    assert f"'{state.read_command_for()}'" in missing_write.stderr

    state.staged(project, "edits.json", json.dumps({"edits": [{"old": "x", "new": "y"}]}))
    missing_edit = examples.run_cli(
        project, "file", "edit", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--edits", "edits.json", sink=kind,
    )
    assert missing_edit.returncode == 1, missing_edit.stdout + missing_edit.stderr
    assert "--read-token is required" in missing_edit.stderr

    assert (project / BASE_PY).read_bytes() == state.base_text().encode("utf-8")
    assert state.receipt_count(project, kind) == 0



def test_a_missing_payload_file_is_refused_before_anything_is_checked(tmp_path, kind):
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    (project / ".arbite" / "scratch" / "base.py").unlink()

    proc = write(project, BASE_PY, token, payload="base.py", kind=kind)

    assert proc.returncode == 1
    assert "no payload named 'base.py'" in proc.stderr
    assert state.receipt_count(project, kind) == 0


def test_a_write_piped_on_stdin_reports_what_arrived_and_records_it(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """`--input -`: no staged copy, the same evidence, and the shape of the bytes named."""
    project = state.init_project(tmp_path, kind)
    state.discovery.written(project, BASE_PY, state.base_text())
    state.discovery.put_claim(project, BASE_PY, HOLDER_TICKET, HOLDER, generation=1, sink_kind=kind)
    token = state.read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, kind)
    payload = state.base_text(append=state.APPENDED_LINES)

    proc = examples.run_cli(
        project, "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "-", sink=kind, stdin=payload,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("(payload read from stdin: 588 lines, ")
    assert (project / BASE_PY).read_text() == payload
    assert list((project / ".arbite" / "scratch").glob("*")) == [], "nothing to consume"
    before, after = state.recorded_versions(project, kind)
    assert state.artifact_bytes(project, after[BASE_PY], kind) == payload.encode("utf-8")


def test_the_json_payload_carries_the_facts_the_text_prints(tmp_path, project_kind):
    kind = project_kind  # a sink that can record the evidence (see the SQLite test below)
    """`--json` is the same story in fields: the token it spent, the versions, the receipt."""
    project = state.edits_project(tmp_path, kind)
    token = state.token_for_edits(project, kind)

    proc = edit(project, BASE_PY, token, kind=kind, *("--json",))
    # the flag order is the command's business, so the call above falls back to text
    if proc.returncode != 0:
        proc = examples.run_cli(
            project, "file", "edit", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
            "--read-token", token, "--edits", "edits.json", "--json", sink=kind,
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["path"] == BASE_PY
    assert payload["token"] == {"id": token, "spent_by": payload["receipt"]}
    assert payload["claim_generation"] == 2
    assert payload["before"]["lines"] == state.EDITS_LINES
    assert payload["after"]["lines"] == state.EDITS_LINES + 3
    assert [one["line"] for one in payload["edits"]] == [state.IN_PLACE_LINE, state.AMBIGUOUS_LINES[1]]
    assert payload["payload"] == {
        "name": "edits.json",
        "source": ".arbite/scratch/edits.json",
        "consumed": True,
    }


def test_help_text_names_only_commands_that_exist():
    """A refusal may not point at a command this build does not have: the hints here name
    `file read`, `file claim`, `file write`, `file edit`, `file remove`, `file rename`,
    `scratch list`, `scratch clear`, `reopen` and `close`, all of which exist.

    The second list is the same rule in the other direction, and it is why the check is worth
    having: a slice that has not landed must not be named by anything output today. tic-74e2
    landed `file remove` and `file rename`, tic-95c0 landed `scratch list|clear`, so they
    moved to the first list; the receipt and change views (tic-7c42) are still ahead."""
    from arbite import cli

    for path in (
        "file read", "file claim", "file write", "file edit", "file remove", "file rename",
        "scratch list", "scratch clear", "reopen", "close",
    ):
        assert cli.knows_command(path), path
    for path in ("receipt", "changes"):
        assert not cli.knows_command(path), f"{path} is another slice's"


def test_the_schema_revision_was_raised_for_the_spent_token_field():
    """Revision 2 exists because a token now records the operation that spent it; a store
    written before it is refused by name and needs tic-008f's migration pass."""
    assert coordination_records.COORDINATION_SCHEMA_REVISION == 2
    assert "spent_by" in {
        spec.name for spec in __import__("dataclasses").fields(coordination_records.ReadObservation)
    }
    with pytest.raises(coordination_records.RecordError) as refusal:
        coordination_records.ReadObservation.from_dict(
            {
                "record": "observation",
                "id": "op-0001",
                "path": "src/arbite/cli.py",
                "digest": "sha256:" + "0" * 64,
                "observed_at": "2026-09-21T13:12:04Z",
                "schema_revision": 1,
            }
        )
    assert "schema revision 1" in str(refusal.value)


def test_the_sqlite_backend_refuses_a_mutation_before_changing_bytes(tmp_path):
    """The sink-side half of "fail before modifying bytes", on the sink that has the limit.

    A receipt whose before and after bytes cannot be stored is not a receipt: the engine
    refuses the operation by name (and names the ticket that decides how a database holds
    content) *before* it stages anything, so the file is untouched and no receipt is written.
    This is the deliberate state of the SQLite coordination backend, not a failure of the
    write command -- and it is asserted, because "refuses honestly" is the property this
    slice must preserve on both sinks."""
    kind = "sqlite"
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    before = (project / BASE_PY).read_bytes()

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert EVIDENCE_UNAVAILABLE in proc.stderr and "tic-7c42" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert (project / BASE_PY).read_bytes() == before
    assert state.receipt_count(project, kind) == 0
    assert state.spent_by(project, token, kind) is None
    assert (project / ".arbite" / "scratch" / "base.py").exists(), "the payload is still there"


def test_a_refusal_never_leaves_a_staged_copy_behind(tmp_path, kind):
    """Staging is the engine's, and a refused operation never reaches it: the directory the
    target lives in holds no leftover of an operation that did not happen."""
    project = state.write_project(tmp_path, kind)
    token = state.token_for_write(project, kind)
    state.external_edit(project)
    state.staged(project, "base.py", state.base_text(append=state.APPENDED_LINES))

    proc = write(project, BASE_PY, token, kind=kind)

    assert proc.returncode == 5
    leftovers = [
        path.name
        for path in (project / "src" / "arbite" / "sinks").iterdir()
        if path.name not in {"base.py", "file.py", "sqlite.py", "old.py", "__init__.py"}
    ]
    assert leftovers == [], leftovers
