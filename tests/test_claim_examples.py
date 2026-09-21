"""The frozen file-claim transcripts this slice owns: FC1-FC8, LS6 and RD5.

Each scenario is asserted against its block in
`.arbite/planning/interaction-examples.md` -- command, exit code and the stream the
transcript belongs to -- with ids, times, paths and content digests normalised on both
sides (`examples.py`). `claims_state.py` builds the state each block starts from, and
the chain the document describes is built by running the earlier blocks for real
(FC1's acquisition, then FC2's), so "the transcript passes" means the real command
printed exactly what the document says about a project in the state it describes.

Two blocks are refusals of the *read* surface, which does not exist yet (tic-1c4f):
LS6's escaping path and RD5's missing path. Their wording belongs to this slice's
validation layer, so they are asserted through it -- LS6 through `file claim`, which
raises the same refusal, and RD5 through the refusal function the read slice will call.
Neither is faked: no `file read` command is added here, and the last test in this file
pins that.
"""

from __future__ import annotations

import json

import claims_state as claims
import examples
from arbite.coordination import paths as coordination_paths
from arbite.coordination import records as coordination_records
from arbite.coordination import results as outcomes

HOLDER_TICKET = claims.HOLDER_TICKET
HOLDER = claims.HOLDER
RIVAL = claims.RIVAL


def coordination(project, sink_kind: str = "file"):
    return claims.coordination(project, sink_kind)


def digest_of(project, relative: str) -> str:
    return coordination_records.digest_bytes((project / relative).read_bytes())


# --- FC1 --------------------------------------------------------------------


def test_FC1_claim_one_path(tmp_path):
    """One path, one generation, and the read the caller owes before writing."""
    project = claims.holder_project(tmp_path)

    examples.assert_scenario(examples.scenario_block("FC1"), project)

    claim = coordination(project).claims_for_path(claims.FILE_PY)[0]
    assert claim.generation == 1
    assert (claim.ticket_id, claim.attempt_id) == (HOLDER_TICKET, HOLDER)
    assert claim.observed_version == digest_of(project, claims.FILE_PY), (
        "the claim records the digest of the bytes it authorises"
    )
    assert [event.kind for event in coordination(project).events()] == ["claim.file"]
    assert coordination(project).events()[0].subject == claims.FILE_PY


# --- FC2 --------------------------------------------------------------------


def test_FC2_claim_several_all_or_nothing(tmp_path):
    """Two paths in one acquisition: one generation, canonical order, one commit."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))

    examples.assert_scenario(examples.scenario_block("FC2"), project)

    store = coordination(project)
    by_path = {claim.path: claim for claim in store.active_claims()}
    assert sorted(by_path) == [claims.BASE_PY, claims.FILE_PY], "canonical path order"
    assert {claim.generation for claim in by_path.values()} == {2}
    assert {claim.attempt_id for claim in by_path.values()} == {HOLDER}
    acquired = [event.subject for event in store.events() if event.kind == "claim.file"]
    assert acquired == [claims.FILE_PY, claims.BASE_PY, claims.FILE_PY]


# --- FC3 --------------------------------------------------------------------


def test_FC3_one_busy_path_in_a_multi_path_claim(tmp_path):
    """No waiting, no stealing: the whole request is refused and nothing is claimed."""
    project = claims.released_and_schema(claims.holder_project(tmp_path))

    examples.assert_scenario(examples.scenario_block("FC3"), project)

    store = coordination(project)
    assert store.claims_for_path(claims.FILE_PY) == [], "the free path was not claimed"
    assert {claim.attempt_id for claim in store.active_claims()} == {HOLDER}, (
        "the rival's attempt holds nothing"
    )
    assert store.active_attempts("tic-9b57"), "and its attempt is still active"


def test_FC3_the_json_payload_is_the_same_facts(tmp_path):
    """The block documents the branchable form, so the fields a caller branches on
    cannot drift from the text that promises them."""
    project = claims.released_and_schema(claims.holder_project(tmp_path))
    documented = examples.scenario_json_blocks("FC3")
    assert documented, "FC3 must document its JSON payload in a second block"

    proc = claims.run(
        project,
        "file",
        "claim",
        claims.FILE_PY,
        claims.SCHEMA_PY,
        "--ticket",
        "tic-9b57",
        "--attempt",
        RIVAL,
        "--json",
        expect=4,
    )
    payload = json.loads(proc.stdout)
    expected = documented[0]
    assert set(expected) <= set(payload)
    assert payload["error"] == expected["error"]
    assert payload["claimed"] == expected["claimed"] == []
    assert [entry["path"] for entry in payload["held"]] == [
        entry["path"] for entry in expected["held"]
    ]
    assert payload["held"][0]["generation"] == expected["held"][0]["generation"]
    assert payload["free"] == expected["free"]
    assert payload["next_actions"] == expected["next_actions"]


# --- FC4 --------------------------------------------------------------------


def test_FC4_claim_a_path_that_does_not_exist_yet(tmp_path):
    """Creating a file is a claimable act, and the claim records the absent version."""
    project = claims.claimed_pair(claims.holder_project(tmp_path))

    examples.assert_scenario(examples.scenario_block("FC4"), project)

    claim = coordination(project).claims_for_path(claims.RECORDS_PY)[0]
    assert claim.observed_version == coordination_records.ABSENT
    assert claim.generation == 3
    assert not (project / claims.RECORDS_PY).exists(), "claiming creates nothing"


# --- FC5 --------------------------------------------------------------------


def test_FC5_claim_for_an_attempt_that_does_not_own_the_ticket(tmp_path):
    """A file operation has to name the ticket's own attempt; the refusal says which."""
    project = claims.holder_project(tmp_path)
    before = coordination(project).records("claim")

    examples.assert_scenario(examples.scenario_block("FC5"), project)

    assert coordination(project).records("claim") == before, "nothing was claimed"


# --- FC6 --------------------------------------------------------------------


def test_FC6_what_is_held_right_now(tmp_path):
    """`file claims` is the one command that answers "who holds what"."""
    project = claims.released_and_schema(claims.holder_project(tmp_path))

    examples.assert_scenario(examples.scenario_block("FC6"), project)

    store = coordination(project)
    assert sorted(claim.path for claim in store.active_claims()) == [
        claims.SCHEMA_PY,
        claims.BASE_PY,
    ], "'schema.py' sorts before 'sinks/base.py'"
    assert store.claims_for_path(claims.FILE_PY) == [], "the released path is not listed"


def test_FC6_no_active_claims_is_an_answer_not_an_error(tmp_path):
    """The empty case: nothing held is exit 2, because a query that matched nothing is
    not an error (the same code `arbite events` uses for "nothing new")."""
    project = claims.holder_project(tmp_path)

    proc = claims.run(project, "file", "claims", expect=2)

    assert proc.stdout.strip().endswith("no active claims in " + coordination(project).get_workspace().id)


def test_FC6_all_shows_the_released_history(tmp_path):
    """A released claim stays in the store, so `--all` can show it -- and it says when
    it was released, which is the fact that explains why the path is free."""
    project = claims.released_base(claims.holder_project(tmp_path))

    proc = claims.run(project, "file", "claims", "--all")

    rows = proc.stdout.splitlines()
    assert rows[0].endswith("(1 active, 1 released):")
    assert any(
        row.strip().startswith(claims.BASE_PY) and " released " in row for row in rows
    ), proc.stdout
    assert any(
        row.strip().startswith(claims.FILE_PY) and " since " in row for row in rows
    ), proc.stdout
    assert sorted(claim.state for claim in coordination(project).records("claim")) == [
        coordination_records.CLAIM_ACTIVE,
        coordination_records.CLAIM_RELEASED,
    ]


# --- FC7 --------------------------------------------------------------------


def test_FC7_release_one_of_several(tmp_path):
    """The generation is revoked, the record stays as history, the attempt stays active,
    and the bytes are not touched."""
    project = claims.claimed_pair(claims.holder_project(tmp_path))
    bytes_before = (project / claims.BASE_PY).read_bytes()

    examples.assert_scenario(examples.scenario_block("FC7"), project)

    store = coordination(project)
    released = store.find_record("claim", coordination_records.claim_id_for(
        store.get_workspace().id, claims.BASE_PY
    ))
    assert released.state == coordination_records.CLAIM_RELEASED
    assert released.generation == 2
    assert released.release_reason == "edits complete"
    assert store.claims_for_path(claims.BASE_PY) == [], "it is out of the active index"
    assert store.find_record("claim", coordination_records.claim_id_for(
        store.get_workspace().id, claims.FILE_PY
    )).is_active, "the other claim of the pair is untouched"
    assert store.active_attempts(HOLDER_TICKET), "the attempt stays active"
    assert (project / claims.BASE_PY).read_bytes() == bytes_before


# --- FC8 --------------------------------------------------------------------


def test_FC8_re_acquire_after_release(tmp_path):
    """A released path is re-claimable, and the new generation is what kills the old
    token -- checked here with a read observation that was taken under generation 2."""
    project = claims.released_base(claims.holder_project(tmp_path))

    examples.assert_scenario(examples.scenario_block("FC8"), project)

    store = coordination(project)
    workspace = store.get_workspace().id
    claim = store.claims_for_path(claims.BASE_PY)[0]
    assert claim.generation == 3
    stale = _observation(claim, generation=2)
    fresh = _observation(claim, generation=3)
    assert not stale.authorizes_write(claim), "a token from generation 2 is dead"
    assert fresh.authorizes_write(claim)


def _observation(claim, generation: int):
    """A read observation of a claimed path, as a writer would present it."""
    return coordination_records.ReadObservation(
        id="op-4f19",
        path=claim.path,
        digest=claim.observed_version,
        observed_at=coordination_records.utc_now(),
        attempt_id=claim.attempt_id,
        claim_generation=generation,
    )


# --- LS6 --------------------------------------------------------------------


def test_LS6_refuse_a_protected_or_escaping_path(tmp_path):
    """Two refusals, and both are exactly the frozen text: an escape explains what was
    validated, and `.git` metadata is refused by name with no hint at all."""
    project = claims.holder_project(tmp_path)
    escaping = examples.scenario_block("LS6")
    protected = _block_without_command(examples.fenced_blocks("LS6")[1])

    escape_proc = claims.run(
        project,
        "file",
        "claim",
        "../../etc/passwd",
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        expect=escaping.exit_code,
    )
    protected_proc = claims.run(
        project, "file", "claim", ".git", "--ticket", HOLDER_TICKET, "--attempt", HOLDER, expect=1
    )

    assert escape_proc.stdout == "" and protected_proc.stdout == ""
    assert _normalised(escape_proc.stderr, project) == _normalised(escaping.stdout, project)
    assert _normalised(protected_proc.stderr, project) == _normalised(protected, project)
    assert coordination(project).records("claim") == [], "a refused path claims nothing"


# --- RD5 --------------------------------------------------------------------


def test_RD5_read_a_path_that_does_not_exist(tmp_path):
    """The read surface's refusal for a missing path, exactly as frozen -- raised by the
    function the read slice calls, and reachable in the same shape through this slice's
    own command, which *claims* that path instead (FC4)."""
    project = claims.holder_project(tmp_path)
    frozen = examples.scenario_block("RD5")

    refusal = coordination_paths.missing_path_refusal(claims.OLD_PY, HOLDER_TICKET, HOLDER)
    rendered = "error: " + str(refusal) + "\n" + outcomes.text_hint_of(refusal)

    assert _normalised(rendered) == _normalised(frozen.stdout)
    assert frozen.exit_code == 1
    claims.run(
        project,
        "file",
        "claim",
        claims.OLD_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
    )
    assert not (project / claims.OLD_PY).exists()


def test_the_read_surface_is_not_claimed_by_this_slice(tmp_path):
    """`file read` is tic-1c4f's, so it does not exist yet -- and this asserts that
    rather than leaving a reader to wonder whether the guide forgot it."""
    project = claims.holder_project(tmp_path)

    proc = claims.run(
        project,
        "file",
        "read",
        claims.FILE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        expect=2,
    )

    assert "invalid choice" in proc.stderr


# --- the harness itself -----------------------------------------------------


def test_the_harness_reads_a_refusal_as_stderr():
    """A block that starts with an outcome label belongs on stderr, which is where the
    CLI reports the exit-code vocabulary."""
    for scenario_id in ("LS6", "FC5", "FC3"):
        assert examples.scenario_block(scenario_id).on_stderr, scenario_id
    assert not examples.scenario_block("FC1").on_stderr


def test_the_harness_normalises_content_digests():
    """A digest is normalised like an id, because the document's blocks describe an
    earlier revision of this repo's own files -- while every other token stays literal."""
    raw = (
        "  src/arbite/sinks/file.py   sha256:4b8a1f0c9d2e  412 lines\n"
        "before: sha256:77c0ab19d3f1 (570 lines)   after: sha256:" + "a" * 64 + " (588 lines)\n"
    )

    normalised = examples.normalise(raw)

    assert normalised.startswith("  src/arbite/sinks/file.py   sha256:<DIGEST>  412 lines")
    assert "sha256:<DIGEST> (570 lines)" in normalised
    assert "sha256:<DIGEST> (588 lines)" in normalised
    assert "412 lines" in normalised and "570 lines" in normalised


def test_the_claim_scenarios_print_the_line_counts_the_document_states(tmp_path):
    """The one content fact the transcripts do assert literally, pinned where a fixture
    change would break it: 412 lines for `sinks/file.py`, 570 for `sinks/base.py`."""
    project = claims.holder_project(tmp_path)

    assert len((project / claims.FILE_PY).read_text().splitlines()) == claims.FILE_LINES
    assert len((project / claims.BASE_PY).read_text().splitlines()) == claims.BASE_LINES


# --- helpers ----------------------------------------------------------------


def _block_without_command(block: str) -> str:
    """A frozen block's body: the command line and the `# exit N` comment removed."""
    lines = [
        line
        for line in block.splitlines()
        if not line.startswith("$ ") and not line.strip().startswith("# exit")
    ]
    return "\n".join(lines).strip("\n")


def _normalised(text: str, root=None) -> str:
    return examples.normalise(text, root).strip("\n")
