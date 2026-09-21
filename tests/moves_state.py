"""Projects in the states the removal and rename transcripts describe (RN1-RN4).

The RN blocks assert facts about *bytes and ownership*: a digest leaves one path and appears
at another, a claim ends while the other keeps the version it now holds, a removal keeps the
version it deleted, and a directory is never a target. So the files here are built to the
shapes the blocks print (`dead.py` is 84 lines, the number RN3 reports), the claims are taken
by the real `file claim` through the harness -- including the *one* acquisition a rename
needs, which is what gives its two paths the generation RN1 prints -- and every command's
read token comes from a real `file read`, because a token a fixture minted could pass while
the read surface disagrees.

The tickets and attempts are the document's own (`tic-9b57` / `att-4c81`, the pair RN1-RN3
name), seeded by `claims_state`, so the frozen commands run as written.
"""

from __future__ import annotations

from pathlib import Path

import claims_state as claims
import discovery_state as discovery
import writes_state as writes_state
from arbite.coordination import records as coordination_records

HOLDER = claims.HOLDER
RIVAL = claims.RIVAL
HOLDER_TICKET = claims.HOLDER_TICKET
RIVAL_TICKET = claims.RIVAL_TICKET

#: The paths the blocks name, spelled as they are there.
OLD_PY = "src/arbite/old.py"
NEW_PY = "src/arbite/new.py"
A_PY = "src/arbite/a.py"
B_PY = "src/arbite/b.py"
DEAD_PY = "src/arbite/dead.py"
LEGACY_DIR = "src/arbite/legacy"
LEGACY_FILE = f"{LEGACY_DIR}/gone.py"

#: RN3's shape: the 84 lines its one report line prints.
DEAD_LINES = 84
OLD_LINES = 84


def text(lines: int, label: str) -> str:
    """`lines` newline-terminated lines whose content names where they came from."""
    return "".join(f"# {label} line {index}\n" for index in range(1, lines + 1))


def dead_text() -> str:
    """RN3's file: exactly the 84 lines its report describes."""
    return text(DEAD_LINES, "dead")


def old_text() -> str:
    """RN1's source: the bytes whose digest appears at the destination afterwards."""
    return text(OLD_LINES, "old")


# ---------------------------------------------------------------------------
# The store, as the CLI resolves it
# ---------------------------------------------------------------------------


def store_for(project: Path, sink_kind: str = "file"):
    return claims.coordination(project, sink_kind)


def claims_for(project: Path, path: str, sink_kind: str = "file") -> list:
    """Every *active* claim on a path."""
    return store_for(project, sink_kind).claims_for_path(path)


def released_claim(project: Path, path: str, sink_kind: str = "file"):
    """The released claim record for a path, or None."""
    store = store_for(project, sink_kind)
    workspace = store.get_workspace()
    record = store.find_record(
        "claim", coordination_records.claim_id_for(workspace.id, path)
    )
    if record is None or record.is_active:
        return None
    return record


def receipts(project: Path, sink_kind: str = "file") -> list:
    return store_for(project, sink_kind).records("receipt")


def spent_by(project: Path, token: str, sink_kind: str = "file"):
    """The operation a read token records as having spent it, or None."""
    record = store_for(project, sink_kind).find_record("observation", token)
    return None if record is None else record.spent_by


def digest_of(project: Path, path: str) -> str:
    """The whole-file digest of what is on disk at `path`."""
    return coordination_records.digest_bytes((project / path).read_bytes())


def artifact(project: Path, digest: str, sink_kind: str = "file") -> bytes:
    """The bytes a receipt's artifact holds for a version."""
    return store_for(project, sink_kind).get_artifact_bytes(digest)


# ---------------------------------------------------------------------------
# RN1: rename onto a free name
# ---------------------------------------------------------------------------


def rename_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """RN1's world: `old.py` exists, and *both* paths are held at generation 2.

    The two acquisitions are the harness's, not a fixture's: the first takes `old.py` at
    generation 1, and the second takes `old.py` and `new.py` together at generation 2 -- which
    is the one acquisition a rename needs (its two paths share a generation, so one read
    token authorises the whole move) and the generation the frozen report prints."""
    project = claims.holder_project(tmp_path, sink_kind)
    discovery.written(project, OLD_PY, old_text())
    claims.run(
        project, "file", "claim", OLD_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=sink_kind,
    )
    claims.run(
        project, "file", "claim", OLD_PY, NEW_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=sink_kind,
    )
    return project


def token_for_rename(project: Path, sink_kind: str = "file") -> str:
    """The token RN1 presents: a read of the source under the claim."""
    return writes_state.read_token(project, OLD_PY, RIVAL_TICKET, RIVAL, sink_kind)


def rename_command(path: str, dest: str, token: str, extra=()) -> tuple:
    """The command an RN block runs, built the way the block spells it."""
    return (
        "file", "rename", path, dest,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL,
        "--read-token", token,
        *extra,
    )


# ---------------------------------------------------------------------------
# RN2: rename onto a path that already exists
# ---------------------------------------------------------------------------


def collision_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """RN2's world: `a.py` and `b.py` both exist and are held together at generation 1.

    Both files exist before the acquisition, so the claim records `b.py`'s version -- the one
    the refusal tells the caller to name."""
    project = claims.holder_project(tmp_path, sink_kind)
    discovery.written(project, A_PY, "# a\n")
    discovery.written(project, B_PY, text(DEAD_LINES, "b"))
    claims.run(
        project, "file", "claim", A_PY, B_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=sink_kind,
    )
    return project


def token_for_collision(project: Path, sink_kind: str = "file") -> str:
    return writes_state.read_token(project, A_PY, RIVAL_TICKET, RIVAL, sink_kind)


def collision_command(token: str, extra=()) -> tuple:
    return rename_command(A_PY, B_PY, token, extra)


# ---------------------------------------------------------------------------
# RN3: remove
# ---------------------------------------------------------------------------


def remove_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """RN3's world: the 84-line `dead.py`, held at generation 1."""
    project = claims.holder_project(tmp_path, sink_kind)
    discovery.written(project, DEAD_PY, dead_text())
    claims.run(
        project, "file", "claim", DEAD_PY,
        "--ticket", RIVAL_TICKET, "--attempt", RIVAL, sink_kind=sink_kind,
    )
    return project


def token_for_remove(project: Path, sink_kind: str = "file") -> str:
    return writes_state.read_token(project, DEAD_PY, RIVAL_TICKET, RIVAL, sink_kind)


def remove_command(path: str = DEAD_PY, token: str = None) -> tuple:
    """The command an RN block runs; the token is the one fact a block cannot carry."""
    command = ("file", "remove", path, "--ticket", RIVAL_TICKET, "--attempt", RIVAL)
    return command if token is None else (*command, "--read-token", token)


# ---------------------------------------------------------------------------
# RN4: a directory
# ---------------------------------------------------------------------------


def directory_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """RN4's world: a tree with a file in it, which no mutation may touch.

    The directory is not claimed and nothing holds it: the refusal the block prints needs no
    ticket, no attempt and no token, which is the point of it coming first."""
    project = claims.holder_project(tmp_path, sink_kind)
    discovery.written(project, LEGACY_FILE, "# inside the tree\n")
    return project
