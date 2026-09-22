"""The frozen discovery transcripts this slice owns: LS1-LS6.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command,
exit code, the stream the body belongs to, and the rows -- with ids, times, paths and
digests normalised on both sides (`examples.py`). `discovery_state.py` builds the world
each block starts from, to the counts and shapes the block prints, so "the transcript
passes" means the real command printed exactly what the document says about a project
in the state it describes.

Two of the blocks need something said out loud, and each says it where it is used:

- LS2 and LS4 elide rows (`… 99 more files`). The harness asserts the *count* where the
  elision stands and compares every line around it byte for byte; the content of the
  elided rows is pinned by the property tests instead.
- LS6's two refusals are both frozen blocks, and both now go through the real commands:
  the escape through `file read` (which needs no `--ticket`, which is why the block has
  none) and the protected path through `file list`.

LS5 is byte for byte on both halves. C15 corrected its listing, which had hand-aligned its
note column and listed `project.yaml` before an agent scratchpad (canonical path order is
`.arbite/AGENTS.md`, `.arbite/agents/...`, `.arbite/project.yaml`, `.arbite/scratch/`) and
omitted the scratchpad row's version columns.
"""

from __future__ import annotations

import examples
import discovery_state as state


# --- LS1 --------------------------------------------------------------------


def test_LS1_bounded_listing(tmp_path):
    """Four files, their shape, and the one that is held: the row a caller plans from."""
    project = state.listing_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("LS1"), project)


# --- LS2 --------------------------------------------------------------------


def test_LS2_deterministic_truncation_with_a_continuation_token(tmp_path):
    """A hundred rows, a stated remainder, and a hint naming the last row it printed."""
    project = state.truncated_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("LS2"), project)


def test_LS2_the_continuation_is_the_command_the_hint_names(tmp_path):
    """The token in the hint is executable: following it reaches every remaining row.

    This is the property the block exists for, asserted where the transcript cannot:
    each page's hint continues from the last row that page printed, the pages
    concatenate into the canonical order, and no row is repeated or dropped."""
    project = state.truncated_project(tmp_path)

    rows, pages = _page_through(project, "file", "list", "src/arbite", "--count", "100")

    assert pages == 3, "100 rows, then 100, then the last 37"
    assert len(rows) == state.LS2_TOTAL
    assert rows == sorted(rows), "the pages concatenate into canonical order"
    assert len(set(rows)) == state.LS2_TOTAL, "no row is repeated across pages"
    assert len(rows) - state.LS2_SHOWN == state.LS2_REMAINING


def _page_through(project, *argv) -> tuple:
    """Every row a listing reaches by following the token its own hint prints."""
    rows, pages = [], 0
    while True:
        proc = examples.run_cli(project, *argv)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        rows.extend(line.split()[0] for line in lines[:-1])
        pages += 1
        if not lines[-1].startswith("truncated:"):
            return rows, pages
        assert lines[-1].startswith(
            "truncated: "
        ) and "continue with 'arbite file list src/arbite --after " in lines[-1], lines[-1]
        token = lines[-1].split("--after ")[1].split(" --count")[0]
        assert token == lines[-2].split()[0], "the token is the last row this page printed"
        argv = ("file", "list", "src/arbite", "--after", token, "--count", "100")


# --- LS3 --------------------------------------------------------------------


def test_LS3_search(tmp_path):
    """One literal match, printed with its file, its line and the line's text."""
    project = state.search_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("LS3"), project)


# --- LS4 --------------------------------------------------------------------


def test_LS4_search_truncation(tmp_path):
    """The remainder, the files it is spread over, and both halves of the hint."""
    project = state.search_truncated_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("LS4"), project)


def test_LS4_the_after_token_continues_the_search(tmp_path):
    """`--after PATH:LINE` resumes after exactly the match the hint printed."""
    project = state.search_truncated_project(tmp_path)

    first = examples.run_cli(project, "file", "search", "def ", "src/arbite")
    assert first.returncode == 0, first.stderr
    rest = examples.run_cli(
        project, "file", "search", "def ", "src/arbite", "--after", "src/arbite/schema.py:470"
    )
    assert rest.returncode == 0, rest.stderr

    page = first.stdout.splitlines()
    assert page[0] == "src/arbite/cli.py:56    def _split_csv(value):"
    assert len(page) == state.LS4_SHOWN + 2, "500 matches, the elision line and the hint"
    assert rest.stdout.splitlines()[-1] == (
        f"{state.LS4_REMAINING} matches in {state.LS4_REMAINING_FILES} files (no truncation)"
    )
    assert len(rest.stdout.splitlines()) == state.LS4_REMAINING + 1


# --- LS5 --------------------------------------------------------------------


def test_LS5_scratch_is_invisible_to_discovery(tmp_path):
    """Two answers about one area, both byte for byte.

    The listing's four entries are asserted exactly -- canonical order, the generated
    guide row, the scratchpad row with its own version columns, and the scratch area as
    one line of transport -- and the coordination tree is absent entirely. The search
    transcript is exact too, and its exit code 2 is the "matched nothing" answer.
    """
    project = state.arbite_dir_project(tmp_path)
    listing, search = _ls5_halves(examples.fenced_blocks("LS5")[0])

    listed = examples.run_cli(project, "file", "list", ".arbite")

    assert listed.returncode == 0, listed.stderr
    assert listed.stderr == ""
    assert examples.normalise(listed.stdout, project).strip("\n") == examples.normalise(
        listing, project
    ).strip("\n")
    assert "coordination" not in listed.stdout, "the coordination tree is invisible"

    found = examples.run_cli(project, "file", "search", "base.py", ".arbite/scratch")

    assert found.returncode == 2, found.stderr
    assert found.stderr == ""
    assert examples.normalise(found.stdout, project).strip("\n") == examples.normalise(
        search, project
    ).strip("\n")


# --- LS6 --------------------------------------------------------------------


def test_LS6_refuse_a_protected_or_escaping_path(tmp_path):
    """Both frozen refusals, both now through the commands they name.

    C04 asserted these at the refusal layer because neither `file read` nor `file list`
    existed; the wording was already right, and what this adds is that the commands
    raise it."""
    project = state.listing_project(tmp_path)
    escaping = examples.scenario_block("LS6")
    protected = _block_body(examples.fenced_blocks("LS6")[1])

    examples.assert_scenario(escaping, project)
    _assert_refusal(project, ["file", "list", ".git"], protected, exit_code=1)


# --- helpers ----------------------------------------------------------------


def _assert_refusal(project, argv, expected_body, exit_code: int) -> None:
    """One command's refusal compared with a frozen block: stderr only, exact."""
    proc = examples.run_cli(project, *argv)

    assert proc.returncode == exit_code, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert examples.normalise(proc.stderr, project).strip("\n") == examples.normalise(
        expected_body, project
    ).strip("\n")


def _block_body(block: str) -> str:
    """A fenced block's body: the command line and the `# exit N` comment removed."""
    return "\n".join(
        line
        for line in block.splitlines()
        if not line.startswith("$ ") and not line.strip().startswith("# exit")
    ).strip("\n")


def _ls5_halves(block: str) -> tuple:
    """LS5's one fence as its two transcripts: the listing and the scratch search."""
    commands = [index for index, line in enumerate(block.splitlines()) if line.startswith("$ ")]
    assert len(commands) == 2, "LS5 documents two commands in one block"
    lines = block.splitlines()
    listing = "\n".join(lines[commands[0] + 1 : commands[1]]).strip("\n")
    search = _block_body("\n".join(lines[commands[1] :]))
    return listing, search
