"""Projects in the states the discovery and read transcripts describe (LS1-LS6, RD1-RD5).

The LS and RD blocks assert facts about *content*: line counts, sizes, a digest that
covers a whole file, rows that appear in canonical order. So the fixtures here are
built to the document's own numbers rather than around them -- `sized_file` and
`exact_size` make a file whose line count and byte count are exactly what a row prints,
and the LS2/LS4 sets are laid out so the ordering, the counts and the continuation
tokens are what those blocks say they are. Where a block's numbers are unreachable
(`142 KiB` for a size the report renders as `142.0 KiB`), the test that asserts it says
so and compares the facts instead.

Attempts and claims are written through the store, because the transcripts treat both
as already existing when a block starts; the one acquisition a block actually performs
(`file claim` in LS1's world) goes through the real command.
"""

from __future__ import annotations

from pathlib import Path

import claims_state as claims
import examples
import lifecycle_state as state
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

C03_TITLE = state.C03_TITLE
C04_TITLE = state.C04_TITLE

HOLDER = claims.HOLDER
HOLDER_WORKER = claims.HOLDER_WORKER
RIVAL = claims.RIVAL
RIVAL_WORKER = claims.RIVAL_WORKER
HOLDER_TICKET = claims.HOLDER_TICKET
RIVAL_TICKET = claims.RIVAL_TICKET

BASE_PY = "src/arbite/sinks/base.py"
FILE_PY = "src/arbite/sinks/file.py"
SQLITE_PY = "src/arbite/sinks/sqlite.py"
INIT_PY = "src/arbite/sinks/__init__.py"
CLI_PY = "src/arbite/cli.py"
SCHEMA_PY = "src/arbite/schema.py"
OLD_PY = "src/arbite/sinks/old.py"

#: The line count and byte count LS1's four rows print, with the size spelled the way
#: the row renders it: 2.1 KiB, 26.4 KiB, 18.4 KiB and 24.9 KiB.
SINK_FILES = (
    (INIT_PY, 78, 2150),
    (BASE_PY, 570, 27000),
    (FILE_PY, 412, 18800),
    (SQLITE_PY, 520, 25500),
)

#: The lines RD1 samples: the gutter format and the numbering are the assertion, so the
#: body has to hold exactly these at exactly these numbers.
BASE_SAMPLE = {
    144: "class TicketSink(ABC):",
    145: '    """A ticket store. Implementations: `FileSink`, `SqliteSink`.',
}
#: RD3's sample, and the 3015-line/142 KiB shape its header prints.
CLI_SAMPLE = {1254: "def cmd_claim(args):", 1255: '    """Claim a ticket for an agent: ...'}

#: The scratchpad and payload LS5's listing reports.
SCRATCHPAD = ".arbite/agents/claude.opus.001.md"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def project_fixture(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project with the two attempts the streams name.

    `tic-cf9f` is in progress for `claude.opus.001`/`att-91bd` and `tic-9b57` for
    `claude.sonnet.002`/`att-4c81`, exactly as the blocks use them: the first is the
    holder a read must respect, the second is the attempt that reads a path somebody
    else holds. Nothing else is created -- each scenario builds its own files."""
    project = state.initialise(tmp_path, sink_kind)
    for ticket, worker, title in (
        (HOLDER_TICKET, HOLDER_WORKER, C03_TITLE),
        (RIVAL_TICKET, RIVAL_WORKER, C04_TITLE),
    ):
        state.put(
            project,
            ticket,
            sink_kind,
            title=title,
            status="in_progress",
            assignee=worker,
            priority=1,
            **state.epic_ticket(),
        )
    claims.put_attempt(project, HOLDER, HOLDER_TICKET, HOLDER_WORKER, sink_kind)
    claims.put_attempt(project, RIVAL, RIVAL_TICKET, RIVAL_WORKER, sink_kind)
    return project


def store_for(project: Path, sink_kind: str = "file"):
    """The project's coordination store, as the CLI resolves it."""
    return open_coordination_store(state.sink_for(project, sink_kind))


# ---------------------------------------------------------------------------
# Files with an exact shape
# ---------------------------------------------------------------------------


def exact_size(label: str, lines: int, size: int, replacements=None) -> str:
    """Text with exactly `lines` newline-terminated lines and `size` bytes.

    Rows are built from the shape rather than written by hand, because a transcript
    asserts the line count *and* the size: short filler lines, the lines a block
    quotes put in place by number, and the last line padded so the byte count is
    exact (padding the last line never changes the line count)."""
    body = [f"# {index}" for index in range(1, lines + 1)]
    for number, content in (replacements or {}).items():
        body[number - 1] = content
    text = "".join(f"{line}\n" for line in body)
    pad = size - len(text)
    assert pad >= 0, f"{label}: {lines} lines cannot fit in {size} bytes"
    return text[:-1] + (" " * pad) + "\n"


def sized_file(project: Path, relative: str, lines: int, size: int, replacements=None) -> Path:
    """Write a file with exactly `lines` lines and `size` bytes."""
    return written(project, relative, exact_size(relative, lines, size, replacements))


def written(project: Path, relative: str, text: str) -> Path:
    """Write text to a project-relative path, creating the directories it needs."""
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def lines_with_matches(relative: str, lines: int, matches) -> str:
    """A text file of `lines` lines, the ones in `matches` containing the pattern.

    Filler lines never contain the search pattern, which is how a fixture controls the
    match count a transcript asserts."""
    body = [matches.get(number, f"# {relative} line {number}") for number in range(1, lines + 1)]
    return "".join(f"{line}\n" for line in body)


def match_lines(count: int, first: int = 1, step: int = 4, label: str = "f") -> dict:
    """`count` matching lines, numbered from `first` with a fixed step."""
    return {
        first + index * step: f"def {label}_{first}_{index}(value):" for index in range(count)
    }


def put_claim(
    project: Path,
    relative: str,
    ticket_id: str,
    attempt_id: str,
    generation: int,
    sink_kind: str = "file",
    acquired: str = "2026-09-21T13:12:04Z",
) -> None:
    """Record an active claim on `relative`, as the blocks treat it: already held.

    Written through the store because the *generation* is the fact a banner prints and
    the command that would have produced it is another ticket's (claims are acquired by
    `file claim`, which LS1 does exercise for real)."""
    store = store_for(project, sink_kind)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init records the workspace"
    store.put_record(
        coordination_records.FileClaim(
            id=coordination_records.claim_id_for(workspace.id, relative),
            workspace_id=workspace.id,
            path=relative,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            generation=generation,
            acquired=acquired,
            observed_version=coordination_records.digest_bytes(
                (project / relative).read_bytes()
            ),
        )
    )


# ---------------------------------------------------------------------------
# LS1-LS5: the discovery worlds
# ---------------------------------------------------------------------------


def listing_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LS1's world: the four source files, `base.py` claimed by the first attempt.

    The claim is acquired by the real command, so what the listing labels is a claim
    the proxy actually made."""
    project = project_fixture(tmp_path, sink_kind)
    for relative, lines, size in SINK_FILES:
        sized_file(project, relative, lines, size)
    proc = examples.run_cli(
        project,
        "file",
        "claim",
        BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink=sink_kind,
    )
    assert proc.returncode == 0, proc.stderr
    return project


#: LS2's counts: 237 files, 100 shown, 137 left over. The ordering has to put the
#: hundredth row -- the token the continuation prints -- at `cli.py`.
LS2_TOTAL = 237
LS2_SHOWN = 100
LS2_REMAINING = 137


def truncated_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LS2's world: 237 files under `src/arbite`, whose first 100 end at `cli.py`.

    The columns are the document's too, so the row it prints lands in the columns it
    prints it in: the widest path is 27 characters, the widest line count four digits
    (so `1000.0`-style sizes aside, the count column is six wide) and the widest size
    eight characters (`11.2 KiB`)."""
    project = project_fixture(tmp_path, sink_kind)
    sized_file(project, "src/arbite/__init__.py", 12, 400)
    for index in range(98):
        sized_file(project, f"src/arbite/a{index:03d}.py", 20, 1000)
    sized_file(project, CLI_PY, 1200, 11500)
    for index in range(LS2_TOTAL - LS2_SHOWN - 1):
        sized_file(project, f"src/arbite/z{index:03d}.py", 20, 900)
    sized_file(project, "src/arbite/zzzzzzzzzzzzz.py", 30, 5000)
    return project


def search_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LS3's world: exactly one line containing `O_CREAT`, at `file.py:141`."""
    project = project_fixture(tmp_path, sink_kind)
    sized_file(
        project,
        FILE_PY,
        412,
        18800,
        {141: "fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)"},
    )
    return project


#: LS4's counts: 500 matches shown, 499 left in 11 files, the last shown at
#: `schema.py:470` and the next one under `src/arbite/sinks` (which is what the
#: "narrow with" half of the hint names).
LS4_SHOWN = 500
LS4_REMAINING = 499
LS4_REMAINING_FILES = 11


def search_truncated_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LS4's world: 999 `def ` matches, the 500th printed at `schema.py:470`.

    Laid out so the ordering itself is the assertion: 60 matches in `cli.py` (the first
    at line 56, the frozen row), 360 across `coordination/`, 80 in `schema.py` (the last
    at 470, which is the continuation token) and 499 left in 11 files under `sinks/`,
    which is the directory the hint tells the caller to narrow to."""
    project = project_fixture(tmp_path, sink_kind)
    cli_matches = {56: "def _split_csv(value):"}
    cli_matches.update(match_lines(59, first=60, step=4, label="cli"))
    written(project, CLI_PY, lines_with_matches(CLI_PY, 400, cli_matches))
    for index in range(9):
        relative = f"src/arbite/coordination/mod_{index}.py"
        written(
            project,
            relative,
            lines_with_matches(relative, 300, match_lines(40, label=f"mod{index}")),
        )
    schema_matches = {1 + index * 6: f"def schema_{index}(value):" for index in range(79)}
    schema_matches[470] = "def schema_last(value):"
    written(project, SCHEMA_PY, lines_with_matches(SCHEMA_PY, 500, schema_matches))
    for index in range(LS4_REMAINING_FILES - 1):
        relative = f"src/arbite/sinks/sink_{index:02d}.py"
        written(
            project,
            relative,
            lines_with_matches(relative, 400, match_lines(45, label=f"sink{index}")),
        )
    last = f"src/arbite/sinks/sink_{LS4_REMAINING_FILES - 1:02d}.py"
    written(project, last, lines_with_matches(last, 400, match_lines(49, label="last")))
    return project


def arbite_dir_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """LS5's world: an initialised project with one scratchpad and one payload.

    No tickets, because the listing is of `.arbite` itself and a ticket would add a row
    the transcript does not have: the four entries are the generated guide,
    `project.yaml`, the agent scratchpad and the scratch area's single line."""
    project = state.initialise(tmp_path, sink_kind)
    written(project, SCRATCHPAD, "# claude.opus.001 scratchpad\n")
    payload = project / ".arbite" / "scratch" / "payload.py"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("x" * 128, encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# RD1-RD4: the read worlds
# ---------------------------------------------------------------------------


def read_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """RD1-RD4's world: `base.py` and `file.py` with the shapes the blocks print.

    `base.py` is unclaimed (RD1 reads it, RD4 reads it after an external edit), `cli.py`
    is the 3015-line/142 KiB file RD3 ranges over, and `file.py` is held by the first
    attempt at generation 3 -- the holder RD2's banner names."""
    project = project_fixture(tmp_path, sink_kind)
    written(project, BASE_PY, exact_size(BASE_PY, 570, 27000, BASE_SAMPLE))
    written(project, CLI_PY, exact_size(CLI_PY, 3015, 145400, CLI_SAMPLE))
    sized_file(project, FILE_PY, 412, 18800)
    put_claim(project, FILE_PY, HOLDER_TICKET, HOLDER, generation=3, sink_kind=sink_kind)
    return project


def edited_base(project: Path) -> Path:
    """The unattributed external edit RD4 reads after: 588 lines, 27.1 KiB.

    Written straight to disk, because that is what an external edit *is* -- no command
    of arbite's made it, and the read's job is to notice and attribute it to nobody."""
    return written(project, BASE_PY, exact_size(BASE_PY, 588, 27800, BASE_SAMPLE))
