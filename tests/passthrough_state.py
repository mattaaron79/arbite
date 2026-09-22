"""Projects in the states the passthrough transcripts describe (PC1, PC5, PC6).

The PC blocks assert facts about a *command's effect*: a one-line sed replacement that
reads `+1 -1`, a shell redirect whose output leaves the managed paths alone, and three
refusals that must happen before anything is started. So the fixtures here are the files
those commands act on, built to the shapes the blocks print -- `src/arbite/sinks/file.py`
is 412 lines with the `O_EXCL` flag on exactly one of them, which is what makes the sed
change exactly one line -- and the run itself always goes through the real CLI, because
"a familiar tool's changes are captured" is a claim about a real process making a real
change.

Everything about *ownership* comes from the state the other slices' fixtures already
build (`discovery.project_fixture`): `tic-cf9f` in progress for `claude.opus.001` and
the attempt `att-91bd`, plus the rival `tic-9b57` / `att-4c81` a claim conflict needs.
"""

from __future__ import annotations

from pathlib import Path

import discovery_state as discovery
import examples
from arbite.coordination.store import open_coordination_store

HOLDER = discovery.HOLDER
HOLDER_WORKER = discovery.HOLDER_WORKER
HOLDER_TICKET = discovery.HOLDER_TICKET
RIVAL = discovery.RIVAL
RIVAL_WORKER = discovery.RIVAL_WORKER
RIVAL_TICKET = discovery.RIVAL_TICKET

#: PC1's target: the file the block's sed rewrites, at the line count its claim row
#: prints (`412 lines`), with `O_EXCL` on one line -- the substitution changes that
#: line and nothing else, which is what makes the row's `+1 -1` true of the bytes.
FILE_PY = "src/arbite/sinks/file.py"
FILE_LINES = 412
O_EXCL_LINE = 141
O_EXCL_TEXT = "        flags = os.O_CREAT | os.O_EXCL\n"

#: PC5's input: the shell block counts `def` in this file and redirects the count into
#: the scratch area, so the file has to contain at least one (a `grep -c` with no match
#: exits 1, and the block exits 0).
CLI_PY = "src/arbite/cli.py"
CLI_LINES = 120
CLI_DEF_LINE = 40
CLI_DEF_TEXT = "def cmd_claim(args):\n"

#: Where PC5's redirect lands. Scratch is transport: the file is written, and it must
#: *not* appear as an observed change, because the manifest covers managed paths only.
SCRATCH_COUNT = ".arbite/scratch/count.txt"

#: PC4's pair: the path a guarded run declares, and the one the same command edits anyway.
#: Both carry an `x` on one line, because the block's rows say `+1 -1` and a substitution
#: over a file with no match would change nothing at all.
SCHEMA_PY = "src/arbite/schema.py"
QUERY_PY = "src/arbite/query.py"
SCHEMA_LINES = 40
QUERY_LINES = 25
SCHEMA_LINE = 3
QUERY_LINE = 7
X_LINE = "x\n"

#: PC3's holder: the generation the block's busy row prints (`gen 3`).
HOLD_GENERATION = 3

#: PC2's holder has claimed three paths already (generations 1-3), which is what makes the
#: banner print `generation 4`: a generation counts the acquisitions of *one attempt*.
PRIOR_PATHS = ("src/arbite/schema.py", "src/arbite/sinks/base.py", "src/arbite/sinks/sqlite.py")

#: A ticket and a scratchpad under `.arbite/`: documents, not runtime state, so a change
#: to one of them is an observed change like any source file's.
TICKET_FILE = f".arbite/open/{HOLDER_TICKET}.md"
AGENT_FILE = ".arbite/agents/claude.opus.001.md"

#: Generated output: a mutation refuses it, and the manifest does not cover it, so a
#: tool that writes there is not reported as an observed change.
CACHE_FILE = ".pytest_cache/lastfailed"


def project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """PC1/PC5's world: the two attempts, the two source files and a scratch area."""
    project = discovery.project_fixture(tmp_path, sink_kind)
    source_file(project, FILE_PY, FILE_LINES, {O_EXCL_LINE: O_EXCL_TEXT})
    source_file(project, CLI_PY, CLI_LINES, {CLI_DEF_LINE: CLI_DEF_TEXT})
    (project / ".arbite" / "scratch").mkdir(parents=True, exist_ok=True)
    return project


def source_file(project: Path, relative: str, lines: int, replacements=None) -> Path:
    """A source file with exactly `lines` newline-terminated lines.

    `replacements` places the lines a block quotes by number, because a transcript's
    `+1 -1` is a fact about the line that changed being the *only* one that changed."""
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# {relative} line {index}\n" for index in range(1, lines + 1)]
    for number, content in (replacements or {}).items():
        body[number - 1] = content
    path.write_text("".join(body), encoding="utf-8")
    return path


def store_for(project: Path, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return open_coordination_store(discovery.state.sink_for(project, sink_kind))


def run(project: Path, *args, sink_kind: str = "file", expect: int = 0):
    """Run one arbite command in the project, asserting its exit code."""
    proc = examples.run_cli(project, *args, sink=sink_kind)
    assert proc.returncode == expect, (
        f"arbite {' '.join(args)} exited {proc.returncode}, expected {expect}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc


def run_json(project: Path, *args, sink_kind: str = "file", expect: int = 0):
    """Run one arbite command with `--json` and return the parsed payload."""
    import json

    proc = run(project, *args, "--json", sink_kind=sink_kind, expect=expect)
    return json.loads(proc.stdout)


def receipts(project: Path, sink_kind: str = "file") -> list:
    """Every operation receipt the store holds, in id order."""
    return sorted(store_for(project, sink_kind).receipts(), key=lambda receipt: receipt.id)


def receipts_in_log_order(project: Path, sink_kind: str = "file") -> list:
    """The receipts in the order their own events happened.

    A receipt id is opaque, so the log order -- the cursor of the `passthrough.changed`
    event that names the operation -- is the only order there is, and a test that says
    "the create came first" has to use it."""
    store = store_for(project, sink_kind)
    by_id = {receipt.id: receipt for receipt in store.receipts()}
    ordered = []
    for event in sorted(store.events(), key=lambda each: each.cursor):
        receipt = by_id.get(event.operation_id)
        if receipt is not None and receipt not in ordered:
            ordered.append(receipt)
    return ordered


def events(project: Path, kind: str, sink_kind: str = "file") -> list:
    """Every event of one kind, in cursor order."""
    found = [
        event
        for event in store_for(project, sink_kind).events()
        if event.kind == kind
    ]
    return sorted(found, key=lambda event: event.cursor)


def claim(project: Path, path: str, ticket: str, attempt: str, sink_kind: str = "file"):
    """Take a real claim on `path` through the CLI, as a rival worker would."""
    return run(
        project,
        "file",
        "claim",
        path,
        "--ticket",
        ticket,
        "--attempt",
        attempt,
        sink_kind=sink_kind,
    )


def write(project: Path, relative: str, text: str) -> Path:
    """Put content at a path the fixture itself writes (not a passthrough change)."""
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Guarded mode (PC2, PC3, PC4)
# ---------------------------------------------------------------------------


def guarded(
    project,
    *command,
    paths,
    ticket=HOLDER_TICKET,
    attempt=HOLDER,
    kind="file",
    expect=0,
    shell=False,
):
    """One `arbite cmd --claim PATH...`, invoked the way the blocks write it."""
    args = ["cmd", "--ticket", ticket, "--attempt", attempt, "--claim", *paths]
    if shell:
        args += ["--shell"]
    args += ["--", *command]
    return run(project, *args, sink_kind=kind, expect=expect)


def observed_json(
    project, *command, ticket=HOLDER_TICKET, attempt=HOLDER, kind="file", expect=0
):
    """One `arbite cmd --json` in observed mode.

    `--json` is arbite's own flag and the wrapped command's argv is a REMAINDER, so it has
    to come *before* the `--` -- which is exactly why `run_json` cannot be used for `cmd`."""
    import json

    args = ["cmd", "--ticket", ticket, "--attempt", attempt, "--json", "--", *command]
    return json.loads(run(project, *args, sink_kind=kind, expect=expect).stdout)


def guarded_json(
    project,
    *command,
    paths,
    ticket=HOLDER_TICKET,
    attempt=HOLDER,
    kind="file",
    expect=0,
    shell=False,
):
    """The same run with `--json`, parsed: the branchable half of the same facts."""
    import json

    args = ["cmd", "--ticket", ticket, "--attempt", attempt, "--json", "--claim", *paths]
    if shell:
        args += ["--shell"]
    args += ["--", *command]
    return json.loads(run(project, *args, sink_kind=kind, expect=expect).stdout)


def report_line(proc, prefix: str) -> str:
    """The one line of a report that starts with `prefix`, which the caller asserts on."""
    return next(line for line in proc.stdout.splitlines() if line.startswith(prefix))


def prior_claims(
    project, paths=PRIOR_PATHS, ticket=HOLDER_TICKET, attempt=HOLDER, sink_kind="file"
):
    """Acquire `paths` for one attempt first, so its next acquisition is generation 4.

    The acquisitions go through the CLI rather than being written into the store, because
    the generation a banner prints counts what that attempt really acquired, and each
    path has to exist for the claim to record the version it observed."""
    for path in paths:
        source_file(project, path, 30)
        claim(project, path, ticket, attempt, sink_kind=sink_kind)


def generation_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """PC2's world: three earlier acquisitions by the holder, and the file the sed rewrites."""
    world = project(tmp_path, sink_kind)
    prior_claims(world, sink_kind=sink_kind)
    return world


def busy_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """PC3's world: the declared path already held by the other ticket's attempt at gen 3."""
    world = project(tmp_path, sink_kind)
    source_file(world, SCHEMA_PY, SCHEMA_LINES)
    hold_claim(world, SCHEMA_PY, HOLDER_TICKET, HOLDER, HOLD_GENERATION, sink_kind)
    return world


def escape_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """PC4's world: the declared path and the undeclared one the same command edits.

    Both files carry `x` on exactly one line, so `sed -i 's/x/y/'` changes one line of
    each -- which is what makes the block's two `+1 -1` rows true of the bytes."""
    world = project(tmp_path, sink_kind)
    source_file(world, SCHEMA_PY, SCHEMA_LINES, {SCHEMA_LINE: X_LINE})
    source_file(world, QUERY_PY, QUERY_LINES, {QUERY_LINE: X_LINE})
    return world


def hold_claim(
    project,
    relative: str,
    ticket_id: str,
    attempt_id: str,
    generation: int,
    sink_kind: str = "file",
    acquired: str = "2026-09-21T13:12:04Z",
) -> None:
    """Record an active claim *somebody else* holds, at a chosen generation (PC3).

    Written through the store because the generation is the fact the busy row prints and
    the command that would have produced it belongs to another slice's world: the case a
    guarded run has to refuse is a path another attempt acquired earlier, not one this
    fixture's own run acquired."""
    discovery.put_claim(project, relative, ticket_id, attempt_id, generation, sink_kind, acquired)


def active_claims(project: Path, sink_kind: str = "file") -> list:
    """The claims the store says are live, in canonical path order."""
    return sorted(
        store_for(project, sink_kind).active_claims(), key=lambda each: each.path
    )


def claims_for(project: Path, relative: str, sink_kind: str = "file") -> list:
    """The *active* claims on `relative`, as the store answers that question for a writer."""
    return store_for(project, sink_kind).claims_for_path(relative)


def claim_records(project: Path, relative: str, sink_kind: str = "file") -> list:
    """Every claim record the store holds for `relative`, released ones included.

    `claims_for` deliberately sees only what is held *now*; a test that asserts a release
    was recorded rather than erased has to read the records."""
    return [
        record
        for record in store_for(project, sink_kind).records("claim")
        if record.path == relative
    ]
