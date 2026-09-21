"""One file mutation, in its own process, for the crash-boundary tests.

The boundaries of the recoverable write protocol are only interesting if the process
really dies at one of them: no mock can leave a staged copy on disk beside a target and
then vanish, and "the next operation reconciles what a dead run left" is a claim about
*processes*. So the writes and renames here are the real engine, with the real store, run
against the project the parent prepared.

Usage: python3 tests/mutation_worker.py <operation> <project-dir> <sink-kind> <args...>

Operations:
  write  TICKET PATH CONTENT BOUNDARY   write PATH (create when it is absent), dying at BOUNDARY
  rename TICKET SOURCE DEST BOUNDARY    rename SOURCE to DEST, dying at BOUNDARY
  retry  TICKET PATH CONTENT TOKEN OP   re-run an operation under the caller's own token

Exit codes: 0 for an operation that ran to the end (printing what it did), 9 for a
deliberate death at a boundary, 4/5 for a refusal (busy/stale), 1 for an error -- the
engine's own outcomes, so the parent asserts on what the CLI would report.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arbite.coordination import records as coordination_records  # noqa: E402
from arbite.coordination.app import CoordinationApp  # noqa: E402
from arbite.coordination.lifecycle import TicketLifecycle  # noqa: E402
from arbite.coordination.mutations import (  # noqa: E402
    BOUNDARIES,
    FileMutations,
    MutationRequest,
    PathChange,
)
from arbite.coordination.paths import probe  # noqa: E402
from arbite.errors import Busy, CoordinationError, Stale  # noqa: E402
from arbite.sinks import SinkSpec, build_sink  # noqa: E402

AGENT = "deepseek.code.006"


def scene(project, kind, ticket):
    """The objects one operation needs, for the project the parent prepared."""
    arbite_dir = Path(project) / ".arbite"
    sink = build_sink(SinkSpec(kind=kind), arbite_dir)
    app = CoordinationApp.open(sink, Path(project), arbite_dir, store_source="worker")
    lifecycle = TicketLifecycle(sink, app)
    active = app.store.active_attempts(ticket)
    if not active:
        raise CoordinationError(f"no active attempt for {ticket} in this store")
    return sink, app.store, lifecycle, active[0]


def read_token(store, ticket, path, attempt) -> tuple:
    """The token a write presents: a whole-file observation of the version on disk.

    Minted here rather than passed in, because that is what the read command will do
    (tic-1c4f) and the token has to name the *current* claim generation for the write to be
    authorised at all -- a token from before the claim authorises nothing."""
    claims = store.claims_for_path(path)
    if not claims:
        raise CoordinationError(f"{path} is not claimed, so no read can authorize a write")
    version = probe(store.get_workspace().root, path)
    token = coordination_records.new_id(
        "observation", {one.id for one in store.records("observation")}
    )
    store.put_record(
        coordination_records.ReadObservation(
            id=token,
            path=path,
            digest=version.digest,
            observed_at=coordination_records.utc_now(),
            attempt_id=attempt.id,
            claim_generation=claims[0].generation,
        )
    )
    return token, version.digest


def write_request(ticket, attempt, path, content, token, expect, operation_id=None):
    payload = content.encode("utf-8")
    return MutationRequest(
        kind="create" if expect == coordination_records.ABSENT else "write",
        ticket_id=ticket,
        attempt_id=attempt.id,
        actor=AGENT,
        operation_id=operation_id,
        changes=(
            PathChange(
                path=path,
                expect=expect,
                becomes=coordination_records.digest_bytes(payload),
                payload=payload,
                token=token,
            ),
        ),
    )


def rename_request(store, ticket, attempt, source, dest, source_token, dest_token):
    """A rename of `source` onto `dest`, with both versions read at use time.

    The destination's version is whatever is there now (absent or not): a rename onto an
    existing path is allowed here because the caller states the version it expects to
    replace, which is the rule tic-74e2's command exposes as `--expect-dest`."""
    root = store.get_workspace().root
    source_version = probe(root, source).digest
    return MutationRequest(
        kind="rename",
        ticket_id=ticket,
        attempt_id=attempt.id,
        actor=AGENT,
        changes=(
            PathChange(path=source, expect=source_version,
                       becomes=coordination_records.ABSENT, token=source_token),
            PathChange(path=dest, expect=probe(root, dest).digest,
                       becomes=source_version, token=dest_token),
        ),
    )


def run(operation, project, kind, ticket, *args) -> int:
    sink, store, lifecycle, attempt = scene(project, kind, ticket)
    mutations = FileMutations(sink, lifecycle)
    try:
        if operation == "write":
            path, content, boundary = args
            token, expect = read_token(store, ticket, path, attempt)
            request = write_request(ticket, attempt, path, content, token, expect)
        elif operation == "retry":
            path, content, token, operation_id = args
            expect = probe(store.get_workspace().root, path).digest
            request = write_request(
                ticket, attempt, path, content, token, expect, operation_id=operation_id
            )
        elif operation == "rename":
            source, dest, boundary = args
            source_token, _ = read_token(store, ticket, source, attempt)
            dest_token, _ = read_token(store, ticket, dest, attempt)
            request = rename_request(
                store, ticket, attempt, source, dest, source_token, dest_token
            )
        else:
            print(f"unknown operation '{operation}'", file=sys.stderr)
            return 2
        if operation in ("write", "rename"):
            if boundary not in BOUNDARIES:
                print(f"unknown boundary '{boundary}'", file=sys.stderr)
                return 2

            def die_at(name: str) -> None:
                if name == boundary:
                    os._exit(9)

            mutations.crash_hook = die_at
    except CoordinationError as error:
        print(str(error), file=sys.stderr)
        return 1

    try:
        outcome = mutations.apply(request)
    except Stale as refusal:
        print(str(refusal))
        return 5
    except Busy as refusal:
        print(str(refusal))
        return 4
    except CoordinationError as refusal:
        print(str(refusal))
        return 1
    print(json.dumps({"operation": outcome.operation_id, "applied": outcome.applied}))
    return 0


def main(argv) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    return run(argv[0], argv[1], argv[2], argv[3], *argv[4:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
