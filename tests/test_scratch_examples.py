"""The frozen scratch transcripts this slice owns: SC1-SC5 and DR3.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command, exit code,
stream and rows -- with ids, times, paths and digests normalised on both sides (`examples.py`),
on **both** sinks: a payload is a file in the project whichever store holds the records, and a
mutation reads it the same way either way. `scratch_state.py` builds the worlds the blocks
describe, using the real commands wherever a command is supposed to produce the state.

SC1 and SC4 each hold two fences for one narrative, so each fence is asserted against the world
its own numbers describe: SC1's list shows a `4.1 KiB` payload while its write turns a 570-line
file into a 588-line one, which no single staged file can be.

Beyond the transcripts, the invariants the ticket's validation plan names are asserted directly,
because a frozen block cannot state them: `--keep`, what a refusal does and does not say about
the payload, stdin leaving nothing behind, a clear refusing to touch a file outside the area,
and scratch staying invisible to discovery, claims and the doctor's exit code.
"""

from __future__ import annotations

import json

import examples
import scratch_state as state
import writes_state as writes

BASE_PY = writes.BASE_PY
HOLDER = writes.HOLDER
HOLDER_TICKET = writes.HOLDER_TICKET


def _fence(scenario_id: str, index: int):
    """One fenced transcript of a block that holds more than one (`examples.py` reads the
    first for `scenario_block`; SC1 and SC4 hold a second)."""
    return examples.scenario_from_block(examples.fenced_blocks(scenario_id)[index], scenario_id)


# --- SC1: a payload is consumed on success -------------------------------------


def test_SC1_a_payload_is_consumed_on_success(tmp_path, kind):
    """Both halves of SC1: the list that shows what is staged, and the write that clears it.

    The list must name the payload's size and age *and* the agent the store can name; the
    write must report the consumed payload so the caller knows the bytes now live in the
    receipt. The two halves are asserted against the two worlds their own numbers describe:
    the listing on both sinks, and the write on the file sink, whose transcript the block
    is (both sinks record a successful mutation since tic-7c42, and the cross-sink evidence
    round trips are asserted in test_change_evidence.py)."""
    for_list = tmp_path / "list"
    for_list.mkdir()
    listed = examples.assert_scenario(_fence("SC1", 0), state.transport(for_list, kind))
    assert (for_list / "project" / ".arbite" / "scratch" / "base.py").exists(), "listed, not moved"

    for_write = tmp_path / "write"
    for_write.mkdir()
    project = state.consumed(for_write)
    token = writes.token_for_write(project)
    written = examples.assert_scenario(
        examples.with_token(_fence("SC1", 1), token), project
    )

    assert "consumed and cleared" in written
    assert not state.payload_path(project).exists(), "success consumes the payload"
    assert "570 -> 588 lines  +18 -0" in written
    assert listed.startswith("1 file in .arbite/scratch/:")


def test_SC1_the_row_names_the_stores_last_active_attempt(tmp_path, kind):
    """The attribution is a fact about the store, not a decoration: the row names the one
    attempt this workspace has, and a store that names none says so instead."""
    project = state.transport(tmp_path, kind)
    output = examples.run_cli(project, "scratch", "list", sink=kind).stdout

    assert "(agent claude.opus.001)" in output

    empty = tmp_path / "no-attempts"
    empty.mkdir()
    bare = state.initialise_without_attempts(empty, kind)
    assert state.staged_payload(bare).exists()
    assert "(no attempt recorded)" in examples.run_cli(bare, "scratch", "list", sink=kind).stdout


# --- SC2: a payload survives a failure -----------------------------------------


def test_SC2_a_payload_survives_a_failure(tmp_path, kind):
    """The refusal of a write whose file moved: both digests, no bytes changed, and the
    payload the caller may re-apply -- which is the asymmetry that keeps a recoverable
    refusal cheap. The block is asserted byte for byte, which is what makes that a promise."""
    project = state.refused(tmp_path, kind)
    token = state.rival_token(project, kind)
    writes.external_edit(project)
    before = (project / BASE_PY).read_bytes()

    output = examples.assert_scenario(examples.with_token(examples.scenario_block("SC2"), token), project)

    assert "kept, so you can re-apply without re-sending the file" in output
    assert (project / BASE_PY).read_bytes() == before, "a refused write changed no bytes"
    assert state.payload_path(project).exists(), "the payload is still there to re-apply"
    assert state.receipt_count(project, kind) == 0, "a refusal records no receipt"


def test_SC2_the_payload_is_reusable_after_the_fresh_read_the_refusal_names(tmp_path):
    """The refusal's hint is runnable, and the payload it kept is still the change: read
    again under the same claim, retry, and the same file lands."""
    project = state.refused(tmp_path)
    token = state.rival_token(project)
    writes.external_edit(project)
    refused = state.run(
        project,
        "file", "write", BASE_PY, "--ticket", writes.RIVAL_TICKET, "--attempt", writes.RIVAL,
        "--read-token", token, "--input", "base.py",
    )
    assert refused.returncode == 5, refused.stdout + refused.stderr

    fresh = writes.read_token(project, BASE_PY, writes.RIVAL_TICKET, writes.RIVAL)
    retried = state.run(
        project,
        "file", "write", BASE_PY, "--ticket", writes.RIVAL_TICKET, "--attempt", writes.RIVAL,
        "--read-token", fresh, "--input", "base.py",
    )

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert not state.payload_path(project).exists(), "the second write consumed it"


# --- SC3: stdin payload, no file at all ----------------------------------------


def test_SC3_stdin_payload_no_file_at_all(tmp_path):
    """A creation whose bytes arrive on stdin: the notice names what arrived, and there is no
    payload file before or after -- the one shape where nothing can be consumed.

    The file sink, because the write succeeds: `created, 84 lines` is the shape a text
    creation prints, and the sqlite coordination backend cannot hold its evidence yet."""
    project = state.creation(tmp_path)
    assert list((project / ".arbite" / "scratch").glob("*")) == [], "nothing staged"

    output = examples.assert_scenario(
        examples.scenario_block("SC3"), project, stdin=state.stdin_payload()
    )

    assert output.startswith("(payload read from stdin: 84 lines, 2.3 KiB)")
    assert "created, 84 lines" in output
    assert (project / state.CREATED_PATH).read_text(encoding="utf-8") == state.stdin_payload()
    assert list((project / ".arbite" / "scratch").glob("*")) == [], "stdin leaves nothing"


# --- SC4: clear scratch --------------------------------------------------------


def test_SC4_clear_scratch(tmp_path, kind):
    """Both spellings of a deliberate clear: `--all` reports the count and the files, and a
    named clear reports the one file and hands back the list to run next."""
    for_all = tmp_path / "all"
    for_all.mkdir()
    project = state.clearable(for_all, kind)
    cleared = examples.assert_scenario(_fence("SC4", 0), project)

    assert "cleared 1 file from .arbite/scratch/ (base.py, 4.1 KiB)" in cleared
    assert list((project / ".arbite" / "scratch").glob("*")) == [], "the area is empty"

    for_one = tmp_path / "one"
    for_one.mkdir()
    project = state.clearable(for_one, kind)
    named = examples.assert_scenario(_fence("SC4", 1), project)

    assert "cleared .arbite/scratch/base.py (4.1 KiB)" in named
    assert "next: 'arbite scratch list' to see what remains" in named
    assert not state.payload_path(project).exists()


# --- SC5: refuse a payload from outside the project ----------------------------


def test_SC5_refuse_a_payload_from_outside_the_project(tmp_path, kind):
    """The only payload paths are the project's own scratch area and stdin, and the refusal
    says so with both alternatives runnable. Nothing is read, written or recorded."""
    project = state.consumed(tmp_path, kind)
    token = writes.token_for_write(project, kind)
    before = (project / BASE_PY).read_bytes()

    examples.assert_scenario(
        examples.with_token(examples.scenario_block("SC5"), token), project, sink=kind
    )

    assert (project / BASE_PY).read_bytes() == before, "a refused payload changed no bytes"
    assert state.payload_path(project).exists(), "the staged payload was not touched either"
    assert state.receipt_count(project, kind) == 0


# --- DR3: clean store with empty scratch ---------------------------------------


def test_DR3_clean_store_with_empty_scratch(tmp_path, kind):
    """The clean report, with the payload note as its last line -- and the exit code is the
    findings' alone: exit 3 needs a problem, and a payload is not one."""
    project = state.doctor_project(tmp_path, kind)

    examples.assert_scenario(examples.scenario_block("DR3"), project, sink=kind)


def test_DR3_a_leftover_payload_is_a_note_and_never_an_exit_code(tmp_path, kind):
    """The same store with transport left behind: exit 0, the count and size reported, the
    guidance naming `arbite scratch clear --all`, and `--fix` reporting the fact without the
    guidance (the shape DR2 prints). The payload is never a *problem*."""
    project = state.doctor_project(tmp_path, kind)
    state.staged_payload(project, size=2048)

    reported = state.run(project, "doctor", sink_kind=kind)

    assert reported.returncode == 0, "a leftover payload never changes the exit code"
    assert "no problems found" in reported.stdout
    assert "note: .arbite/scratch/ holds 1 file (2.0 KiB)" in reported.stdout
    assert "clear with 'arbite scratch clear --all'" in reported.stdout
    assert "problem [" not in reported.stdout, "transport is a note, not a finding"

    fixed = state.run(project, "doctor", "--fix", sink_kind=kind)

    assert fixed.returncode == 0
    assert "note: .arbite/scratch/ holds 1 file (2.0 KiB)" in fixed.stdout
    assert "clear with" not in fixed.stdout, "the frozen DR2 note carries no guidance"

    facts = json.loads(state.run(project, "doctor", "--json", sink_kind=kind).stdout)
    assert facts["scratch"] == {"files": 1, "bytes": 2048}
    assert facts["remaining"] == 0


# --- what the transcripts cannot state -----------------------------------------


def test_keep_leaves_the_payload_and_says_so(tmp_path):
    """`--keep` is the opt-out: the change still lands and is still recorded, and the report
    says the staged copy survived rather than leaving the caller to check."""
    project = state.consumed(tmp_path)
    token = writes.token_for_write(project)

    proc = state.run(
        project,
        "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "base.py", "--keep",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "payload: .arbite/scratch/base.py kept (--keep)" in proc.stdout
    assert "consumed and cleared" not in proc.stdout
    assert state.payload_path(project).exists(), "the staged copy survived"
    assert (project / BASE_PY).read_text(encoding="utf-8") == writes.base_text(
        append=writes.APPENDED_LINES
    ), "the write happened anyway"
    assert state.receipt_count(project) == 1


def test_keep_is_branchable_in_json_and_folds_into_an_edit_receipt(tmp_path):
    """The flag's own effect has to be visible to a machine too, and an edit -- whose receipt
    line carries the payload's fate -- says the same thing in the same words."""
    kept = tmp_path / "kept"
    kept.mkdir()
    project = state.consumed(kept)
    token = writes.token_for_write(project)

    facts = json.loads(
        state.run(
            project,
            "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
            "--read-token", token, "--input", "base.py", "--keep", "--json",
        ).stdout
    )

    assert facts["payload"] == {
        "name": "base.py",
        "source": ".arbite/scratch/base.py",
        "consumed": False,
        "keep": True,
    }

    edits = tmp_path / "edits"
    edits.mkdir()
    project = writes.edits_project(edits)
    token = writes.token_for_edits(project)
    edited = state.run(
        project,
        "file", "edit", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--edits", "edits.json", "--keep",
    )

    assert edited.returncode == 0, edited.stdout + edited.stderr
    assert "payload .arbite/scratch/edits.json kept (--keep)" in edited.stdout
    assert state.payload_path(project, "edits.json").exists()


def test_keep_is_refused_for_a_payload_that_arrived_on_stdin(tmp_path, kind):
    """A piped payload has no staged copy, so `--keep` would be a request arbite cannot
    honour: it is refused before anything is read, and nothing changes. Asserted on both
    sinks, because the refusal precedes anything a backend stores."""
    project = state.creation(tmp_path, kind)

    proc = state.run(
        project,
        "file", "write", state.CREATED_PATH, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--input", "-", "--keep",
        sink_kind=kind,
        stdin=state.stdin_payload(),
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "'--input -' reads the payload from stdin" in proc.stderr
    assert "'--keep' to keep" in proc.stderr
    assert not (project / state.CREATED_PATH).exists(), "nothing was created"
    assert state.receipt_count(project, kind) == 0


def test_a_refusal_that_never_reached_the_bytes_keeps_the_payload_silently(tmp_path):
    """A spent token leaves the payload alone too, but says nothing about re-applying it: the
    change it carried already landed, so the frozen WR3 block prints no payload line there.
    The payload is nevertheless still on disk -- keeping it is the behaviour, the sentence is
    for the refusal whose next step is to use it."""
    project = writes.write_project(tmp_path)
    token = writes.token_for_write(project)
    first = state.run(
        project,
        "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "base.py",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    writes.staged(project, "base.py", writes.base_text(append=writes.APPENDED_LINES))

    replay = state.run(
        project,
        "file", "write", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", "base.py",
    )

    assert replay.returncode == 5, replay.stdout + replay.stderr
    assert "was already spent by" in replay.stderr
    assert "kept, so you can re-apply" not in replay.stderr
    assert state.payload_path(project).exists(), "kept all the same -- the copy is transport"


def test_scratch_list_is_an_answer_even_when_it_is_empty(tmp_path, kind):
    """Nothing staged exits 2, the same answer an empty listing gives, and it offers no next
    step: there is no command a caller should run about an area that is already empty."""
    project = state.doctor_project(tmp_path, kind)

    proc = state.run(project, "scratch", "list", sink_kind=kind)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "no payloads in .arbite/scratch/"
    assert "next:" not in proc.stdout
    assert proc.stderr == ""


def test_scratch_list_reports_the_same_facts_as_json(tmp_path, kind):
    """Text is primary and JSON carries the same facts: name, size, age, path and agent."""
    project = state.transport(tmp_path, kind)

    facts = json.loads(state.run(project, "scratch", "list", "--json", sink_kind=kind).stdout)

    assert facts["count"] == 1 and facts["bytes"] == state.BASE_PAYLOAD_BYTES
    entry = facts["files"][0]
    assert entry["name"] == "base.py"
    assert entry["path"] == ".arbite/scratch/base.py"
    assert entry["bytes"] == state.BASE_PAYLOAD_BYTES
    assert entry["agent"] == "claude.opus.001"
    assert entry["written"].endswith("Z"), "JSON carries the UTC instant, not the printed clock"


def test_clear_refuses_a_name_it_does_not_have_and_a_path_outside_the_area(tmp_path, kind):
    """A clear is deliberate: a name that is not staged is refused with the list to run, and a
    name pointing out of the area is refused *before* anything is deleted -- `scratch clear`
    must never be a way to remove somebody else's file."""
    project = state.clearable(tmp_path, kind)
    outside = project / "outside.py"
    outside.write_text("not a payload\n", encoding="utf-8")

    missing = state.run(project, "scratch", "clear", "nope.py", sink_kind=kind)
    escaping = state.run(project, "scratch", "clear", "../outside.py", sink_kind=kind)

    assert missing.returncode == 1, missing.stdout + missing.stderr
    assert "no payload named 'nope.py'" in missing.stderr
    assert "'arbite scratch list' to see what is staged" in missing.stderr
    assert escaping.returncode == 1, escaping.stdout + escaping.stderr
    assert "takes a name inside .arbite/scratch/" in escaping.stderr
    assert "../outside.py" in escaping.stderr, "the refusal names what the caller asked for"
    assert outside.exists(), "a file outside the payload area was never a candidate"
    assert state.payload_path(project).exists(), "and the staged payload is still there"


def test_clear_needs_a_target_and_takes_either_a_name_or_all(tmp_path, kind):
    """An unclearable invocation is bad input (exit 1) rather than a no-op: the caller has to
    say which payloads they mean, and they cannot say both."""
    project = state.clearable(tmp_path, kind)

    nothing = state.run(project, "scratch", "clear", sink_kind=kind)
    both = state.run(project, "scratch", "clear", "base.py", "--all", sink_kind=kind)

    assert nothing.returncode == 1 and "needs a payload name" in nothing.stderr
    assert both.returncode == 1 and "not both" in both.stderr
    assert state.payload_path(project).exists(), "nothing was cleared either way"
    assert "cleared" not in nothing.stdout and "cleared" not in both.stdout

    # A name copied out of a listing clears as it reads, `./` and all.
    copied = state.run(project, "scratch", "clear", "./base.py", sink_kind=kind)

    assert copied.returncode == 0, copied.stdout + copied.stderr
    assert not state.payload_path(project).exists()


def test_clear_all_on_an_empty_area_is_an_honest_zero(tmp_path, kind):
    """The tidy-up is idempotent: an empty area is a success that reports zero, not an error,
    because the caller's intent (an empty area) is already satisfied. A clear also reports
    the same facts as JSON and hands back the list."""
    project = state.doctor_project(tmp_path, kind)

    empty = state.run(project, "scratch", "clear", "--all", sink_kind=kind)

    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert "cleared 0 files from .arbite/scratch/ (nothing was staged)" in empty.stdout

    state.staged_payload(project)
    facts = json.loads(
        state.run(project, "scratch", "clear", "base.py", "--json", sink_kind=kind).stdout
    )

    assert facts == {
        "cleared": [
            {
                "name": "base.py",
                "path": ".arbite/scratch/base.py",
                "bytes": state.BASE_PAYLOAD_BYTES,
                "written": facts["cleared"][0]["written"],
                "agent": None,
            }
        ],
        "count": 1,
        "bytes": state.BASE_PAYLOAD_BYTES,
        "next_actions": ["arbite scratch list"],
    }


def test_scratch_is_invisible_to_discovery_and_never_claimable(tmp_path, kind):
    """Scratch is transport, not content: a listing reports it as one line rather than walking
    it, a search inside it matches nothing, and no payload can be claimed -- which is what
    stops a payload from ever being a write target or an ownership record."""
    project = writes.write_project(tmp_path, kind)
    claims_before = state.claimed_paths(project, kind)

    listed = state.run(project, "file", "list", ".arbite", sink_kind=kind)
    searched = state.run(project, "file", "search", "base.py", ".arbite/scratch", sink_kind=kind)
    claimed = state.run(
        project,
        "file", "claim", ".arbite/scratch/base.py",
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        sink_kind=kind,
    )
    written = state.run(
        project,
        "file", "write", ".arbite/scratch/base.py",
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--input", "base.py",
        sink_kind=kind,
    )

    assert ".arbite/scratch/" in listed.stdout
    assert "1 file -- not listed as a file, never claimable" in listed.stdout
    assert ".arbite/scratch/base.py" not in listed.stdout.split("(transport")[0].splitlines()[-1]
    assert searched.returncode == 2
    assert searched.stdout.startswith("no matches (scratch is excluded from discovery")
    assert claimed.returncode == 1 and "arbite does not manage its own runtime state" in claimed.stderr
    assert written.returncode == 1 and "arbite does not manage its own runtime state" in written.stderr
    assert state.claimed_paths(project, kind) == claims_before, "no claim was minted"
