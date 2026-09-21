"""The frozen removal and rename transcripts this slice owns: RN1-RN4, plus FC4.

Each block is asserted against `.arbite/planning/interaction-examples.md` -- command, exit
code, stream and rows -- with ids, times, paths and digests normalised on both sides
(`examples.py`). `moves_state.py` builds the world each block starts from: the 84-line
`dead.py` RN3 reports by line count, the two paths RN1's rename holds at generation 2 in one
acquisition, the destination RN2 refuses to replace, the tree RN4 refuses to delete, and a
real read token for every command that presents one.

The frozen blocks are the *shape* of the reports; the facts behind them are asserted here
against the store and the tree, so a transcript cannot pass while the bytes or the ownership
say something else: RN1's digest has to be the bytes at the destination and the claim that
now holds them, RN3's has to be the artifact the receipt kept.

FC4 is this slice's too, and deliberately: the creation case (`file claim` on a path that does
not exist yet) is what the new commands must leave exactly as it was, so it is asserted in a
project where a rename and a removal have already run.
"""

from __future__ import annotations

import examples
import claims_state as claims
import discovery_state as discovery
import moves_state as moves
import writes_state as writes_state
from arbite.coordination import records as coordination_records

RIVAL = moves.RIVAL
RIVAL_TICKET = moves.RIVAL_TICKET


# --- RN1 --------------------------------------------------------------------


def test_RN1_rename_claims_both_paths(tmp_path):
    """A move onto a free name: the bytes arrive, both paths are recorded, ownership moves.

    Every fact the block prints is checked against the store as well as the transcript: the
    receipt names both paths with the versions they had and have (`absent` on the side the
    bytes left), both versions are kept as artifacts, the source's claim is released, and the
    destination's claim is the one that now holds the digest that moved."""
    project = moves.rename_project(tmp_path)
    token = moves.token_for_rename(project)
    moved = moves.old_text().encode("utf-8")

    examples.assert_scenario(
        examples.with_token(examples.scenario_block("RN1"), token), project
    )

    assert not (project / moves.OLD_PY).exists(), "the source holds nothing afterwards"
    assert (project / moves.NEW_PY).read_bytes() == moved, "the bytes are the same bytes"

    store = moves.store_for(project)
    digest = coordination_records.digest_bytes(moved)
    assert store.claims_for_path(moves.OLD_PY) == [], "the source path is free again"
    held = store.claims_for_path(moves.NEW_PY)[0]
    assert held.generation == 2, "the destination keeps the acquisition's generation"
    assert held.observed_version == digest, "and records the bytes it now holds"
    released = moves.released_claim(project, moves.OLD_PY)
    assert released is not None and released.generation == 2
    assert released.release_reason == f"renamed to {moves.NEW_PY}"
    assert released.observed_version == coordination_records.ABSENT

    receipt = moves.receipts(project)[0]
    assert receipt.kind == "rename"
    assert receipt.paths == [moves.OLD_PY, moves.NEW_PY]
    assert receipt.claim_generation == 2
    assert receipt.before[moves.OLD_PY] == digest
    assert receipt.after[moves.NEW_PY] == digest
    assert receipt.after[moves.OLD_PY] == coordination_records.ABSENT
    assert receipt.before[moves.NEW_PY] == coordination_records.ABSENT
    assert moves.artifact(project, digest) == moved, "the receipt holds the bytes"
    assert moves.spent_by(project, token) == receipt.id, "one token, one mutation"


# --- RN2 --------------------------------------------------------------------


def test_RN2_rename_onto_an_existing_path(tmp_path):
    """A destination that exists is replaced deliberately or not at all.

    The refusal names the version that is there -- in the short form a caller can paste back
    -- and nothing moves: both files keep their bytes, no receipt is written, the read token
    is still unspent, and both claims are untouched."""
    project = moves.collision_project(tmp_path)
    token = moves.token_for_collision(project)
    before = {
        path: (project / path).read_bytes() for path in (moves.A_PY, moves.B_PY)
    }
    claims_before = {
        claim.path: (claim.generation, claim.state)
        for claim in (moves.claims_for(project, moves.A_PY)
                      + moves.claims_for(project, moves.B_PY))
    }

    output = examples.assert_scenario(
        examples.with_token(examples.scenario_block("RN2"), token), project
    )

    assert "--expect-dest" in output, "the repair is the flag that makes it deliberate"
    for path, content in before.items():
        assert (project / path).read_bytes() == content, f"{path} did not move"
    assert moves.receipts(project) == [], "a refusal records no receipt"
    assert moves.spent_by(project, token) is None, "a refusal spends nothing"
    assert {
        claim.path: (claim.generation, claim.state)
        for claim in (moves.claims_for(project, moves.A_PY)
                      + moves.claims_for(project, moves.B_PY))
    } == claims_before, "and it moves no ownership"


# --- RN3 --------------------------------------------------------------------


def test_RN3_remove(tmp_path):
    """A removal keeps what it deleted: the version in the receipt, the bytes as an artifact.

    The claim is deliberately left in place -- the path is now the absent case a creation is
    authorised from -- and it records `absent`, which is what `file claims` prints for a path
    with nothing under it."""
    project = moves.remove_project(tmp_path)
    token = moves.token_for_remove(project)
    removed = moves.dead_text().encode("utf-8")

    examples.assert_scenario(
        examples.with_token(examples.scenario_block("RN3"), token), project
    )

    assert not (project / moves.DEAD_PY).exists()
    store = moves.store_for(project)
    receipt = moves.receipts(project)[0]
    assert receipt.kind == "remove"
    assert receipt.paths == [moves.DEAD_PY]
    assert receipt.after[moves.DEAD_PY] == coordination_records.ABSENT
    assert moves.artifact(project, receipt.before[moves.DEAD_PY]) == removed
    held = store.claims_for_path(moves.DEAD_PY)[0]
    assert held.observed_version == coordination_records.ABSENT
    assert moves.spent_by(project, token) == receipt.id


# --- RN4 --------------------------------------------------------------------


def test_RN4_no_recursive_directory_deletion(tmp_path):
    """A tree is refused with the manual alternative, and nothing is touched.

    Checked without a ticket, an attempt or a token -- exactly as the block writes it --
    because the rule is a fact about the path, and it comes before everything a mutation
    would otherwise ask for."""
    project = moves.directory_project(tmp_path)
    tree = sorted(path.name for path in (project / moves.LEGACY_DIR).rglob("*"))

    examples.assert_scenario(examples.scenario_block("RN4"), project)

    assert (project / moves.LEGACY_FILE).exists(), "the tree is untouched"
    assert sorted(path.name for path in (project / moves.LEGACY_DIR).rglob("*")) == tree
    assert moves.receipts(project) == [], "and nothing was recorded"


# --- FC4 --------------------------------------------------------------------


def test_FC4_the_creation_case_still_passes_beside_the_move_commands(tmp_path):
    """The ticket's explicit check: FC4 unchanged, in a project where moves have happened.

    The world is FC4's (two acquisitions for `tic-cf9f` / `att-91bd`, so the block's third
    generation is the real one) with a rename and a removal already run by the *other*
    attempt -- which is the arrangement that would show it if the new commands disturbed the
    claim chain, the generations or the absent-path row."""
    project = claims.claimed_pair(claims.holder_project(tmp_path))
    discovery.written(project, moves.OLD_PY, moves.old_text())
    discovery.written(project, moves.DEAD_PY, moves.dead_text())
    claims.run(
        project, "file", "claim", moves.OLD_PY, moves.NEW_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
    )
    rename_token = writes_state.read_token(
        project, moves.OLD_PY, RIVAL_TICKET, RIVAL
    )
    renamed = examples.run_cli(
        project, *moves.rename_command(moves.OLD_PY, moves.NEW_PY, rename_token)
    )
    assert renamed.returncode == 0, renamed.stdout + renamed.stderr
    claims.run(
        project, "file", "claim", moves.DEAD_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
    )
    removed = examples.run_cli(
        project,
        *moves.remove_command(
            moves.DEAD_PY,
            writes_state.read_token(project, moves.DEAD_PY, RIVAL_TICKET, RIVAL),
        ),
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr

    examples.assert_scenario(examples.scenario_block("FC4"), project)

    claim = moves.claims_for(project, claims.RECORDS_PY)[0]
    assert claim.observed_version == coordination_records.ABSENT
    assert claim.generation == 3, "the block's generation, unaffected by the moves"
    assert not (project / claims.RECORDS_PY).exists(), "claiming creates nothing"
