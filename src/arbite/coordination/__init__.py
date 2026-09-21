"""Coordination: the records, the vocabulary and the operations of the file proxy.

The proxy lets two independently started agents work in one checkout without a
successful write silently overwriting a competing one. This package is its
storage-neutral core, deliberately separate from the ticket store:

- `records` -- workspace, work attempt, file claim, read observation, operation
  receipt, artifact and event, each versioned, validated and serialised once;
- `results` -- the outcome vocabulary: exit codes 0-5, the `next:` line rendered
  from one hint table, and the JSON result shape;
- `store` -- `CoordinationStore`, the interface both backends implement, plus the
  transaction, revision and recovery hooks it implements on top of their primitives;
- `file_backend` / `sqlite_backend` -- the two implementations;
- `app` -- the application layer, where guarded multi-record operations live;
- `mutations` -- the recoverable write protocol: stage, verify, apply, finalise;
- `recovery` -- the intent journal's other half: what a staged operation left behind,
  and the one honest answer about it;
- `scratch` -- the payload area, and how it is reported.

Nothing here requires SQLite, and nothing here is imported by the ticket schema or
by a sink: coordination state is local runtime state, tickets are the git-tracked
development record, and the epic's whole design rests on not confusing the two.
"""

from __future__ import annotations

from . import records, results, scratch  # noqa: F401  (the vocabulary, re-exported)
from .app import CoordinationApp, Guard, GuardedOperation  # noqa: F401
from .records import (  # noqa: F401
    ABSENT,
    COORDINATION_SCHEMA_REVISION,
    RECORD_TYPES,
    Artifact,
    Event,
    FileClaim,
    OperationReceipt,
    ReadObservation,
    Record,
    WorkAttempt,
    Workspace,
    derived_workspace_id,
    digest_bytes,
    new_id,
    parse_record,
    short_digest,
    utc_now,
)
from .results import (  # noqa: F401
    EXIT_BUSY,
    EXIT_STALE,
    OperationResult,
    Outcome,
    next_actions_for,
    register_next_actions,
    render_next_line,
)
from .scratch import (  # noqa: F401
    ScratchSummary,
    ensure_scratch_dir,
    scratch_root,
    scratch_summary,
)
from .store import (  # noqa: F401
    CoordinationInfo,
    CoordinationStore,
    open_coordination_store,
)
