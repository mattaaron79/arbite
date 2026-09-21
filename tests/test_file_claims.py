"""Exclusive file claims: the rules the frozen transcripts cannot show, and the races.

The FC blocks pin the *output* of acquisition, release and inspection. What they cannot
show is the property the whole slice exists for -- that two agents cannot both hold one
path, that a conflict leaves nothing behind, that aliases collapse onto the same claim,
that a generation dies with its release -- so those are asserted here, against real
processes and against the store, on both sinks.

The races are real: several `arbite file claim` processes wait for a starting-gun file
and then run at the same moment, and the assertions are about what the store holds
afterwards. Mocks would prove nothing here -- the guarantee is the compare-and-swap on
the claim record's revision, which only a second process can lose.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import claims_state as claims
import examples
from arbite.coordination import records as coordination_records
from arbite.coordination.store import open_coordination_store

SINK_KINDS = ("file", "sqlite")
WORKER = Path(__file__).resolve().parent / "coordination_worker.py"
REPO_SRC = Path(__file__).resolve().parents[1] / "src"
HOLDER_TICKET = claims.HOLDER_TICKET
HOLDER = claims.HOLDER
RIVAL_TICKET = claims.RIVAL_TICKET
RIVAL = claims.RIVAL


def store_for(project, kind: str = "file"):
    return open_coordination_store(claims.state.sink_for(project, kind))


def run_file_claim(project, paths, ticket, attempt, kind: str = "file", *extra):
    """One `arbite file claim`, with everything the frozen commands pass."""
    return examples.run_cli(
        project,
        "file",
        "claim",
        *paths,
        "--ticket",
        ticket,
        "--attempt",
        attempt,
        *extra,
        sink=kind,
    )


def race_file_claims(project, requests, kind: str = "file"):
    """Run several `arbite file claim` invocations at once, released by a starting gun.

    `requests` is one `(ticket, attempt, paths)` triple per process. The gun is what
    makes the race a race: processes the parent happens to schedule one after another
    may never overlap, and a race that did not overlap proves nothing."""
    gun = project / "go"
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                "file-claim",
                str(project),
                kind,
                ticket,
                attempt,
                str(gun),
                ",".join(paths),
            ],
            cwd=str(project),
            env=dict(os.environ, PYTHONPATH=str(REPO_SRC)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ticket, attempt, paths in requests
    ]
    time.sleep(0.6)  # let every worker reach the gun before firing it
    gun.write_text("go", encoding="utf-8")
    return [(worker.communicate(timeout=120) + (worker.returncode,)) for worker in workers]


# --- one path, one owner ----------------------------------------------------


def test_two_claims_on_one_path_produce_one_winner_and_one_busy(tmp_path):
    """The acceptance criterion, through the real command: the loser is told who holds
    it, and what it was told it could not have is exactly what the winner holds."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))

    proc = run_file_claim(project, [claims.FILE_PY], RIVAL_TICKET, RIVAL)

    assert proc.returncode == 4
    assert f"held by {HOLDER_TICKET} / {HOLDER}" in proc.stderr
    assert "gen 1" in proc.stderr
    store = store_for(project)
    assert [claim.attempt_id for claim in store.claims_for_path(claims.FILE_PY)] == [HOLDER]
    assert store.claims_for_path(claims.BASE_PY) == [], "the loser claimed nothing else"


def test_a_held_path_cannot_be_reached_through_an_alias(tmp_path):
    """Aliases and escapes cannot bypass ownership, whatever route they arrive by: every
    spelling of the path resolves to the same canonical claim, so every one is busy."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))
    aliases = [
        "./" + claims.FILE_PY,
        claims.FILE_PY.replace("sinks/", "sinks/../sinks/"),
        claims.FILE_PY.replace("sinks/", "sinks//"),
        str(project / claims.FILE_PY),
    ]

    for alias in aliases:
        proc = run_file_claim(project, [alias], RIVAL_TICKET, RIVAL)
        assert proc.returncode == 4, alias
        assert claims.FILE_PY in proc.stderr, f"{alias} must be reported canonically"

    assert [claim.path for claim in store_for(project).active_claims()] == [claims.FILE_PY]


def test_a_conflict_leaves_no_partial_claims(tmp_path):
    """All-or-nothing: the free half of a refused request is not claimed either, so a
    caller never has to work out which half of its request it got."""
    project = claims.claimed_pair(claims.holder_project(tmp_path))  # base.py + file.py held
    store = store_for(project)
    # Free the pair's second path, then ask for both again from the rival: one busy path
    # must refuse the whole request.
    claims.run(
        project,
        "file",
        "release",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "done",
    )

    proc = run_file_claim(project, [claims.BASE_PY, claims.FILE_PY], RIVAL_TICKET, RIVAL)

    assert proc.returncode == 4
    assert store_for(project).claims_for_path(claims.BASE_PY) == [], "the free path was not taken"
    assert [claim.attempt_id for claim in store_for(project).active_claims()] == [HOLDER]


def test_the_canonical_order_is_the_recorded_order(tmp_path):
    """The whole point of the ordering rule: acquisition is written in canonical path
    order whatever order the caller typed, so two agents requesting an overlapping set
    always contend in the same sequence."""
    project = claims.holder_project(tmp_path)
    reversed_order = [claims.SCHEMA_PY, claims.BASE_PY]

    proc = run_file_claim(project, reversed_order, HOLDER_TICKET, HOLDER)
    assert proc.returncode == 0, proc.stderr

    store = store_for(project)
    # The claim *index* is keyed by record id, so it is read sorted; what the ordering
    # rule is about is the order things happen in, which is the event stream and the
    # printed rows.
    assert sorted(claim.path for claim in store.active_claims()) == sorted(reversed_order)
    assert [event.subject for event in store.events()] == sorted(reversed_order)
    assert proc.stdout.index(claims.SCHEMA_PY) < proc.stdout.index(claims.BASE_PY)


def test_a_request_that_names_one_path_twice_is_one_claim(tmp_path):
    """`a.py a.py` and `a.py ./a.py` are one path, so the count, the generation and the
    claim records agree with the canonical set rather than with the argument list."""
    project = claims.holder_project(tmp_path)

    proc = run_file_claim(project, [claims.BASE_PY, "./" + claims.BASE_PY], HOLDER_TICKET, HOLDER)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("claimed 1 path")
    assert len(store_for(project).records("claim")) == 1


# --- generation lifecycle ---------------------------------------------------


def test_release_revokes_the_generation_and_reacquiring_mints_a_new_one(tmp_path):
    """One claim record per path (the current-state index), so the record's *revision*
    is what acquisition contends on and its `generation` is what identifies the
    acquisition that owns it now."""
    project = claims.holder_project(tmp_path)
    claims.run(
        project, "file", "claim", claims.BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER
    )
    store = store_for(project)
    workspace = store.get_workspace().id
    record_id = coordination_records.claim_id_for(workspace, claims.BASE_PY)
    first = store.get_record("claim", record_id)
    first_revision = store.revision("claim", record_id)

    claims.run(
        project,
        "file",
        "release",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "handing back",
    )
    released = store_for(project).get_record("claim", record_id)
    assert released.state == coordination_records.CLAIM_RELEASED
    assert released.observed_version == first.observed_version, "the version is kept"
    assert released.release_reason == "handing back"

    claims.run(
        project, "file", "claim", claims.BASE_PY, "--ticket", HOLDER_TICKET, "--attempt", HOLDER
    )
    again = store_for(project).get_record("claim", record_id)
    assert again.id == first.id, "the same record speaks for the path"
    assert again.generation == first.generation + 1
    assert store_for(project).revision("claim", record_id) > first_revision
    assert [claim.state for claim in store_for(project).records("claim")] == [
        coordination_records.CLAIM_ACTIVE
    ], "the released state is gone, and the release event is the history"


def test_only_the_holder_releases_a_claim(tmp_path):
    """A release by another attempt is refused rather than quietly freeing somebody
    else's path..."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))

    proc = claims.run(
        project,
        "file",
        "release",
        claims.FILE_PY,
        "--ticket",
        RIVAL_TICKET,
        "--attempt",
        RIVAL,
        "--reason",
        "not mine",
        expect=1,
    )

    assert "not by your attempt" in proc.stderr
    assert store_for(project).claims_for_path(claims.FILE_PY)[0].attempt_id == HOLDER


def test_releasing_an_unclaimed_path_says_so(tmp_path):
    """...and so is a release of a path nobody holds, because the caller's command is
    the thing to fix."""
    project = claims.holder_project(tmp_path)

    proc = claims.run(
        project,
        "file",
        "release",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "nothing here",
        expect=1,
    )

    assert "is not claimed by attempt" in proc.stderr


def test_a_taken_over_attempt_cannot_acquire_anything(tmp_path):
    """After a forced takeover the ticket belongs to a *new* attempt, so the old one's
    claim command is refused (exit 1, not busy): it named an attempt the ticket no longer
    has, and only the takeover itself could have made that happen. The claims such an
    attempt leaves behind are released by the close cascade (tic-e9ed)."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))
    takeover = claims.run(
        project,
        "claim",
        HOLDER_TICKET,
        "--agent",
        "claude.haiku.003",
        "--force",
        "--reason",
        "the previous worker stopped",
    )
    assert "taken over" in takeover.stdout

    proc = claims.run(
        project,
        "file",
        "claim",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        expect=1,
    )

    assert "does not own this ticket" in proc.stderr
    assert store_for(project).claims_for_path(claims.BASE_PY) == []


def test_an_attempt_whose_ticket_was_released_is_stale(tmp_path):
    """The other half of the ownership guard: an attempt that ended no longer owns
    anything, so its token is stale (exit 5) rather than merely wrong."""
    project = claims.claimed_file_py(claims.holder_project(tmp_path))
    claims.run(
        project,
        "release",
        HOLDER_TICKET,
        "--agent",
        "claude.opus.001",
        "--reason",
        "handing the ticket back",
    )

    proc = claims.run(
        project,
        "file",
        "claim",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        expect=5,
    )

    assert "no longer current" in proc.stderr
    assert store_for(project).claims_for_path(claims.BASE_PY) == []


def test_a_claim_for_an_attempt_that_does_not_own_the_ticket_is_an_error(tmp_path):
    """Not busy and not stale: the caller named the wrong attempt for the ticket, so the
    command has to be corrected (exit 1)."""
    project = claims.holder_project(tmp_path)

    proc = run_file_claim(project, [claims.BASE_PY], HOLDER_TICKET, RIVAL)

    assert proc.returncode == 1
    assert "does not own this ticket" in proc.stderr
    assert store_for(project).records("claim") == []


# --- path validation --------------------------------------------------------


def test_paths_outside_the_root_are_refused(tmp_path):
    """Traversal and absolute escapes are the same refusal, and the message says what
    was validated against what (the frozen LS6 text)."""
    project = claims.holder_project(tmp_path)

    for raw in ("../../etc/passwd", "/etc/passwd", "src/../../outside.py"):
        proc = run_file_claim(project, [raw], HOLDER_TICKET, HOLDER)
        assert proc.returncode == 1, raw
        assert "resolves outside the workspace root" in proc.stderr, raw
        assert "paths are validated against the project root" in proc.stderr, raw


def test_protected_paths_are_refused_by_name(tmp_path):
    """`.git` metadata, arbite's runtime state and arbite's configuration are not the
    proxy's business, and each says which rule it tripped."""
    project = claims.holder_project(tmp_path)
    cases = {
        ".git": "arbite does not manage .git metadata",
        ".git/config": "arbite does not manage .git metadata",
        ".arbite/coordination/claims/clm-1234.json": "arbite does not manage its own runtime state",
        ".arbite/scratch/payload.py": "arbite does not manage its own runtime state",
        ".arbite/project.yaml": "arbite does not manage its own configuration",
    }

    for raw, expected in cases.items():
        proc = run_file_claim(project, [raw], HOLDER_TICKET, HOLDER)
        assert proc.returncode == 1, raw
        assert f"'{raw}' is protected: {expected}" in proc.stderr, raw

    assert store_for(project).records("claim") == []


def test_directories_and_special_files_are_not_claimable(tmp_path):
    """v1 owns whole regular files, so a directory, a fifo and a hard-linked target are
    all refused at use time rather than followed or guessed at."""
    project = claims.holder_project(tmp_path)
    os.link(project / claims.SCHEMA_PY, project / "hard.py")
    os.mkfifo(project / "pipe.py")

    directory = run_file_claim(project, ["src/arbite"], HOLDER_TICKET, HOLDER)
    hard = run_file_claim(project, ["hard.py"], HOLDER_TICKET, HOLDER)
    fifo = run_file_claim(project, ["pipe.py"], HOLDER_TICKET, HOLDER)

    assert directory.returncode == 1 and "is a directory" in directory.stderr
    assert hard.returncode == 1 and "hard links" in hard.stderr
    assert fifo.returncode == 1 and "not a regular file" in fifo.stderr
    assert store_for(project).records("claim") == []


def test_a_symlinked_component_is_refused(tmp_path):
    """A path reached through a link is refused, because the file behind it is not the
    path a claim would name -- and a redirect can be swapped between two commands."""
    project = claims.holder_project(tmp_path)
    (project / "link").symlink_to(project / "src", target_is_directory=True)

    proc = run_file_claim(project, ["link/arbite/schema.py"], HOLDER_TICKET, HOLDER)

    assert proc.returncode == 1
    assert "symbolic link" in proc.stderr
    assert store_for(project).records("claim") == []


def test_a_claim_whose_parent_directory_is_missing_is_refused(tmp_path):
    """Creating a file is claimable; creating the tree for it is not this slice's job,
    and the refusal says so rather than failing halfway."""
    project = claims.holder_project(tmp_path)

    proc = run_file_claim(project, ["src/new/deep/thing.py"], HOLDER_TICKET, HOLDER)

    assert proc.returncode == 1
    assert "cannot be created" in proc.stderr


def test_odd_spellings_are_refused_or_canonicalised(tmp_path):
    """A backslash is not a separator here, the root is not a file, and an absolute path
    *inside* the root is an alias of the relative one."""
    project = claims.holder_project(tmp_path)

    backslash = run_file_claim(project, ["src\\arbite\\schema.py"], HOLDER_TICKET, HOLDER)
    root = run_file_claim(project, ["."], HOLDER_TICKET, HOLDER)
    empty = run_file_claim(project, [""], HOLDER_TICKET, HOLDER)

    assert backslash.returncode == 1 and "backslash" in backslash.stderr
    assert root.returncode == 1 and "workspace root itself" in root.stderr
    assert empty.returncode == 1

    inside = run_file_claim(project, [str(project / claims.SCHEMA_PY)], HOLDER_TICKET, HOLDER)
    assert inside.returncode == 0, inside.stderr
    assert [claim.path for claim in store_for(project).active_claims()] == [claims.SCHEMA_PY]


# --- both sinks -------------------------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_the_claim_index_survives_a_fresh_store_and_means_the_same_on_both_sinks(tmp_path, kind):
    """Persistence and parity: the same acquisitions produce the same generations,
    states and versions whichever backend holds them."""
    project = claims.holder_project(tmp_path, kind)

    claims.run(
        project,
        "file",
        "claim",
        claims.BASE_PY,
        claims.SCHEMA_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=kind,
    )
    claims.run(
        project,
        "file",
        "release",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "handing back",
        sink_kind=kind,
    )
    claims.run(
        project,
        "file",
        "claim",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=kind,
    )

    store = store_for(project, kind)  # a brand-new handle, reading what was written
    held = {claim.path: claim for claim in store.active_claims()}
    assert sorted(held) == sorted([claims.BASE_PY, claims.SCHEMA_PY])
    assert held[claims.SCHEMA_PY].generation == 1
    assert held[claims.BASE_PY].generation == 2, "the re-acquisition minted the next one"
    assert all(claim.observed_version.startswith("sha256:") for claim in held.values())
    assert store.info().claims_active == 2
    # One event per path acquired or released: the pair (2), the release (1), the
    # re-acquisition (1).
    assert store.info().events == 4
    assert [claim.state for claim in store.records("claim")] == [
        coordination_records.CLAIM_ACTIVE,
        coordination_records.CLAIM_ACTIVE,
    ]


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_claim_survives_a_reopen_and_the_released_record_stays_history(tmp_path, kind):
    """A released claim is kept, not deleted: `--all` can show it and the release event
    still names it after the record has been re-used."""
    project = claims.holder_project(tmp_path, kind)
    claims.run(
        project,
        "file",
        "claim",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        sink_kind=kind,
    )
    claims.run(
        project,
        "file",
        "release",
        claims.BASE_PY,
        "--ticket",
        HOLDER_TICKET,
        "--attempt",
        HOLDER,
        "--reason",
        "handing back",
        sink_kind=kind,
    )

    history = claims.run(project, "file", "claims", "--all", sink_kind=kind)
    released_events = [
        event
        for event in store_for(project, kind).events()
        if event.kind == "release.file"
    ]

    assert "1 released" in history.stdout
    assert len(released_events) == 1
    assert released_events[0].subject == claims.BASE_PY
    assert released_events[0].payload["reason"] == "handing back"


# --- real races -------------------------------------------------------------


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_three_attempts_race_for_one_path(tmp_path, kind):
    """Three processes, one path: exactly one wins, the others are busy, the store holds
    one claim -- and the losers wrote nothing anywhere."""
    project = claims.holder_project(tmp_path, kind)
    requests = [
        (HOLDER_TICKET, HOLDER, [claims.BASE_PY]),
        (RIVAL_TICKET, RIVAL, [claims.BASE_PY]),
        (claims.FOREIGN_TICKET, claims.FOREIGN, [claims.BASE_PY]),
    ]

    results = race_file_claims(project, requests, kind)
    codes = sorted(result[2] for result in results)

    assert codes == [0, 4, 4], [result[1] for result in results]
    store = store_for(project, kind)
    held = store.claims_for_path(claims.BASE_PY)
    assert len(held) == 1, "exactly one claim on the path"
    assert held[0].generation == 1
    winner = [index for index, result in enumerate(results) if result[2] == 0][0]
    assert held[0].attempt_id == requests[winner][1], "the winner is the attempt that reported it"
    assert [event.kind for event in store.events()] == ["claim.file"], "one acquisition happened"


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_two_processes_of_one_attempt_racing_on_one_path_end_up_one_claim(tmp_path, kind):
    """A race between two processes of the *same* attempt is not a conflict: the loser
    either finds its own claim already written (exit 5: re-read, then retry) or acquires
    it again as a new generation (exit 0). Either way the path has one owner and one
    record, because the attempt is one writer."""
    project = claims.holder_project(tmp_path, kind)
    requests = [
        (HOLDER_TICKET, HOLDER, [claims.BASE_PY]),
        (HOLDER_TICKET, HOLDER, [claims.BASE_PY]),
    ]

    results = race_file_claims(project, requests, kind)

    assert set(result[2] for result in results) <= {0, 5}, [result[1] for result in results]
    assert 0 in [result[2] for result in results], "an attempt can always end up holding it"
    held = store_for(project, kind).claims_for_path(claims.BASE_PY)
    assert len(held) == 1 and held[0].attempt_id == HOLDER


@pytest.mark.parametrize("kind", SINK_KINDS)
def test_a_racing_pair_claim_leaves_nobody_holding_half(tmp_path, kind):
    """Two processes claim the same *pair* at once: one attempt ends up with both paths,
    the other with neither. That is the property the canonical ordering exists for."""
    project = claims.holder_project(tmp_path, kind)
    requests = [
        (HOLDER_TICKET, HOLDER, [claims.BASE_PY, claims.SCHEMA_PY]),
        (RIVAL_TICKET, RIVAL, [claims.SCHEMA_PY, claims.BASE_PY]),
    ]

    results = race_file_claims(project, requests, kind)
    codes = sorted(result[2] for result in results)

    assert codes == [0, 4], [result[1] for result in results]
    store = store_for(project, kind)
    held = {claim.path: claim for claim in store.active_claims()}
    assert sorted(held) == sorted(requests[0][2]), "both paths, whichever order they were typed"
    assert len({claim.attempt_id for claim in held.values()}) == 1, "one attempt holds the pair"
    assert {claim.generation for claim in held.values()} == {1}, "one acquisition"
