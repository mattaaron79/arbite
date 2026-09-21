"""The frozen read transcripts this slice owns: RD1-RD5.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command,
exit code, stream and rows -- with ids, times, paths and digests normalised on both
sides (`examples.py`). `discovery_state.py` builds the world each block starts from: the
570-line `base.py` RD1 reads, the 3015-line `cli.py` RD3 ranges over, and the claim on
`file.py` at generation 3 that RD2's banner names.

Two blocks are visibly abridged by the document, and each test says which abridgement it
accepts (`examples.assert_scenario_abridged`):

- RD3's header prints `142 KiB` for a size the report renders as `142.0 KiB`, and its
  body shows two of the seven lines it asked for. The facts -- the whole-file digest,
  the range, the token, the two sampled lines -- are asserted.
- RD4 drops the claim parenthetical and the token's that RD1 prints in full. The facts
  -- including the drift note's wording -- are asserted, and the note is checked against
  the store as well, because "the last version arbite observed" is a stored fact.

Everything else is byte for byte: RD1, RD2 (both halves) and RD5.
"""

from __future__ import annotations

import examples
import discovery_state as state
from arbite.coordination import records as coordination_records

BASE_PY = state.BASE_PY
CLI_PY = state.CLI_PY
FILE_PY = state.FILE_PY
HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET
RIVAL = state.RIVAL
RIVAL_TICKET = state.RIVAL_TICKET


# --- RD1 --------------------------------------------------------------------


def test_RD1_read_an_unclaimed_file(tmp_path):
    """The whole version, the claim, the token, and the gutters a body prints in.

    The block shows two lines of a 570-line file, so the harness matches the sample by
    the line number it names: the assertion is that line 144 is `class TicketSink(ABC):`
    and line 145 is the docstring line, printed with the gutter the document shows."""
    project = state.read_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("RD1"), project)

    observations = state.store_for(project).records("observation")
    assert len(observations) == 1
    assert observations[0].digest == coordination_records.digest_bytes(
        (project / BASE_PY).read_bytes()
    ), "the token names the version of the bytes that were served"


def test_RD1_a_pre_claim_read_authorises_nothing(tmp_path):
    """The read RD1 serves is an observation of a free path: it records generation 0,
    so the claim a writer takes afterwards does not make it writable."""
    project = state.read_project(tmp_path)
    examples.assert_scenario(examples.scenario_block("RD1"), project)

    observation = state.store_for(project).records("observation")[0]
    assert observation.claim_generation == 0
    assert observation.authorizes_write(None) is False

    claimed = examples.run_cli(
        project, "file", "claim", BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER
    )
    assert claimed.returncode == 0, claimed.stderr
    claim = state.store_for(project).claims_for_path(BASE_PY)[0]

    assert observation.authorizes_write(claim) is False, "a read taken before the claim"
    assert claim.observed_version == observation.digest, "the claim records the same version"


# --- RD2 --------------------------------------------------------------------


def test_RD2_read_a_file_another_attempt_holds(tmp_path):
    """Bytes are served, the holder is named, and the token says it is read-only."""
    project = state.read_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("RD2"), project)

    observation = state.store_for(project).records("observation")[0]
    claim = state.store_for(project).claims_for_path(FILE_PY)[0]
    assert observation.claim_generation == claim.generation == 3
    assert observation.authorizes_write(claim) is False, "a foreign read cannot authorise"
    assert observation.digest == coordination_records.digest_bytes((project / FILE_PY).read_bytes())


def test_RD2_fail_if_busy_refuses_with_no_bytes_served(tmp_path):
    """`--fail-if-busy` is the other half of the block: exit 4, and nothing recorded.

    "No bytes were served" is asserted the only way it can be: no observation exists
    afterwards, so there is no token a caller could present."""
    project = state.read_project(tmp_path)
    busy = examples.scenario_from_block(examples.fenced_blocks("RD2")[1], "RD2 busy")

    examples.assert_scenario(busy, project)

    assert state.store_for(project).records("observation") == []


# --- RD3 --------------------------------------------------------------------


def test_RD3_ranged_read_keeps_the_whole_file_digest(tmp_path):
    """A range never narrows the digest: the whole file's version is what is recorded.

    The document abridges this block twice (a `142 KiB` size the report writes as
    `142.0 KiB`, and two of the seven lines of the range), so its facts are asserted --
    and the digest relationship, which is the block's whole point, is checked against
    the file and against the observation the command stored."""
    project = state.read_project(tmp_path)

    examples.assert_scenario_abridged(examples.scenario_block("RD3"), project)

    observation = state.store_for(project).records("observation")[0]
    assert observation.line_start == 1254 and observation.line_end == 1260
    assert observation.digest == coordination_records.digest_bytes((project / CLI_PY).read_bytes())
    assert len((project / CLI_PY).read_bytes()) == 145400, "the whole file, not the range"


# --- RD4 --------------------------------------------------------------------


def test_RD4_read_after_an_unattributed_external_edit(tmp_path):
    """A read notices bytes that changed under it, and blames no ticket for them.

    The drift note is the block's subject, so it is asserted twice: as the transcript
    prints it, and against the store -- the digest it names is the version the earlier
    read recorded, and the digest the report prints is what is on disk now."""
    project = state.read_project(tmp_path)
    examples.assert_scenario_abridged(examples.scenario_block("RD1"), project)
    first = state.store_for(project).records("observation")
    state.edited_base(project)

    report = examples.assert_scenario_abridged(examples.scenario_block("RD4"), project)

    assert "note: on-disk bytes differ from the last version arbite observed" in report
    assert "an external edit is attributable to no ticket" in report
    observations = state.store_for(project).records("observation")
    current = coordination_records.digest_bytes((project / BASE_PY).read_bytes())
    assert len(first) == 1 and observations != first, "the edit changed what a read records"
    versions = {observation.digest for observation in observations}
    assert len(versions) == 2 and current in versions, "both versions are kept as observations"
    before = (versions - {current}).pop()
    assert first[0].digest == before, "the first read observed the pre-edit bytes"
    assert coordination_records.short_digest(before) in report, "the note names that version"


# --- RD5 --------------------------------------------------------------------


def test_RD5_read_a_path_that_does_not_exist(tmp_path):
    """The missing-path refusal, now raised by the command it names.

    C04 asserted this wording at the refusal layer because `file read` did not exist;
    the wording is unchanged and this asserts the real command prints it, and that a
    refused read records nothing."""
    project = state.read_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("RD5"), project)

    assert state.store_for(project).records("observation") == []
    assert not (project / state.OLD_PY).exists(), "a read creates nothing"


# --- the transcripts the slice must keep honest -----------------------------


def test_the_read_blocks_name_the_attempts_the_fixture_holds(tmp_path):
    """The commands a read block runs are runnable: the ticket and the attempt they
    name exist, and the attempt owns the ticket (which is what makes the attribution
    the observation records real rather than assumed)."""
    project = state.read_project(tmp_path)
    rival = state.store_for(project).get_attempt(RIVAL)

    assert rival is not None and rival.ticket_id == RIVAL_TICKET
    assert rival.worker_id == state.RIVAL_WORKER
    assert state.store_for(project).get_attempt(HOLDER).ticket_id == HOLDER_TICKET
