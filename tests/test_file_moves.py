"""What the remove and rename commands promise, on both sinks and under real processes.

`test_move_examples.py` asserts the frozen transcripts. This file asserts the guarantees those
transcripts stand for, and it asserts them the only way a guarantee about *bytes and ownership*
can be asserted: by reading the tree and the store before and after.

- **A rename needs both paths, at one generation.** A destination another attempt holds is
  busy (4) and claims nothing; a destination this attempt does not hold is an error (1) whose
  hint claims both together; two generations are stale (5) with a re-acquisition named. Each of
  those is checked against the bytes, the claim records and the receipt count, because "no
  half-claim" is exactly a claim about records.
- **A destination that exists needs its version.** Without one it is refused; with a version
  that no longer matches it is stale; with the right one it is replaced, and the bytes it
  replaced are kept as evidence. The short digest a refusal prints is accepted, so its own hint
  is runnable.
- **A removal and a rename round-trip through their receipts.** The version that left the tree
  is in the artifact store byte for byte -- including binary -- and re-applying it through the
  proxy restores the digest that was removed.
- **One token, one mutation, and a closed ticket freezes everything** -- the guarantees C07
  established for writes, checked here for the two commands this slice adds.
- **No directory is ever created or deleted.** A tree is refused with the manual alternative, a
  destination whose parent does not exist is refused rather than `mkdir`ed, and a race between
  two renames of one source moves the bytes exactly once.

Both sinks store artifact content (tic-7c42: the file backend as a file named by the digest,
SQLite as a BLOB in its own database), so `project_kind` is both of them: a removal and a
rename round-trip through their receipts on either store, and every rule that does not depend
on a recorded mutation also runs on both through `kind`.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import claims_state as claims
import discovery_state as discovery
import examples
import moves_state as moves
import writes_state as writes_state
from arbite.coordination import records as coordination_records

RIVAL = moves.RIVAL
RIVAL_TICKET = moves.RIVAL_TICKET
HOLDER = moves.HOLDER
HOLDER_TICKET = moves.HOLDER_TICKET

#: Two directories, so a move across them is a real move rather than a rename in place.
SOURCE_DIR = "src/arbite"
DEST_DIR = "docs/notes"
ACROSS_SOURCE = f"{SOURCE_DIR}/old.py"
ACROSS_DEST = f"{DEST_DIR}/renamed.py"
OTHER_DEST = f"{DEST_DIR}/other.py"


@pytest.fixture(params=("file", "sqlite"))
def project_kind(request) -> str:
    """The sink a *successful* mutation runs on: both of them (see the module docstring)."""
    return request.param


def rename(project, source, dest, token, kind="file", *extra):
    return examples.run_cli(
        project,
        "file", "rename", source, dest,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
        "--read-token", token, *extra,
        sink=kind,
    )


def remove(project, path, token, kind="file"):
    return examples.run_cli(
        project,
        "file", "remove", path,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
        "--read-token", token,
        sink=kind,
    )


def claim_together(project, *paths, kind="file"):
    """One acquisition for every path, which is what a rename needs."""
    claims.run(
        project, "file", "claim", *paths,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=kind,
    )


def read_token(project, path, kind="file") -> str:
    return writes_state.read_token(project, path, RIVAL_TICKET, RIVAL, kind)


def create_tree(tmp_path, kind, *paths, payload=b"# a file\n", absent=()):
    """The RN world (both tickets and both attempts) with every path created or free, claimed.

    `paths` are written and then claimed; `absent` names paths that are left without a file but
    whose *directory* is made, which is what a rename destination needs: arbite creates no
    directory for a mutation (the path rules refuse a missing parent), so a caller that wants to
    move a file into a new directory makes the directory first and lets the proxy do the move.

    The acquisition is the *one* a rename needs: every claimed path lands at one generation, and
    a destination that does not exist is claimed as the absent path it is."""
    project = claims.holder_project(tmp_path, kind)
    for path in paths:
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    for path in absent:
        (project / path).parent.mkdir(parents=True, exist_ok=True)
    claim_together(project, *paths, *absent, kind=kind)
    return project


def receipts(project, kind="file") -> list:
    return moves.receipts(project, kind)


def claim_state(project, kind="file") -> dict:
    """Every claim record as `path -> (generation, state, observed version)`.

    The comparison that makes "nothing was claimed and nothing was released" checkable: it
    covers the active index and the released history in one value."""
    store = moves.store_for(project, kind)
    return {
        claim.path: (claim.generation, claim.state, claim.observed_version)
        for claim in store.records("claim")
    }


def unchanged(project, kind, state_before, receipt_count=0) -> None:
    """`nothing moved, nothing was claimed, nothing was recorded` -- the refusal assertion."""
    assert claim_state(project, kind) == state_before, "no claim was made or released"
    assert len(receipts(project, kind)) == receipt_count, "no receipt was written"


# --- the move itself, across directories --------------------------------------


def test_a_rename_moves_the_bytes_and_the_ownership_across_directories(tmp_path, project_kind):
    """Bytes, claim and receipt all end up on the destination's side -- and a later write
    there needs only a fresh read, which is what "the ownership moved" has to mean.

    The two directories are different trees, because a rename that only worked inside one
    directory would not be the command this slice promises."""
    kind = project_kind
    moved = b"".join(f"# moved line {index}\n".encode() for index in range(1, 40))
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, payload=moved, absent=(ACROSS_DEST,))
    token = read_token(project, ACROSS_SOURCE, kind)

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (project / ACROSS_SOURCE).exists()
    assert (project / ACROSS_DEST).read_bytes() == moved, "the same bytes, a new name"
    assert moves.claims_for(project, ACROSS_SOURCE, kind) == []
    assert moves.claims_for(project, ACROSS_DEST, kind)[0].observed_version == (
        coordination_records.digest_bytes(moved)
    )
    assert len(receipts(project, kind)) == 1

    # The destination is now the path this attempt owns, so one read authorises a write.
    writes_state.staged(project, "payload.py", "# rewritten after the move\n")
    written = examples.run_cli(
        project, "file", "write", ACROSS_DEST,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
        "--read-token", read_token(project, ACROSS_DEST, kind), "--input", "payload.py",
        sink=kind,
    )

    assert written.returncode == 0, written.stdout + written.stderr
    assert (project / ACROSS_DEST).read_text(encoding="utf-8") == "# rewritten after the move\n"


# --- both sinks ---------------------------------------------------------------


def test_both_sinks_perform_a_rename_and_keep_its_evidence(tmp_path, kind):
    """The same move on either store: the bytes arrive, one receipt says so, and the moved
    version is in the artifact store byte for byte -- the evidence a receipt has to be able to
    reproduce, and the reason both backends had to store content (tic-7c42)."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, absent=(ACROSS_DEST,))
    token = read_token(project, ACROSS_SOURCE, kind)
    before = (project / ACROSS_SOURCE).read_bytes()

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (project / ACROSS_DEST).read_bytes() == before
    assert not (project / ACROSS_SOURCE).exists(), "the bytes left the source"
    recorded = receipts(project, kind)
    assert len(recorded) == 1
    assert moves.artifact(project, recorded[0].before[ACROSS_SOURCE], kind) == before


# --- the claim rules a rename has to satisfy ----------------------------------


def test_a_destination_another_attempt_holds_is_busy_and_claims_nothing(tmp_path, kind):
    """Outcome 4, and nothing anywhere: the source's bytes, the destination's bytes, both
    claims and the receipt count are all exactly what they were before.

    This is the "no half-claim" case at the command level: a rename is all-or-nothing over its
    two paths, so a destination somebody else holds cannot leave this attempt owning the source
    any differently."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE)
    discovery.written(project, ACROSS_DEST, "# somebody else's\n")
    claims.run(
        project, "file", "claim", ACROSS_DEST,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER, sink_kind=kind,
    )
    token = read_token(project, ACROSS_SOURCE, kind)
    state_before = claim_state(project, kind)
    source_before = (project / ACROSS_SOURCE).read_bytes()
    dest_before = (project / ACROSS_DEST).read_bytes()

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert ACROSS_DEST in proc.stderr and HOLDER in proc.stderr, "the holder is named"
    assert (project / ACROSS_SOURCE).read_bytes() == source_before
    assert (project / ACROSS_DEST).read_bytes() == dest_before
    unchanged(project, kind, state_before)
    assert moves.spent_by(project, token, kind) is None, "nothing was spent"


def test_a_source_another_attempt_holds_is_busy_too(tmp_path, kind):
    """The other half of the ownership rule: a rename needs *both* paths, so a source somebody
    else holds refuses the whole operation (4) before any version is looked at.

    Nothing changes either: the source's bytes stay, the destination stays free, both claims
    are as they were, and the token the caller presented is still unspent."""
    project = claims.holder_project(tmp_path, kind)
    discovery.written(project, ACROSS_SOURCE, "# somebody else's source\n")
    (project / ACROSS_DEST).parent.mkdir(parents=True, exist_ok=True)
    claims.run(
        project, "file", "claim", ACROSS_SOURCE,
        "--ticket", HOLDER_TICKET, "--attempt", HOLDER, sink_kind=kind,
    )
    claims.run(
        project, "file", "claim", ACROSS_DEST,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=kind,
    )
    state_before = claim_state(project, kind)
    holder_token = writes_state.read_token(project, ACROSS_SOURCE, HOLDER_TICKET, HOLDER, kind)
    before = (project / ACROSS_SOURCE).read_bytes()

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, holder_token, kind)

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert ACROSS_SOURCE in proc.stderr and HOLDER in proc.stderr, "the holder is named"
    assert (project / ACROSS_SOURCE).read_bytes() == before
    assert not (project / ACROSS_DEST).exists()
    unchanged(project, kind, state_before)
    assert moves.spent_by(project, holder_token, kind) is None


def test_a_destination_this_attempt_does_not_hold_names_the_one_acquisition(tmp_path, kind):
    """An error (1), with the hint that claims *both* paths -- acquiring the destination on its
    own is the mistake this refusal exists to prevent, because separate acquisitions give the
    two paths separate generations and one token cannot authorise both.

    The destination's *directory* exists -- arbite creates none for a mutation -- and the path
    itself is nobody's, which is the state the refusal is about."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE)
    (project / ACROSS_DEST).parent.mkdir(parents=True, exist_ok=True)
    token = read_token(project, ACROSS_SOURCE, kind)
    state_before = claim_state(project, kind)

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"arbite file claim {ACROSS_SOURCE} {ACROSS_DEST}" in proc.stderr, (
        "the repair names both paths at once"
    )
    assert "no_claim" in proc.stderr
    assert (project / ACROSS_SOURCE).exists(), "the source is still there"
    assert not (project / ACROSS_DEST).exists()
    unchanged(project, kind, state_before)


def test_two_generations_are_stale_and_name_the_re_acquisition(tmp_path, kind):
    """Paths claimed separately did not change hands together, and the repair is a
    re-acquisition rather than the fresh read a moved version would want.

    The two paths are claimed one at a time, so they carry different generations -- the exact
    state that makes one read token cover only half of the move."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, absent=(ACROSS_DEST,))
    claims.run(
        project, "file", "claim", ACROSS_DEST,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=kind,
    )
    assert (
        moves.claims_for(project, ACROSS_SOURCE, kind)[0].generation
        != moves.claims_for(project, ACROSS_DEST, kind)[0].generation
    ), "the fixture has to hold the paths at different generations"
    token = read_token(project, ACROSS_SOURCE, kind)
    state_before = claim_state(project, kind)

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)

    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "no bytes were changed" in examples.normalise(proc.stderr, project)
    assert f"arbite file claim {ACROSS_SOURCE} {ACROSS_DEST}" in proc.stderr
    assert (project / ACROSS_SOURCE).exists()
    assert not (project / ACROSS_DEST).exists()
    unchanged(project, kind, state_before)


# --- the destination's version -------------------------------------------------


def test_a_replacing_rename_keeps_the_destination_it_replaced(tmp_path, project_kind):
    """With the right version the move happens, the old destination is evidence, and the short
    digest a refusal prints is accepted -- so the hint's own command is runnable.

    The two refusals in front of it are asserted here as well: no version at all, and a version
    that no longer matches the destination."""
    kind = project_kind
    source_bytes = b"# source\n"
    replaced = b"".join(f"# old dest {index}\n".encode() for index in range(1, 85))
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, ACROSS_DEST, payload=source_bytes)
    (project / ACROSS_DEST).write_bytes(replaced)
    claim_together(project, ACROSS_SOURCE, ACROSS_DEST, kind=kind)
    token = read_token(project, ACROSS_SOURCE, kind)

    no_version = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind)
    assert no_version.returncode == 1, no_version.stdout + no_version.stderr
    assert "no destination version was given" in no_version.stderr

    wrong = rename(
        project, ACROSS_SOURCE, ACROSS_DEST, token, kind, "--expect-dest", "sha256:" + "0" * 64
    )
    assert wrong.returncode == 5, wrong.stdout + wrong.stderr
    assert "no bytes were changed" in examples.normalise(wrong.stderr, project)
    assert (project / ACROSS_DEST).read_bytes() == replaced, "a stale version changes nothing"
    assert receipts(project, kind) == []
    assert moves.spent_by(project, token, kind) is None

    short = coordination_records.short_digest(coordination_records.digest_bytes(replaced))
    assert short in no_version.stderr, "the refusal prints the version to name"
    replaced_proc = rename(
        project, ACROSS_SOURCE, ACROSS_DEST, token, kind, "--expect-dest", short
    )

    assert replaced_proc.returncode == 0, replaced_proc.stdout + replaced_proc.stderr
    assert "replaced" in replaced_proc.stdout, "the report says what it replaced"
    assert short in replaced_proc.stdout
    assert (project / ACROSS_DEST).read_bytes() == source_bytes
    receipt = receipts(project, kind)[0]
    assert receipt.before[ACROSS_DEST] == coordination_records.digest_bytes(replaced)
    assert moves.artifact(project, receipt.before[ACROSS_DEST], kind) == replaced


def test_a_version_for_a_destination_that_is_not_there_is_an_error(tmp_path, kind):
    """Stating a version for a name that is free is a mistake about the world, not a stale
    read: the fix is to drop the flag (or pick another name), and the answer says which."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, absent=(ACROSS_DEST,))
    token = read_token(project, ACROSS_SOURCE, kind)

    proc = rename(
        project, ACROSS_SOURCE, ACROSS_DEST, token, kind, "--expect-dest", "sha256:" + "a" * 64
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "is not there" in proc.stderr
    assert "without '--expect-dest'" in proc.stderr
    assert (project / ACROSS_SOURCE).exists()
    assert not (project / ACROSS_DEST).exists()
    assert receipts(project, kind) == []


def test_a_malformed_version_is_refused_before_anything_is_read(tmp_path, kind):
    """`--expect-dest not-a-digest` is bad input (1), not a stale version (5)."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, ACROSS_DEST)
    token = read_token(project, ACROSS_SOURCE, kind)

    proc = rename(project, ACROSS_SOURCE, ACROSS_DEST, token, kind, "--expect-dest", "md5:nope")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--expect-dest takes a whole-file digest" in proc.stderr
    assert moves.spent_by(project, token, kind) is None


# --- round-tripping through the receipts --------------------------------------


def test_a_remove_round_trips_through_its_receipt_with_binary_bytes(tmp_path, project_kind):
    """The removed version is in the artifact store byte for byte, and writing it back through
    the proxy reproduces the digest that was removed.

    Binary on purpose: a receipt that kept only a text rendering could not reproduce these
    bytes, and this is the case WR6's shape exists for."""
    kind = project_kind
    binary = writes_state.icon_bytes(2048)
    project = create_tree(tmp_path, kind, "assets/icon.png", payload=binary)
    token = read_token(project, "assets/icon.png", kind)

    proc = remove(project, "assets/icon.png", token, kind)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (project / "assets/icon.png").exists()
    receipt = receipts(project, kind)[0]
    removed = receipt.before["assets/icon.png"]
    assert moves.artifact(project, removed, kind) == binary, "the evidence is the bytes"
    assert receipt.after["assets/icon.png"] == coordination_records.ABSENT
    held = moves.claims_for(project, "assets/icon.png", kind)[0]
    assert held.observed_version == coordination_records.ABSENT, "the claim records an empty path"

    # A path with no bytes is what a creation is authorised from, so the same claim suffices
    # and the probe the write records is the whole authorisation it needs.
    writes_state.staged(project, "icon.png", binary)
    restored = examples.run_cli(
        project, "file", "write", "assets/icon.png",
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
        "--input", "icon.png",
        sink=kind,
    )

    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert (project / "assets/icon.png").read_bytes() == binary
    assert coordination_records.digest_bytes(
        (project / "assets/icon.png").read_bytes()
    ) == removed, "the bytes that came back are the bytes that left"
    assert len(receipts(project, kind)) == 2


def test_a_rename_round_trips_through_its_receipts_with_binary_bytes(tmp_path, project_kind):
    """There and back again: both moves record both paths, and the second puts the original
    digest back -- so a rename is reversible from its evidence even though nothing does it
    automatically.

    Going back needs a fresh acquisition of both paths at one generation, which is the rule the
    stale refusal in the suite above names; this proves the repair it names actually works."""
    kind = project_kind
    binary = writes_state.icon_bytes(1536)
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, payload=binary, absent=(ACROSS_DEST,))
    original = coordination_records.digest_bytes(binary)
    forward_token = read_token(project, ACROSS_SOURCE, kind)

    forward = rename(project, ACROSS_SOURCE, ACROSS_DEST, forward_token, kind)
    assert forward.returncode == 0, forward.stdout + forward.stderr
    assert not (project / ACROSS_SOURCE).exists()
    assert coordination_records.digest_bytes((project / ACROSS_DEST).read_bytes()) == original

    claim_together(project, ACROSS_SOURCE, ACROSS_DEST, kind=kind)
    back = rename(project, ACROSS_DEST, ACROSS_SOURCE, read_token(project, ACROSS_DEST, kind), kind)

    assert back.returncode == 0, back.stdout + back.stderr
    assert (project / ACROSS_SOURCE).read_bytes() == binary
    assert not (project / ACROSS_DEST).exists()
    # The store's record order is not the order of events, so the two receipts are told apart
    # by what each one recorded: one left the source empty, the other left the destination empty.
    recorded = receipts(project, kind)
    assert len(recorded) == 2
    outbound = next(receipt for receipt in recorded
                    if receipt.after[ACROSS_SOURCE] == coordination_records.ABSENT)
    homeward = next(receipt for receipt in recorded
                    if receipt.after[ACROSS_DEST] == coordination_records.ABSENT)
    assert outbound.before[ACROSS_SOURCE] == outbound.after[ACROSS_DEST] == original
    assert homeward.before[ACROSS_DEST] == homeward.after[ACROSS_SOURCE] == original
    assert moves.artifact(project, original, kind) == binary


# --- the guarantees C07 established, for the paths this slice touches ----------


def test_a_replayed_remove_is_stale_and_records_nothing(tmp_path, project_kind):
    """One token authorises one mutation, for a removal exactly as for a write: the replay
    changes no bytes, writes no second receipt, and names the operation that spent the token.

    The file sink, because a successful removal is what a replay is a replay *of* -- on SQLite
    the first removal is the refusal asserted in `test_both_sinks_answer_...` above."""
    kind = project_kind
    project = create_tree(tmp_path, kind, "src/arbite/dead.py")
    token = read_token(project, "src/arbite/dead.py", kind)
    first = remove(project, "src/arbite/dead.py", token, kind)
    assert first.returncode == 0, first.stdout + first.stderr
    spender = moves.spent_by(project, token, kind)

    replay = remove(project, "src/arbite/dead.py", token, kind)

    assert replay.returncode == 5, replay.stdout + replay.stderr
    assert f"was already spent by {spender}" in replay.stderr, replay.stderr
    assert "no bytes were changed" in examples.normalise(replay.stderr, project)
    assert len(receipts(project, kind)) == 1, "the replay recorded nothing"


def test_a_remove_after_the_ticket_closed_is_stale(tmp_path, kind):
    """The close wins the race: the attempt is no longer current, the bytes stay, and no
    receipt is written -- the rule C07 checked for writes, one command further on."""
    project = create_tree(tmp_path, kind, "src/arbite/dead.py")
    token = read_token(project, "src/arbite/dead.py", kind)
    closed = examples.run_cli(project, "close", RIVAL_TICKET, sink=kind)
    assert closed.returncode == 0, closed.stdout + closed.stderr
    before = (project / "src/arbite/dead.py").read_bytes()

    proc = remove(project, "src/arbite/dead.py", token, kind)

    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "no bytes were changed" in examples.normalise(proc.stderr, project)
    assert (project / "src/arbite/dead.py").read_bytes() == before
    assert receipts(project, kind) == []
    assert moves.spent_by(project, token, kind) is None


# --- directories, and the race -------------------------------------------------


def test_no_mutation_creates_a_directory_or_targets_a_tree(tmp_path, kind):
    """A missing parent is refused rather than created, and a directory is never a target --
    on the source side of a rename, on its destination side, and in a removal.

    The RN4 block freezes the removal's wording; what this adds is the rest of the rule, on
    both sinks, with the tree checked afterwards."""
    project = create_tree(tmp_path, kind, ACROSS_SOURCE, ACROSS_DEST)
    token = read_token(project, ACROSS_SOURCE, kind)
    tree = f"{DEST_DIR}/tree"
    discovery.written(project, f"{tree}/inside.py", "# in a tree\n")

    missing_parent = rename(
        project, ACROSS_SOURCE, f"{DEST_DIR}/made/up/place.py", token, kind
    )
    assert missing_parent.returncode == 1, missing_parent.stdout + missing_parent.stderr
    assert "does not exist" in missing_parent.stderr
    assert not (project / DEST_DIR / "made").exists(), "nothing was created"
    assert (project / ACROSS_SOURCE).exists()

    onto_a_tree = rename(project, ACROSS_SOURCE, tree, token, kind)
    assert onto_a_tree.returncode == 1, onto_a_tree.stdout + onto_a_tree.stderr
    assert "is a directory" in onto_a_tree.stderr
    assert (project / tree / "inside.py").exists(), "the tree is untouched"

    from_a_tree = rename(project, tree, ACROSS_DEST, token, kind)
    assert from_a_tree.returncode == 1, from_a_tree.stdout + from_a_tree.stderr
    assert "is a directory" in from_a_tree.stderr

    removed = examples.run_cli(
        project, "file", "remove", tree,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, "--read-token", token,
        sink=kind,
    )
    assert removed.returncode == 1, removed.stdout + removed.stderr
    assert "recursive deletion is not supported" in removed.stderr
    assert (project / tree / "inside.py").exists()
    assert receipts(project, kind) == [], "no refusal recorded anything"
    assert moves.spent_by(project, token, kind) is None, "and none of them spent the token"


def test_two_processes_racing_one_rename_move_the_bytes_once(tmp_path, project_kind):
    """Two `arbite file rename` processes, one source, two destinations: exactly one moves it.

    The invariants are the assertion, and they are about bytes and records rather than about
    which process won: the source is gone, exactly one destination holds the bytes, the other
    holds nothing, exactly one receipt says a rename happened, and the source's claim is
    released exactly once. The loser's exit code depends on whether it saw the source already
    moved (1, "no such path") or could not take the store's lock in time (4) -- either way it
    changed nothing, which is what the tree and the store below check."""
    kind = project_kind
    moved = b"".join(f"# raced line {index}\n".encode() for index in range(1, 60))
    project = create_tree(
        tmp_path, kind, ACROSS_SOURCE, payload=moved, absent=(ACROSS_DEST, OTHER_DEST)
    )
    # Both tokens are taken before either process starts: a second read after the winner moved
    # the source would refuse (there are no bytes to serve), which is the race under test rather
    # than a fixture to be caught by it.
    destinations = (ACROSS_DEST, OTHER_DEST)
    tokens = [read_token(project, ACROSS_SOURCE, kind) for _ in destinations]

    environment = dict(os.environ, PYTHONPATH=str(examples.SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    racers = [
        subprocess.Popen(
            [
                sys.executable, "-m", "arbite.cli", "file", "rename",
                ACROSS_SOURCE, destinations[index],
                "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
                "--read-token", tokens[index],
            ],
            cwd=str(project),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(len(destinations))
    ]
    outcomes = [(*racer.communicate(), racer.returncode) for racer in racers]
    codes = sorted(code for _, _, code in outcomes)

    assert codes[0] == 0, outcomes
    assert codes[1] in (1, 4, 5), outcomes
    assert not (project / ACROSS_SOURCE).exists(), "the source is gone exactly once"
    landed = [
        path for path in destinations
        if (project / path).exists() and (project / path).read_bytes() == moved
    ]
    assert len(landed) == 1, outcomes
    empty = next(path for path in destinations if path not in landed)
    assert not (project / empty).exists(), "the loser left no half a move behind"

    finished = receipts(project, kind)
    assert [receipt.kind for receipt in finished] == ["rename"], "one move, one receipt"
    released = moves.released_claim(project, ACROSS_SOURCE, kind)
    assert released is not None and released.release_reason == f"renamed to {landed[0]}"
    assert sorted(
        claim.path for claim in moves.store_for(project, kind).active_claims()
    ) == sorted([landed[0], empty]), "ownership moved once, and only once"
    assert moves.claims_for(project, landed[0], kind)[0].observed_version == (
        coordination_records.digest_bytes(moved)
    )
