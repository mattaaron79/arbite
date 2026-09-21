"""The read surface's guarantees, asserted where a transcript cannot show them.

The RD blocks pin the report: the version line, the claim, the banner, the token and the
gutters. What they cannot show is what a token *is* -- an observation of a version, taken
under a claim generation -- so those relationships are asserted here against the store,
on both sinks: a range never narrows the digest, a foreign read cannot mint a writable
token, a pre-claim read authorises nothing, and a read changes no ownership and no bytes.
"""

from __future__ import annotations

import json

import pytest

import discovery_state as state
import examples
from arbite.coordination import records as coordination_records

SINKS = ("file", "sqlite")
BASE_PY = state.BASE_PY
CLI_PY = state.CLI_PY
FILE_PY = state.FILE_PY
HOLDER = state.HOLDER
HOLDER_TICKET = state.HOLDER_TICKET
RIVAL = state.RIVAL
RIVAL_TICKET = state.RIVAL_TICKET


def read(
    project,
    path,
    *extra,
    kind: str = "file",
    expect: int = 0,
    ticket=HOLDER_TICKET,
    attempt=HOLDER,
):
    """One `file read`, with the attribution the transcripts use unless told otherwise."""
    argv = ["file", "read", path]
    if ticket:
        argv += ["--ticket", ticket]
    if attempt:
        argv += ["--attempt", attempt]
    proc = examples.run_cli(project, *argv, *extra, sink=kind)
    assert proc.returncode == expect, (proc.stdout, proc.stderr)
    return proc


def store(project, kind: str = "file"):
    return state.store_for(project, kind)


def digest_of(project, relative: str) -> str:
    return coordination_records.digest_bytes((project / relative).read_bytes())


# --- the version ------------------------------------------------------------


@pytest.mark.parametrize("kind", SINKS)
def test_a_range_never_narrows_the_digest(tmp_path, kind):
    """The acceptance criterion: the digest is over the whole file, whatever is served."""
    project = state.read_project(tmp_path, kind)

    payload = json.loads(read(project, CLI_PY, "--lines", "1254:1260", "--json", kind=kind).stdout)

    assert payload["digest"] == digest_of(project, CLI_PY)
    assert payload["range"] == {"start": 1254, "end": 1260}
    observation = store(project, kind).records("observation")[0]
    assert observation.digest == digest_of(project, CLI_PY)
    assert (observation.line_start, observation.line_end) == (1254, 1260)
    served = [
        line
        for line in read(project, CLI_PY, "--lines", "1254:1260", kind=kind).stdout.splitlines()
        if " | " in line
    ]
    assert len(served) == 7, "the seven lines the range asked for"
    assert served[0].startswith("1254 | ") and served[-1].startswith("1260 | ")


@pytest.mark.parametrize("kind", SINKS)
def test_a_read_records_the_whole_version_as_one_observation(tmp_path, kind):
    """The report and the record are the same fact, on either sink."""
    project = state.read_project(tmp_path, kind)

    payload = json.loads(read(project, BASE_PY, "--json", kind=kind).stdout)

    observation = store(project, kind).records("observation")[0]
    assert payload["path"] == BASE_PY
    assert payload["digest"] == observation.digest == digest_of(project, BASE_PY)
    assert payload["lines"] == 570 and payload["bytes"] == 27000
    assert payload["token"]["id"] == observation.id, "the token *is* the observation id"
    assert payload["range"] is None
    event = [event for event in store(project, kind).events() if event.kind == "read.observed"][0]
    assert event.operation_id == observation.id and event.category == "read"
    assert event.subject == BASE_PY


# --- what a token is worth --------------------------------------------------


@pytest.mark.parametrize("kind", SINKS)
def test_a_foreign_read_cannot_mint_a_writable_token(tmp_path, kind):
    """The acceptance criterion's other half: someone else's claim is not yours."""
    project = state.read_project(tmp_path, kind)

    payload = json.loads(
        read(project, FILE_PY, "--json", kind=kind, ticket=RIVAL_TICKET, attempt=RIVAL).stdout
    )

    claim = store(project, kind).claims_for_path(FILE_PY)[0]
    observation = store(project, kind).records("observation")[0]
    assert claim.attempt_id == HOLDER and claim.generation == 3
    assert payload["claim"]["held_by_you"] is False
    assert payload["token"]["authorizes_write"] is False
    assert observation.authorizes_write(claim) is False
    assert observation.claim_generation == claim.generation, "the generation it observed"
    text = read(project, FILE_PY, kind=kind, ticket=RIVAL_TICKET, attempt=RIVAL).stdout
    assert "(read-only)" in text
    assert "bytes are served, but this read token cannot authorize a write" in text


def test_a_read_under_your_own_claim_can_authorize_one_mutation(tmp_path):
    """The same path, read by the attempt that holds it: the token is writable.

    This is the difference the claim line reports, and the reason a writer claims and then
    reads rather than reusing a read it took earlier."""
    project = state.read_project(tmp_path)

    text = read(project, FILE_PY).stdout
    payload = json.loads(read(project, FILE_PY, "--json").stdout)

    assert "claim: HELD by your attempt tic-cf9f / att-91bd, generation 3" in text
    assert "this read token authorizes one mutation of this path under this claim" in text
    assert payload["claim"]["held_by_you"] is True
    assert payload["token"]["authorizes_write"] is True


def test_a_released_claim_kills_the_tokens_taken_under_it(tmp_path):
    """Ownership is a live record, not a fact a read remembers.

    The released claim is still in the store as the path's history, and the observation
    still names its generation -- but neither is ownership, and a write under either is
    refused (tic-60c7 re-checks exactly this)."""
    project = state.read_project(tmp_path)
    read(project, FILE_PY)
    claim = store(project).claims_for_path(FILE_PY)[0]
    observation = store(project).records("observation")[0]
    assert observation.authorizes_write(claim) is True

    released = examples.run_cli(
        project,
        "file",
        "release",
        FILE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "handing back",
    )

    assert released.returncode == 0, released.stderr
    history = store(project).find_record("claim", claim.id)
    assert history.state == coordination_records.CLAIM_RELEASED
    assert observation.authorizes_write(history) is False
    assert observation.authorizes_write(None) is False


def test_a_read_records_the_attempt_it_was_attributed_to(tmp_path):
    """Attribution is a stored fact, so a token can be traced to its reader."""
    project = state.read_project(tmp_path)

    read(project, FILE_PY)

    observation = store(project).records("observation")[0]
    assert observation.attempt_id == HOLDER
    assert observation.actor == state.HOLDER_WORKER
    assert observation.path == FILE_PY


def test_an_unattributed_read_records_no_attempt_and_authorises_nothing(tmp_path):
    """A read may name nobody (the frozen LS6 block reads with no flags at all), and an
    unattributed observation is still a token -- one no mutation can use."""
    project = state.read_project(tmp_path)

    proc = read(project, BASE_PY, ticket=None, attempt=None)

    observation = store(project).records("observation")[0]
    assert observation.attempt_id is None and observation.actor is None
    assert observation.claim_generation == 0
    assert observation.authorizes_write(None) is False
    assert proc.stdout.splitlines()[2].startswith("read token: ")


# --- nothing else changes ---------------------------------------------------


def test_a_read_changes_no_ownership_and_no_bytes(tmp_path):
    """Serving bytes is the whole of a read's effect beyond the observation."""
    project = state.read_project(tmp_path)
    before_claims = [claim.to_dict() for claim in store(project).active_claims()]
    before_bytes = (project / BASE_PY).read_bytes()

    read(project, BASE_PY)

    assert [claim.to_dict() for claim in store(project).active_claims()] == before_claims
    assert (project / BASE_PY).read_bytes() == before_bytes
    assert store(project).records("receipt") == [], "no operation happened"
    assert len(store(project).records("observation")) == 1


def test_fail_if_busy_serves_nothing_and_records_nothing(tmp_path):
    """The flag exists so a caller that cannot use the bytes pays nothing for them."""
    project = state.read_project(tmp_path, "sqlite")

    proc = read(
        project,
        FILE_PY,
        "--fail-if-busy",
        kind="sqlite",
        expect=4,
        ticket=RIVAL_TICKET,
        attempt=RIVAL,
    )

    assert proc.stdout == "", "a refusal goes to stderr"
    assert proc.stderr.startswith("busy: ") and "no bytes were served" in proc.stderr
    assert "held by tic-cf9f / att-91bd (claude.opus.001)" in proc.stderr
    assert store(project, "sqlite").records("observation") == []


def test_a_read_that_is_refused_records_nothing(tmp_path):
    """Every refusal leaves the store exactly as it was: no token, no event."""
    project = state.read_project(tmp_path)

    missing = read(project, state.OLD_PY, expect=1)
    escape = examples.run_cli(project, "file", "read", "../../etc/passwd")
    bad_range = read(project, BASE_PY, "--lines", "900:910", expect=1)
    reversed_range = read(project, BASE_PY, "--lines", "12:4", expect=1)
    malformed = read(project, BASE_PY, "--lines", "abc", expect=1)

    assert missing.stderr.startswith("error: no such path")
    assert escape.returncode == 1 and "resolves outside the workspace root" in escape.stderr
    assert bad_range.returncode == 1 and "starts past the end" in bad_range.stderr
    assert reversed_range.returncode == 1 and "may not end before it starts" in reversed_range.stderr
    assert malformed.returncode == 1 and "START:END" in malformed.stderr
    assert store(project).records("observation") == []


def test_reading_a_directory_or_a_link_is_refused(tmp_path):
    """Only whole regular files are served, and never through a redirect."""
    project = state.read_project(tmp_path)
    (project / "link.py").symlink_to(project / FILE_PY)

    directory = read(project, "src/arbite/sinks", expect=1)
    link = read(project, "link.py", expect=1)

    assert "is a directory" in directory.stderr
    assert "symbolic link" in link.stderr
    assert store(project).records("observation") == []


def test_a_binary_file_is_served_as_a_version_without_gutters(tmp_path):
    """Text gutters would be a lie about bytes that are not text."""
    project = state.project_fixture(tmp_path)
    blob = project / "src/arbite/blob.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00\x01\x02\xff")

    payload = json.loads(read(project, "src/arbite/blob.bin", "--json").stdout)
    text = read(project, "src/arbite/blob.bin").stdout

    assert payload["digest"] == coordination_records.digest_bytes(b"\x00\x01\x02\xff")
    assert payload["lines"] is None and payload["bytes"] == 4
    assert text.splitlines()[0].endswith("0.0 KiB (binary)")
    assert " | " not in text, "no line gutters for bytes that are not text"
    assert "the digest above is the version" in text
    assert store(project).records("observation")[0].digest == payload["digest"]


# --- attribution refusals ---------------------------------------------------


def test_a_read_for_an_attempt_that_does_not_own_the_ticket_is_refused(tmp_path):
    """A read names an attempt the way a claim does, so a wrong one is the caller's to fix."""
    project = state.read_project(tmp_path)

    proc = read(project, FILE_PY, expect=1, ticket=HOLDER_TICKET, attempt=RIVAL)

    assert "does not own this ticket" in proc.stderr
    assert store(project).records("observation") == []


def test_half_an_attribution_is_refused(tmp_path):
    """`--ticket` and `--attempt` go together: attribution is a pair or nothing."""
    project = state.read_project(tmp_path)

    proc = read(project, BASE_PY, expect=1, attempt=None)

    assert "--ticket and --attempt go together" in proc.stderr
    assert store(project).records("observation") == []


def test_a_read_by_a_released_attempt_is_stale(tmp_path):
    """A token is only as current as the attempt that presents it."""
    project = state.read_project(tmp_path)
    examples.run_cli(
        project,
        "release",
        HOLDER_TICKET,
        "--agent",
        state.HOLDER_WORKER,
        "--reason",
        "handing the ticket back",
    )

    proc = read(project, FILE_PY, expect=5)

    assert "no longer current" in proc.stderr
    assert store(project).records("observation") == []


# --- the stream -------------------------------------------------------------


def test_reads_are_their_own_event_category(tmp_path):
    """A read observation is evidence, not job activity: the default view leaves it out.

    This is the split C05 fixed: the observation stream needs `--include-reads`, while a
    read that is part of an *operation* (tic-60c7's) is ordinary file activity. Nothing
    here writes into the job stream."""
    project = state.read_project(tmp_path)
    read(project, BASE_PY)

    default = json.loads(examples.run_cli(project, "events", "--json").stdout)
    included = json.loads(
        examples.run_cli(project, "events", "--include-reads", "--json").stdout
    )

    assert default["events"] == []
    kinds = {event["kind"] for event in included["events"]}
    assert kinds == {"read.observed"}
    assert included["events"][0]["result"] in {"read-only", "one mutation"}
    assert included["events"][0]["subject"] == BASE_PY


def test_every_read_is_its_own_token(tmp_path):
    """Two reads of one path are two observations: a token is spent by one mutation, so
    a second read has to be able to hand out a second one."""
    project = state.read_project(tmp_path)

    first = json.loads(read(project, BASE_PY, "--json").stdout)
    second = json.loads(read(project, BASE_PY, "--json").stdout)

    assert first["token"]["id"] != second["token"]["id"]
    tokens = {observation.id for observation in store(project).records("observation")}
    assert tokens == {first["token"]["id"], second["token"]["id"]}
