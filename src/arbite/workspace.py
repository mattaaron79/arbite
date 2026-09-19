"""Explicit workspace <-> authoritative-store binding (planning key C04).

C03 shipped a documented stopgap: the CLI derived a workspace id deterministically
from the project root (`stable_workspace_id`) and treated whatever sink a command
happened to select as authoritative. That was enough for one sink against one
ticket store, but it cannot *detect* the failure the plan warns about -- "one
workspace cannot safely coordinate against two independently selected sinks".
Two sinks each stored their own binding record, neither could see the other, and a
workspace could quietly split its ownership across both.

This module replaces the stopgap with an explicit binding. The authoritative
selection is recorded once, in a small marker file under the project's `.arbite`
directory, so *any* sink selection discovers it:

```
.arbite/workspace-binding.json
```

Both shipped sinks anchor at the same `.arbite` directory (the file sink's
coordination tree and the SQLite database both live there), so the marker is the
one place a conflicting selection is visible before any coordination record is
written. On top of that marker the per-store `CoordinationStore.bind_store()`
record is still kept, so each store also refuses to be re-bound to a different
location.

Binding rules, all enforced here:

- **Idempotent** for an identical sink kind and location: the same workspace id is
  returned and nothing is rewritten.
- **Conflicting** sink kind or location is refused with `StoreBindingConflict`,
  naming both the bound and the requested store and setting `rebind_required`.
- **Relocation of the workspace root** is refused: root relocation is an explicit
  operation, not a side effect of opening a store from somewhere else.
- **Explicit quiescent rebind** is the only way to change the binding. It requires
  `rebind=True`, and it verifies -- by opening the *previous* store named in the
  marker -- that no active work attempt or file claim remains for this workspace.
  If quiescence cannot be verified the rebind is refused rather than guessed at.

Nothing here schedules, retries or scans, and nothing infers that a stopped worker
is dead. A rebind is a one-shot administrative decision made by a caller who has
already stopped the work.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .coordination import (
    EVENT_PAYLOAD_VERSION,
    Event,
    StoreBinding,
    new_record_id,
    utc_now,
)
from .errors import (
    CoordinationConflict,
    CoordinationError,
    InvalidRecord,
    StoreBindingConflict,
    UnsupportedCoordination,
)

#: Marker filename inside the project's `.arbite` directory.
MARKER_FILENAME = "workspace-binding.json"
#: Version of the marker payload, so a later migration can tell layouts apart.
MARKER_VERSION = 1


def stable_workspace_id(root: str) -> str:
    """A workspace id derived deterministically from a project root.

    `ws-` plus the first 16 hex characters of the SHA-256 of the real
    (symlink-resolved) root. Deriving it rather than storing a registry lookup
    means two independent CLI invocations on the same directory agree on the
    workspace id; the *binding* that says which store that workspace coordinates
    against is what is now explicit (see `ensure_binding`).
    """
    digest = hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()
    return f"ws-{digest[:16]}"


@dataclass(frozen=True)
class BindingResolution:
    """The outcome of `ensure_binding`: the workspace and its one store."""

    workspace_id: str
    root: str
    binding: StoreBinding
    created: bool
    rebound: bool
    previous: Optional[StoreBinding] = None


def marker_path(arbite_dir) -> Path:
    """Where the authoritative binding marker lives for a project."""
    return Path(arbite_dir) / MARKER_FILENAME


def load_marker(arbite_dir) -> Optional[dict]:
    """The validated binding marker, or None when the project is unbound.

    A marker that exists but cannot be read or versioned is an error, not a
    "no binding": silently treating it as unbound is exactly how a project would
    end up coordinating against a second store.
    """
    path = marker_path(arbite_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InvalidRecord(
            f"workspace binding marker {path} is unreadable: {error}",
            details={"marker": str(path)},
        )
    if not isinstance(payload, dict) or payload.get("schema_version") != MARKER_VERSION:
        raise InvalidRecord(
            f"workspace binding marker {path} is not a supported version "
            f"(expected schema_version {MARKER_VERSION})",
            details={"marker": str(path)},
        )
    for key in ("workspace_id", "root", "sink_kind", "location", "bound_at"):
        if not payload.get(key):
            raise InvalidRecord(
                f"workspace binding marker {path} is missing {key!r}",
                details={"marker": str(path), "missing": key},
            )
    return payload


def ensure_binding(
    arbite_dir,
    *,
    root: str,
    sink_kind: str,
    location: str,
    store,
    clock=None,
    rebind: bool = False,
    allow_unquiescent: bool = False,
    override_reason: Optional[str] = None,
) -> BindingResolution:
    """Ensure `root` is bound to exactly the `(sink_kind, location)` store.

    Idempotent for a matching binding. A conflicting selection raises
    `StoreBindingConflict` unless `rebind=True`, and an explicit rebind verifies
    that the previously bound store has no active work before it rewrites the
    marker (see `require_quiescent`). ``allow_unquiescent`` with a non-empty
    ``override_reason`` is the explicit administrative override of that check.
    """
    moment = (clock or utc_now)()
    real_root = os.path.realpath(str(root))
    marker = load_marker(arbite_dir)

    if marker is None:
        workspace_id = stable_workspace_id(real_root)
        binding = _store_binding(
            store,
            StoreBinding(
                id=new_record_id("store_binding"),
                workspace_id=workspace_id,
                sink_kind=sink_kind,
                location=location,
                bound_at=moment,
            ),
        )
        _write_marker(arbite_dir, workspace_id, real_root, binding)
        _emit_bound_event(
            store, workspace_id, binding, created=True, rebound=False, moment=moment
        )
        return BindingResolution(
            workspace_id=workspace_id,
            root=real_root,
            binding=binding,
            created=True,
            rebound=False,
        )

    workspace_id = marker["workspace_id"]
    if os.path.realpath(str(marker["root"])) != real_root:
        raise StoreBindingConflict(
            f"workspace {workspace_id} is bound to root {marker['root']}, not "
            f"{real_root}; relocating a workspace root is an explicit operation and "
            "is not done implicitly by selecting a store from a different directory",
            details={
                "workspace_id": workspace_id,
                "bound_root": marker["root"],
                "requested_root": real_root,
            },
        )

    previous = StoreBinding(
        id=new_record_id("store_binding"),
        workspace_id=workspace_id,
        sink_kind=marker["sink_kind"],
        location=marker["location"],
        bound_at=marker["bound_at"],
    )

    if previous.matches(sink_kind, location):
        binding = _store_binding(
            store,
            StoreBinding(
                id=new_record_id("store_binding"),
                workspace_id=workspace_id,
                sink_kind=sink_kind,
                location=location,
                bound_at=previous.bound_at,
            ),
        )
        return BindingResolution(
            workspace_id=workspace_id,
            root=real_root,
            binding=binding,
            created=False,
            rebound=False,
        )

    if not rebind:
        raise StoreBindingConflict(
            f"workspace {workspace_id} is already bound to "
            f"{previous.sink_kind}:{previous.location}; selecting "
            f"{sink_kind}:{location} would coordinate the same workspace against two "
            "stores -- stop all work and rebind explicitly (rebind=True) once it is "
            "quiescent",
            details={
                "workspace_id": workspace_id,
                "bound": {"sink_kind": previous.sink_kind, "location": previous.location},
                "requested": {"sink_kind": sink_kind, "location": location},
                "rebind_required": True,
            },
        )

    require_quiescent(
        previous,
        workspace_id,
        allow_unquiescent=allow_unquiescent,
        override_reason=override_reason,
    )
    binding = _store_binding(
        store,
        StoreBinding(
            id=new_record_id("store_binding"),
            workspace_id=workspace_id,
            sink_kind=sink_kind,
            location=location,
            bound_at=moment,
        ),
    )
    _write_marker(arbite_dir, workspace_id, real_root, binding)
    _emit_bound_event(
        store,
        workspace_id,
        binding,
        created=False,
        rebound=True,
        moment=moment,
        previous=previous,
    )
    return BindingResolution(
        workspace_id=workspace_id,
        root=real_root,
        binding=binding,
        created=False,
        rebound=True,
        previous=previous,
    )


def require_quiescent(
    previous: StoreBinding,
    workspace_id: str,
    *,
    allow_unquiescent: bool = False,
    override_reason: Optional[str] = None,
) -> None:
    """Refuse unless the previously bound store has no active work.

    Quiescence means: no active `WorkAttempt` and no active `FileClaim` for this
    workspace in the store the marker names. If that store cannot be opened, the
    rebind is refused -- an unverifiable "probably stopped" is not good enough to
    hand a workspace's ownership to a second store.

    ``allow_unquiescent`` is the explicit administrative override (planning rule:
    an override needs a reason and retains history): with a non-empty
    ``override_reason`` live work no longer raises here, so a caller that has
    already reported the override can complete it. A missing reason is not an
    override and raises exactly as before. The job-board half of quiescence
    (live reservations/offers/packages) is checked by
    `coordination_export.require_quiescent_store`, which the same callers run.
    """
    old_store = open_store(previous.sink_kind, previous.location)
    if old_store is None:
        raise UnsupportedCoordination(
            f"cannot verify quiescence for the previously bound store "
            f"{previous.sink_kind}:{previous.location}: unsupported sink kind; "
            "rebind explicitly only after confirming the workspace has no active work",
            details={
                "workspace_id": workspace_id,
                "sink_kind": previous.sink_kind,
                "location": previous.location,
            },
        )
    try:
        active_attempts, active_claims = _active_work(old_store, workspace_id)
    except CoordinationError:
        raise
    if active_attempts or active_claims:
        if allow_unquiescent and isinstance(override_reason, str) and override_reason.strip():
            return
        raise CoordinationConflict(
            f"refusing to rebind workspace {workspace_id}: the store it is bound to "
            f"({previous.sink_kind}:{previous.location}) still has "
            f"{len(active_attempts)} active attempt(s) and {len(active_claims)} active "
            "file claim(s); end them before rebinding",
            details={
                "workspace_id": workspace_id,
                "active_attempt_ids": [a.id for a in active_attempts],
                "active_claim_paths": sorted(c.path for c in active_claims),
            },
        )


def open_store(sink_kind: str, location: str):
    """The `CoordinationStore` a marker names, or None for an unknown kind.

    Imports the sink lazily so this module stays cheap for the common bound case
    and never imports `sqlite3` for a file-sink project.
    """
    if sink_kind == "file":
        from .sinks.file import FileSink

        return FileSink(Path(location)).coordination()
    if sink_kind == "sqlite":
        from .sinks.sqlite import SqliteSink

        return SqliteSink(Path(location)).coordination()
    return None


def _active_work(store, workspace_id: str):
    with store.transaction(write=False) as tx:
        attempts = [
            attempt
            for attempt in tx.find("work_attempt", workspace_id=workspace_id)
            if attempt.is_active
        ]
        claims = [
            claim
            for claim in tx.find("file_claim", workspace_id=workspace_id)
            if claim.is_active
        ]
    return attempts, claims


def _store_binding(store, binding: StoreBinding) -> StoreBinding:
    """Record `binding` and return the authoritative stored one.

    `bind_store` is idempotent for a matching sink/location and refuses a
    different one, so a second process racing a first bind converges on one
    binding id instead of storing two.
    """
    stored = store.bind_store(binding)
    return stored if stored is not None else binding


def _write_marker(arbite_dir, workspace_id: str, root: str, binding: StoreBinding) -> None:
    path = marker_path(arbite_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": MARKER_VERSION,
        "workspace_id": workspace_id,
        "root": root,
        "sink_kind": binding.sink_kind,
        "location": binding.location,
        "bound_at": binding.bound_at,
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=".arbite-binding-", dir=str(path.parent)
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise


def _emit_bound_event(
    store,
    workspace_id: str,
    binding: StoreBinding,
    *,
    created: bool,
    rebound: bool,
    moment: str,
    previous: Optional[StoreBinding] = None,
) -> None:
    """Append a `workspace_bound` event, best-effort.

    The binding itself is authoritative (it is stored and mirrored in the marker);
    the event is durable evidence of *when* a workspace was bound or rebound. A
    store that cannot append it must not prevent the binding from being usable, so
    only coordination failures are swallowed -- anything else propagates.
    """
    event = Event(
        id=new_record_id("event"),
        cursor=None,
        kind_="workspace_bound",
        category="lifecycle",
        timestamp=moment,
        subject_ids=[workspace_id],
        operation_id=None,
        payload={
            "workspace_id": workspace_id,
            "sink_kind": binding.sink_kind,
            "location": binding.location,
            "created": created,
            "rebound": rebound,
            "previous": (
                None
                if previous is None
                else {"sink_kind": previous.sink_kind, "location": previous.location}
            ),
        },
        payload_version=EVENT_PAYLOAD_VERSION,
    )
    try:
        with store.transaction() as tx:
            tx.append_event(event)
    except CoordinationError:
        pass


__all__ = [
    "BindingResolution",
    "MARKER_FILENAME",
    "MARKER_VERSION",
    "ensure_binding",
    "load_marker",
    "marker_path",
    "open_store",
    "require_quiescent",
    "stable_workspace_id",
]
