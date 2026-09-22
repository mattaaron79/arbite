"""The frozen write and edit transcripts this slice owns: WR1-WR7, ED1-ED3, BY2.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command, exit
code, stream and rows -- with ids, times, paths and digests normalised on both sides
(`examples.py`). `writes_state.py` builds the world each block starts from: the 570-line
`base.py` the edit blocks quote by line number, the claim at generation 2 the reports name,
the 1024-byte image WR6 replaces, and a real read token for every command that presents one.

Every block is asserted byte for byte: the command, its exit code, the stream the body
belongs to, and every line -- with ids, times, paths and digests normalised on both sides
(`examples.py`). `writes_state.py` builds the world each block starts from: the 570-line
`base.py` the edit blocks quote by line number, the claim at generation 2 the reports name,
the 1024-byte image WR6 replaces, and a real read token for every command that presents one.

WR2 and WR3 are asserted as the *targets* the ticket's acceptance names rather than as frozen
blocks (WR2's transcript is also SC2, which tic-95c0 owns): "a stale token changes no bytes"
and "one token authorises one mutation", which are checked against the store and the bytes.

C15 made WR6, ED1, ED2 and ED3 byte-exact. The document had abridged three of them (showing
fewer lines than the report prints) and mis-stated ED1's diff columns; it now shows every
line, and ED1's `+5 -2` is the corrected arithmetic -- a one-line replacement is a removal
and an insertion, so the block's old `+4 -1` was impossible. Each delta is still recomputed
from the receipt's recorded bytes in the test below, so the transcript is not the only
witness.
"""

from __future__ import annotations

from dataclasses import replace

import examples
import writes_state as state
from arbite.coordination import records as coordination_records
from arbite.coordination.writes import line_delta

BASE_PY = state.BASE_PY
SCHEMA_PY = state.SCHEMA_PY
HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET


def with_token(scenario, token: str):
    """The frozen command with the document's illustrative read token replaced.

    The token is the one fact a transcript cannot carry: it is minted by the fixture's own
    `arbite file read`, so the block's `op-4f19` is a placeholder exactly as `tic-cf9f` is.
    The harness normalises both sides, and the command has to name the real one to run."""
    return replace(
        scenario,
        command=tuple(
            token if argument.startswith("op-") else argument for argument in scenario.command
        ),
    )


def write_command(path: str, token: str, payload: str = "base.py"):
    """The command a write block runs, built the way the blocks spell it."""
    return (
        "file", "write", path,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER,
        "--read-token", token, "--input", payload,
    )


# --- WR1 --------------------------------------------------------------------


def test_WR1_write_with_a_valid_token(tmp_path):
    """The write, its receipt, the payload it consumed, and the token it spent.

    Byte for byte, which means the fixture has to make every number true: 570 lines become
    588 by eighteen appended lines (`+18 -0`), the claim is at generation 2, and the token is
    the one the block prints (normalised). The delta is recomputed from the receipt's own
    before and after bytes below, so the transcript is not the only witness."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)

    output = examples.assert_scenario(with_token(examples.scenario_block("WR1"), token), project)

    assert "570 -> 588 lines  +18 -0" in output
    assert state.spent_by(project, token), "the write spent the token it used"
    before, after = state.recorded_versions(project)
    assert line_delta(
        state.artifact_bytes(project, before[BASE_PY]),
        state.artifact_bytes(project, after[BASE_PY]),
    ) == (18, 0), "the report's numbers are the diff of the recorded bytes"
    assert not (project / ".arbite" / "scratch" / "base.py").exists(), "success consumes it"


# --- WR2 (target: a stale token changes no bytes) ----------------------------


def test_WR2_stale_token_after_a_concurrent_change(tmp_path):
    """The plan-a-version-that-changed case: both digests, and a repair path.

    The substance the ticket's acceptance names: exit 5, the digest the token observed, the
    digest that is on disk now, "no bytes were changed", and a `next:` line naming the read
    that takes a fresh token -- plus, in the store, a token that is *not* spent, because a
    refusal must leave the caller exactly where it was."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)
    observed = coordination_records.digest_bytes((project / BASE_PY).read_bytes())
    state.external_edit(project)
    on_disk = coordination_records.digest_bytes((project / BASE_PY).read_bytes())
    before_bytes = (project / BASE_PY).read_bytes()

    proc = examples.run_cli(project, *write_command(BASE_PY, token))

    assert proc.returncode == 5, proc.stdout + proc.stderr
    # The digests are read off the raw stream: `short_digest` is the exact text the report
    # prints, and normalising first would replace the very facts being asserted.
    assert coordination_records.short_digest(observed) in proc.stderr, "the version it observed"
    assert coordination_records.short_digest(on_disk) in proc.stderr, "the version that is now"
    text = examples.normalise(proc.stderr, project)
    assert "no bytes were changed" in text
    read_command = f"arbite file read {BASE_PY} --ticket {HOLDER_TICKET} --attempt {HOLDER}"
    assert f"'{read_command}'" in proc.stderr, "the hint names the fresh read, runnable as written"
    assert (project / BASE_PY).read_bytes() == before_bytes, "a refused write changed no bytes"
    assert state.spent_by(project, token) is None, "a refused write does not spend the token"
    assert (project / ".arbite" / "scratch" / "base.py").exists(), "the payload is kept"
    assert state.receipt_count(project) == 0


def test_WR2_a_fresh_read_makes_the_change_possible(tmp_path):
    """The repair path the refusal names is not decoration: re-read, then retry."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)
    state.external_edit(project)
    refused = examples.run_cli(project, *write_command(BASE_PY, token))
    assert refused.returncode == 5, refused.stdout + refused.stderr

    retried = examples.run_cli(project, *write_command(BASE_PY, state.reread_token(project)))

    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert (project / BASE_PY).read_bytes() == state.base_text(
        append=state.APPENDED_LINES
    ).encode("utf-8")


# --- WR3 (target: one token authorises one mutation) ------------------------


def test_WR3_one_token_two_writes(tmp_path):
    """The second write with a spent token is stale, and names the operation that spent it.

    The guarantee is the assertion: one read token authorises exactly one mutation, the
    replay changes nothing, and the refusal says *which* operation used the token -- the fact
    that tells a caller this is not somebody else's edit."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)
    first = examples.run_cli(project, *write_command(BASE_PY, token))
    assert first.returncode == 0, first.stdout + first.stderr
    spender = state.spent_by(project, token)
    written = (project / BASE_PY).read_bytes()
    state.staged(project, "base.py", state.base_text(append=state.APPENDED_LINES))

    replay = examples.run_cli(project, *write_command(BASE_PY, token))

    assert replay.returncode == 5, replay.stdout + replay.stderr
    assert f"was already spent by {spender}" in replay.stderr, replay.stderr
    assert "no bytes were changed" in examples.normalise(replay.stderr, project)
    assert (project / BASE_PY).read_bytes() == written, "the replay changed no bytes"
    assert state.receipt_count(project) == 1, "the replay recorded nothing new"


# --- WR4 --------------------------------------------------------------------


def test_WR4_write_without_a_claim(tmp_path):
    """A read authorises nothing on its own: no claim is outcome 1, not a stale retry."""
    project = state.unclaimed_project(tmp_path)
    token = state.token_for_unclaimed(project)

    examples.assert_scenario(with_token(examples.scenario_block("WR4"), token), project)

    assert (project / BASE_PY).read_bytes() == state.base_text().encode("utf-8")


# --- WR5 --------------------------------------------------------------------


def test_WR5_write_after_the_ticket_closed(tmp_path):
    """The close wins the race: the attempt is no longer current and the bytes are frozen."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)
    closed = examples.run_cli(project, "close", HOLDER_TICKET)
    assert closed.returncode == 0, closed.stderr

    examples.assert_scenario(with_token(examples.scenario_block("WR5"), token), project)

    assert (project / BASE_PY).read_bytes() == state.base_text().encode("utf-8")
    assert state.receipt_count(project) == 0, "a refused write records no receipt"


# --- WR6 --------------------------------------------------------------------


def test_WR6_write_a_binary_file(tmp_path):
    """The binary shape: sizes, a byte-payload receipt, and no line-based report."""
    project = state.binary_project(tmp_path)
    token = state.token_for_binary(project)

    examples.assert_scenario(with_token(examples.scenario_block("WR6"), token), project)

    written = (project / state.ICON_PNG).read_bytes()
    assert written == state.icon_bytes(state.ICON_WRITTEN_BYTES), "the bytes are the payload"
    before, after = state.recorded_versions(project)
    assert len(state.artifact_bytes(project, before[state.ICON_PNG])) == state.ICON_BYTES
    assert state.artifact_bytes(project, after[state.ICON_PNG]) == written


# --- WR7 --------------------------------------------------------------------


def test_WR7_a_mutation_with_no_attempt(tmp_path):
    """An unattributed change is refused, and the refusal names the command that starts one."""
    project = state.write_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("WR7"), project)

    assert state.receipt_count(project) == 0


# --- ED1 --------------------------------------------------------------------


def test_ED1_edit_batch(tmp_path):
    """Two exact replacements, reported by the line each was found on.

    Byte for byte, including the `+5 -2` delta. The block's own `+4 -1` was impossible for a
    batch whose second edit changes a line in place, so C15 corrected the block; the delta is
    recomputed here from the bytes the receipt recorded, which is the fact the number stands
    for, so the transcript is not the only witness."""
    project = state.edits_project(tmp_path)
    token = state.token_for_edits(project)
    scenario = with_token(examples.scenario_block("ED1"), token)

    output = examples.assert_scenario(scenario, project)

    assert "570 -> 573 lines  +5 -2" in output
    assert output.splitlines()[1].endswith(
        f'replace at line {state.IN_PLACE_LINE}: "{state.IN_PLACE_OLD}" -> "{state.IN_PLACE_NEW}"'
    )
    assert output.splitlines()[2].endswith(
        f'replace at line {state.AMBIGUOUS_LINES[1]}: "{state.AMBIGUOUS_TEXT}" -> '
        f'"{state.AMBIGUOUS_TEXT} or StaleRead"'
    )
    before, after = state.recorded_versions(project)
    assert line_delta(
        state.artifact_bytes(project, before[BASE_PY]),
        state.artifact_bytes(project, after[BASE_PY]),
    ) == (5, 2), "the report's columns are the diff of the recorded bytes"
    assert not (project / ".arbite" / "scratch" / "edits.json").exists(), "success consumes it"


# --- ED2 --------------------------------------------------------------------


def test_ED2_an_ambiguous_edit_changes_nothing(tmp_path):
    """Three occurrences and no selector: the refusal lists them, and no byte moves."""
    project = state.ambiguous_project(tmp_path)
    token = state.token_for_edits(project)
    before = (project / BASE_PY).read_bytes()

    output = examples.assert_scenario(with_token(examples.scenario_block("ED2"), token), project)

    assert "occurs 3 times (lines 85, 229, 366)" in output
    assert (project / BASE_PY).read_bytes() == before, "an ambiguous batch changed nothing"
    assert (project / ".arbite" / "scratch" / "edits.json").exists(), "the payload is kept"
    assert state.receipt_count(project) == 0, "nothing was recorded"


# --- ED3 --------------------------------------------------------------------


def test_ED3_edit_from_stdin(tmp_path):
    """A batch piped in: the notice, the delta, and the receipt -- no payload file anywhere."""
    project = state.stdin_edits_project(tmp_path)
    token = state.token_for_schema(project)
    scenario = with_token(examples.scenario_block("ED3"), token)

    output = examples.assert_scenario(
        scenario, project, stdin=state.ed3_stdin_batch().decode("utf-8")
    )

    assert output.startswith("(payload read from stdin: 2 edits)")
    assert "657 -> 660 lines  +3 -0" in output
    assert "receipt: " in output and "claim gen 3" in output
    expected = state.schema_text().replace(
        state.SCHEMA_IMPORT_LINE, f"{state.SCHEMA_IMPORT_LINE}\nimport re", 1
    ).replace(
        state.SCHEMA_FIRST_LINE, f"{state.SCHEMA_FIRST_LINE}\n\n# typed by tic-cf9f", 1
    )
    assert (project / SCHEMA_PY).read_text(encoding="utf-8") == expected
    assert list((project / ".arbite" / "scratch").glob("*")) == [], "the batch was stdin only"


# --- BY2 --------------------------------------------------------------------


def test_BY2_generated_output_is_refused_by_default(tmp_path):
    """A cache path is refused by policy, before a ticket, a claim or a token is consulted."""
    project = state.generated_project(tmp_path)
    token = state.token_for_generated(project)

    examples.assert_scenario(with_token(examples.scenario_block("BY2"), token), project)

    assert (project / ".pytest_cache" / "v" / "cache" / "lastfailed").read_text() == "{}\n"
    assert state.receipt_count(project) == 0


# --- the transcripts' own premises -------------------------------------------


def test_the_write_blocks_name_the_attempts_the_fixture_holds(tmp_path):
    """The commands a write block runs are runnable: the attempt owns the ticket, the claim is
    the generation the report prints, and the token observes that very version."""
    project = state.write_project(tmp_path)
    token = state.token_for_write(project)
    store = state.store_for(project)
    claim = store.claims_for_path(BASE_PY)[0]
    observation = store.get_record("observation", token)

    assert claim.generation == 2, "WR1's report prints claim gen 2"
    assert observation.claim_generation == 2
    assert observation.digest == coordination_records.digest_bytes(
        (project / BASE_PY).read_bytes()
    )
    assert observation.spent_by is None, "a fixture token starts unspent"
