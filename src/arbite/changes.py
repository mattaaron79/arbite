"""One-shot, bounded, read-only change/evidence queries (planning key C10).

This is the *query* half of "capture mutation evidence automatically". Up to C09
the shared-directory layer records durable evidence -- an `OperationReceipt` per
operation, content-addressed `Artifact`s, `drift_detected` events, read
observations -- but nothing turns that evidence back into a question an agent or
a human can ask: *what changed for this ticket, and can I verify it?*

The module answers exactly that, and nothing else:

- It is **storage-neutral**. It reads through the `CoordinationStore` surface
  (`transaction(write=False)`, `find`, `get`, `event_log`, `has_artifact`,
  `read_artifact_bytes`) and never imports SQLite or reaches into a sink. The
  file sink and the SQLite sink answer identically, and the file sink never needs
  SQLite to do it.
- It is **read-only and one-shot**. It opens short read-only transactions,
  returns, and never writes, schedules, retries or scans.
- It is **bounded**. Ticket and attempt views honour `limit`/`offset`, report an
  explicit truncation marker and a `next_offset`, and cap an oversized limit
  loudly rather than silently.

The payload keeps three things apart, because collapsing them is what the plan
warns against:

1. ``evidence`` -- the *mechanical* record: the file-change operations
   (write/edit/remove/rename) folded in deterministic order, each with
   before/after digests, artifact references and a per-artifact *verifiable* flag
   (resolved by reading the bytes back and hashing them; `ArtifactCorrupt`/absent
   content is reported, never trusted). Claim/release/lifecycle receipts are
   bookkeeping with no bytes behind them, so they are counted separately
   (`counts["other_receipts"]`) rather than listed as changes.
2. ``summaries`` -- the *agent-authored* ticket notes (`schema.parse_notes`).
   Clearly labelled prose, and explicitly **not** an input to the net change.
3. ``unattributed`` -- external drift (`drift_detected`) and operations recorded
   under a *different* attempt than the one being viewed. Labelled
   observed/unattributed, never attributed to the current agent.

Read observations are a separate stream on purpose: they are evidence that bytes
were served, not that anything changed, so they never appear in the ordinary view
and are only populated when the caller asks (`include_reads=True`).

The net change view is a *fold*, not a replacement: for each touched path the
first `before` and the latest `after` are compared, so ``edit-then-revert``
reports ``reverted`` while every receipt that produced it stays in the ordered
history. Renames contribute two paths (source ``digest -> ABSENT``, destination
``ABSENT -> digest``); creates and removes are the same one-path shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import coordination, schema
from .coordination import ABSENT, artifact_id_for_digest, is_digest, utc_now
from .errors import (
    ArtifactCorrupt,
    CoordinationNotFound,
    InvalidRecord,
    UnsupportedCoordination,
)

# ---------------------------------------------------------------------------
# Bounds. Every one of these is reported in the result when it bites, so a caller
# never has to guess whether output was complete.
# ---------------------------------------------------------------------------

#: Operations/read observations returned when the caller states no limit.
DEFAULT_LIMIT = 100
#: Hard ceiling on either stream; an oversized request is capped and marked.
MAX_LIMIT = 2000

#: Markers a caller can branch on instead of parsing prose.
MARKER_LIMIT_CAPPED = "limit_capped"
MARKER_OFFSET_ADVANCED = "offset_advanced"
MARKER_OUTPUT_TRUNCATED = "output_truncated"
MARKER_READS_EXCLUDED = "read_observations_excluded"
MARKER_UNATTRIBUTED = "unattributed_observed"

#: The attribution label applied to drift and to another attempt's operations.
ATTRIBUTION_OBSERVED = "observed/unattributed"
#: The one sentence that says what an agent-authored summary is (and is not).
PROSE_LABEL = (
    "agent-authored prose from the ticket's notes; never used to derive the "
    "mechanical net change"
)

#: Change classification for the net view.
CHANGE_CREATED = "created"
CHANGE_MODIFIED = "modified"
CHANGE_REMOVED = "removed"
CHANGE_REVERTED = "reverted"
CHANGE_UNCHANGED = "unchanged"


def _bound_limit(value: Optional[int], default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT):
    """`(limit, capped)`; refuses a nonsensical value, caps an oversized one."""
    if value is None:
        return default, False
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedCoordination(f"limit must be an integer, got {value!r}")
    if value < 1:
        raise UnsupportedCoordination(f"limit must be >= 1, got {value!r}")
    if value > maximum:
        return maximum, True
    return value, False


def _bound_offset(value: Optional[int]) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedCoordination(f"offset must be an integer, got {value!r}")
    if value < 0:
        raise UnsupportedCoordination(f"offset must be >= 0, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Artifact verification
# ---------------------------------------------------------------------------


def _artifact_status(store, digest: Optional[str]) -> Dict[str, Any]:
    """Whether `digest`'s content is stored *and verifies*, as a JSON dict.

    Verification is the whole point: a receipt naming a digest proves nothing if
    the bytes behind it are missing or corrupted. So this reads the content back
    through the store (which re-hashes it) and reports `verifiable` honestly.
    ``ArtifactCorrupt``, absent content and a store without artifact storage are
    all reported as *not* verifiable with a reason -- never trusted by omission.
    """
    if digest is None or digest == ABSENT or not is_digest(digest):
        return {
            "digest": digest,
            "artifact_id": None,
            "stored": False,
            "verifiable": False,
            "size": None,
            "reason": "absent" if digest in (None, ABSENT) else "not_a_digest",
        }
    artifact_id = artifact_id_for_digest(digest)
    try:
        stored = bool(store.has_artifact(digest))
    except UnsupportedCoordination:
        return {
            "digest": digest,
            "artifact_id": artifact_id,
            "stored": False,
            "verifiable": False,
            "size": None,
            "reason": "artifact_storage_unsupported",
        }
    if not stored:
        return {
            "digest": digest,
            "artifact_id": artifact_id,
            "stored": False,
            "verifiable": False,
            "size": None,
            "reason": "missing",
        }
    try:
        data = store.read_artifact_bytes(digest)
    except ArtifactCorrupt:
        return {
            "digest": digest,
            "artifact_id": artifact_id,
            "stored": True,
            "verifiable": False,
            "size": None,
            "reason": "artifact_corrupt",
        }
    except UnsupportedCoordination:
        return {
            "digest": digest,
            "artifact_id": artifact_id,
            "stored": True,
            "verifiable": False,
            "size": None,
            "reason": "artifact_storage_unsupported",
        }
    if data is None:
        return {
            "digest": digest,
            "artifact_id": artifact_id,
            "stored": False,
            "verifiable": False,
            "size": None,
            "reason": "missing",
        }
    # `read_artifact_bytes` already verified the content against the digest.
    return {
        "digest": digest,
        "artifact_id": artifact_id,
        "stored": True,
        "verifiable": True,
        "size": len(data),
        "reason": None,
    }


def _artifact_ref_status(store, records: Dict[str, Any], ref: str) -> Dict[str, Any]:
    """Verification status for one `artifact_refs` entry (an artifact *id*)."""
    record = records.get(ref)
    if record is None:
        return {
            "artifact_id": ref,
            "digest": None,
            "size": None,
            "available": None,
            "verifiable": False,
            "reason": "unknown_artifact_record",
        }
    status = _artifact_status(store, getattr(record, "digest", None))
    status.update(
        {
            "artifact_id": ref,
            "available": bool(getattr(record, "available", False)),
            "size": getattr(record, "size", status.get("size")),
        }
    )
    if not status.get("available", False) and status.get("verifiable"):
        status["verifiable"] = False
        status["reason"] = "unavailable"
    return status


# ---------------------------------------------------------------------------
# Ordering / folding
# ---------------------------------------------------------------------------


def _receipt_paths(receipt) -> List[str]:
    """Every path an operation touched, in receipt order.

    `paths` is authoritative; a legacy receipt with an empty `paths` still
    contributes the keys of its before/after maps, so a fold never silently drops
    a path it can see.
    """
    if receipt.paths:
        return list(receipt.paths)
    seen: List[str] = []
    for path in list(receipt.before) + list(receipt.after):
        if path not in seen:
            seen.append(path)
    return seen


def _version(mapping: Dict[str, str], path: str) -> str:
    return mapping.get(path, ABSENT)


def _order_key(receipt, cursors: Dict[str, Optional[int]]):
    """Deterministic operation order: timestamp, then event cursor, then id.

    Timestamps can tie (and, in a test or a fast run, collide), so a receipt that
    has an `operation_recorded` event is ordered by that event's monotonic cursor;
    an unattached receipt still sorts after cursor-bearing ones at the same
    instant, and the operation id breaks the final tie. This is why the fold does
    not rely on timestamps alone.
    """
    cursor = cursors.get(receipt.id)
    return (
        receipt.timestamp,
        0 if cursor is not None else 1,
        cursor if cursor is not None else 0,
        receipt.id,
    )


def _classify(before: str, after: str, reverted: bool) -> str:
    if before == after:
        return CHANGE_REVERTED if reverted else CHANGE_UNCHANGED
    if before == ABSENT:
        return CHANGE_CREATED
    if after == ABSENT:
        return CHANGE_REMOVED
    return CHANGE_MODIFIED


def _net_changes(store, receipts: List[Any]) -> List[Dict[str, Any]]:
    """Fold `receipts` (already in deterministic order) into a per-path net view.

    For each path: the FIRST `before` and the LATEST `after`, plus every operation
    id that touched it. A successful edit-then-revert therefore yields
    ``before == after`` -- classified `reverted` -- while both operations remain in
    `operations`, so the net view never erases history. Failed (`error`) receipts
    are excluded from the fold (their bytes were reverted) but still appear in the
    ordered evidence.
    """
    first_before: Dict[str, str] = {}
    last_after: Dict[str, str] = {}
    operations: Dict[str, List[str]] = {}
    for receipt in receipts:
        if receipt.result != "ok":
            continue
        for path in _receipt_paths(receipt):
            if path not in first_before:
                first_before[path] = _version(receipt.before, path)
            last_after[path] = _version(receipt.after, path)
            operations.setdefault(path, []).append(receipt.id)

    net: List[Dict[str, Any]] = []
    for path in sorted(first_before):
        before = first_before[path]
        after = last_after[path]
        ops = operations[path]
        reverted = before == after and len(ops) > 1
        net.append(
            {
                "path": path,
                "before": before,
                "after": after,
                "change": _classify(before, after, reverted),
                "changed": before != after,
                "reverted": reverted,
                "operations": list(ops),
                "operation_count": len(ops),
                "first_operation_id": ops[0],
                "last_operation_id": ops[-1],
                "before_artifact": _artifact_status(store, before),
                "after_artifact": _artifact_status(store, after),
            }
        )
    return net


def _operation_payload(
    store,
    artifact_records: Dict[str, Any],
    cursors: Dict[str, Optional[int]],
    receipt,
    *,
    attribution: str = "receipt",
    attribution_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """One ordered operation receipt: mechanical evidence plus verification."""
    paths = _receipt_paths(receipt)
    changes = [
        {
            "path": path,
            "before": _version(receipt.before, path),
            "after": _version(receipt.after, path),
            "before_artifact": _artifact_status(store, _version(receipt.before, path)),
            "after_artifact": _artifact_status(store, _version(receipt.after, path)),
        }
        for path in paths
    ]
    return {
        "operation_id": receipt.id,
        "operation_kind": receipt.operation_kind,
        "ticket_id": receipt.ticket_id,
        "attempt_id": receipt.attempt_id,
        "actor": receipt.actor,
        "timestamp": receipt.timestamp,
        "event_cursor": cursors.get(receipt.id),
        "claim_generation": receipt.claim_generation,
        "result": receipt.result,
        "error": receipt.error,
        "retry_of": receipt.retry_of,
        "paths": paths,
        "before": dict(receipt.before),
        "after": dict(receipt.after),
        "artifact_refs": list(receipt.artifact_refs),
        "changes": changes,
        "artifacts": [
            _artifact_ref_status(store, artifact_records, ref)
            for ref in receipt.artifact_refs
        ],
        "attribution": attribution,
        "attribution_reason": attribution_reason,
        # The raw mechanical record, so a consumer that needs a field this view
        # did not surface does not have to re-query the store.
        "receipt": receipt.to_dict(),
    }


def _drift_payload(event) -> Dict[str, Any]:
    """A `drift_detected` event as an observed/unattributed finding."""
    payload = dict(event.payload or {})
    return {
        "kind": "drift",
        "attribution": ATTRIBUTION_OBSERVED,
        "attribution_reason": (
            "an external/unattributed filesystem change was observed by recovery; "
            "it is deliberately NOT assigned to any agent"
        ),
        "operation_id": payload.get("operation_id"),
        "operation_kind": payload.get("operation_kind"),
        "paths": list(payload.get("paths") or []),
        "detail": payload.get("detail"),
        "timestamp": event.timestamp,
        "event_cursor": event.cursor,
        "subject_ids": list(event.subject_ids or []),
    }


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeView:
    """The materialised answer to one `changes` question.

    Fields mirror the JSON payload exactly, so a caller that wants structured
    access (a future C11/B06 extension, or a test) reads attributes and a caller
    that wants JSON calls `to_dict()`.
    """

    scope: str
    ticket_id: str
    attempt_id: Optional[str]
    generated_at: str
    operations: List[Dict[str, Any]] = field(default_factory=list)
    net_changes: List[Dict[str, Any]] = field(default_factory=list)
    touched_paths: List[str] = field(default_factory=list)
    summaries: Dict[str, Any] = field(default_factory=dict)
    unattributed: List[Dict[str, Any]] = field(default_factory=list)
    read_observations: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    bounds: Dict[str, Any] = field(default_factory=dict)
    markers: List[str] = field(default_factory=list)
    #: The ticket's live attempt, whatever `attempt_id` scoped the view to.
    active_attempt_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "ticket_id": self.ticket_id,
            "attempt_id": self.attempt_id,
            "active_attempt_id": self.active_attempt_id,
            "generated_at": self.generated_at,
            "view": "arbite.changes",
            "read_only": True,
            "evidence": {
                "operations": list(self.operations),
                "net_changes": list(self.net_changes),
                "touched_paths": list(self.touched_paths),
                "operation_count": len(self.operations),
            },
            "summaries": dict(self.summaries),
            "unattributed": list(self.unattributed),
            "read_observations": list(self.read_observations),
            "counts": dict(self.counts),
            "bounds": dict(self.bounds),
            "markers": list(self.markers),
        }


class ChangesQuery:
    """A bounded read-only view over one store's recorded change evidence.

    Construct with the sink's `CoordinationStore` (and, when summaries are wanted,
    the `schema.Ticket` so `schema.parse_notes` can read its prose). `view()` is
    the single entry point; it opens read-only transactions, builds the payload and
    returns -- it never writes and never holds a lock.
    """

    def __init__(self, store, *, ticket: Optional["schema.Ticket"] = None) -> None:
        self.store = store
        self.ticket = ticket

    # -- public API -------------------------------------------------------

    def view(
        self,
        ticket_id: str,
        *,
        attempt_id: Optional[str] = None,
        include_reads: bool = False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ChangeView:
        """The ticket view, or the per-attempt view when `attempt_id` is given.

        `include_reads` is the *only* way read observations appear: they are a
        separate evidence stream (bytes were served, nothing changed) and adding
        them to the ordinary view would drown the operations that did.
        """
        if self.ticket is not None and self.ticket.id != ticket_id:
            raise InvalidRecord(
                f"ChangesQuery was built for ticket {self.ticket.id!r} but asked "
                f"for {ticket_id!r}"
            )
        bounded_offset = _bound_offset(offset)
        bounded_limit, limit_capped = _bound_limit(limit)

        with self.store.transaction(write=False) as tx:
            receipts = list(tx.find("operation_receipt", ticket_id=ticket_id))
            attempts = list(tx.find("work_attempt", ticket_id=ticket_id))
            attempt = tx.get("work_attempt", attempt_id) if attempt_id else None
            observations = list(tx.find("read_observation"))
            artifact_records = {record.id: record for record in tx.find("artifact")}

        if attempt_id is not None:
            if attempt is None:
                raise CoordinationNotFound(
                    f"no work attempt {attempt_id!r} is recorded for ticket "
                    f"{ticket_id!r}",
                    details={"ticket_id": ticket_id, "attempt_id": attempt_id},
                )
            if attempt.ticket_id != ticket_id:
                raise CoordinationNotFound(
                    f"attempt {attempt_id!r} belongs to ticket {attempt.ticket_id!r}, "
                    f"not {ticket_id!r}",
                    details={
                        "ticket_id": ticket_id,
                        "attempt_id": attempt_id,
                        "attempt_ticket_id": attempt.ticket_id,
                    },
                )

        events = self.store.event_log()
        cursors = {
            event.operation_id: event.cursor
            for event in events
            if event.kind_ == "operation_recorded" and event.operation_id
        }
        drift_events = [
            event
            for event in events
            if event.kind_ == "drift_detected" and ticket_id in (event.subject_ids or [])
        ]

        # A *change* view lists change operations (the mutation kinds), not every
        # coordination receipt: claim/release/lifecycle receipts are bookkeeping
        # with no bytes behind them, so they are counted (`other_receipts`) rather
        # than drowning the evidence that explains a file-change manifest.
        change_receipts = [
            r for r in receipts if r.operation_kind in coordination.MUTATION_KINDS
        ]
        other_receipts = [
            r for r in receipts if r.operation_kind not in coordination.MUTATION_KINDS
        ]

        if attempt_id is not None:
            evidence_receipts = [r for r in change_receipts if r.attempt_id == attempt_id]
            foreign_receipts = [r for r in change_receipts if r.attempt_id != attempt_id]
        else:
            evidence_receipts = list(change_receipts)
            foreign_receipts = []

        ordered = sorted(evidence_receipts, key=lambda r: _order_key(r, cursors))
        total_operations = len(ordered)
        window = ordered[bounded_offset : bounded_offset + bounded_limit]
        operations_truncated = bounded_offset + len(window) < total_operations
        operations_next = (
            bounded_offset + len(window) if operations_truncated else None
        )

        net_changes = _net_changes(self.store, ordered)
        touched_paths = [entry["path"] for entry in net_changes]

        # Unattributed: external drift (always) plus, in an attempt view, the
        # operations recorded under a *different* attempt for the same ticket.
        unattributed: List[Dict[str, Any]] = [_drift_payload(e) for e in drift_events]
        for receipt in sorted(foreign_receipts, key=lambda r: _order_key(r, cursors)):
            unattributed.append(
                _operation_payload(
                    self.store,
                    artifact_records,
                    cursors,
                    receipt,
                    attribution=ATTRIBUTION_OBSERVED,
                    attribution_reason=(
                        f"recorded under attempt {receipt.attempt_id!r}, not the "
                        f"viewed attempt {attempt_id!r}"
                    ),
                )
            )

        # Read observations: separate stream, only materialised on request.
        attempt_ids = {attempt_id} if attempt_id is not None else {a.id for a in attempts}
        scoped_reads = sorted(
            (o for o in observations if o.attempt_id in attempt_ids),
            key=lambda o: (o.observed_at, o.id),
        )
        if include_reads:
            read_window = scoped_reads[bounded_offset : bounded_offset + bounded_limit]
            reads_truncated = bounded_offset + len(read_window) < len(scoped_reads)
            reads_next = bounded_offset + len(read_window) if reads_truncated else None
            read_observations = [o.to_dict() for o in read_window]
        else:
            read_window = []
            reads_truncated = False
            reads_next = None
            read_observations = []

        markers: List[str] = []
        if bounded_offset:
            markers.append(MARKER_OFFSET_ADVANCED)
        if limit_capped:
            markers.append(MARKER_LIMIT_CAPPED)
        if operations_truncated:
            markers.append(MARKER_OUTPUT_TRUNCATED)
        if not include_reads and scoped_reads:
            markers.append(MARKER_READS_EXCLUDED)
        if unattributed:
            markers.append(MARKER_UNATTRIBUTED)

        summaries = self._summaries()

        bounds = {
            "operations": {
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": total_operations,
                "returned": len(window),
                "truncated": operations_truncated,
                "next_offset": operations_next,
                "limit_capped": limit_capped,
            },
            "read_observations": {
                "included": include_reads,
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": len(scoped_reads),
                "returned": len(read_window),
                "truncated": reads_truncated,
                "next_offset": reads_next,
            },
        }

        counts = {
            "total_operations": total_operations,
            "returned_operations": len(window),
            "error_operations": sum(1 for r in ordered if r.result != "ok"),
            "other_receipts": len(other_receipts),
            "touched_paths": len(touched_paths),
            "net_changed_paths": sum(1 for n in net_changes if n["changed"]),
            "net_reverted_paths": sum(1 for n in net_changes if n["reverted"]),
            "total_read_observations": len(scoped_reads),
            "returned_read_observations": len(read_window),
            "unattributed": len(unattributed),
        }

        active = max((a for a in attempts if a.is_active), key=lambda a: a.generation, default=None)
        return ChangeView(
            active_attempt_id=active.id if active is not None else None,
            scope="attempt" if attempt_id is not None else "ticket",
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            generated_at=utc_now(),
            operations=[
                _operation_payload(self.store, artifact_records, cursors, receipt)
                for receipt in window
            ],
            net_changes=net_changes,
            touched_paths=touched_paths,
            summaries=summaries,
            unattributed=unattributed,
            read_observations=read_observations,
            counts=counts,
            bounds=bounds,
            markers=markers,
        )

    # -- summaries --------------------------------------------------------

    def _summaries(self) -> Dict[str, Any]:
        """The agent-authored ticket notes, labelled as prose, never as evidence."""
        if self.ticket is None:
            return {"source": None, "label": PROSE_LABEL, "notes": []}
        notes = [
            {
                "ordinal": note.ordinal,
                "date": note.date,
                "agent": note.agent,
                "message": note.message,
            }
            for note in schema.parse_notes(self.ticket.body)
        ]
        return {
            "source": "ticket_notes",
            "ticket_id": self.ticket.id,
            "label": PROSE_LABEL,
            "notes": notes,
        }


def change_view(
    store,
    ticket_id: str,
    *,
    ticket: Optional["schema.Ticket"] = None,
    attempt_id: Optional[str] = None,
    include_reads: bool = False,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> ChangeView:
    """One-shot convenience wrapper around `ChangesQuery.view`."""
    return ChangesQuery(store, ticket=ticket).view(
        ticket_id,
        attempt_id=attempt_id,
        include_reads=include_reads,
        limit=limit,
        offset=offset,
    )


__all__ = [
    "ATTRIBUTION_OBSERVED",
    "CHANGE_CREATED",
    "CHANGE_MODIFIED",
    "CHANGE_REMOVED",
    "CHANGE_REVERTED",
    "CHANGE_UNCHANGED",
    "ChangeView",
    "ChangesQuery",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MARKER_LIMIT_CAPPED",
    "MARKER_OFFSET_ADVANCED",
    "MARKER_OUTPUT_TRUNCATED",
    "MARKER_READS_EXCLUDED",
    "MARKER_UNATTRIBUTED",
    "PROSE_LABEL",
    "change_view",
]
