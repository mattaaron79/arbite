"""Projects in the states the write and edit transcripts describe (WR1-WR7, ED1-ED3, BY2).

The WR and ED blocks assert facts about *content*: a line count that grows by eighteen,
a delta of `+18 -0`, a replacement found on line 229, an ambiguous text that occurs three
times on lines 85, 229 and 366. So the files here are built to those numbers rather than
around them, exactly as the discovery fixtures are -- `exact_size` gives a file an exact
line count and byte count, and the lines the blocks quote are placed by number.

Two things the blocks treat as already true are produced by the *real* commands instead of
being written by hand, because they are facts a fixture could otherwise assert into
existence: the read token every mutation presents comes from `arbite file read`, and the
claims that make a path writable are recorded with the generation the transcript names
(a second claim, which `arbite file claim` would mint on a re-acquisition).
"""

from __future__ import annotations

import json
from pathlib import Path

import claims_state as claims
import discovery_state as discovery
import examples
import lifecycle_state as state
from arbite.coordination import records as coordination_records

HOLDER = claims.HOLDER
HOLDER_WORKER = claims.HOLDER_WORKER
RIVAL = claims.RIVAL
RIVAL_WORKER = claims.RIVAL_WORKER
HOLDER_TICKET = claims.HOLDER_TICKET
RIVAL_TICKET = claims.RIVAL_TICKET

BASE_PY = discovery.BASE_PY
FILE_PY = discovery.FILE_PY
SCHEMA_PY = discovery.SCHEMA_PY
BASE_SAMPLE = discovery.BASE_SAMPLE
ICON_PNG = "assets/icon.png"

#: WR1's numbers: `base.py` grows from 570 to 588 lines by eighteen appended lines, which
#: is what makes the block's `+18 -0` true of the *bytes* rather than of the report.
BASE_LINES = 570
WRITTEN_LINES = 588
APPENDED_LINES = WRITTEN_LINES - BASE_LINES

#: WR6's numbers: a 1024-byte image replaced by a 1187-byte one.
ICON_BYTES = 1024
ICON_WRITTEN_BYTES = 1187

#: ED3's numbers: a 657-line schema edited to 660 by two insertions.
SCHEMA_LINES = 657
SCHEMA_EDITED_LINES = 660

#: ED1/ED2's file: 570 lines, with "The one write path" on line 222 (once) and
#: "Raises Conflict" on lines 85, 229 and 366 (three times, which is the ambiguity ED2
#: reports and ED1 resolves with an explicit occurrence).
EDITS_LINES = 570
EDITS_SIZE = 27000
EDITS_REPLACEMENTS = {
    85: "        # Raises Conflict is one of the ways a claim can be lost",
    222: '        """The one write path for every kind of change."""',
    229: "        claim all arrive here. Raises Conflict when `expect` is not satisfied,",
    366: "            # Raises Conflict, not StaleRead: the caller retries against a fresh read",
}
AMBIGUOUS_TEXT = "Raises Conflict"
AMBIGUOUS_LINES = (85, 229, 366)
IN_PLACE_LINE = 222
IN_PLACE_OLD = "The one write path"
IN_PLACE_NEW = "The single write path"

#: ED3's file: the two insertions its block reports (`+3 -0` over 657 -> 660 lines).
SCHEMA_FIRST_LINE = "from __future__ import annotations"
SCHEMA_IMPORT_LINE = "import os"


def init_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project with the two attempts the blocks name.

    The attempts are put on generation 2, which is the number WR5's refusal prints: the
    fixture has no history to reconstruct, and the generation a report names has to be the
    record's own."""

    project = discovery.project_fixture(tmp_path, sink_kind)
    for attempt_id, ticket_id, worker in (
        (HOLDER, HOLDER_TICKET, HOLDER_WORKER),
        (RIVAL, RIVAL_TICKET, RIVAL_WORKER),
    ):
        _attempt(project, attempt_id, ticket_id, worker, generation=2, sink_kind=sink_kind)
    return project


def _attempt(project: Path, attempt_id, ticket_id, worker, generation: int, sink_kind: str) -> None:
    """Rewrite one attempt at `generation`, keeping it active for its ticket."""
    store = store_for(project, sink_kind)
    current = store.get_attempt(attempt_id)
    store.put_record(
        coordination_records.WorkAttempt(
            id=attempt_id,
            ticket_id=ticket_id,
            worker_id=worker,
            workspace_id=current.workspace_id,
            generation=generation,
            state=current.state,
            started=current.started,
            last_activity=current.last_activity,
        )
    )


def store_for(project: Path, sink_kind: str = "file"):
    return discovery.store_for(project, sink_kind)


def staged(project: Path, name: str, data) -> Path:
    """Write one scratch payload, returning its path.

    Straight to disk, because a payload is transport a *caller* stages: `arbite scratch
    list|clear` reports and clears the area, but no arbite command produces a payload, and
    the mutation commands read whatever is in it."""
    payload = project / ".arbite" / "scratch" / name
    payload.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        payload.write_bytes(data)
    else:
        payload.write_text(data, encoding="utf-8")
    return payload


def read_token(project: Path, path: str, ticket: str, attempt: str, sink_kind: str = "file") -> str:
    """The token a mutation presents: a real `arbite file read` of the current version.

    Taken through the command rather than written into the store, because the token has to
    name the claim generation the path is held at for the write to be authorised at all,
    and a fixture that minted its own could pass while the read surface disagrees."""
    proc = examples.run_cli(
        project,
        "file",
        "read",
        path,
        "--ticket",
        ticket,
        "--attempt",
        attempt,
        "--json",
        sink=sink_kind,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["token"]["id"]


def put_absent_claim(
    project: Path,
    relative: str,
    ticket_id: str,
    attempt_id: str,
    generation: int = 1,
    sink_kind: str = "file",
) -> None:
    """Record an active claim on a path that does not exist.

    `discovery.put_claim` reads the file's bytes for the version it observed, which is the
    wrong thing for a creation: the claim on a path that is not there records `absent`, which
    is exactly what a create's probe is checked against."""
    store = store_for(project, sink_kind)
    workspace = store.get_workspace()
    assert workspace is not None, "arbite init records the workspace"
    (project / relative).parent.mkdir(parents=True, exist_ok=True)
    store.put_record(
        coordination_records.FileClaim(
            id=coordination_records.claim_id_for(workspace.id, relative),
            workspace_id=workspace.id,
            path=relative,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            generation=generation,
            acquired=coordination_records.utc_now(),
            observed_version=coordination_records.ABSENT,
        )
    )


def base_text(append: int = 0) -> str:
    """`base.py`'s bytes: ED1/ED2's 570 lines, optionally with lines appended (WR1)."""
    text = discovery.exact_size(BASE_PY, EDITS_LINES, EDITS_SIZE, EDITS_REPLACEMENTS)
    if append:
        text += "".join(f"# appended by tic-cf9f {index}\n" for index in range(1, append + 1))
    return text


# ---------------------------------------------------------------------------
# WR1, WR2-target, WR3-target, WR5: one claimed path, one live token, one payload
# ---------------------------------------------------------------------------


def write_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """WR1's world: `base.py` held at generation 2, read, and a payload staged for it."""
    project = init_project(tmp_path, sink_kind)
    discovery.written(project, BASE_PY, base_text())
    discovery.put_claim(project, BASE_PY, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind)
    staged(project, "base.py", base_text(append=APPENDED_LINES))
    return project


def token_for_write(project: Path, sink_kind: str = "file") -> str:
    """The token WR1, WR3 and WR5 present: a read of `base.py` under its claim."""
    return read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, sink_kind)


def reread_token(project: Path, sink_kind: str = "file") -> str:
    """A *fresh* token after a change: the one the refusals tell the caller to take."""
    return read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, sink_kind)


def external_edit(project: Path) -> Path:
    """A direct write to `base.py` -- the unattributed change WR2's target is about.

    Written straight to disk, because that is what an external edit *is*: no arbite
    command made it, and the write's job is to notice that its version moved."""
    return discovery.written(project, BASE_PY, base_text().replace("The one write path", "The only write path"))


# ---------------------------------------------------------------------------
# WR4: the attempt is live, the path is nobody's
# ---------------------------------------------------------------------------


def unclaimed_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """WR4's world: `base.py` exists, the attempt is live, and nothing holds the path."""
    project = init_project(tmp_path, sink_kind)
    discovery.written(project, BASE_PY, base_text())
    staged(project, "base.py", base_text(append=APPENDED_LINES))
    return project


def token_for_unclaimed(project: Path, sink_kind: str = "file") -> str:
    """A pre-claim read of `base.py`: the token WR4 presents, which authorises nothing."""
    return read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, sink_kind)


# ---------------------------------------------------------------------------
# WR6: a claimed binary path
# ---------------------------------------------------------------------------


def binary_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """WR6's world: a 1024-byte image, held at generation 1, with its replacement staged."""
    project = init_project(tmp_path, sink_kind)
    path = project / ICON_PNG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(icon_bytes(ICON_BYTES))
    discovery.put_claim(project, ICON_PNG, RIVAL_TICKET, RIVAL, generation=1, sink_kind=sink_kind)
    staged(project, "icon.png", icon_bytes(ICON_WRITTEN_BYTES))
    return project


def token_for_binary(project: Path, sink_kind: str = "file") -> str:
    return read_token(project, ICON_PNG, RIVAL_TICKET, RIVAL, sink_kind)


def icon_bytes(size: int) -> bytes:
    """`size` bytes that are not UTF-8 text: a PNG signature, then binary filler."""
    signature = b"\x89PNG\r\n\x1a\n"
    filler = bytes((index * 37) % 256 for index in range(size - len(signature)))
    return signature + filler


# ---------------------------------------------------------------------------
# ED1, ED2, ED3: claimed text files and their batches
# ---------------------------------------------------------------------------


def edits_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """ED1's world: the 570-line `base.py`, held at generation 2, with a batch staged."""
    project = init_project(tmp_path, sink_kind)
    discovery.written(project, BASE_PY, base_text())
    discovery.put_claim(project, BASE_PY, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind)
    staged(project, "edits.json", json.dumps(ed1_batch()))
    return project


def token_for_edits(project: Path, sink_kind: str = "file") -> str:
    return read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, sink_kind)


def ed1_batch() -> dict:
    """ED1's two edits: one in place, one resolved by occurrence.

    The first replacement carries three extra lines, which is what makes the block's
    `570 -> 573 lines` true of the file: a batch whose replacements all keep the line count
    could not grow it."""
    return {
        "edits": [
            {
                "old": IN_PLACE_OLD,
                "new": "\n".join(
                    [IN_PLACE_NEW, "        It is the only way a change reaches the sink."]
                    + ["        Every caller arrives here."] * 2
                ),
            },
            {"old": AMBIGUOUS_TEXT, "new": f"{AMBIGUOUS_TEXT} or StaleRead", "occurrence": 2},
        ]
    }


def ed2_batch() -> dict:
    """ED2's batch: three edits, the second ambiguous (three occurrences, no selector).

    The first and the third select one place each, so the batch is refused by the *second*
    edit -- the edit the frozen refusal counts (`edit 2/3`) -- which is why both texts are
    taken from the file rather than invented: an edit that does not occur would be refused
    as absent instead of as ambiguous."""
    return {
        "edits": [
            {"old": "for every kind of change", "new": "for every kind of change to a ticket"},
            {"old": AMBIGUOUS_TEXT, "new": f"{AMBIGUOUS_TEXT} or StaleRead"},
            {"old": "the caller retries against a fresh read", "new": "the caller re-reads"},
        ]
    }


def ambiguous_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """ED2's world: the same file and claim, with the ambiguous batch staged."""
    project = edits_project(tmp_path, sink_kind)
    staged(project, "edits.json", json.dumps(ed2_batch()))
    return project


def stdin_edits_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """ED3's world: a 657-line `schema.py` held at generation 3, with no payload file.

    The batch arrives on stdin, so nothing is staged: the fixture's whole contribution is
    the file, the claim generation the block prints, and the real read token."""
    project = init_project(tmp_path, sink_kind)
    discovery.written(project, SCHEMA_PY, schema_text())
    discovery.put_claim(project, SCHEMA_PY, HOLDER_TICKET, HOLDER, generation=3, sink_kind=sink_kind)
    return project


def token_for_schema(project: Path, sink_kind: str = "file") -> str:
    return read_token(project, SCHEMA_PY, HOLDER_TICKET, HOLDER, sink_kind)


def schema_text() -> str:
    """`schema.py`: 657 lines, with the two lines ED3's insertions anchor on."""
    return discovery.exact_size(
        SCHEMA_PY,
        SCHEMA_LINES,
        22000,
        {1: SCHEMA_FIRST_LINE, 2: SCHEMA_IMPORT_LINE},
    )


def ed3_stdin_batch() -> bytes:
    """ED3's batch: two insertions, `+3 -0`, which is what the block reports."""
    document = {
        "edits": [
            {
                "old": SCHEMA_IMPORT_LINE,
                "new": f"{SCHEMA_IMPORT_LINE}\nimport re",
                "line": 2,
            },
            {
                "old": SCHEMA_FIRST_LINE,
                "new": f"{SCHEMA_FIRST_LINE}\n\n# typed by tic-cf9f",
                "line": 1,
            },
        ]
    }
    return json.dumps(document).encode("utf-8")


# ---------------------------------------------------------------------------
# BY2: generated output
# ---------------------------------------------------------------------------


def generated_project(tmp_path: Path, sink_kind: str = "file") -> Path:
    """BY2's world: a `base.py` the attempt holds, and a cache file it does not touch."""
    project = init_project(tmp_path, sink_kind)
    discovery.written(project, BASE_PY, base_text())
    discovery.put_claim(project, BASE_PY, HOLDER_TICKET, HOLDER, generation=2, sink_kind=sink_kind)
    cache = project / ".pytest_cache" / "v" / "cache" / "lastfailed"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{}\n", encoding="utf-8")
    staged(project, "lastfailed", "{}\n")
    return project


def token_for_generated(project: Path, sink_kind: str = "file") -> str:
    """A token of *some* real observation: BY2 is refused before the token is consulted."""
    return read_token(project, BASE_PY, HOLDER_TICKET, HOLDER, sink_kind)


def spent_by(project: Path, token: str, sink_kind: str = "file"):
    """The operation a token records as having spent it, or None."""
    record = store_for(project, sink_kind).find_record("observation", token)
    return None if record is None else record.spent_by


def read_command_for(path: str = BASE_PY) -> str:
    """The read command a stale or unowned refusal names: the hint a caller passes on."""
    return f"arbite file read {path} --ticket {HOLDER_TICKET} --attempt {HOLDER}"


def receipt_count(project: Path, sink_kind: str = "file") -> int:
    return len(store_for(project, sink_kind).records("receipt"))


def recorded_versions(project: Path, sink_kind: str = "file"):
    """`(before, after)` digests of the newest receipt: what the evidence says changed."""
    receipts = store_for(project, sink_kind).records("receipt")
    assert receipts, "a mutation must leave a receipt behind"
    newest = receipts[-1]
    return newest.before, newest.after


def newest_receipt(project: Path, sink_kind: str = "file"):
    """The receipt a mutation left behind, for the assertions about what it recorded."""
    receipts = store_for(project, sink_kind).records("receipt")
    assert receipts, "a mutation must leave a receipt behind"
    return receipts[-1]


def artifact_bytes(project: Path, digest: str, sink_kind: str = "file") -> bytes:
    return store_for(project, sink_kind).get_artifact_bytes(digest)
