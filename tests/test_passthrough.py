"""What passthrough observation promises, on both sinks.

`test_passthrough_examples.py` asserts the frozen PC1, PC5 and PC6 transcripts. This file
asserts the guarantees those transcripts stand for, and the ones a transcript cannot
state:

- **The wrapped command's exit code survives untouched**, including 0-5, which the rest
  of arbite reserves for its own vocabulary -- a tool really can exit 4.
- **A refusal never runs the command.** Each refusal returns 125, 126 or 127, is decided
  before any process starts, leaves no `passthrough.exec` event behind and changes no
  bytes, and says so (`command did not run`, and `ran: false` in JSON).
- **Observation is not exclusivity.** A change made while a *different* attempt holds the
  claim is reported as an observed, attributed change: the receipt records claim
  generation 0 rather than the holder's, the holder is named, and the run's payload says
  which paths were unclaimed -- the data a decision about mandating passthrough needs.
- **The evidence is real.** Every changed path gets its own receipt with both digests and
  the version the command left, the run gets one `passthrough.exec` event naming the tool,
  the argv hash, the exit code and the duration, and `arbite receipt` and `arbite changes`
  read the result back.
- **The manifest is the managed paths.** A change to a ticket or a scratchpad is observed;
  a change inside the coordination tree, scratch, the project config, `.git` or generated
  output is not, because a mutation there is refused too.
- **Bounds are bounds.** Output capture is bounded by lines and bytes and the command's
  output is stored nowhere, and a process the command leaves holding its pipes does not
  make arbite wait for it.

Both sinks run the recording tests: a receipt written by one backend and unreadable by the
other would be two different products.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

import examples
import passthrough_state as state
from arbite.coordination import passthrough as coordination_passthrough
from arbite.coordination import writes as coordination_writes
from arbite.coordination.records import digest_bytes

HOLDER_TICKET = state.HOLDER_TICKET
HOLDER = state.HOLDER
RIVAL_TICKET = state.RIVAL_TICKET
RIVAL = state.RIVAL
FILE_PY = state.FILE_PY
O_EXCL = "s/O_EXCL/O_EXCL|O_NOFOLLOW/"


def passthrough(
    project, *command, ticket=HOLDER_TICKET, attempt=HOLDER, shell=False, kind="file", expect=0
):
    """One `arbite cmd`, as the CLI is really invoked: argv, or one shell line.

    `expect` is 0 for a run that works; the exit-code tests pass the code the wrapped
    command returns, because the point there is that arbite does not touch it."""
    args = ["cmd"]
    if ticket:
        args += ["--ticket", ticket, "--attempt", attempt]
    if shell:
        args += ["--shell"]
    args += ["--", *command]
    return state.run(project, *args, sink_kind=kind, expect=expect)


# --- exit codes are the tool's ---------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5])
def test_the_wrapped_exit_code_survives_untouched(tmp_path, kind, code):
    """0-5 are the vocabulary's, and a tool that returns one of them still returned it.

    This is the collision the passthrough exit codes exist for: `arbite cmd -- sh -c
    'exit 4'` must read as the tool's 4, not as "busy", and arbite must not translate a
    failure into one of its own codes."""
    project = state.project(tmp_path, kind)
    proc = passthrough(project, "sh", "-c", f"exit {code}", kind=kind, expect=code)

    assert proc.returncode == code
    assert f"exit: {code} (" in proc.stdout
    execs = state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind)
    assert [event.payload["exit_code"] for event in execs] == [code]


def test_a_tool_killed_by_a_signal_reports_the_shell_convention(tmp_path):
    """A process with no exit code of its own gets the shell's 128+signal, not a negative
    number that a caller could mistake for one."""
    project = state.project(tmp_path)
    proc = passthrough(project, "sh", "-c", "kill -TERM $$", expect=128 + 15)

    assert proc.returncode == 128 + 15
    assert state.events(project, coordination_passthrough.EXEC_KIND)[0].payload["exit_code"] == (
        128 + 15
    )


# --- refusals: decided before anything runs --------------------------------------


def _refusals(project, kind="file"):
    """Every refusal this slice makes, as `(label, exit code, run)`."""
    return [
        (
            "shell syntax",
            126,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--",
                "sed", "-i", "s/a/b/", state.CLI_PY, ">", "/tmp/arbite-refused.txt",
                sink_kind=kind, expect=126,
            ),
        ),
        (
            "interactive",
            126,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--",
                "vim", state.CLI_PY, sink_kind=kind, expect=126,
            ),
        ),
        (
            "watcher",
            126,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--",
                "tail", "-f", state.CLI_PY, sink_kind=kind, expect=126,
            ),
        ),
        (
            "background (shell)",
            126,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--shell",
                "--", f"touch {state.AGENT_FILE} &", sink_kind=kind, expect=126,
            ),
        ),
        (
            "not on PATH",
            127,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--",
                "definitely-not-a-real-tool", sink_kind=kind, expect=127,
            ),
        ),
        (
            "guarded mode (C14)",
            125,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
                "--claim", FILE_PY, "--", "true", sink_kind=kind, expect=125,
            ),
        ),
        (
            "no coordination store",
            125,
            lambda: _run_without_a_store(project.parent, kind),
        ),
        (
            "attempt pair",
            126,
            lambda: state.run(
                project, "cmd", "--ticket", HOLDER_TICKET, "--", "true",
                sink_kind=kind, expect=126,
            ),
        ),
        (
            "no command",
            126,
            lambda: state.run(project, "cmd", sink_kind=kind, expect=126),
        ),
    ]


def _run_without_a_store(parent, kind):
    """A project arbite was never initialised in: the run cannot be recorded, so it is
    refused rather than run silently unrecorded."""
    import lifecycle_state as lifecycle

    bare = parent / "no-store"
    bare.mkdir(exist_ok=True)
    (bare / ".arbite").mkdir(exist_ok=True)
    (bare / ".arbite" / "project.yaml").write_text(f"sink: {kind}\n", encoding="utf-8")
    return examples.run_cli(bare, "cmd", "--", "touch", "made.txt", sink=kind)


def test_every_refusal_says_the_command_did_not_run(tmp_path, kind):
    """The refusals, their codes, and the proof: no event, no receipt, no bytes moved.

    Each command in the list *would* have changed the workspace (a sed, an editor, a
    `touch`), so the assertions after the refusal are not ceremonial: the manifest of the
    paths it aimed at is untouched and the store holds no execution event."""
    project = state.project(tmp_path, kind)
    before = _manifest_texts(project)
    for label, code, run in _refusals(project, kind):
        proc = run()
        assert proc.returncode == code, label
        assert proc.stdout == "", f"{label} wrote to stdout"
        assert "error:" in proc.stderr, label
        assert state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind) == []
        assert state.receipts(project, sink_kind=kind) == []
    assert _manifest_texts(project) == before, "a refused command changed a file"


def test_a_refusal_says_ran_false_in_json(tmp_path):
    """`command did not run` is a fact, so it is in the branchable form too -- including
    for the two refusals whose frozen text cannot carry the sentence."""
    project = state.project(tmp_path)
    proc = state.run(
        project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--json", "--", "vim",
        state.CLI_PY, sink_kind="file", expect=126,
    )
    payload = json.loads(proc.stdout)

    assert payload["ran"] is False and payload["exit_code"] == 126
    assert payload["mode"] == "observed"
    assert payload["error"] == coordination_passthrough.INTERACTIVE_MESSAGE
    assert payload["reason"] == coordination_passthrough.REASON_INTERACTIVE


def test_a_stale_attempt_is_refused_before_running(tmp_path, kind):
    """An attempt the store does not have cannot attribute a run, so nothing runs: the
    lifecycle's own refusal, at 125, with 5 left free for a wrapped tool."""
    project = state.project(tmp_path, kind)
    proc = state.run(
        project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", "att-0000", "--", "true",
        sink_kind=kind, expect=125,
    )

    assert "att-0000 does not exist in this store" in proc.stderr
    assert coordination_passthrough.NO_RUN in proc.stderr
    assert state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind) == []


def test_shell_syntax_is_refused_only_when_a_token_is_syntax(tmp_path):
    """A `|` inside an argument is a character the program asked for, and an operator as
    its own token is syntax the caller expected a shell to act on."""
    project = state.project(tmp_path)
    ran = passthrough(project, "sed", "-i", O_EXCL, FILE_PY)
    assert ran.returncode == 0, ran.stderr

    refused = state.run(
        project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--", "echo", "hi",
        "|", "cat", sink_kind="file", expect=126,
    )
    assert "'|' style shell syntax needs '--shell'" in refused.stderr
    assert state.receipts(project)[0].paths == [FILE_PY], "the refusal added no receipt"


# --- observation, attribution and the data a later decision needs ----------------


def test_a_change_outside_any_claim_is_observed_and_attributed(tmp_path, kind):
    """A passthrough run of one ticket changes a path another attempt holds.

    Nothing authorised that change and nothing claims it: the receipt records generation
    0 (not the holder's), the JSON change names the holder and says `claimed: false`, and
    the run's payload lists the path as unclaimed. The bytes are recorded and left alone
    -- arbite does not undo a command it did not perform."""
    project = state.project(tmp_path, kind)
    state.claim(project, state.FILE_PY, RIVAL_TICKET, RIVAL, sink_kind=kind)

    proc = passthrough(project, "sed", "-i", O_EXCL, FILE_PY, kind=kind)

    assert "mode: observed (no exclusivity claimed)" in proc.stdout
    assert "NOT claimed" not in proc.stdout, "observed mode has no claimed set to report against"

    receipt = state.receipts(project, sink_kind=kind)[0]
    assert receipt.claim_generation == 0
    assert receipt.ticket_id == HOLDER_TICKET and receipt.attempt_id == HOLDER

    payload = state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind)[0].payload
    assert payload["unclaimed"] == [FILE_PY]
    assert payload["claims"][FILE_PY] == {
        "mine": False,
        "generation": 1,
        "holder": f"{RIVAL_TICKET}/{RIVAL}",
    }
    assert payload["exclusive"] is False and payload["mode"] == "observed"


def test_the_json_report_carries_the_same_facts_as_the_text(tmp_path, kind):
    """Text is the primary interface and JSON is the branchable form of the same facts.

    The one fact the text states as a promise is the exclusivity seam: the frozen PC1 hint
    names `--claim` as the way to get exclusivity, so the JSON says out loud that guarded
    mode is not available yet (`exclusivity.available: false`) rather than letting a
    consumer read the hint as something arbite already does.
    """
    as_text = tmp_path / "text"
    as_text.mkdir()
    text = passthrough(state.project(as_text, kind), "sed", "-i", O_EXCL, FILE_PY, kind=kind)
    as_json = tmp_path / "json"
    as_json.mkdir()
    payload = json.loads(
        state.run(
            state.project(as_json, kind), "cmd", "--ticket", HOLDER_TICKET, "--attempt",
            HOLDER, "--json", "--", "sed", "-i", O_EXCL, FILE_PY, sink_kind=kind,
        ).stdout
    )

    assert payload["cmdline"] == text.stdout.splitlines()[0][len("arbite cmd: "):]
    assert f"exit: {payload['exit_code']} (" in text.stdout
    assert payload["mode"] == "observed" and "mode: observed (no exclusivity claimed)" in (
        text.stdout
    )
    assert payload["exclusive"] is False
    row = next(line for line in text.stdout.splitlines() if line.strip().startswith("M "))
    assert payload["changed"][0]["path"] in row and payload["changed"][0]["detail"] in row
    assert f"tool: {payload['tool']}" in text.stdout
    assert payload["event"] == coordination_passthrough.EXEC_KIND
    assert f"event: {payload['event']}" in text.stdout
    for key in ("ticket", "attempt", "actor"):
        assert payload[key] and payload[key] in text.stdout
    assert payload["ran"] is True
    assert payload["exclusivity"] == {
        "claimed": [],
        "available": False,
        "hint": f"arbite cmd --claim {FILE_PY} -- <command>",
        "reason": coordination_passthrough.REASON_GUARDED,
    }


def test_the_run_is_recorded_with_both_events_and_the_argv_hash(tmp_path, kind):
    """One receipt per changed path, one `passthrough.changed` per receipt, one
    `passthrough.exec` for the run -- and the hash covers the argv, not the shell line."""
    project = state.project(tmp_path, kind)
    proc = passthrough(project, "sed", "-i", O_EXCL, FILE_PY, kind=kind)
    operation = state.receipts(project, sink_kind=kind)[0].id

    changed = state.events(project, coordination_passthrough.CHANGED_KIND, sink_kind=kind)
    execs = state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind)
    assert [event.subject for event in changed] == [FILE_PY]
    assert [event.operation_id for event in changed] == [operation]
    assert changed[0].category == "file" and execs[0].category == "passthrough"

    payload = execs[0].payload
    assert payload["argv"] == ["sed", "-i", O_EXCL, FILE_PY]
    assert payload["argv_hash"] == digest_bytes(
        ("\0".join(["sed", "-i", O_EXCL, FILE_PY])).encode("utf-8")
    )
    assert payload["tool"] == "sed" and payload["shell"] is False
    assert payload["receipts"] == [operation] and payload["changed"] == [FILE_PY]
    assert payload["duration_ms"] >= 0 and payload["exit_code"] == 0
    assert "exit " in execs[0].result
    assert operation in proc.stdout


def test_the_argv_hash_is_the_commands_argv_not_arbites(tmp_path, kind):
    """A shell run hashes the line, and says `sh` is the tool, so the stream can tell the
    two modes apart without guessing from the command's name."""
    project = state.project(tmp_path, kind)
    line = "true"
    state.run(
        project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--shell", "--",
        line, sink_kind=kind,
    )
    payload = state.events(project, coordination_passthrough.EXEC_KIND, sink_kind=kind)[0].payload

    assert payload["tool"] == "sh" and payload["shell"] is True
    assert payload["argv"] == [line]
    assert payload["argv_hash"] == digest_bytes(("\0".join([line]) + "\0shell").encode("utf-8"))


def test_creations_and_removals_are_recorded_with_an_absent_endpoint(tmp_path, kind):
    """A create and a remove are the two changes a digest pair cannot describe, so both
    rows say what happened and the receipts record `absent` on the side that had nothing."""
    project = state.project(tmp_path, kind)
    new_file = "src/arbite/created_by_a_tool.py"
    passthrough(project, "sh", "-c", f"printf 'created\\n' > {new_file}", kind=kind)
    passthrough(project, "rm", new_file, kind=kind)

    created, removed = state.receipts_in_log_order(project, sink_kind=kind)
    assert created.paths == [new_file] and created.after[new_file] != "absent"
    assert created.before[new_file] == "absent"
    assert removed.before[new_file] == created.after[new_file]
    assert removed.after[new_file] == "absent"

    rows = state.run(project, "changes", HOLDER_TICKET, "--all", sink_kind=kind).stdout
    assert f"A {new_file}" in rows and f"D {new_file}" in rows
    assert "created" in rows and "removed" in rows


def test_a_change_to_a_document_is_observed_and_one_to_state_is_not(tmp_path, kind):
    """The manifest is the managed paths: documents under `.arbite/` are content, while
    runtime state, the project config and generated output are not -- the same rule a
    mutation runs into."""
    project = state.project(tmp_path, kind)
    state.write(project, state.AGENT_FILE, "scratchpad\n")
    state.write(project, ".arbite/scratch/left.txt", "x\n")
    state.write(project, state.CACHE_FILE, "{}\n")
    state.write(project, ".arbite/project.yaml", f"sink: {kind}\n")

    passthrough(
        project,
        "sh",
        "-c",
        (
            f"printf 'observed\\n' >> {state.AGENT_FILE};"
            f" printf 'x\\n' >> .arbite/scratch/left.txt;"
            f" printf 'x\\n' >> {state.CACHE_FILE};"
            f" printf 'x\\n' >> .arbite/project.yaml"
        ),
        kind=kind,
    )

    assert [receipt.paths for receipt in state.receipts(project, sink_kind=kind)] == [
        [state.AGENT_FILE]
    ]


# --- bounds, and what arbite does not wait for -----------------------------------


def test_output_capture_is_bounded_and_the_output_is_never_stored(tmp_path, kind):
    """Two hundred lines in, forty lines plus a truncation note out, and nothing kept.

    A read-only run changes no path, so it is an execution event and *only* that: no
    receipt, no artifact, and nowhere for the prose to live -- which is the difference
    between a captured stream and evidence."""
    project = state.project(tmp_path, kind)
    proc = passthrough(project, "seq", "1", "200", kind=kind)

    assert "stdout truncated: 40 of 200 lines shown (the command's output is captured" in (
        proc.stdout
    )
    assert "1\n" in proc.stdout and "40\n" in proc.stdout and "41\n" not in proc.stdout
    assert state.receipts(project, sink_kind=kind) == []
    assert state.store_for(project, kind).records("artifact") == []
    assert [event.kind for event in state.events(
        project, coordination_passthrough.EXEC_KIND, sink_kind=kind
    )] == [coordination_passthrough.EXEC_KIND]

    payload = json.loads(
        state.run(project, "cmd", "--json", "--", "seq", "1", "200", sink_kind=kind).stdout
    )
    assert payload["output"]["stdout"]["truncated"] is True
    assert payload["output"]["stdout"]["lines"] == 200
    assert payload["output"]["stdout"]["shown"] == coordination_passthrough.CAPTURE_LINES
    assert payload["output"]["stdout"]["bytes"] >= 200


def test_a_byte_heavy_command_is_bounded_by_bytes_too(tmp_path, kind):
    """One enormous line is cut by the byte bound rather than buffered whole, and the
    incomplete tail is dropped instead of printed as if the command had written it."""
    project = state.project(tmp_path, kind)
    proc = state.run(
        project,
        "cmd",
        "--json",
        "--shell",
        "--",
        "printf 'x%.0s' $(seq 1 20000)",
        sink_kind=kind,
    )
    payload = json.loads(proc.stdout)

    stdout = payload["output"]["stdout"]
    assert stdout["truncated"] is True
    assert stdout["bytes"] == 20000
    assert len(stdout["text"]) <= coordination_passthrough.CAPTURE_BYTES


def test_a_process_the_command_leaves_holding_its_pipes_does_not_delay_arbite(tmp_path):
    """A background child inherits the pipes, so the stream is still open after the
    command exits. arbite is one-shot: it stops waiting, says so, and returns."""
    project = state.project(tmp_path)
    started = time.monotonic()
    proc = passthrough(project, "sh", "-c", "sleep 5 & echo forked")

    assert time.monotonic() - started < 5
    assert proc.returncode == 0
    assert "was still open when the command exited" in proc.stdout
    assert "forked" in proc.stdout


# --- the diff agrees with the engine --------------------------------------------


@pytest.mark.parametrize(
    "before,after",
    [
        ("one\ntwo\n", "one\nTWO\n"),
        ("one\n", "one\ntwo\nthree\n"),
        ("a\nb\nc\n", "c\nb\na\n"),
        ("", "fresh\n"),
    ],
)
def test_the_observed_line_delta_is_the_engines_line_delta(before, after):
    """`+N -M` is computed from line identities in the manifest, because the version the
    command replaced no longer exists to diff against. The numbers must be the ones the
    mutation engine's own line diff produces, or the same change would read two ways."""
    entry_before = coordination_passthrough.ManifestEntry(
        digest=digest_bytes(before.encode()), size=len(before), lines=_identities(before)
    )
    entry_after = coordination_passthrough.ManifestEntry(
        digest=digest_bytes(after.encode()), size=len(after), lines=_identities(after)
    )

    assert coordination_passthrough.observed_line_delta(entry_before, entry_after) == (
        coordination_writes.line_delta(before.encode(), after.encode())
    )


def test_a_binary_change_reports_sizes_rather_than_a_line_count(tmp_path, kind):
    """A line delta needs both sides read as text; bytes that are not print their sizes
    instead of a line count nobody could reproduce."""
    project = state.project(tmp_path, kind)
    blob = "assets/blob.bin"
    path = project / blob
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x01\x02\x03\xff\xfe" * 8)

    proc = passthrough(
        project, "sh", "-c", f"printf 'abcdefghijklmnopqrstuvwxyz' > {blob}", kind=kind
    )

    assert f"M {blob}" in proc.stdout
    assert "48 -> 26 bytes (binary)" in proc.stdout
    assert state.receipts(project, sink_kind=kind)[0].paths == [blob]
    assert blob in state.run(project, "changes", HOLDER_TICKET, sink_kind=kind).stdout


# --- the helpers this file needs -------------------------------------------------


def _manifest_texts(project) -> dict:
    """`path -> bytes` for the files a refusal could have touched, read straight off disk."""
    texts = {}
    for relative in (state.CLI_PY, state.FILE_PY, state.AGENT_FILE):
        path = project / relative
        if path.exists():
            texts[relative] = path.read_bytes()
    return texts


def _identities(text: str) -> tuple:
    return tuple(hash(line) for line in text.splitlines())
