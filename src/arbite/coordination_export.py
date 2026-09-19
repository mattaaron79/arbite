"""Storage-neutral export, verification and import of coordination history.

This is the C11 (planning key) module for *moving* shared-directory coordination
history between stores -- a file sink and a SQLite sink, or one store of either
kind and another. It is deliberately storage-neutral: it knows only the
`CoordinationStore` / `CoordinationTransaction` surface (plus
`read_artifact_bytes`, `has_artifact`, `store_artifact_bytes`, `event_log`,
`recover_pending`, `record_namespace`, `namespaces`, `cursor_namespace`,
`contract_version`) and it never imports the SQLite module nor either concrete
sink module. It never assumes a layout on disk and it never opens a database
directly.

## What a "bundle" is

An *export bundle* is a single JSON-serialisable mapping holding one store's
history for one (or every) workspace. It is deliberately a plain `dict` so the
CLI can serialise it, diffs are readable, and `bundle_problems` can inspect it
purely. Its keys are exactly:

- ``schema_version`` / ``arbite_coordination_export`` -- both equal
  `EXPORT_VERSION`; the version is stated twice so an accidental rename of one
  key is still caught.
- ``contract_version`` -- the source store's `contract_version()`.
- ``exported_at`` -- `coordination.utc_now()` at export time.
- ``cursor_namespace`` -- the source store's `cursor_namespace()`. Every event's
  ``source_cursor`` is a cursor *in this namespace*; cursors are never reused as
  destination cursors.
- ``retained_history`` -- always `True`: evidence is retained past ticket
  closure and nothing here garbage-collects it (artifacts grow; see
  `artifacts`).
- ``workspace_id`` -- the requested/selected workspace id, or `None`.
- ``bound_at`` -- the matching `StoreBinding.bound_at` for the selected
  workspace, or `None`.
- ``records`` -- a mapping of these eight **exact** group keys, always present
  (possibly empty), each a list of ``record.to_dict()``:

  ========================  ==========================
  group key                 record kind
  ========================  ==========================
  ``workspaces``            workspace
  ``store_bindings``        store_binding
  ``work_attempts``         work_attempt
  ``file_claims``           file_claim
  ``read_observations``     read_observation
  ``operation_receipts``    operation_receipt
  ``operation_intents``     operation_intent
  ``recovery_reports``      recovery_report
  ========================  ==========================

  ``artifact`` records are *not* a record group: their content lives in
  ``artifacts`` (below), keyed by digest, because content addressing is what
  makes a bundle self-describing.
- ``events`` -- ``Event.to_dict()`` entries, each augmented with
  ``source_cursor`` (the event's cursor in the source store) and
  ``source_namespace`` (equal to the bundle's ``cursor_namespace``).
- ``artifacts`` -- entries ``{"digest", "size", "media_type", "data_b64",
  "created"}`` for the distinct digests referenced by the exported
  receipts/intents ``artifact_refs`` plus stored ``Artifact`` records, read via
  ``store.read_artifact_bytes``. With ``include_artifacts=False`` (or when a
  source blob cannot be served) the entry omits ``data_b64`` and carries
  ``"data_omitted": True`` instead.
- ``counts`` -- ``bundle_counts()``: one key per record group plus ``events``
  and ``artifacts``.

## Workspace filtering rules

When ``workspace_id`` is given:

- records that *carry* a ``workspace_id`` (workspace id, store binding, work
  attempt, file claim, operation intent, recovery report) are kept only for that
  workspace;
- records that do not carry one (operation receipt, read observation) are kept
  only when their ``attempt_id`` is one of the kept attempts (a receipt always
  belongs to an attempt); when the reference cannot be resolved the record is
  kept so a dangling reference is reported rather than silently dropped;
- events are kept when **any** of: ``payload["workspace_id"]`` equals the
  requested id, the requested id is in ``subject_ids``, the event's
  ``operation_id`` names an exported operation, or the event carries neither an
  operation nor an explicit workspace mention (an unattributed lifecycle marker
  that cannot be assigned to any workspace). Everything else is excluded.

With ``workspace_id=None`` everything is exported.

Reads create nothing: every read path is guarded by ``store.is_initialised()``
and an untouched legacy store yields a well-formed *empty* bundle (correct
namespace and contract, no records).

## Importing

`import_coordination` writes records in one store transaction (artifact *blobs*
are stored first because `store_artifact_bytes` is a store-level operation), then
records and events together, then the namespace registry. Events are appended in
ascending ``source_cursor`` order through ``tx.append_event_once`` so a repeated
import cannot duplicate them; the source -> destination cursor mapping is recorded
in the namespace registry under the **source** namespace, so the destination's own
cursors are never confused with the source's.

### Foreign `StoreBinding` records are skipped

A bundle may carry ``store_binding`` records naming the *source* store. Importing
one into a different store would leave a binding that names a foreign
``(sink_kind, location)``, which (a) makes
``workspace.ensure_binding(..., rebind=True)`` fail with `StoreBindingConflict`
and (b) is exactly what `doctor` reports as ``coordination_binding_drift``. So a
bundle binding is written **only** when it already matches the destination's own
authoritative binding for that workspace; anything else is counted as
``bindings_skipped`` and left for the caller's explicit rebind to replace. This
module never writes a binding marker and never calls
`workspace.ensure_binding`.

`bundle_problems` reports on the eight doctor kinds this layer can decide
(``coordination_bundle_invalid``, ``coordination_orphan_claim``,
``coordination_dangling_reference``, ``coordination_missing_artifact``,
``coordination_artifact_corrupt``), plus structural validation; it is pure and
never mutates the bundle it inspects.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import artifacts, coordination
from .coordination import Event
from .errors import ArtifactCorrupt, CoordinationConflict, InvalidRecord, UnsupportedCoordination
from .sinks.base import Problem

#: Version of the export bundle envelope. Bumped when the bundle shape changes
#: incompatibly; `read_bundle` refuses anything but this value.
EXPORT_VERSION = 1

#: The eight record groups, in a fixed order, so the bundle shape is stable even
#: when a group is empty.
RECORD_GROUPS = (
    "workspaces",
    "store_bindings",
    "work_attempts",
    "file_claims",
    "read_observations",
    "operation_receipts",
    "operation_intents",
    "recovery_reports",
)

#: Record kind -> bundle group. The mapping is total over coordination records
#: except `artifact`, whose content is carried in `artifacts` instead.
GROUP_FOR_KIND = {
    "workspace": "workspaces",
    "store_binding": "store_bindings",
    "work_attempt": "work_attempts",
    "file_claim": "file_claims",
    "read_observation": "read_observations",
    "operation_receipt": "operation_receipts",
    "operation_intent": "operation_intents",
    "recovery_report": "recovery_reports",
}

#: Every top-level key a well-formed bundle must have.
BUNDLE_KEYS = (
    "schema_version",
    "arbite_coordination_export",
    "contract_version",
    "exported_at",
    "cursor_namespace",
    "retained_history",
    "workspace_id",
    "bound_at",
    "records",
    "events",
    "artifacts",
    "counts",
)

#: Recovery/operation states that block migrating a store: a leftover file-sink
#: journal that has not been cleanly finalised. `applied`/`reverted` are the
#: unambiguous cases and do not block.
BLOCKING_RECOVERY_STATES = ("pending", "unknown", "drifted")

_MISSING = object()


# ---------------------------------------------------------------------------
# Sink/store resolution
# ---------------------------------------------------------------------------


def _store_of(sink):
    """The `CoordinationStore` behind `sink`.

    Accepts either a `TicketSink` (anything with a callable ``coordination()``)
    or a `CoordinationStore` directly. A sink whose ``coordination()`` returns
    `None` does not implement the shared-directory contract and is refused
    explicitly rather than treated as a store.
    """
    method = getattr(sink, "coordination", None)
    if callable(method):
        store = method()
        if store is None:
            raise UnsupportedCoordination(
                f"the {getattr(sink, 'kind', '?')} sink does not implement the "
                "shared-directory coordination contract, so its history cannot be "
                "exported or imported"
            )
        return store
    return sink


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _empty_bundle(store, workspace_id: Optional[str]) -> dict:
    """A well-formed empty bundle for `store` (namespace/contract correct).

    Used for an untouched legacy store: reading must not create anything, so the
    only honest answer is "this store has no history yet".
    """
    bundle = {
        "schema_version": EXPORT_VERSION,
        "arbite_coordination_export": EXPORT_VERSION,
        "contract_version": store.contract_version(),
        "exported_at": coordination.utc_now(),
        "cursor_namespace": store.cursor_namespace(),
        "retained_history": True,
        "workspace_id": workspace_id,
        "bound_at": None,
        "records": {group: [] for group in RECORD_GROUPS},
        "events": [],
        "artifacts": [],
        "counts": {},
    }
    bundle["counts"] = bundle_counts(bundle)
    return bundle


def _bound_at(store, workspace_id: Optional[str], workspace_ids: list) -> Optional[str]:
    """The authoritative binding's ``bound_at`` for the selected workspace.

    Read through `CoordinationStore.store_binding` (the authoritative surface),
    not through `store_binding` *records*, which normal binding never writes.
    Called outside any transaction because the file sink's `store_binding` takes
    the same coarse lock a transaction holds.
    """
    target = workspace_id
    if target is None:
        if len(workspace_ids) != 1:
            return None
        target = workspace_ids[0]
    try:
        binding = store.store_binding(target)
    except UnsupportedCoordination:
        return None
    return getattr(binding, "bound_at", None) if binding is not None else None


def _keep_observation(obs, workspace_id, kept_attempt_ids) -> bool:
    if workspace_id is None:
        return True
    attempt_id = getattr(obs, "attempt_id", None)
    return attempt_id is None or attempt_id in kept_attempt_ids


def _keep_receipt(receipt, workspace_id, kept_attempt_ids) -> bool:
    if workspace_id is None:
        return True
    return getattr(receipt, "attempt_id", None) in kept_attempt_ids


def _event_matches(event, workspace_id, exported_operation_ids) -> bool:
    """Whether `event` belongs to the requested workspace (documented above)."""
    if workspace_id is None:
        return True
    payload = getattr(event, "payload", None) or {}
    if payload.get("workspace_id") == workspace_id:
        return True
    if workspace_id in list(getattr(event, "subject_ids", None) or []):
        return True
    operation_id = getattr(event, "operation_id", None)
    if operation_id and operation_id in exported_operation_ids:
        return True
    # Unattributed: carries neither an operation nor an explicit workspace
    # mention, so it cannot be assigned to any *other* workspace either.
    if not operation_id and not payload.get("workspace_id"):
        return True
    return False


def export_coordination(sink, *, workspace_id: Optional[str] = None,
                        include_artifacts: bool = True) -> dict:
    """Export `sink`'s coordination history as a bundle mapping.

    Accepts a `TicketSink` or a `CoordinationStore`. With ``workspace_id=None``
    everything is exported; with an id, only that workspace's history is (see the
    module docstring for the exact filtering rule). ``include_artifacts=False``
    emits artifact *metadata* without ``data_b64`` (marked ``data_omitted``), for
    a bundle that records what exists without carrying the bytes. Reading a store
    that has never been initialised returns a well-formed empty bundle and
    creates nothing.
    """
    store = _store_of(sink)
    if not store.is_initialised():
        return _empty_bundle(store, workspace_id)

    with store.transaction(write=False) as tx:
        raw = {}
        for kind in list(GROUP_FOR_KIND) + ["artifact"]:
            raw[kind] = list(tx.find(kind))
        events = list(tx.find("event"))

        kept_workspaces = [
            w for w in raw["workspace"]
            if workspace_id is None or w.id == workspace_id
        ]
        workspace_ids = sorted(w.id for w in kept_workspaces)

        kept_attempts = [
            a for a in raw["work_attempt"]
            if workspace_id is None or a.workspace_id == workspace_id
        ]
        kept_attempt_ids = {a.id for a in kept_attempts}

        kept = {
            "workspaces": kept_workspaces,
            "store_bindings": [
                b for b in raw["store_binding"]
                if workspace_id is None or b.workspace_id == workspace_id
            ],
            "work_attempts": kept_attempts,
            "file_claims": [
                c for c in raw["file_claim"]
                if workspace_id is None or c.workspace_id == workspace_id
            ],
            "read_observations": [
                o for o in raw["read_observation"]
                if _keep_observation(o, workspace_id, kept_attempt_ids)
            ],
            "operation_receipts": [
                r for r in raw["operation_receipt"]
                if _keep_receipt(r, workspace_id, kept_attempt_ids)
            ],
            "operation_intents": [
                i for i in raw["operation_intent"]
                if workspace_id is None or i.workspace_id == workspace_id
            ],
            "recovery_reports": [
                r for r in raw["recovery_report"]
                if workspace_id is None or r.workspace_id == workspace_id
            ],
        }

        exported_operation_ids = {r.id for r in kept["operation_receipts"]}
        exported_operation_ids |= {i.operation_id for i in kept["operation_intents"]}
        kept_events = [
            e for e in events
            if _event_matches(e, workspace_id, exported_operation_ids)
        ]

        # Resolve artifact digests: the refs are deterministic artifact ids, so a
        # stored Artifact record is what turns a ref into a digest. Stored
        # artifact records are also included on their own (content-addressed
        # evidence is retained, not reference-counted).
        stored_by_id = {a.id: a for a in raw["artifact"]}
        digest_meta: dict = {}
        refs = []
        for record in kept["operation_receipts"] + kept["operation_intents"]:
            refs.extend(getattr(record, "artifact_refs", None) or [])
        for ref in refs:
            record = stored_by_id.get(ref)
            if record is not None:
                digest_meta.setdefault(record.digest, record)
        for record in raw["artifact"]:
            digest_meta.setdefault(record.digest, record)

        artifact_entries = []
        for digest in sorted(digest_meta):
            meta = digest_meta[digest]
            data = store.read_artifact_bytes(digest)
            entry = {
                "digest": digest,
                "size": len(data) if data is not None else int(getattr(meta, "size", 0)),
                "media_type": getattr(meta, "media_type", artifacts.DEFAULT_MEDIA_TYPE),
                "created": getattr(meta, "created", None),
            }
            if include_artifacts and data is not None:
                entry["data_b64"] = base64.b64encode(data).decode("ascii")
            else:
                entry["data_omitted"] = True
            artifact_entries.append(entry)

    bundle = {
        "schema_version": EXPORT_VERSION,
        "arbite_coordination_export": EXPORT_VERSION,
        "contract_version": store.contract_version(),
        "exported_at": coordination.utc_now(),
        "cursor_namespace": store.cursor_namespace(),
        "retained_history": True,
        "workspace_id": workspace_id,
        "bound_at": _bound_at(store, workspace_id, workspace_ids),
        "records": {
            group: [record.to_dict() for record in
                    sorted(kept[group], key=lambda r: r.record_id)]
            for group in RECORD_GROUPS
        },
        "events": [
            {**event.to_dict(), "source_cursor": event.cursor,
             "source_namespace": store.cursor_namespace()}
            for event in kept_events
        ],
        "artifacts": artifact_entries,
        "counts": {},
    }
    # The cursor namespace is computed once; if a sink's probe is expensive this
    # keeps it stable within one bundle and avoids a second call.
    bundle["counts"] = bundle_counts(bundle)
    return bundle


def bundle_counts(bundle) -> dict:
    """``{group: count, ..., "events": n, "artifacts": n}`` for `bundle`.

    Pure and defensive: anything that is not the expected shape counts as zero,
    so the CLI can print counts for a hand-written or half-built bundle without
    crashing.
    """
    counts = {}
    records = bundle.get("records") if isinstance(bundle, dict) else None
    for group in RECORD_GROUPS:
        items = records.get(group) if isinstance(records, dict) else None
        counts[group] = len(items) if isinstance(items, list) else 0
    events = bundle.get("events") if isinstance(bundle, dict) else None
    counts["events"] = len(events) if isinstance(events, list) else 0
    artifact_items = bundle.get("artifacts") if isinstance(bundle, dict) else None
    counts["artifacts"] = len(artifact_items) if isinstance(artifact_items, list) else 0
    return counts


# ---------------------------------------------------------------------------
# Writing and reading a bundle on disk
# ---------------------------------------------------------------------------


def write_bundle(bundle, path) -> str:
    """Write `bundle` to `path` atomically as versioned JSON; return the path.

    Temp file in the destination directory, ``fsync``, ``os.replace`` -- the same
    crash-safe pattern `workspace._write_marker` and the file sink's
    ``_write_json`` use, so a reader never sees a half-written bundle. Keys are
    sorted and a trailing newline is written so the file is diff-friendly.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".arbite-export-", dir=str(target.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(bundle, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    return str(target)


def read_bundle(path) -> dict:
    """Read and structurally validate a bundle file; raise `InvalidRecord`.

    A missing/unreadable file, a non-JSON file, a wrong version, or a bundle
    missing a required key all raise `InvalidRecord` with the details, so a
    caller never proceeds on a bundle it cannot honestly interpret.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidRecord(
            f"coordination bundle {target} is unreadable: {error}",
            details={"path": str(target)},
        )
    try:
        bundle = json.loads(text)
    except ValueError as error:
        raise InvalidRecord(
            f"coordination bundle {target} is not valid JSON: {error}",
            details={"path": str(target)},
        )
    problems = _structural_problems(bundle)
    if problems:
        raise InvalidRecord(
            f"coordination bundle {target} is malformed or not a supported "
            f"version (expected {EXPORT_VERSION})",
            details={
                "path": str(target),
                "problems": [p.to_dict() for p in _sorted_problems(problems)],
            },
        )
    return bundle


# ---------------------------------------------------------------------------
# Verification (pure)
# ---------------------------------------------------------------------------


def _sorted_problems(problems: list) -> list:
    """Deterministic ordering: by kind, then detail."""
    return sorted(problems, key=lambda p: (p.kind, p.detail))


def _invalid(detail: str) -> Problem:
    return Problem("coordination_bundle_invalid", detail, fixed=False)


def _iter_record_dicts(bundle):
    records = bundle.get("records") if isinstance(bundle, dict) else None
    if not isinstance(records, dict):
        return
    for group, items in records.items():
        if isinstance(items, list):
            for record in items:
                if isinstance(record, dict):
                    yield group, record


def _artifact_id(value: Any) -> bool:
    """True for a deterministic artifact id (`art-<16 hex>`)."""
    return coordination.is_opaque_id(value, coordination.ID_PREFIXES["artifact"])


def _coordination_prefix(value: Any) -> Optional[str]:
    """The coordination id prefix `value` carries, or None."""
    for prefix in coordination.ID_PREFIXES.values():
        if coordination.is_opaque_id(value, prefix):
            return prefix
    return None


def _structural_problems(bundle) -> list:
    """Shape/version/digest-syntax problems, shared by `bundle_problems` and
    `read_bundle`. Pure: it never touches the input beyond reading it."""
    problems = []
    if not isinstance(bundle, dict):
        return [_invalid("bundle is not a JSON object")]

    for key in ("schema_version", "arbite_coordination_export"):
        value = bundle.get(key)
        if value != EXPORT_VERSION:
            problems.append(
                _invalid(f"{key} must be {EXPORT_VERSION}, got {value!r}")
            )
    for key in BUNDLE_KEYS:
        if key not in bundle:
            problems.append(_invalid(f"missing required key {key!r}"))

    records = bundle.get("records")
    if not isinstance(records, dict):
        problems.append(_invalid(
            f"records must be a mapping of group -> list, got "
            f"{type(records).__name__}"
        ))
    else:
        for group in RECORD_GROUPS:
            if group not in records:
                problems.append(_invalid(f"records.{group} is missing"))
            elif not isinstance(records[group], list):
                problems.append(_invalid(
                    f"records.{group} must be a list, got "
                    f"{type(records[group]).__name__}"
                ))

    for key in ("events", "artifacts"):
        if key in bundle and not isinstance(bundle[key], list):
            problems.append(_invalid(
                f"{key} must be a list, got {type(bundle[key]).__name__}"
            ))

    # Digest syntax on every record that carries a digest-shaped field.
    for group, record in _iter_record_dicts(bundle):
        label = f"{group} record {record.get('id')!r}"
        if "digest" in record and not coordination.is_digest(record.get("digest")):
            problems.append(_invalid(
                f"{label} has an invalid digest {record.get('digest')!r}"
            ))
        observed = record.get("observed_version", _MISSING)
        if observed is not _MISSING and not coordination.is_digest_or_absent(observed):
            problems.append(_invalid(
                f"{label} has an invalid observed_version {observed!r}"
            ))
        for field in ("before", "after"):
            mapping = record.get(field)
            if isinstance(mapping, dict):
                for path, version in mapping.items():
                    if not coordination.is_digest_or_absent(version):
                        problems.append(_invalid(
                            f"{label} {field}[{path!r}] is not a digest or "
                            f"{coordination.ABSENT!r}: {version!r}"
                        ))
        refs = record.get("artifact_refs")
        if isinstance(refs, list):
            for ref in refs:
                if not (coordination.is_digest(ref) or _artifact_id(ref)):
                    problems.append(_invalid(
                        f"{label} artifact_refs entry {ref!r} is neither a "
                        "content digest nor an artifact id"
                    ))

    artifact_items = bundle.get("artifacts")
    if isinstance(artifact_items, list):
        for entry in artifact_items:
            if not isinstance(entry, dict):
                problems.append(_invalid(
                    f"artifacts entry is not a mapping: {entry!r}"
                ))
                continue
            label = f"artifact {entry.get('digest')!r}"
            if not coordination.is_digest(entry.get("digest")):
                problems.append(_invalid(
                    f"{label} has an invalid digest {entry.get('digest')!r}"
                ))
            size = entry.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                problems.append(Problem(
                    "coordination_artifact_corrupt",
                    f"{label} has an invalid size {size!r}",
                    fixed=False,
                ))
    return problems


def bundle_problems(bundle) -> list:
    """Every integrity problem in `bundle`, as sorted `Problem` entries.

    PURE and non-mutating. Findings use stable kinds:

    - ``coordination_bundle_invalid`` -- wrong/missing ``schema_version`` /
      ``arbite_coordination_export``, a missing required top-level key, a
      non-list group, or a malformed digest in any ``digest``,
      ``observed_version``, ``before``/``after`` value or ``artifact_refs``
      entry.
    - ``coordination_orphan_claim`` -- a claim whose ``attempt_id``,
      ``ticket_id`` or ``workspace_id`` is not present in the bundle.
    - ``coordination_dangling_reference`` -- an attempt/binding/receipt/intent/
      observation/event naming a workspace, attempt, ticket, operation or
      coordination subject the bundle does not contain.
    - ``coordination_missing_artifact`` -- a receipt/intent ``artifact_refs``
      digest (or artifact id) with no matching ``artifacts`` entry, or an
      artifact entry whose data was required but omitted.
    - ``coordination_artifact_corrupt`` -- an artifact whose base64 data does not
      decode, or does not verify against its ``digest``/``size``.

    Every Problem has ``fixed=False``: verification reports, it never repairs.
    """
    if not isinstance(bundle, dict):
        return [_invalid("bundle is not a JSON object")]
    problems = list(_structural_problems(bundle))

    records = bundle.get("records")
    if not isinstance(records, dict):
        return _sorted_problems(problems)

    def group(name):
        items = records.get(name)
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []

    workspaces = group("workspaces")
    bindings = group("store_bindings")
    attempts = group("work_attempts")
    claims = group("file_claims")
    observations = group("read_observations")
    receipts = group("operation_receipts")
    intents = group("operation_intents")
    reports = group("recovery_reports")

    workspace_ids = {w.get("id") for w in workspaces}
    attempt_ids = {a.get("id") for a in attempts}
    claim_ids = {c.get("id") for c in claims}
    observation_ids = {o.get("id") for o in observations}
    receipt_ids = {r.get("id") for r in receipts}
    intent_ids = {i.get("id") for i in intents}
    report_ids = {r.get("id") for r in reports}
    binding_ids = {b.get("id") for b in bindings}

    ticket_ids = set()
    for record in attempts + claims + receipts + intents:
        ticket = record.get("ticket_id")
        if ticket:
            ticket_ids.add(ticket)

    operation_ids = set(receipt_ids)
    operation_ids |= {i.get("operation_id") for i in intents if i.get("operation_id")}

    known_ids = (
        workspace_ids | attempt_ids | claim_ids | observation_ids
        | receipt_ids | intent_ids | report_ids | binding_ids
    )

    # -- orphan claims --------------------------------------------------
    for claim in claims:
        label = f"claim {claim.get('id')!r}"
        if claim.get("attempt_id") not in attempt_ids:
            problems.append(Problem(
                "coordination_orphan_claim",
                f"{label} names attempt {claim.get('attempt_id')!r}, which is not "
                "present in the bundle",
                ticket_id=claim.get("ticket_id"),
                fixed=False,
            ))
        if claim.get("workspace_id") not in workspace_ids:
            problems.append(Problem(
                "coordination_orphan_claim",
                f"{label} names workspace {claim.get('workspace_id')!r}, which is "
                "not present in the bundle",
                ticket_id=claim.get("ticket_id"),
                fixed=False,
            ))
        if claim.get("ticket_id") not in ticket_ids:
            problems.append(Problem(
                "coordination_orphan_claim",
                f"{label} names ticket {claim.get('ticket_id')!r}, which no other "
                "bundle record references",
                ticket_id=claim.get("ticket_id"),
                fixed=False,
            ))

    # -- dangling references --------------------------------------------
    for binding in bindings:
        if binding.get("workspace_id") not in workspace_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"store_binding {binding.get('id')!r} names workspace "
                f"{binding.get('workspace_id')!r}, which is not in the bundle",
                fixed=False,
            ))
    for attempt in attempts:
        if attempt.get("workspace_id") not in workspace_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"work_attempt {attempt.get('id')!r} names workspace "
                f"{attempt.get('workspace_id')!r}, which is not in the bundle",
                ticket_id=attempt.get("ticket_id"),
                fixed=False,
            ))
    for receipt in receipts:
        label = f"receipt {receipt.get('id')!r}"
        if receipt.get("attempt_id") not in attempt_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} names attempt {receipt.get('attempt_id')!r}, which is "
                "not in the bundle",
                ticket_id=receipt.get("ticket_id"),
                fixed=False,
            ))
        if receipt.get("ticket_id") not in ticket_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} names ticket {receipt.get('ticket_id')!r}, which no "
                "bundle record references",
                ticket_id=receipt.get("ticket_id"),
                fixed=False,
            ))
    for intent in intents:
        label = f"intent {intent.get('id')!r}"
        if intent.get("attempt_id") not in attempt_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} names attempt {intent.get('attempt_id')!r}, which is "
                "not in the bundle",
                ticket_id=intent.get("ticket_id"),
                fixed=False,
            ))
        if intent.get("workspace_id") not in workspace_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} names workspace {intent.get('workspace_id')!r}, which is "
                "not in the bundle",
                ticket_id=intent.get("ticket_id"),
                fixed=False,
            ))
        # A *finalized* intent promises a receipt; a pending/applied intent
        # legitimately has none yet, so only the finalized case is dangling.
        if intent.get("state") == "finalized" and intent.get("operation_id") not in operation_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} is finalized but its operation "
                f"{intent.get('operation_id')!r} has no receipt in the bundle",
                ticket_id=intent.get("ticket_id"),
                fixed=False,
            ))
    for observation in observations:
        attempt_id = observation.get("attempt_id")
        if attempt_id is not None and attempt_id not in attempt_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"read_observation {observation.get('id')!r} names attempt "
                f"{attempt_id!r}, which is not in the bundle",
                fixed=False,
            ))
    for report in reports:
        if report.get("workspace_id") not in workspace_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"recovery_report {report.get('id')!r} names workspace "
                f"{report.get('workspace_id')!r}, which is not in the bundle",
                fixed=False,
            ))
    for event in bundle.get("events") or []:
        if not isinstance(event, dict):
            continue
        label = f"event {event.get('id')!r}"
        operation_id = event.get("operation_id")
        if operation_id and operation_id not in operation_ids:
            problems.append(Problem(
                "coordination_dangling_reference",
                f"{label} names operation {operation_id!r}, which is not in the "
                "bundle",
                fixed=False,
            ))
        for subject in event.get("subject_ids") or []:
            if _coordination_prefix(subject) is not None and subject not in known_ids:
                problems.append(Problem(
                    "coordination_dangling_reference",
                    f"{label} names subject {subject!r}, which is not in the bundle",
                    fixed=False,
                ))

    # -- artifacts -------------------------------------------------------
    artifact_items = bundle.get("artifacts")
    if not isinstance(artifact_items, list):
        artifact_items = []
    artifact_digests = set()
    artifact_ids = set()
    for entry in artifact_items:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        if coordination.is_digest(digest):
            artifact_digests.add(digest)
            artifact_ids.add(coordination.artifact_id_for_digest(digest))
        if entry.get("data_b64") is None and not entry.get("data_omitted"):
            problems.append(Problem(
                "coordination_missing_artifact",
                f"artifact {digest!r} carries no data and does not declare "
                "data_omitted, so its content cannot be verified",
                fixed=False,
            ))

    for record in receipts + intents:
        label = f"{record.get('kind')} {record.get('id')!r}"
        for ref in record.get("artifact_refs") or []:
            if coordination.is_digest(ref):
                present = ref in artifact_digests
            else:
                present = ref in artifact_ids
            if not present:
                problems.append(Problem(
                    "coordination_missing_artifact",
                    f"{label} references artifact {ref!r}, which has no entry in "
                    "the bundle's artifacts",
                    ticket_id=record.get("ticket_id"),
                    fixed=False,
                ))

    for entry in artifact_items:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        if not coordination.is_digest(digest):
            continue
        data_b64 = entry.get("data_b64")
        if data_b64 is None:
            continue
        if not isinstance(data_b64, str):
            problems.append(Problem(
                "coordination_artifact_corrupt",
                f"artifact {digest} data_b64 is not a string",
                fixed=False,
            ))
            continue
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as error:
            problems.append(Problem(
                "coordination_artifact_corrupt",
                f"artifact {digest} data_b64 does not decode: {error}",
                fixed=False,
            ))
            continue
        expected_size = entry.get("size")
        try:
            artifacts.verify_artifact(
                data, expected_digest=digest, expected_size=expected_size
            )
        except ArtifactCorrupt as error:
            problems.append(Problem(
                "coordination_artifact_corrupt",
                f"artifact {digest} does not verify: {error}",
                fixed=False,
            ))

    return _sorted_problems(problems)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _group_items(bundle, group) -> list:
    records = bundle.get("records") if isinstance(bundle, dict) else None
    items = records.get(group) if isinstance(records, dict) else None
    return items if isinstance(items, list) else []


def _source_cursor_key(event: dict) -> tuple:
    cursor = event.get("source_cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        return (0, cursor)
    return (1, 0)


def _binding_matches_destination(store, record) -> bool:
    """True when `record` already matches the destination's own binding.

    Read outside any open transaction (the file sink's `store_binding` takes the
    same coarse lock a transaction holds). A destination with no binding for the
    workspace returns False, so a foreign binding is never written: the caller's
    explicit rebind writes the authoritative one.
    """
    try:
        existing = store.store_binding(record.workspace_id)
    except UnsupportedCoordination:
        return False
    return existing is not None and existing.matches_binding(record)


def import_coordination(sink, bundle, *, verify: bool = True,
                        overwrite: bool = False) -> dict:
    """Import a bundle into `sink`; return counts describing what happened.

    Accepts a `TicketSink` or a `CoordinationStore`. With ``verify=True`` (the
    default) the bundle is checked with `bundle_problems` first and the import is
    refused with `InvalidRecord` -- writing nothing -- if any problem is found.
    With ``overwrite=False`` records are written through ``tx.put_if_absent`` so
    an existing local record is never replaced; with ``overwrite=True`` the
    bundle's record replaces whatever is there. Either way a differing local
    record is *reported* in ``skipped_existing`` rather than silently dropped.

    Events are appended in ascending ``source_cursor`` order with
    ``tx.append_event_once`` (so a repeat import cannot duplicate them) and the
    source -> destination cursor mapping is recorded in the namespace registry
    under the **source** namespace. Foreign `store_binding` records are skipped
    (see the module docstring). Returns::

        {"source_namespace", "target_namespace", "records": {group: count},
         "events", "artifacts", "artifacts_skipped", "bindings_skipped",
         "skipped_existing", "imported_at"}

    where ``events``/``records``/``artifacts`` count what *this* import newly
    wrote and ``skipped_existing`` counts records/events that already matched.
    """
    store = _store_of(sink)
    if verify:
        problems = bundle_problems(bundle)
        if problems:
            raise InvalidRecord(
                "refusing to import a bundle that fails verification",
                details={"problems": [p.to_dict() for p in problems]},
            )

    source_namespace = bundle.get("cursor_namespace")
    source_contract = bundle.get("contract_version")
    imported_at = coordination.utc_now()

    record_counts = {group: 0 for group in RECORD_GROUPS}
    skipped_existing = 0
    bindings_skipped = 0
    events_appended = 0
    artifacts_stored = 0
    artifacts_skipped = 0
    cursor_map: dict = {}

    # Resolve which bundle bindings may be written BEFORE opening the transaction:
    # store.store_binding takes the same coarse lock a file-sink transaction holds.
    bindings_keep = {}
    for data in _group_items(bundle, "store_bindings"):
        record = coordination.record_from_dict(data)
        bindings_keep[record.id] = _binding_matches_destination(store, record)

    # Artifact blobs first: store_artifact_bytes is a store-level operation, not
    # a transaction verb.
    artifact_descriptors = []
    for entry in bundle.get("artifacts") or []:
        if not isinstance(entry, dict):
            continue
        data_b64 = entry.get("data_b64")
        if data_b64 is None:
            artifacts_skipped += 1
            continue
        data = base64.b64decode(data_b64)
        media_type = entry.get("media_type") or artifacts.DEFAULT_MEDIA_TYPE
        descriptor = store.store_artifact_bytes(data, media_type=media_type)
        artifact_descriptors.append((entry, descriptor))
        artifacts_stored += 1

    # One transaction: artifact records, then every record group, then events.
    with store.transaction() as tx:
        for entry, descriptor in artifact_descriptors:
            record = coordination.Artifact(
                id=descriptor.id,
                digest=descriptor.digest,
                size=descriptor.size,
                created=entry.get("created") or descriptor.created,
                location=descriptor.location,
                media_type=entry.get("media_type") or descriptor.media_type,
            )
            _stored, created = tx.put_if_absent(record)
            if not created:
                skipped_existing += 1

        for kind, group in GROUP_FOR_KIND.items():
            for data in _group_items(bundle, group):
                record = coordination.record_from_dict(data)
                if kind == "store_binding" and not bindings_keep.get(record.id):
                    bindings_skipped += 1
                    continue
                if overwrite:
                    tx.put(record, expect_revision=None)
                    record_counts[group] += 1
                else:
                    _stored, created = tx.put_if_absent(record)
                    if created:
                        record_counts[group] += 1
                    else:
                        skipped_existing += 1

        events = sorted(
            (e for e in (bundle.get("events") or []) if isinstance(e, dict)),
            key=_source_cursor_key,
        )
        for data in events:
            event = Event.from_dict(data)
            event.cursor = None
            stored = tx.append_event_once(event)
            if stored is event:
                events_appended += 1
            else:
                skipped_existing += 1
            destination_cursor = getattr(stored, "cursor", None)
            if destination_cursor is None:
                destination_cursor = getattr(event, "cursor", None)
            source_cursor = data.get("source_cursor")
            if source_cursor is not None and destination_cursor is not None:
                cursor_map[str(int(source_cursor))] = int(destination_cursor)

    if source_namespace is not None:
        store.record_namespace(
            source_namespace,
            imported_at=imported_at,
            event_count=events_appended,
            cursor_map=cursor_map,
            source_contract_version=(
                source_contract
                if isinstance(source_contract, int) and not isinstance(source_contract, bool)
                else store.contract_version()
            ),
        )

    return {
        "source_namespace": source_namespace,
        "target_namespace": store.cursor_namespace(),
        "records": record_counts,
        "events": events_appended,
        "artifacts": artifacts_stored,
        "artifacts_skipped": artifacts_skipped,
        "bindings_skipped": bindings_skipped,
        "skipped_existing": skipped_existing,
        "imported_at": imported_at,
    }


# ---------------------------------------------------------------------------
# Quiescence
# ---------------------------------------------------------------------------


def quiescence_blockers(store, workspace_id) -> dict:
    """What would block migrating `workspace_id` out of `store`, or `{}`-ish.

    Accepts a `TicketSink` or a `CoordinationStore`. Returns
    ``{"active_attempt_ids", "active_claim_paths", "pending_intent_ids",
    "pending_operations"}``; every list is empty when the workspace is quiescent.
    An uninitialised store is trivially quiescent and the check creates nothing.

    Active work is read in one read transaction (active `work_attempt`,
    `file_claim`, and `operation_intent` in `INTENT_ACTIVE_STATES`), and leftover
    file-sink journals come from ``store.recover_pending``: a report whose state
    is not clean (`pending`, `unknown`, `drifted`) blocks.
    """
    store = _store_of(store)
    blockers = {
        "active_attempt_ids": [],
        "active_claim_paths": [],
        "pending_intent_ids": [],
        "pending_operations": [],
    }
    if not store.is_initialised():
        return blockers

    with store.transaction(write=False) as tx:
        if workspace_id is None:
            attempts = [a for a in tx.find("work_attempt") if a.is_active]
            claims = [c for c in tx.find("file_claim") if c.is_active]
        else:
            attempts = [
                a for a in tx.find("work_attempt", workspace_id=workspace_id)
                if a.is_active
            ]
            claims = [
                c for c in tx.find("file_claim", workspace_id=workspace_id)
                if c.is_active
            ]
        intents = [
            i for i in tx.find("operation_intent")
            if i.state in coordination.INTENT_ACTIVE_STATES
            and (workspace_id is None or i.workspace_id == workspace_id)
        ]

    blockers["active_attempt_ids"] = sorted(a.id for a in attempts)
    blockers["active_claim_paths"] = sorted({c.path for c in claims})
    blockers["pending_intent_ids"] = sorted(i.id for i in intents)

    pending_operations = []
    for report in store.recover_pending(workspace_id or ""):
        if report.state in BLOCKING_RECOVERY_STATES:
            pending_operations.append(
                {"operation_id": report.operation_id, "state": report.state}
            )
    deduped = {}
    for operation in pending_operations:
        deduped[(operation["operation_id"], operation["state"])] = operation
    blockers["pending_operations"] = [
        deduped[key] for key in sorted(deduped)
    ]
    return blockers


def require_quiescent_store(store, workspace_id) -> None:
    """Raise `CoordinationConflict` unless `workspace_id` has no active work.

    Naming the blocking attempts/claims/intents/journals is the point: a caller
    must be able to see *why* a migration was refused rather than guess. Inspects
    nothing else and writes nothing.
    """
    blockers = quiescence_blockers(store, workspace_id)
    if not any(blockers.values()):
        return
    pieces = []
    if blockers["active_attempt_ids"]:
        pieces.append("active attempt(s): " + ", ".join(blockers["active_attempt_ids"]))
    if blockers["active_claim_paths"]:
        pieces.append("active file claim(s): " + ", ".join(blockers["active_claim_paths"]))
    if blockers["pending_intent_ids"]:
        pieces.append("pending intent(s): " + ", ".join(blockers["pending_intent_ids"]))
    if blockers["pending_operations"]:
        pieces.append(
            "unreconciled operation(s): "
            + ", ".join(
                f"{op['operation_id']} ({op['state']})"
                for op in blockers["pending_operations"]
            )
        )
    raise CoordinationConflict(
        f"refusing to migrate workspace {workspace_id!r}: it is not quiescent "
        f"({'; '.join(pieces)}); end the work and reconcile it first",
        details={"workspace_id": workspace_id, **blockers},
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def _workspace_ids(store) -> list:
    if not store.is_initialised():
        return []
    with store.transaction(write=False) as tx:
        return sorted(w.id for w in tx.find("workspace"))


def _history_fingerprint(bundle) -> str:
    """A cursor-free digest of a bundle's *history*.

    Hashes a deterministic, sorted projection of attempt ids/state/generation,
    claim ids/path/generation/state, receipt ids + sorted artifact_refs, and event
    ids/kinds/operation_ids/subject_ids. Cursors, ``source_cursor``, per-store
    namespaces and revisions are deliberately excluded, so a source and its
    re-exported destination compare equal even though every destination cursor is
    freshly assigned.

    ``workspace_bound`` events are excluded: they record where a workspace *is*
    bound, which is per-store administrative state rather than migrated history.
    A destination gains its own bind event when the caller rebinds it after a
    successful migration, so including them would make re-running an
    already-migrated destination look like history drift even though every
    attempt/claim/receipt/operation event was preserved.
    """
    records = bundle.get("records") if isinstance(bundle, dict) else {}
    if not isinstance(records, dict):
        records = {}

    def group(name):
        items = records.get(name)
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []

    attempts = sorted(
        (str(r.get("id")), str(r.get("state")), str(r.get("generation")))
        for r in group("work_attempts")
    )
    claims = sorted(
        (
            str(r.get("id")),
            str(r.get("path")),
            str(r.get("generation")),
            str(r.get("state")),
        )
        for r in group("file_claims")
    )
    receipts = sorted(
        (
            str(r.get("id")),
            tuple(sorted(str(ref) for ref in (r.get("artifact_refs") or []))),
        )
        for r in group("operation_receipts")
    )
    events = sorted(
        (
            str(e.get("id")),
            str(e.get("event_kind")),
            str(e.get("operation_id")),
            tuple(sorted(str(s) for s in (e.get("subject_ids") or []))),
        )
        for e in (bundle.get("events") or [])
        if isinstance(e, dict) and e.get("event_kind") != "workspace_bound"
    )
    projection = {
        "attempts": attempts,
        "claims": claims,
        "receipts": receipts,
        "events": events,
    }
    blob = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def migrate_coordination(source_sink, target_sink, *, workspace_id: Optional[str] = None,
                         overwrite: bool = False) -> dict:
    """Move one workspace's history from `source_sink` to `target_sink`.

    Steps, in order: resolve the workspace (the argument, else the single
    workspace in the source), refuse unless the source is quiescent, export,
    verify the bundle, import it into the target, then **verify the destination**
    by re-exporting and comparing cursor-free history fingerprints. Nothing in
    the target is touched before the source bundle verifies, and this function
    never writes a binding marker and never calls `workspace.ensure_binding` --
    the caller rebinds after a successful return.

    Returns the import counts plus ``verified``, ``source_namespace``,
    ``target_namespace`` and ``source_fingerprint``.
    """
    source_store = _store_of(source_sink)
    # Resolve the workspace first (also validates that the argument, if given,
    # names something real).
    ids = _workspace_ids(source_store)
    if workspace_id is None:
        if not ids:
            raise InvalidRecord(
                "no coordination workspace exists in the source store, so there "
                "is nothing to migrate",
                details={"source_namespace": source_store.cursor_namespace()},
            )
        if len(ids) > 1:
            raise CoordinationConflict(
                "the source store holds more than one workspace; pass "
                "workspace_id to choose one",
                details={"workspace_ids": ids},
            )
        workspace_id = ids[0]
    elif workspace_id not in ids:
        raise CoordinationConflict(
            f"workspace {workspace_id!r} does not exist in the source store",
            details={"workspace_id": workspace_id, "workspace_ids": ids},
        )

    require_quiescent_store(source_store, workspace_id)

    bundle = export_coordination(source_sink, workspace_id=workspace_id)
    problems = bundle_problems(bundle)
    if problems:
        raise CoordinationConflict(
            f"refusing to migrate workspace {workspace_id!r}: its exported bundle "
            "fails verification, so nothing was written to the destination",
            details={
                "workspace_id": workspace_id,
                "problems": [p.to_dict() for p in problems],
            },
        )

    result = import_coordination(
        target_sink, bundle, verify=False, overwrite=overwrite
    )

    recheck = export_coordination(target_sink, workspace_id=workspace_id)
    recheck_problems = bundle_problems(recheck)
    if recheck_problems:
        raise CoordinationConflict(
            f"migration of workspace {workspace_id!r} produced an invalid "
            "destination bundle; the destination must be inspected",
            details={
                "workspace_id": workspace_id,
                "problems": [p.to_dict() for p in recheck_problems],
            },
        )

    source_fingerprint = _history_fingerprint(bundle)
    target_fingerprint = _history_fingerprint(recheck)
    if source_fingerprint != target_fingerprint:
        raise CoordinationConflict(
            f"migration of workspace {workspace_id!r} did not preserve the "
            "history fingerprint; refusing to rebind",
            details={
                "workspace_id": workspace_id,
                "source_fingerprint": source_fingerprint,
                "target_fingerprint": target_fingerprint,
            },
        )

    result.update(
        {
            "verified": True,
            "source_namespace": bundle.get("cursor_namespace"),
            "target_namespace": recheck.get("cursor_namespace"),
            "source_fingerprint": source_fingerprint,
        }
    )
    return result


__all__ = [
    "EXPORT_VERSION",
    "RECORD_GROUPS",
    "GROUP_FOR_KIND",
    "BUNDLE_KEYS",
    "export_coordination",
    "bundle_problems",
    "bundle_counts",
    "write_bundle",
    "read_bundle",
    "import_coordination",
    "migrate_coordination",
    "quiescence_blockers",
    "require_quiescent_store",
]
