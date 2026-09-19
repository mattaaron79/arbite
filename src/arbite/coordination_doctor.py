"""Integrity checks for a *stored* shared-directory coordination store.

This is the module `arbite doctor` reaches for once a sink's ticket checks and its
storage-specific checks are done. Where `coordination_export.bundle_problems`
inspects a portable *bundle* (a detached snapshot that a caller is about to move),
this module inspects the live store: workspaces, attempts, claims, intents,
receipts, artifacts, events, journals and the workspace binding marker. It knows
only the `CoordinationStore` surface -- no SQL, no file layout -- so a file store
and a SQLite store are checked by exactly the same code and must agree.

## Public API

    coordination_problems(sink, *, fix=False, tickets=None) -> list[Problem]

`sink` may be a `TicketSink` (its `coordination()` is used) or a `CoordinationStore`
directly. When the store is `None`, or `store.is_initialised()` is `False`, the
function returns `[]` **and creates nothing** -- a legacy ticket store must never be
conjured into a coordination store by being *checked*.

`tickets` is the set of ticket ids used to judge a claim's `ticket_id`; when it is
`None` the sink's `ids()` is consulted, and if that is unavailable ticket references
are treated as unverifiable (no orphan is reported on that basis alone).

Every individual check is wrapped so that malformed state is *reported* rather than
crashing `arbite doctor`; only `Exception` is caught, so `KeyboardInterrupt` and
`SystemExit` still propagate.

## Fix policy (enforced here, stated here)

AUTO-FIX (`fixed=True`, only when the answer is unambiguous):

- a pending intent whose observed bytes equal its recorded **before** version ->
  `state = "reverted"`;
- a pending intent whose observed bytes equal its recorded **after** version ->
  `state = "finalized"` when a receipt with `id == intent.operation_id` exists,
  else `state = "applied"`;
- leftover write-ahead journals -> `store.replay_journals()` (file sink only);
- a missing *derived* per-operation event index -> `store.rebuild_event_operation_index`;
- an *unversioned* legacy record envelope that validates -> re-stored through the
  store's public API as a normal `RECORD_ENVELOPE_VERSION` envelope;
- a pending lifecycle intent (a transition a crash interrupted between its ticket
  write and its coordination cascade) -> settled exactly as the next lifecycle
  command would settle it: rolled forward when the ticket shows the recorded
  target state, abandoned when it does not. Needs the ticket sink, so a doctor
  given a bare store only reports it.

REPORT ONLY (never repaired here): drifted pending intents (the bytes match neither
side), missing/corrupt artifacts, revision drift, event cursor conflicts, orphan
claims, invalid generations, unreadable records, and `coordination_binding_drift`.

**The stored binding is authoritative and the marker MIRROR is never rewritten.**
Rewriting `.arbite/workspace-binding.json` from here would silently move which store
a workspace coordinates against, which is precisely the split-ownership failure the
marker exists to prevent. Drift is named, with both sides quoted, and left for an
explicit administrative rebind.

`fix=True` never deletes records, artifacts, events or journals: it only advances
intent state, replays journals forward, rebuilds derived indexes, and upgrades
envelopes. The returned list is sorted by `(kind, detail)` and every `Problem` keeps
the existing shape with `fixed=False` unless a fix actually succeeded.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import coordination, workspace
from .coordination import (
    ABSENT,
    INTENT_ACTIVE_STATES,
    FileClaim,
    OperationIntent,
    WorkAttempt,
    digest_of_bytes,
    is_digest,
    is_opaque_id,
)
from .errors import ArtifactCorrupt, InvalidRecord, UnsupportedCoordination
from .sinks.base import Problem

#: Problem kind used when a *check itself* cannot complete because it tripped over
#: malformed state. Reporting rather than raising is the whole point of a doctor.
CHECK_FAILED = "coordination_check_failed"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _store_of(sink):
    """The `CoordinationStore` behind `sink`, or `None`.

    Mirrors `coordination_export._store_of`'s duck typing but stays independent:
    a `TicketSink` (anything with a callable `coordination()`) is unwrapped; a store
    is used as-is; a sink whose `coordination()` is `None` yields `None` rather than
    raising, because "no coordination state" is a valid answer for a doctor.
    """
    if sink is None:
        return None
    method = getattr(sink, "coordination", None)
    if callable(method):
        return method()
    return sink


def _is_pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _safe_list(method, *args) -> list:
    """Call an inspection probe, returning `[]` when it raises. Never creates."""
    try:
        result = method(*args)
    except Exception:
        return []
    return list(result) if result is not None else []


def _safe_call(method, *args):
    try:
        return method(*args)
    except Exception:
        return None


def _store_binding(store, workspace_id):
    """The authoritative binding for `workspace_id`, or `None` (never raises)."""
    try:
        return store.store_binding(workspace_id)
    except UnsupportedCoordination:
        return None
    except Exception:
        return None


def _parse_record(mapping):
    try:
        return coordination.record_from_dict(mapping)
    except Exception:
        return None


def _comparable_location(value):
    """A value comparable across a marker and a binding.

    A path-like value that actually exists on disk is compared by `realpath`, so a
    marker written with a non-canonical path does not look like drift; anything else
    (a database path that is not present, a non-path string) is compared verbatim.
    """
    if isinstance(value, str) and value:
        try:
            if os.path.exists(value):
                return os.path.realpath(value)
        except OSError:
            pass
    return value


def _locations_match(left, right) -> bool:
    return _comparable_location(left) == _comparable_location(right)


# ---------------------------------------------------------------------------
# Reading the raw store
# ---------------------------------------------------------------------------


def _classify(entry):
    """`(state, record)` for one `inspect_records()` entry.

    `state` is one of ``normal`` / ``legacy`` / ``revision_drift`` / ``unreadable``;
    `record` is the parsed coordination record (or `None`). This is the one place
    that decides what an envelope *is*, so both sinks and every check agree.
    """
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return "unreadable", None
    record_payload = payload.get("record")
    has_record = isinstance(record_payload, dict)
    schema_version = payload.get("schema_version")
    revision = payload.get("revision")
    schema_ok = _is_pos_int(schema_version)
    revision_ok = _is_pos_int(revision)
    revision_present = "revision" in payload

    if has_record:
        record = _parse_record(record_payload)
        if schema_ok and revision_ok:
            return ("normal", record) if record is not None else ("unreadable", None)
        if not schema_ok and not revision_ok:
            return ("legacy", record) if record is not None else ("unreadable", None)
        # A versioned envelope whose revision is missing/invalid (or an envelope
        # with a schema_version but no usable revision) is revision drift.
        return "revision_drift", record

    # No inner `record` mapping: maybe a bare legacy record mapping.
    if "kind" in payload and "id" in payload:
        record = _parse_record(payload)
        if record is not None:
            return "legacy", record
        return "unreadable", None
    if revision_present and not revision_ok:
        return "revision_drift", None
    if "schema_version" in payload:
        return "revision_drift", None
    return "unreadable", None


def _read_store(store):
    """`(records, events)` from the two non-creating probes, defensively."""
    records = _safe_list(store.inspect_records)
    events = _safe_list(store.inspect_events)
    return records, events


def _build_context(sink, store, *, fix, tickets, pending_before, allow_binding_read):
    records, events = _read_store(store)
    by_kind: dict = {}
    legacy = []
    unreadable = []
    revision_drift = []
    for entry in records:
        if not isinstance(entry, dict):
            continue
        state, record = _classify(entry)
        if state == "normal" and record is not None:
            by_kind.setdefault(record.kind, []).append(record)
        elif state == "legacy":
            legacy.append((entry, record))
        elif state == "revision_drift":
            revision_drift.append(entry)
        else:
            unreadable.append(entry)

    workspace_roots = {}
    for record in by_kind.get("workspace", []):
        root = getattr(record, "root", None)
        if isinstance(root, str) and root:
            workspace_roots[record.id] = root

    receipt_ids = {r.id for r in by_kind.get("operation_receipt", [])}

    return {
        "sink": sink,
        "store": store,
        "fix": fix,
        "tickets": tickets,
        "by_kind": by_kind,
        "legacy": legacy,
        "unreadable": unreadable,
        "revision_drift": revision_drift,
        "events": events,
        "workspace_roots": workspace_roots,
        "receipt_ids": receipt_ids,
        "pending_before": pending_before,
        "allow_binding_read": allow_binding_read,
        "artifact_state": {"checked": False, "ok": True, "capability": None},
    }


# ---------------------------------------------------------------------------
# Findings 1-12
# ---------------------------------------------------------------------------


def _check_orphan_claims(ctx):
    """Finding 1: an ACTIVE claim whose attempt/ticket/workspace is unknown."""
    problems = []
    attempts = {r.id for r in ctx["by_kind"].get("work_attempt", [])}
    workspaces = {r.id for r in ctx["by_kind"].get("workspace", [])}
    tickets = ctx["tickets"]
    for claim in ctx["by_kind"].get("file_claim", []):
        if not isinstance(claim, FileClaim) or not claim.is_active:
            continue
        label = f"claim {claim.id!r}"
        if claim.attempt_id not in attempts:
            problems.append(
                Problem(
                    "coordination_orphan_claim",
                    f"{label} names attempt {claim.attempt_id!r}, which is not stored",
                    ticket_id=claim.ticket_id,
                    fixed=False,
                )
            )
        if tickets is not None and claim.ticket_id not in tickets:
            problems.append(
                Problem(
                    "coordination_orphan_claim",
                    f"{label} names ticket {claim.ticket_id!r}, which is not in the store",
                    ticket_id=claim.ticket_id,
                    fixed=False,
                )
            )
        if ctx["allow_binding_read"]:
            binding = _store_binding(ctx["store"], claim.workspace_id)
            if binding is None:
                problems.append(
                    Problem(
                        "coordination_orphan_claim",
                        f"{label} names workspace {claim.workspace_id!r}, which has no "
                        "stored binding",
                        ticket_id=claim.ticket_id,
                        fixed=False,
                    )
                )
            elif claim.workspace_id not in workspaces:
                problems.append(
                    Problem(
                        "coordination_orphan_claim",
                        f"{label} names workspace {claim.workspace_id!r}, which has no "
                        "workspace record",
                        ticket_id=claim.ticket_id,
                        fixed=False,
                    )
                )
    return problems


def _check_invalid_generations(ctx):
    """Finding 2: bad generations, duplicate active owners, and regressions."""
    problems = []
    attempts = ctx["by_kind"].get("work_attempt", [])
    claims = ctx["by_kind"].get("file_claim", [])

    for record in list(claims) + list(attempts):
        generation = getattr(record, "generation", None)
        if not _is_pos_int(generation):
            problems.append(
                Problem(
                    "coordination_invalid_generation",
                    f"{record.kind} {record.id!r} has generation {generation!r}; a "
                    "generation must be an integer >= 1",
                    ticket_id=getattr(record, "ticket_id", None),
                    fixed=False,
                )
            )

    active_claims: dict = {}
    for claim in claims:
        if not isinstance(claim, FileClaim) or not claim.is_active:
            continue
        active_claims.setdefault((claim.workspace_id, claim.path), []).append(claim)
    for key in sorted(active_claims):
        members = active_claims[key]
        if len(members) > 1:
            ids = ", ".join(sorted(c.id for c in members))
            problems.append(
                Problem(
                    "coordination_invalid_generation",
                    f"path {key[1]!r} in workspace {key[0]!r} has {len(members)} active "
                    f"claims ({ids}); exclusive ownership is one active claim per "
                    "(workspace, path)",
                    ticket_id=members[0].ticket_id,
                    fixed=False,
                )
            )

    active_attempts: dict = {}
    for attempt in attempts:
        if not isinstance(attempt, WorkAttempt) or not attempt.is_active:
            continue
        active_attempts.setdefault(attempt.ticket_id, []).append(attempt)
    for ticket_id in sorted(active_attempts):
        members = active_attempts[ticket_id]
        if len(members) > 1:
            ids = ", ".join(sorted(a.id for a in members))
            problems.append(
                Problem(
                    "coordination_invalid_generation",
                    f"ticket {ticket_id!r} has {len(members)} active attempts ({ids}); "
                    "one active attempt per ticket",
                    ticket_id=ticket_id,
                    fixed=False,
                )
            )

    released_max: dict = {}
    for claim in claims:
        if not isinstance(claim, FileClaim) or claim.is_active:
            continue
        if _is_pos_int(claim.generation):
            key = (claim.workspace_id, claim.path)
            released_max[key] = max(released_max.get(key, 0), claim.generation)
    for claim in claims:
        if not isinstance(claim, FileClaim) or not claim.is_active:
            continue
        if not _is_pos_int(claim.generation):
            continue
        key = (claim.workspace_id, claim.path)
        highest = released_max.get(key)
        if highest is not None and claim.generation < highest:
            problems.append(
                Problem(
                    "coordination_invalid_generation",
                    f"claim {claim.id!r} for {claim.path!r} has generation "
                    f"{claim.generation}, below the highest retained released generation "
                    f"{highest} for the same (workspace, path); generations must not go "
                    "backwards",
                    ticket_id=claim.ticket_id,
                    fixed=False,
                )
            )
    return problems


def _artifacts(ctx):
    records = ctx["by_kind"].get("artifact", [])
    by_id = {r.id: r for r in records}
    by_digest = {r.digest: r for r in records if isinstance(getattr(r, "digest", None), str)}
    return records, by_id, by_digest


def _store_has_artifact(ctx, digest) -> bool:
    """`has_artifact(digest)`, or `True` when the store cannot answer.

    A store that does not implement artifact storage must not be reported as
    "missing evidence" for every reference; the honest answer to "is it there?" is
    "I cannot tell", which is not a finding.
    """
    state = ctx["artifact_state"]
    store = ctx["store"]
    if state["capability"] is None:
        try:
            store.has_artifact(digest)
            state["capability"] = True
        except UnsupportedCoordination:
            state["capability"] = False
        except Exception:
            state["capability"] = False
    if not state["capability"]:
        return True
    try:
        return bool(store.has_artifact(digest))
    except Exception:
        return True


def _artifact_refs(ctx):
    for record in ctx["by_kind"].get("operation_receipt", []) + ctx["by_kind"].get(
        "operation_intent", []
    ):
        for ref in getattr(record, "artifact_refs", None) or []:
            yield record, ref


def _check_missing_artifacts(ctx):
    """Finding 3: an artifact reference or record that cannot be served."""
    problems = []
    records, by_id, by_digest = _artifacts(ctx)

    for record, ref in _artifact_refs(ctx):
        label = f"{record.kind} {record.id!r}"
        if is_digest(ref):
            artifact = by_digest.get(ref)
            digest = ref
        elif is_opaque_id(ref, "art"):
            artifact = by_id.get(ref)
            if artifact is None:
                problems.append(
                    Problem(
                        "coordination_missing_artifact",
                        f"{label} references artifact id {ref!r}, which has no Artifact "
                        "record (so its digest cannot be resolved)",
                        ticket_id=getattr(record, "ticket_id", None),
                        fixed=False,
                    )
                )
                continue
            digest = getattr(artifact, "digest", None)
        else:
            problems.append(
                Problem(
                    "coordination_missing_artifact",
                    f"{label} references {ref!r}, which is neither a content digest nor "
                    "an artifact id",
                    ticket_id=getattr(record, "ticket_id", None),
                    fixed=False,
                )
            )
            continue

        if artifact is not None and getattr(artifact, "available", True) is False:
            problems.append(
                Problem(
                    "coordination_missing_artifact",
                    f"{label} references artifact {ref!r}, whose record is marked "
                    "available=False, so its content cannot be served",
                    ticket_id=getattr(record, "ticket_id", None),
                    fixed=False,
                )
            )
            continue
        if isinstance(digest, str) and not _store_has_artifact(ctx, digest):
            problems.append(
                Problem(
                    "coordination_missing_artifact",
                    f"{label} references artifact {ref!r} (digest {digest}), which has no "
                    "stored content",
                    ticket_id=getattr(record, "ticket_id", None),
                    fixed=False,
                )
            )

    for artifact in records:
        if getattr(artifact, "available", True) is False:
            problems.append(
                Problem(
                    "coordination_missing_artifact",
                    f"artifact {artifact.id!r} (digest {getattr(artifact, 'digest', None)}) "
                    "is marked available=False",
                    fixed=False,
                )
            )
    return problems


def _check_corrupt_artifacts(ctx):
    """Finding 4: stored bytes that no longer verify against their identity."""
    problems = []
    store = ctx["store"]
    records, by_id, by_digest = _artifacts(ctx)
    capability = ctx["artifact_state"]["capability"]

    def read(digest):
        try:
            return store.read_artifact_bytes(digest)
        except ArtifactCorrupt as error:
            problems.append(
                Problem(
                    "coordination_artifact_corrupt",
                    f"artifact {digest} failed verification on read: {error}",
                    fixed=False,
                )
            )
        except UnsupportedCoordination:
            raise
        except Exception as error:  # pragma: no cover - defensive
            problems.append(
                Problem(
                    "coordination_artifact_corrupt",
                    f"artifact {digest} could not be read: {error}",
                    fixed=False,
                )
            )
        return None

    for artifact in records:
        digest = getattr(artifact, "digest", None)
        if not isinstance(digest, str) or not is_digest(digest):
            continue
        try:
            data = read(digest)
        except UnsupportedCoordination:
            return problems
        if capability is None:
            try:
                store.has_artifact(digest)
                ctx["artifact_state"]["capability"] = True
            except UnsupportedCoordination:
                ctx["artifact_state"]["capability"] = False
                return problems
            except Exception:
                ctx["artifact_state"]["capability"] = False
                return problems
        if data is None:
            continue
        if not isinstance(getattr(artifact, "size", None), int) or len(data) != artifact.size:
            problems.append(
                Problem(
                    "coordination_artifact_corrupt",
                    f"artifact {digest} stores {len(data)} bytes but its record claims "
                    f"{getattr(artifact, 'size', None)!r}",
                    fixed=False,
                )
            )

    # References whose digest has no Artifact record still have stored bytes to
    # verify: the digest itself is the identity.
    for record, ref in _artifact_refs(ctx):
        if not is_digest(ref) or ref in by_digest:
            continue
        try:
            read(ref)
        except UnsupportedCoordination:
            return problems
    return problems


def _observe_intent(intent, workspace_roots):
    """`(outcome, detail)` for one intent, from the real bytes under its root.

    `outcome` is ``before_match`` / ``after_match`` / ``drifted`` / ``cannot_observe``.
    An unknown workspace root or an unreadable path is "cannot observe": the doctor
    reports, it never guesses.
    """
    root = workspace_roots.get(intent.workspace_id)
    if not root:
        return "cannot_observe", f"workspace {intent.workspace_id!r} has no stored root"
    before = intent.before if isinstance(intent.before, dict) else {}
    after = intent.after if isinstance(intent.after, dict) else {}
    paths = sorted(
        set(intent.paths or []) | set(before) | set(after)
    )
    if not paths:
        return "cannot_observe", "the intent names no paths to observe"
    observed = {}
    for relative in paths:
        try:
            candidate = Path(root) / relative
            data = candidate.read_bytes() if candidate.exists() else None
        except Exception as error:
            return "cannot_observe", f"path {relative!r} could not be observed ({error})"
        observed[relative] = ABSENT if data is None else digest_of_bytes(data)
    if all(observed[path] == before.get(path) for path in paths):
        return "before_match", "every observed path equals its recorded before value"
    if all(observed[path] == after.get(path) for path in paths):
        return "after_match", "every observed path equals its recorded after value"
    return "drifted", "observed bytes match neither the recorded before nor after value"


def _check_pending_intents(ctx):
    """Finding 5: an active intent, observed against the real workspace bytes."""
    problems = []
    store = ctx["store"]
    fix = ctx["fix"]
    for intent in ctx["by_kind"].get("operation_intent", []):
        if not isinstance(intent, OperationIntent):
            continue
        if intent.state not in INTENT_ACTIVE_STATES:
            continue
        base = (
            f"operation_intent {intent.id!r} (operation {intent.operation_id!r}) is "
            f"{intent.state!r}"
        )
        outcome, detail = _observe_intent(intent, ctx["workspace_roots"])
        if outcome == "cannot_observe":
            problems.append(
                Problem(
                    "coordination_pending_intent",
                    f"{base}; the workspace bytes cannot be observed ({detail})",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
            continue
        if outcome == "drifted":
            problems.append(
                Problem(
                    "coordination_pending_intent",
                    f"{base}; {detail} (drifted) -- resolve it explicitly, the bytes are "
                    "never guessed at or overwritten",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
            continue
        side = "before" if outcome == "before_match" else "after"
        target = "reverted"
        if outcome == "after_match":
            target = "finalized" if intent.operation_id in ctx["receipt_ids"] else "applied"
        if not fix:
            problems.append(
                Problem(
                    "coordination_pending_intent",
                    f"{base}; {detail}, so it can be set to {target!r} (re-run with --fix)",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
            continue
        original = intent.state
        intent.state = target
        try:
            with store.transaction() as tx:
                tx.put(intent, expect_revision=None)
        except Exception as error:
            intent.state = original
            problems.append(
                Problem(
                    "coordination_pending_intent",
                    f"{base}; setting it to {target!r} failed: {error}",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
        else:
            problems.append(
                Problem(
                    "coordination_pending_intent",
                    f"{base}; observed bytes match the recorded {side} value, so it was "
                    f"set to {target!r}",
                    ticket_id=intent.ticket_id,
                    fixed=True,
                )
            )
    return problems


def _load_marker(marker_path):
    """`(marker_or_None, error_or_None)` for the workspace binding marker."""
    try:
        return workspace.load_marker(os.path.dirname(marker_path)), None
    except InvalidRecord as error:
        return None, str(error)
    except Exception as error:  # pragma: no cover - defensive
        return None, str(error)


def _check_binding_drift(ctx):
    """Finding 6: marker/binding disagreement and bindings foreign to the store."""
    problems = []
    store = ctx["store"]

    expected_kind = None
    try:
        namespace = store.cursor_namespace()
        if isinstance(namespace, str) and ":" in namespace:
            expected_kind = namespace.split(":", 1)[0]
    except Exception:
        expected_kind = None
    expected_location = None
    try:
        expected_location = store.binding_location()
    except Exception:
        expected_location = None

    marker_path = None
    try:
        marker_path = store.binding_marker_path()
    except Exception:
        marker_path = None

    marker = None
    if marker_path and os.path.isfile(marker_path):
        marker, marker_error = _load_marker(marker_path)
        if marker_error is not None:
            problems.append(
                Problem(
                    "coordination_binding_drift",
                    f"binding marker {marker_path} is unreadable or not a supported "
                    f"version: {marker_error}",
                    fixed=False,
                )
            )

    known_workspaces = {r.id for r in ctx["by_kind"].get("workspace", [])}
    if isinstance(marker, dict) and marker.get("workspace_id"):
        workspace_id = marker["workspace_id"]
        known_workspaces.add(workspace_id)
        if ctx["allow_binding_read"]:
            binding = _store_binding(store, workspace_id)
            if binding is None:
                problems.append(
                    Problem(
                        "coordination_binding_drift",
                        f"marker {marker_path} names workspace {workspace_id!r}, which has "
                        "no stored binding",
                        fixed=False,
                    )
                )
            else:
                for field in ("sink_kind", "workspace_id", "bound_at"):
                    marker_value = marker.get(field)
                    binding_value = getattr(binding, field, None)
                    if marker_value != binding_value:
                        problems.append(
                            Problem(
                                "coordination_binding_drift",
                                f"marker {marker_path} records {field}={marker_value!r} but "
                                f"the stored binding for workspace {workspace_id} records "
                                f"{field}={binding_value!r}",
                                fixed=False,
                            )
                        )
                if not _locations_match(marker.get("location"), binding.location):
                    problems.append(
                        Problem(
                            "coordination_binding_drift",
                            f"marker {marker_path} records location="
                            f"{marker.get('location')!r} but the stored binding for "
                            f"workspace {workspace_id} records "
                            f"location={binding.location!r}",
                            fixed=False,
                        )
                    )

    if ctx["allow_binding_read"] and expected_kind is not None:
        for workspace_id in sorted(known_workspaces):
            binding = _store_binding(store, workspace_id)
            if binding is None:
                continue
            if binding.sink_kind != expected_kind:
                problems.append(
                    Problem(
                        "coordination_binding_drift",
                        f"stored binding for workspace {workspace_id} names sink_kind "
                        f"{binding.sink_kind!r}, but this store is {expected_kind!r}",
                        fixed=False,
                    )
                )
            if not _locations_match(binding.location, expected_location):
                problems.append(
                    Problem(
                        "coordination_binding_drift",
                        f"stored binding for workspace {workspace_id} names location "
                        f"{binding.location!r}, but this store is at "
                        f"{expected_location!r}",
                        fixed=False,
                    )
                )
    return problems


def _check_revision_drift(ctx):
    """Finding 7: an envelope with a present-but-invalid (or missing) revision."""
    problems = []
    for entry in ctx["revision_drift"]:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        problems.append(
            Problem(
                "coordination_revision_drift",
                f"{entry.get('kind')} {entry.get('record_id')!r} has a schema_version but "
                f"no usable revision (schema_version={payload.get('schema_version')!r}, "
                f"revision={payload.get('revision')!r}); the envelope cannot be versioned",
                fixed=False,
            )
        )
    return problems


def _event_id(entry):
    payload = entry.get("payload")
    if isinstance(payload, dict):
        event = payload.get("event")
        if isinstance(event, dict) and isinstance(event.get("id"), str):
            return event["id"]
        if isinstance(payload.get("id"), str):
            return payload["id"]
    return None


def _check_event_cursor_conflict(ctx):
    """Finding 8: two events sharing a cursor or an event id."""
    problems = []
    by_cursor: dict = {}
    by_id: dict = {}
    for entry in ctx["events"]:
        cursor = entry.get("cursor")
        if cursor is not None:
            by_cursor.setdefault(cursor, []).append(entry)
        event_id = _event_id(entry)
        if event_id:
            by_id.setdefault(event_id, []).append(entry)
    for cursor in sorted(by_cursor):
        if len(by_cursor[cursor]) > 1:
            problems.append(
                Problem(
                    "coordination_event_cursor_conflict",
                    f"{len(by_cursor[cursor])} stored events share cursor {cursor}; a "
                    "cursor is unique per store",
                    fixed=False,
                )
            )
    for event_id in sorted(by_id):
        if len(by_id[event_id]) > 1:
            problems.append(
                Problem(
                    "coordination_event_cursor_conflict",
                    f"{len(by_id[event_id])} stored events share event id {event_id!r}",
                    fixed=False,
                )
            )
    return problems


def _check_legacy_records(ctx):
    """Finding 9: an unversioned/legacy envelope, upgraded only when unambiguous."""
    problems = []
    store = ctx["store"]
    fix = ctx["fix"]
    for entry, record in ctx["legacy"]:
        label = f"{entry.get('kind')} {entry.get('record_id')!r}"
        valid = False
        if record is not None:
            try:
                valid = record.validate() == []
            except Exception:
                valid = False
        if fix and valid:
            try:
                with store.transaction() as tx:
                    tx.put(record, expect_revision=None)
            except Exception as error:
                problems.append(
                    Problem(
                        "coordination_legacy_record",
                        f"{label} is stored in a legacy (unversioned) envelope and could "
                        f"not be upgraded: {error}",
                        fixed=False,
                    )
                )
                continue
            problems.append(
                Problem(
                    "coordination_legacy_record",
                    f"{label} was stored in a legacy (unversioned) envelope; upgraded to "
                    "a versioned envelope",
                    fixed=True,
                )
            )
            continue
        reason = (
            ""
            if valid
            else " (the record payload does not validate, so it is reported only)"
        )
        problems.append(
            Problem(
                "coordination_legacy_record",
                f"{label} is stored in a legacy (unversioned) envelope; re-run with --fix "
                f"to upgrade it{reason}",
                fixed=False,
            )
        )
    return problems


def _check_unreadable_records(ctx):
    """Finding 10: an entry with no usable envelope and no usable legacy payload."""
    problems = []
    for entry in ctx["unreadable"]:
        detail = entry.get("error") or "the stored record envelope could not be interpreted"
        problems.append(
            Problem(
                "coordination_record_unreadable",
                f"{entry.get('kind')} {entry.get('record_id')!r}: {detail}",
                fixed=False,
            )
        )
    return problems


def _check_pending_operations(ctx):
    """Finding 11: leftover write-ahead journals (file sink)."""
    problems = []
    store = ctx["store"]
    fix = ctx["fix"]
    pending = list(ctx["pending_before"])
    if not pending:
        return problems
    remaining = set(pending)
    if fix:
        _safe_call(store.replay_journals)
        remaining = set(_safe_list(store.pending_journals))
    for operation_id in pending:
        if fix and operation_id not in remaining:
            detail = (
                f"operation {operation_id!r} left a write-ahead journal; it was replayed "
                "forward"
            )
            fixed = True
        elif fix:
            detail = (
                f"operation {operation_id!r} left a write-ahead journal that could not be "
                "replayed; the journal (and its evidence) is preserved"
            )
            fixed = False
        else:
            detail = (
                f"operation {operation_id!r} left a write-ahead journal that was not fully "
                "applied; re-run with --fix to replay it"
            )
            fixed = False
        problems.append(
            Problem("coordination_pending_operation", detail, fixed=fixed)
        )
    return problems


def _check_stale_operation_index(ctx):
    """Finding 12: a missing *derived* per-operation event index (file sink)."""
    problems = []
    store = ctx["store"]
    fix = ctx["fix"]
    for operation_id in _safe_list(store.missing_event_operation_indexes):
        fixed = False
        if fix:
            fixed = bool(_safe_call(store.rebuild_event_operation_index, operation_id))
        if fixed:
            detail = (
                f"derived event index for operation {operation_id!r} was missing; it was "
                "rebuilt from the stored event"
            )
        else:
            detail = (
                f"derived event index for operation {operation_id!r} is missing; re-run "
                "with --fix to rebuild it from the stored event"
            )
        problems.append(
            Problem("coordination_stale_operation_index", detail, fixed=fixed)
        )
    return problems


def _lifecycle_for(ctx, workspace_id):
    """A `TicketLifecycle` able to settle intents for `workspace_id`, or None.

    Built from the *stored* workspace record (which carries its authoritative
    binding) rather than through `coordination_service_for`, so a doctor never
    re-binds or re-registers anything while it is repairing."""
    from .application import Actor, CoordinationService
    from .lifecycle import TicketLifecycle

    sink = ctx["sink"]
    if not callable(getattr(sink, "read", None)):
        return None
    for record in ctx["by_kind"].get("workspace", []):
        if record.id == workspace_id and record.store_binding is not None:
            service = CoordinationService(record, ctx["store"], actor=Actor("arbite.doctor"))
            return TicketLifecycle(service, sink)
    return None


def _check_pending_lifecycle_intents(ctx):
    """A lifecycle transition a crash left between its ticket write and cascade.

    While pending, the ticket and its attempt may disagree (a closed ticket whose
    attempt still reads as active), and the attempt is refused every claim and
    mutation until the transition is settled."""
    problems = []
    store = ctx["store"]
    sink = ctx["sink"]
    for intent in ctx["by_kind"].get("lifecycle_intent", []):
        if not getattr(intent, "is_pending", False):
            continue
        base = (
            f"lifecycle_intent {intent.id!r} ('{intent.transition}' of ticket "
            f"{intent.ticket_id}) is pending"
        )
        ticket = None
        if callable(getattr(sink, "read", None)):
            try:
                ticket = sink.read(intent.ticket_id)
            except Exception:
                ticket = None
        if ticket is None:
            outcome = "the ticket cannot be read, so it would be abandoned"
        elif (
            ticket.revision != intent.expected_revision
            and ticket.status == intent.target_status
            and ticket.assignee == intent.target_assignee
        ):
            outcome = "the ticket write landed, so its coordination cascade would be completed"
        else:
            outcome = "the ticket write never landed, so it would be abandoned"
        lifecycle = _lifecycle_for(ctx, intent.workspace_id) if ctx["fix"] else None
        if lifecycle is None:
            suffix = " (re-run with --fix)" if not ctx["fix"] else " (no ticket sink to settle it)"
            problems.append(
                Problem(
                    "pending_lifecycle_intent",
                    f"{base}; {outcome}{suffix}",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
            continue
        try:
            with store.operation_lock():
                with store.transaction(write=False) as tx:
                    current = tx.get("lifecycle_intent", intent.id)
                state = current.state if current is not None else "missing"
                if current is not None and current.is_pending:
                    state = lifecycle._settle(current)
        except Exception as error:
            problems.append(
                Problem(
                    "pending_lifecycle_intent",
                    f"{base}; settling it failed: {error}",
                    ticket_id=intent.ticket_id,
                    fixed=False,
                )
            )
        else:
            problems.append(
                Problem(
                    "pending_lifecycle_intent",
                    f"{base}; {outcome} -- it is now {state!r}",
                    ticket_id=intent.ticket_id,
                    fixed=True,
                )
            )
    return problems


_CHECKS = (
    ("orphan claims", _check_orphan_claims),
    ("invalid generations", _check_invalid_generations),
    ("missing artifacts", _check_missing_artifacts),
    ("corrupt artifacts", _check_corrupt_artifacts),
    ("pending intents", _check_pending_intents),
    ("binding drift", _check_binding_drift),
    ("revision drift", _check_revision_drift),
    ("event cursor conflicts", _check_event_cursor_conflict),
    ("legacy records", _check_legacy_records),
    ("unreadable records", _check_unreadable_records),
    ("pending operations", _check_pending_operations),
    ("stale operation indexes", _check_stale_operation_index),
    ("pending lifecycle intents", _check_pending_lifecycle_intents),
)


def _ticket_ids(sink, tickets):
    """The ticket-id set for orphan-cited tickets, or `None` when unverifiable."""
    if tickets is not None:
        try:
            return set(tickets)
        except Exception:
            return None
    ids_method = getattr(sink, "ids", None)
    if not callable(ids_method):
        return None
    try:
        return set(ids_method())
    except Exception:
        return None


def coordination_problems(sink, *, fix: bool = False, tickets=None) -> list:
    """Every coordination integrity problem in `sink`'s store, as `Problem`s.

    Accepts a `TicketSink` or a `CoordinationStore`. Returns `[]` (creating nothing)
    when there is no store or it is not initialised. Each check is guarded so broken
    state is reported rather than raised. The result is sorted by `(kind, detail)`;
    see the module docstring for the exact fix policy.
    """
    store = _store_of(sink)
    if store is None:
        return []
    try:
        if not store.is_initialised():
            return []
    except Exception:
        return []

    pending_before = _safe_list(store.pending_journals)
    # Reading a stored binding replays leftover journals on the file sink (that is
    # how its transactions work). In plain report mode that would silently repair
    # the very state finding 11 exists to report, so binding reads are deferred
    # while journals are pending; --fix replays them and then reads freely.
    allow_binding_read = fix or not pending_before

    ticket_ids = _ticket_ids(sink, tickets)

    ctx = _build_context(
        sink,
        store,
        fix=fix,
        tickets=ticket_ids,
        pending_before=pending_before,
        allow_binding_read=allow_binding_read,
    )

    problems: list = []
    for label, check in _CHECKS:
        try:
            problems.extend(check(ctx))
        except Exception as error:  # defensive: malformed state, not a crash
            problems.append(
                Problem(
                    CHECK_FAILED,
                    f"the {label} check could not complete: {error}",
                    fixed=False,
                )
            )

    # Deterministic ordering, and no accidentally duplicated finding.
    seen = set()
    ordered = []
    for problem in sorted(problems, key=lambda p: (p.kind, p.detail)):
        key = (problem.kind, problem.detail, problem.ticket_id, problem.location)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(problem)
    return ordered


__all__ = ["coordination_problems"]
