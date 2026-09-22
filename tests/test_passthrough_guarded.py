"""What guarded passthrough promises: `arbite cmd --claim PATH...` (C14).

`test_passthrough_examples.py` asserts the frozen PC2, PC3 and PC4 transcripts. This file
asserts the guarantees those transcripts stand for, and the ones a transcript cannot state:

- **The claim is real, and it is live while the command runs.** The declared paths are
  acquired through the claim layer's own operation before anything starts, so a second
  worker asking "who holds this" gets this attempt's name *during* the run, and the receipt
  the run leaves records the generation it held rather than a generation nobody granted.
- **A refusal runs nothing and claims nothing.** A declared path another attempt holds
  refuses the whole request (125, nothing claimed and nothing run), appends no execution
  event, writes no receipt and changes no bytes -- including the paths of the same request
  that were free.
- **An escape is detected, attributed and left alone.** A change outside the declared set is
  reported as `unclaimed_write`, the change's own event says which side of the claim it fell
  on, the receipt records no claim for it, the bytes stay exactly where the tool put them,
  and the process returns 1 while `exit_code` still carries the wrapped tool's own code.
- **Ownership does not outlive the run.** The claims are released whether the tool succeeded,
  failed or escaped; the released record and its event stay as history; and a release that
  could not be recorded is reported rather than assumed.
- **Guarded and observed record the same evidence about bytes.** The same command produces
  the same receipts and the same before/after digests in either mode; the modes differ in
  what they can honestly say about ownership, not in what they record about content.

Both sinks run the recording tests: a claim written by one backend and unreadable by the
other would be two different products.
"""

from __future__ import annotations

import json
import sys

import passthrough_state as state
from arbite.coordination import passthrough as coordination_passthrough

HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET
RIVAL = state.RIVAL
RIVAL_TICKET = state.RIVAL_TICKET
FILE_PY = state.FILE_PY
SCHEMA_PY = state.SCHEMA_PY
QUERY_PY = state.QUERY_PY

SED = ("sed", "-i", "s/O_EXCL/O_EXCL|O_NOFOLLOW/", FILE_PY)
GUARDED = "mode: guarded (exclusive on 1 path)"


def observed(project, *command, kind="file", expect=0):
    """The same run in observed mode: no claim, so the report says so."""
    return state.run(
        project, "cmd", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, "--", *command,
        sink_kind=kind, expect=expect,
    )


def _project(path, kind="file"):
    """A fixture project in a directory the caller has already made room for."""
    path.mkdir(parents=True, exist_ok=True)
    return state.project(path, kind)


def _claims_command() -> str:
    """A shell line that asks arbite who holds the declared path, from inside the run.

    It is the same question a second worker would ask, and it is asked by the wrapped tool
    itself -- the only way to show that the claim is live *while the command runs* rather
    than before or after it."""
    return f"'{sys.executable}' -m arbite.cli file claims --json | grep -c {HOLDER}"


# --- the acquisition is real, and it is live -------------------------------------


def test_the_declared_paths_are_held_while_the_command_runs(tmp_path, kind):
    """The wrapped command asks who holds the path, and the answer is this attempt.

    The run exits 0 only because its own `grep` found the attempt id in the claim index,
    and the count it printed is a fact the report carries in place. A claim that was made
    after the command, or never made, cannot produce that line."""
    project = state.project(tmp_path, kind)

    proc = state.guarded(project, _claims_command(), paths=[FILE_PY], kind=kind, shell=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1" in proc.stdout.splitlines()
    assert GUARDED in proc.stdout
    exit_line = state.report_line(proc, "exit: ")
    assert GUARDED in exit_line
    assert coordination_passthrough.SHELL_NOTE in exit_line, (
        "a shell run says where its redirections went, whichever mode it is in"
    )
    assert coordination_passthrough.RELEASED in proc.stdout
    assert state.claims_for(project, FILE_PY, sink_kind=kind) == [], "and released afterwards"
    released = state.claim_records(project, FILE_PY, sink_kind=kind)[0]
    assert not released.is_active
    assert [event.kind for event in state.events(
        project, coordination_passthrough.EXEC_KIND, sink_kind=kind
    )] == [coordination_passthrough.EXEC_KIND]


def test_the_receipt_records_the_generation_the_run_held(tmp_path, kind):
    """The claim generation in the receipt is the one the banner printed, not a guess.

    Observation records 0 here (it holds nothing); a guarded run records the generation of
    the acquisition it made, which is what makes "this change happened under that claim"
    answerable from the evidence after the claims are gone."""
    project = state.project(tmp_path, kind)
    state.prior_claims(project, sink_kind=kind)

    proc = state.guarded(project, *SED, paths=[FILE_PY], kind=kind)

    generation = len(state.PRIOR_PATHS) + 1
    assert f"(generation {generation})" in proc.stdout
    receipt = state.receipts(project, sink_kind=kind)[0]
    assert receipt.claim_generation == generation
    payload = state.events(
        project, coordination_passthrough.EXEC_KIND, sink_kind=kind
    )[0].payload
    assert payload["claim_generation"] == generation
    assert payload["claim_paths"] == [FILE_PY]
    assert payload["unclaimed_write"] == []


def test_a_guarded_run_releases_only_what_it_declared(tmp_path, kind):
    """The attempt's other holdings are not this run's to give away.

    A worker that already holds three paths runs a guarded command for a fourth: the
    release afterwards revokes the fourth and leaves the other three active, because
    "work complete for this command" is a statement about this command."""
    project = state.generation_project(tmp_path, kind)

    state.guarded(project, *SED, paths=[FILE_PY], kind=kind)

    assert [claim.path for claim in state.active_claims(project, sink_kind=kind)] == sorted(
        state.PRIOR_PATHS
    )


def test_a_read_only_guarded_run_still_claims_and_releases(tmp_path, kind):
    """Nothing changed, so there is nothing to review -- but the claim was still taken and
    still given back, and the report says so rather than printing an empty block."""
    project = state.project(tmp_path, kind)

    proc = state.guarded(project, "true", paths=[FILE_PY], kind=kind)

    assert proc.returncode == 0
    assert "changed" not in proc.stdout
    assert coordination_passthrough.RELEASED in proc.stdout
    assert state.receipts(project, sink_kind=kind) == []
    assert state.claims_for(project, FILE_PY, sink_kind=kind) == []


# --- a refusal claims nothing and runs nothing -----------------------------------


def test_a_busy_declared_path_refuses_the_whole_request(tmp_path, kind):
    """One busy path refuses the request, including the part of it that was free.

    The tool would have touched both paths; it never starts, so the busy one keeps the
    holder's bytes and the free one is not quietly claimed by a run that could not do its
    job. That is the all-or-nothing rule the claim layer already enforces, reached through
    the same operation `file claim` uses."""
    project = state.project(tmp_path, kind)
    state.source_file(project, SCHEMA_PY, state.SCHEMA_LINES)
    state.hold_claim(project, SCHEMA_PY, RIVAL_TICKET, RIVAL, state.HOLD_GENERATION, kind)

    proc = state.guarded(
        project, "sed", "-i", "s/a/b/", SCHEMA_PY, FILE_PY,
        paths=[SCHEMA_PY, FILE_PY], kind=kind, expect=125,
    )

    assert proc.stdout == ""
    # The verb agrees with the number held, as the frozen claim refusal writes it (FC3):
    # `1 of 2 paths is held`, not "are".
    assert "1 of 2 declared paths is held; nothing was claimed and the command did not run" in (
        proc.stderr
    )
    assert "2 of 2" not in proc.stderr, "only one of the two is held"
    assert f"  {FILE_PY}  free" in proc.stderr or f"  {FILE_PY}" in proc.stderr
    assert coordination_passthrough.NO_RUN in proc.stderr
    assert state.claims_for(project, FILE_PY, sink_kind=kind) == [], "the free path stayed free"
    assert state.events(
        project, coordination_passthrough.EXEC_KIND, sink_kind=kind
    ) == []
    assert state.receipts(project, sink_kind=kind) == []


def test_the_busy_refusal_publishes_the_structured_alternative(tmp_path):
    """JSON carries the same refusal as a table a caller can branch on.

    `reason` is the claim layer's own key (`file_busy`), the held and free paths are listed
    with the holder, `ran` is false, and the next actions are the commands the text offers
    in prose -- never a retry of the busy path."""
    project = state.busy_project(tmp_path)

    payload = state.guarded_json(
        project, "sed", "-i", "s/a/b/", SCHEMA_PY, paths=[SCHEMA_PY],
        ticket=RIVAL_TICKET, attempt=RIVAL, expect=125,
    )

    assert payload["reason"] == coordination_passthrough.REASON_CLAIM_BUSY == "file_busy"
    assert payload["ran"] is False and payload["mode"] == "guarded"
    assert payload["exit_code"] == 125
    assert payload["claimed"] == [] and payload["free"] == []
    assert payload["held"][0]["attempt"] == HOLDER
    assert payload["held"][0]["generation"] == state.HOLD_GENERATION
    assert payload["next_actions"] == [
        f"arbite list next --claim {state.RIVAL_WORKER}",
        f"arbite changes {HOLDER_TICKET}",
    ]


def test_a_guarded_refusal_never_leaves_a_claim_behind(tmp_path):
    """Every refusal a guarded run can make happens *before* the acquisition.

    The attempt check and the claim itself both refuse here, and in both cases the store
    holds no claim afterwards: nothing was acquired, so there is nothing to release."""
    project = state.project(tmp_path)

    stale = state.guarded(
        project, "true", paths=[FILE_PY], attempt="att-0000", kind="file", expect=125
    )
    assert "att-0000 does not exist in this store" in stale.stderr
    assert state.claims_for(project, FILE_PY) == []

    protected = state.guarded(project, "true", paths=[".git/config"], expect=125)
    assert "protected" in protected.stderr
    assert "command did not run" in protected.stderr
    assert state.active_claims(project) == []


def test_guarded_mode_needs_an_attempt_to_claim_for(tmp_path):
    """`--claim` without the pair is an invocation arbite cannot carry out, not a policy
    refusal: a claim belongs to an attempt, and the run has to name the one it is for."""
    project = state.project(tmp_path)

    proc = state.run(
        project, "cmd", "--claim", FILE_PY, "--", "true", sink_kind="file", expect=126
    )

    assert coordination_passthrough.CLAIM_ATTEMPT_MESSAGE in proc.stderr
    assert state.events(project, coordination_passthrough.EXEC_KIND) == []
    assert state.active_claims(project) == []


# --- an escape is detected, attributed and left alone -----------------------------


def test_an_escape_is_reported_without_undoing_anything(tmp_path, kind):
    """The escaped bytes stay, the evidence says whose they were not, and the code says so.

    `sed` is asked to edit two paths and only one of them was declared. Arbite holds the
    declared one and nothing for the other, so the second is a write it had no
    authorisation for: it is reported, recorded and left exactly where the tool put it.
    Rolling it back would destroy work arbite was asked only to run."""
    project = state.escape_project(tmp_path, kind)

    proc = state.guarded(
        project, "sed", "-i", "s/x/y/", SCHEMA_PY, QUERY_PY, paths=[SCHEMA_PY],
        kind=kind, expect=1,
    )

    assert "1 OUTSIDE the claimed set" in proc.stdout
    assert "NOT claimed" in proc.stdout and "unclaimed_write:" in proc.stdout
    assert (project / QUERY_PY).read_text().splitlines()[state.QUERY_LINE - 1] == "y"
    assert coordination_passthrough.RELEASED_ESCAPED in proc.stdout

    by_path = {receipt.paths[0]: receipt for receipt in state.receipts(project, sink_kind=kind)}
    assert by_path[QUERY_PY].claim_generation == 0
    assert by_path[SCHEMA_PY].claim_generation == 1
    assert by_path[QUERY_PY].after[QUERY_PY] != "absent", "the escaped version is recorded"

    changed = {
        event.subject: event.payload
        for event in state.events(
            project, coordination_passthrough.CHANGED_KIND, sink_kind=kind
        )
    }
    assert changed[QUERY_PY]["claimed"] is False
    assert changed[SCHEMA_PY]["claimed"] is True
    assert state.claims_for(project, SCHEMA_PY, sink_kind=kind) == [], "released anyway"


def test_the_escape_hint_names_every_escaped_path_and_the_review(tmp_path):
    """One claim command for the whole escaped set, and the review beside it.

    The hint is the repair path the ticket asks for, and it is one acquisition because the
    paths escaped together; `re-read them` is there because a pre-claim read authorises
    nothing, which is the rule the claim command's own hint states."""
    project = state.escape_project(tmp_path)
    other = "src/arbite/paths.py"
    state.write(project, other, "x\n")

    proc = state.guarded(
        project, "sh", "-c", f"sed -i s/x/y/ {SCHEMA_PY} {QUERY_PY} {other}",
        paths=[SCHEMA_PY], expect=1,
    )

    assert f"changed 3 paths, 2 OUTSIDE the claimed set:" in proc.stdout
    assert (
        f"next: 'arbite file claim {other} {QUERY_PY} --ticket {HOLDER_TICKET} "
        f"--attempt {HOLDER}' and re-read them," in proc.stdout
    )
    assert f"      or 'arbite changes {HOLDER_TICKET}' and correct by hand" in proc.stdout


def test_an_escape_from_a_path_somebody_else_holds_names_the_holder(tmp_path):
    """`NOT claimed` is about *this run*, so a live foreign claim is named beside it.

    The sentence "modified without being claimed" must not read as "nobody owns this" when
    a claim record says otherwise, so the report adds the holder it found -- and the JSON
    for that change carries the same holder rather than a bare `false`."""
    text_project = state.escape_project(_project(tmp_path / "text"))
    state.hold_claim(text_project, QUERY_PY, RIVAL_TICKET, RIVAL, 2)
    json_project = state.escape_project(_project(tmp_path / "json"))
    state.hold_claim(json_project, QUERY_PY, RIVAL_TICKET, RIVAL, 2)

    proc = state.guarded(
        text_project, "sed", "-i", "s/x/y/", SCHEMA_PY, QUERY_PY, paths=[SCHEMA_PY], expect=1
    )
    payload = state.guarded_json(
        json_project, "sed", "-i", "s/x/y/", SCHEMA_PY, QUERY_PY, paths=[SCHEMA_PY], expect=1
    )

    assert f"note: {RIVAL_TICKET}/{RIVAL} holds it at generation 2" in proc.stdout
    assert payload["changed"][0]["path"] == QUERY_PY, "rows print in canonical path order"
    assert payload["changed"][0]["claimed"] is False
    assert payload["changed"][0]["held_by"] == f"{RIVAL_TICKET}/{RIVAL}"


def test_the_json_splits_the_tools_code_from_arbites_own(tmp_path, kind):
    """A caller can tell which code it is holding, in both directions.

    An escape makes the process exit 1 -- arbite's own finding -- while `exit_code` stays
    the wrapped tool's, and `escaped`/`unclaimed_write` say what was found. A run without
    an escape has the two codes equal and nothing unclaimed."""
    escaped_project = state.escape_project(_project(tmp_path / "escaped", kind), kind)
    escaped = state.guarded_json(
        escaped_project, "sed", "-i", "s/x/y/", SCHEMA_PY, QUERY_PY, paths=[SCHEMA_PY],
        kind=kind, expect=1,
    )
    assert escaped["exit_code"] == 0 and escaped["arbite_exit_code"] == 1
    assert escaped["escaped"] is True and escaped["unclaimed_write"] == [QUERY_PY]
    assert escaped["claims"]["released"] is True
    assert escaped["claims"]["release_reason"] == coordination_passthrough.RELEASE_REASON

    clean_project = _project(tmp_path / "clean", kind)
    clean = state.guarded_json(
        clean_project, "true", paths=[FILE_PY], kind=kind, expect=0
    )
    assert clean["exit_code"] == 0 and clean["arbite_exit_code"] == 0
    assert clean["escaped"] is False and clean["unclaimed_write"] == []
    assert clean["mode"] == "guarded" and clean["exclusive"] is True


# --- release, including when the tool fails --------------------------------------


def test_the_claims_are_released_when_the_tool_fails(tmp_path, kind):
    """A failed command is still a finished run, so its claims come back.

    The tool edited the declared path and then exited 1; the change is recorded, the claim
    is released, and the report's release line says which of the two it was instead of
    claiming the work was complete."""
    project = state.project(tmp_path, kind)

    # The substitution is quoted: `|` is a shell operator, so an unquoted script would be
    # a pipeline rather than an argument and the file would never be touched.
    proc = state.guarded(
        project, "sh", "-c", f"sed -i 's/O_EXCL/O_EXCL|O_NOFOLLOW/' {FILE_PY}; exit 1",
        paths=[FILE_PY], kind=kind, expect=1,
    )

    assert f"exit: 1 (" in proc.stdout
    assert coordination_passthrough.RELEASED_FAILED.format(code=1) in proc.stdout
    assert state.claims_for(project, FILE_PY, sink_kind=kind) == []
    assert [receipt.paths for receipt in state.receipts(project, sink_kind=kind)] == [[FILE_PY]]
    assert [event.kind for event in state.events(
        project, coordination_passthrough.CHANGED_KIND, sink_kind=kind
    )] == [coordination_passthrough.CHANGED_KIND]


def test_a_release_that_cannot_be_recorded_is_reported_not_assumed(tmp_path):
    """A tool that hands the claim back itself leaves the run with nothing to release.

    The command has already run, so this cannot be a refusal and must not be an exception:
    the report says what happened, names the command that shows the truth, and the store --
    which holds the released record and its reason -- is what a reader checks. This is the
    honest failure of the release, staged with a real `file release` rather than a stub."""
    project = state.project(tmp_path)
    inner = (
        f"'{sys.executable}' -m arbite.cli file release {FILE_PY} --ticket {HOLDER_TICKET} "
        f"--attempt {HOLDER} --reason 'the tool gave it back'"
    )

    proc = state.guarded(project, inner, paths=[FILE_PY], shell=True)

    assert proc.returncode == 0, "the release is arbite's business, not the tool's result"
    assert "note: the claims could not be released" in proc.stdout
    assert "'arbite file claims' shows who holds them now" in proc.stdout
    record = state.claim_records(project, FILE_PY)[0]
    assert not record.is_active
    assert record.release_reason == "the tool gave it back"
    assert state.receipts(project) == []


def test_the_release_lines_say_what_the_release_did(tmp_path):
    """Three outcomes, three sentences: complete, failed, or escaped-and-left-alone.

    Built as values rather than run through the CLI, because what is asserted is the
    wording each outcome prints, which is the part a reader has to be able to trust."""
    run = coordination_passthrough.PassthroughRun(
        store=None,
        project_root=None,
        argv=("true",),
        tool="true",
        shell=False,
        ticket_id=HOLDER_TICKET,
        attempt_id=HOLDER,
        claim_paths=(FILE_PY,),
        claim_generation=1,
    )

    assert run._release_line(0, (), (True, None)) == coordination_passthrough.RELEASED
    assert run._release_line(1, (), (True, None)) == (
        coordination_passthrough.RELEASED_FAILED.format(code=1)
    )
    assert run._release_line(0, (FILE_PY,), (True, None)) == (
        coordination_passthrough.RELEASED_ESCAPED
    )
    failed = run._release_line(0, (), (False, "the store said no"))
    assert failed == coordination_passthrough.RELEASE_UNRECORDED.format(
        error="the store said no"
    )
    assert "arbite file claims" in failed


def test_a_guarded_creation_of_a_declared_absent_path_is_inside_the_set(tmp_path, kind):
    """Claiming a path that does not exist yet, then creating it, is not an escape.

    The claim layer records the absent version, so the creation is exactly what was
    declared -- and it is the case a guard would get wrong if it compared the claim
    against the file's existence rather than against what was declared."""
    project = state.project(tmp_path, kind)
    created = "src/arbite/created_by_a_guarded_run.py"

    proc = state.guarded(
        project, "sh", "-c", f"printf 'hello\\n' > {created}", paths=[created], kind=kind
    )

    assert "all inside the claimed set" in proc.stdout
    assert (project / created).read_text() == "hello\n"
    receipt = state.receipts(project, sink_kind=kind)[0]
    assert receipt.before[created] == "absent"
    assert receipt.claim_generation == 1


# --- guarded versus observed: the same evidence about bytes ----------------------


def test_guarded_and_observed_record_the_same_change(tmp_path):
    """The same `sed` produces the same receipt in either mode; only ownership differs.

    Both modes take the same manifest, so both record the same before and after digests for
    the same path: the guarded half adds a claim, not a different account of the bytes. What
    differs is what each may say about them -- the guarded receipt names the generation it
    held, the observed one records 0, because it held nothing."""
    observed_project = _project(tmp_path / "observed")
    guarded_project = _project(tmp_path / "guarded")

    observed(observed_project, *SED)
    state.guarded(guarded_project, *SED, paths=[FILE_PY])

    observed_receipt = state.receipts(observed_project)[0]
    guarded_receipt = state.receipts(guarded_project)[0]
    assert observed_receipt.paths == guarded_receipt.paths == [FILE_PY]
    assert observed_receipt.before[FILE_PY] == guarded_receipt.before[FILE_PY]
    assert observed_receipt.after[FILE_PY] == guarded_receipt.after[FILE_PY]
    assert observed_receipt.ticket_id == guarded_receipt.ticket_id == HOLDER_TICKET
    assert observed_receipt.claim_generation == 0
    assert guarded_receipt.claim_generation == 1

    observed_row = next(
        line for line in observed(observed_project, "true").stdout.splitlines()
        if line.startswith("exit: ")
    )
    assert observed_row.endswith("mode: observed (no exclusivity claimed)")


def test_the_two_modes_say_different_things_about_ownership(tmp_path):
    """`observed` disclaims exclusivity; `guarded` states it, and the JSON agrees.

    The words are the point: a report must not leave a reader guessing whether a claim was
    held. The seam's `available` is true in both, because the flag exists in both; what
    differs is what this run actually held, which is what `claimed` and `exclusive` say."""
    observed_project = _project(tmp_path / "observed")
    guarded_project = _project(tmp_path / "guarded")

    observed_report = observed(observed_project, *SED)
    guarded_report = state.guarded(guarded_project, *SED, paths=[FILE_PY])

    assert "mode: observed (no exclusivity claimed)" in observed_report.stdout
    assert GUARDED in guarded_report.stdout
    assert "no exclusivity claimed" not in guarded_report.stdout
    assert "exclusive on" not in observed_report.stdout

    observed_payload = state.observed_json(observed_project, "true")
    guarded_payload = state.guarded_json(guarded_project, "true", paths=[FILE_PY])
    assert observed_payload["exclusive"] is False
    assert guarded_payload["exclusive"] is True
    assert observed_payload["exclusivity"]["claimed"] == []
    assert guarded_payload["exclusivity"]["claimed"] == [FILE_PY]
    assert observed_payload["exclusivity"]["available"] is True
    assert guarded_payload["exclusivity"]["available"] is True
    assert "claims" not in observed_payload
    assert guarded_payload["claims"]["paths"] == [FILE_PY]


def test_the_guarded_run_is_one_transaction_of_evidence(tmp_path, kind):
    """Every changed path gets its receipt and its `passthrough.changed` event, then the run.

    The exec event the guarded run appends carries the claimed set beside the changes, so
    the stream answers "was this inside the declaration" without a second lookup -- which is
    exactly the question the passthrough stream exists to answer later."""
    project = state.escape_project(tmp_path, kind)

    state.guarded(
        project, "sed", "-i", "s/x/y/", SCHEMA_PY, QUERY_PY, paths=[SCHEMA_PY], kind=kind,
        expect=1,
    )

    store = state.store_for(project, kind)
    receipts = {receipt.id: receipt for receipt in store.receipts()}
    changed = state.events(project, coordination_passthrough.CHANGED_KIND, sink_kind=kind)
    executions = state.events(
        project, coordination_passthrough.EXEC_KIND, sink_kind=kind
    )
    assert {event.operation_id for event in changed} == set(receipts)
    assert all(receipts[event.operation_id].kind == "passthrough" for event in changed)

    payload = executions[0].payload
    assert payload["claim_paths"] == [SCHEMA_PY]
    assert payload["unclaimed_write"] == [QUERY_PY]
    assert payload["changed"] == [QUERY_PY, SCHEMA_PY]
    assert json.loads(json.dumps(payload)) == payload, "the payload is JSON-clean"
