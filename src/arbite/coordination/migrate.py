"""Moving coordination state between stores, and what makes a switch unsafe.

`arbite migrate` copies *tickets* between sinks. This is the other half of that switch: a
project whose tickets moved to SQLite must not leave its claims, attempts, receipts and events
behind, because those records are what the proxy enforces with -- a claim file in an ignored
directory nobody consults is not ownership, it is a stale note. So the copy carries the
coordination state too, and this module is the whole of it.

Three properties shape everything here.

- **It is a copy, and it is exact.** Both backends store the same documents (one serialisation,
  `Record.to_dict()`, was kept for this), so a transfer reads the source's own documents and
  writes them into the target unchanged -- ids, generations, actors, operation ids and event
  cursors included -- and *states* each record's write counter, so the revision a reader's token
  means travels with it. The one change a copy may make is the one it must: a document written by
  an older arbite is refused by `parse_record` rather than silently misread, and is brought
  forward mechanically (`records.upgrade_document`) instead of being dropped.
- **It refuses before it writes anything.** A live claim or a live attempt belongs to a process
  working right now, and moving the store under it would leave the work in one store and the
  ownership in another; a destination already holding work of its own would have that work
  replaced. Both are refused with the holders named. Evidence that cannot be carried -- a version
  over the size limit, or bytes already gone -- is refused up front for the same reason: the copy
  must not produce a receipt whose evidence is missing.
- **The records commit as one unit.** Every record and event is written inside one
  `store.transaction()`, so each backend's own durability rule applies to the switch as a whole
  (a journal the next write replays, or a rolled-back SQL transaction). The artifact *bytes*
  cannot join that commit, because storing content is a separate operation on both backends, so
  they are written first and are idempotent by digest: a transfer that fails after them leaves
  bytes nobody names yet, which the retry reuses.

Nothing here prunes. There is no retention policy for evidence yet, and a switch that deleted the
source's records would be exactly the unrecoverable half of a copy. What grows is documented
instead, by `evidence.ChangeViews.summary` -- the receipt summary a devlog is generated from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..errors import CoordinationError, EvidenceRefused, RecordError
from . import records as record_module
from .records import RECORD_TYPES, OperationReceipt, Record
from .results import (
    BUSY,
    OUTCOME_LABELS,
    OperationResult,
    Outcome,
    next_actions_for,
    register_next_actions,
)
from .scratch import human_size
from .store import SIZE_LIMIT_HINT, CoordinationStore

#: The record types a transfer writes, in order: the workspace binding first, because every
#: other record names the workspace it belongs to.
COPY_ORDER = ("workspace", "attempt", "observation", "claim", "artifact", "receipt", "event")

#: Why a switch cannot happen. One reason per *response*, because what the caller has to do
#: differs: end the live work, empty the destination, or deal with the evidence.
ACTIVE_WORK = "coordination_active_work"
TARGET_WORK = "coordination_target_work"
EVIDENCE_UNCARRIED = "coordination_evidence_uncarried"

#: The steps that follow a refusal. Nothing here waits or retries: what stops a switch is work
#: a person or an agent has to finish in the store the work is in.
register_next_actions(
    ACTIVE_WORK,
    [
        "'arbite doctor' to see what is live, then finish it (or 'arbite doctor --fix' to "
        "release what nobody can use)",
        "re-run the migration once nothing is active",
    ],
)
register_next_actions(
    TARGET_WORK,
    [
        "'arbite doctor --sink <kind>' on the destination to see what it holds",
        "pass --overwrite to replace the destination's coordination records with the source's",
    ],
)
register_next_actions(EVIDENCE_UNCARRIED, [SIZE_LIMIT_HINT])


def store_present(store: CoordinationStore) -> bool:
    """Whether the store's own storage exists at all.

    A store that was never created is ordinary rather than broken -- a project that has always
    used one sink has no coordination state in the other -- and the difference matters because
    reading a SQLite store whose database file is missing is an error, while treating it as empty
    is the truth. The root *is* the storage on both backends: a directory for the file sink, the
    database file for SQLite."""
    return Path(store.root).exists()


def raw_counts(store: CoordinationStore) -> dict:
    """How many documents of each type a store holds, without interpreting any of them.

    Deliberately not a validating read: this answers "is there anything in the way", and a store
    whose documents were written by an older arbite is exactly the store a migration exists for.
    A store that is not there holds none."""
    if not store_present(store):
        return {record_type: 0 for record_type in RECORD_TYPES}
    return {record_type: len(store.raw_documents(record_type)) for record_type in RECORD_TYPES}


@dataclass(frozen=True)
class LoadedRecord:
    """One record as a copy needs it: the parsed value, the counter to keep, and whether the
    document on disk was written by an older arbite."""

    record: Record
    revision: int
    upgraded: bool

    @property
    def id(self) -> str:
        return self.record.id


def load_records(store: CoordinationStore, record_type: str) -> list:
    """Every record of `record_type`, brought forward to this build's schema revision.

    Read from the store's own documents (`raw_documents`) rather than through `records()`: an
    older document is refused by the validating read, and this is the copy that brings it forward
    -- mechanically, and never from fields this build does not understand. Each record keeps the
    write counter it had, which is what makes the copy exact."""
    loaded = []
    for record_id, document in store.raw_documents(record_type):
        loaded.append(
            LoadedRecord(
                record=record_module.read_forward(document),
                revision=store.revision(record_type, record_id),
                upgraded=(
                    record_module.document_revision(document)
                    != record_module.COORDINATION_SCHEMA_REVISION
                ),
            )
        )
    return loaded


@dataclass(frozen=True)
class Blocker:
    """One reason a switch is refused, with the kind a caller branches on."""

    kind: str
    detail: str


@dataclass(frozen=True)
class TransferPlan:
    """What a switch would carry, and what stops it.

    `documents` is the source's own document counts, `upgradable` how many of them were written
    by an older arbite, `pending` how many unfinished operations would travel with their
    receipts, and `target_records` how many the destination would have forgotten (all of them
    under `--overwrite`, its non-binding records otherwise)."""

    source_kind: str
    target_kind: str
    documents: dict = field(default_factory=dict)
    blockers: tuple = ()
    upgradable: int = 0
    pending: int = 0
    artifacts: int = 0
    artifact_bytes: int = 0
    target_records: int = 0

    @property
    def is_blocked(self) -> bool:
        return bool(self.blockers)

    @property
    def total(self) -> int:
        return sum(self.documents.values())

    @property
    def has_state(self) -> bool:
        return self.total > 0

    def to_dict(self) -> dict:
        return {
            "source": self.source_kind,
            "target": self.target_kind,
            "records": dict(self.documents),
            "total": self.total,
            "upgradable": self.upgradable,
            "pending_operations": self.pending,
            "artifacts": self.artifacts,
            "artifact_bytes": self.artifact_bytes,
            "target_records": self.target_records,
            "blockers": [
                {"kind": blocker.kind, "detail": blocker.detail} for blocker in self.blockers
            ],
        }


def plan(
    source: CoordinationStore, target: CoordinationStore, overwrite: bool = False
) -> TransferPlan:
    """What copying `source`'s coordination state into `target` would do, and what stops it.

    Reads both stores and writes nothing, which is what makes it both the `--dry-run` report and
    the check a real run performs *before* it copies a single ticket: a switch that cannot happen
    must fail before anything changes."""
    documents = raw_counts(source)
    blockers = list(_active_work_blockers(source, where=f"the {source.kind} store"))
    blockers += _active_work_blockers(target, where=f"the {target.kind} store")
    if overwrite:
        target_records = sum(raw_counts(target).values())
    else:
        # The destination's *work* is what a copy may not sit beside; its workspace binding is
        # replaced rather than counted, because a store belongs to exactly one workspace (which
        # is why the copy forgets the binding that is not the source's).
        target_records = sum(
            count for record_type, count in raw_counts(target).items()
            if record_type != "workspace"
        )
        if target_records:
            blockers.append(
                Blocker(
                    TARGET_WORK,
                    f"the {target.kind} store already holds {target_records} coordination "
                    "record(s) of its own, which the source's records would sit beside -- pass "
                    "--overwrite to replace them with the source's",
                )
            )
    artifacts = load_records(source, "artifact") if documents["artifact"] else []
    blockers += _evidence_blockers(source, target, artifacts)
    pending = (
        sum(1 for entry in load_records(source, "receipt") if entry.record.is_pending)
        if documents["receipt"]
        else 0
    )
    upgradable = 0
    if any(documents.values()):
        upgradable = sum(
            1
            for record_type in COPY_ORDER
            for _, document in source.raw_documents(record_type)
            if record_module.document_revision(document)
            != record_module.COORDINATION_SCHEMA_REVISION
        )
    return TransferPlan(
        source_kind=source.kind,
        target_kind=target.kind,
        documents=documents,
        blockers=tuple(blockers),
        upgradable=upgradable,
        pending=pending,
        artifacts=len(artifacts),
        artifact_bytes=sum(entry.record.size for entry in artifacts),
        target_records=target_records,
    )


def _active_work_blockers(store: CoordinationStore, where: str) -> list:
    """The live work a switch may not move or replace, in the order a reader acts on it.

    A live claim and a live attempt are both *durable* ownership: a process is working under them
    right now, and moving the store would leave the work in one store and the ownership in
    another. Nothing here infers staleness from a timestamp -- a stopped worker is never guessed
    at -- so "active" means exactly what the records say.

    Read through `load_records`, like the copy itself: a store written by an older arbite is
    exactly the store a migration exists for, so the *plan* must be able to look at it too. A
    document that cannot be brought forward still fails loudly, naming itself."""
    if not store_present(store):
        return []
    blockers = [
        Blocker(
            ACTIVE_WORK,
            f"{where} holds an active claim on {claim.path} (ticket {claim.ticket_id}, attempt "
            f"{claim.attempt_id}, generation {claim.generation})",
        )
        for claim in sorted(
            (entry.record for entry in load_records(store, "claim") if entry.record.is_active),
            key=lambda claim: claim.path,
        )
    ]
    blockers += [
        Blocker(
            ACTIVE_WORK,
            f"{where} holds an active attempt {attempt.id} on {attempt.ticket_id} "
            f"({attempt.worker_id})",
        )
        for attempt in sorted(
            (entry.record for entry in load_records(store, "attempt") if entry.record.is_active),
            key=lambda attempt: attempt.id,
        )
    ]
    return blockers


def _artifact_limit() -> int:
    """The size one stored version may have, read where it is defined.

    Looked up rather than copied at import, because the limit *is* `store`'s own constant: a
    module that copied its value would keep answering with the old number after it changed,
    and the answer must come from the one rule both backends enforce."""
    from . import store as store_module

    return store_module.MAX_ARTIFACT_BYTES


def _evidence_blockers(source: CoordinationStore, target: CoordinationStore, artifacts: list) -> list:
    """Evidence the copy could not carry, checked before anything is copied.

    Two cases, and either would end with a target receipt whose bytes are missing, which is the
    one thing a store must not be able to say: a version over `MAX_ARTIFACT_BYTES`, which this
    proxy refuses to *store* (the same rule on both sinks, so the answer cannot depend on where
    the bytes would go), and bytes already gone from the source. Reading them here is also what
    lets the transfer's own read succeed, since a migration copies every version it knows of."""
    limit = _artifact_limit()
    blockers = []
    for entry in artifacts:
        artifact = entry.record
        if artifact.size > limit:
            blockers.append(
                Blocker(
                    EVIDENCE_UNCARRIED,
                    f"the evidence for {artifact.digest} is {artifact.size} bytes, over the "
                    f"{limit}-byte limit one stored version may have, so the "
                    f"{target.kind} store could not hold it and the source's receipts would "
                    "travel without their bytes",
                )
            )
            continue
        try:
            source.get_artifact_bytes(artifact.digest)
        except (CoordinationError, NotImplementedError) as e:
            blockers.append(
                Blocker(
                    EVIDENCE_UNCARRIED,
                    f"the evidence for {artifact.digest} is not readable in the "
                    f"{source.kind} store ({e})",
                )
            )
    return blockers


def refusal_lines(plan_result: TransferPlan) -> list:
    """The refusal as text: what is in the way, and that nothing was copied.

    The first line carries the outcome's own word (`busy:`), the same way every other
    coordination refusal does, so the printed stream and the exit code say the same thing."""
    lines = [
        f"{OUTCOME_LABELS[BUSY]}: coordination state cannot move from the "
        f"{plan_result.source_kind} store to the {plan_result.target_kind} store:",
    ]
    lines += [f"  {blocker.kind}: {blocker.detail}" for blocker in plan_result.blockers]
    lines.append("no coordination records were copied and no tickets were moved")
    return lines


def refused(plan_result: TransferPlan) -> OperationResult:
    """The refusal as outcome 4 -- busy, because a live holder or a busy destination is what
    stopped it, and nothing changed."""
    kinds = {blocker.kind for blocker in plan_result.blockers}
    for reason in (ACTIVE_WORK, EVIDENCE_UNCARRIED, TARGET_WORK):
        if reason in kinds:
            return OperationResult(
                Outcome(BUSY, reason),
                refusal_lines(plan_result),
                plan_result.to_dict(),
                next_actions_for(BUSY, reason),
            )
    raise CoordinationError("refused() needs a plan that is actually blocked")


@dataclass(frozen=True)
class TransferReport:
    """What a transfer carried, in the terms the migration report prints."""

    source_kind: str
    target_kind: str
    documents: dict = field(default_factory=dict)
    upgraded: int = 0
    pending: int = 0
    artifacts: int = 0
    artifact_bytes: int = 0
    replaced: int = 0
    workspace: Optional[str] = None

    @property
    def total(self) -> int:
        return sum(self.documents.values())

    def to_dict(self) -> dict:
        return {
            "source": self.source_kind,
            "target": self.target_kind,
            "records": dict(self.documents),
            "total": self.total,
            "upgraded": self.upgraded,
            "pending_operations": self.pending,
            "artifacts": self.artifacts,
            "artifact_bytes": self.artifact_bytes,
            "replaced": self.replaced,
            "workspace": self.workspace,
        }


def transfer(
    source: CoordinationStore, target: CoordinationStore, overwrite: bool = False
) -> TransferReport:
    """Copy every coordination record from `source` into `target`, carrying the evidence.

    Refuses when `plan` says the switch is unsafe (nothing is written), then writes every version
    a receipt names and commits every record and event as one unit of work, together with the
    deletions `--overwrite` asks for. The source is left exactly as it was: this is a copy, so a
    migration that turns out to be the wrong choice can be reversed by migrating back."""
    checked = plan(source, target, overwrite=overwrite)
    if checked.is_blocked:
        raise CoordinationError("\n".join(refusal_lines(checked)))

    target.init()
    loaded = {record_type: load_records(source, record_type) for record_type in COPY_ORDER}
    versions, size = _carry_evidence(source, target, loaded["artifact"])
    kept = {entry.id for entry in loaded["workspace"]}
    with target.transaction() as txn:
        replaced = _forget(target, txn, loaded, overwrite=overwrite, keep_bindings=kept)
        for record_type in COPY_ORDER:
            for entry in loaded[record_type]:
                txn.put_record(entry.record, revision=entry.revision)
    return TransferReport(
        source_kind=source.kind,
        target_kind=target.kind,
        documents={record_type: len(entries) for record_type, entries in loaded.items()},
        upgraded=sum(1 for entries in loaded.values() for entry in entries if entry.upgraded),
        pending=sum(
            1
            for entry in loaded["receipt"]
            if isinstance(entry.record, OperationReceipt) and entry.record.is_pending
        ),
        artifacts=versions,
        artifact_bytes=size,
        replaced=replaced,
        workspace=loaded["workspace"][0].id if loaded["workspace"] else None,
    )


def _carry_evidence(source: CoordinationStore, target: CoordinationStore, artifacts: list) -> tuple:
    """Write every stored version into the target, and return `(count, bytes)`.

    Content is stored once by digest on both backends, so this is idempotent: a version the
    target already holds is left as it is, which is what makes a failed transfer safe to re-run.
    The size rule is checked again by the backend itself (`store.require_storable_artifact`), and
    a refusal raises here, before any record is written."""
    versions = 0
    size = 0
    for entry in artifacts:
        digest = entry.record.digest
        try:
            data = source.get_artifact_bytes(digest)
        except CoordinationError as e:
            raise EvidenceRefused(
                f"the evidence for {digest} cannot be read from the {source.kind} store ({e}), "
                "so its receipts would arrive without their bytes",
                [SIZE_LIMIT_HINT],
                text_hint=f"next: {SIZE_LIMIT_HINT}",
            ) from None
        target.put_artifact_bytes(digest, data)
        versions += 1
        size += len(data)
    return versions, size


def _forget(target: CoordinationStore, txn, loaded: dict, overwrite: bool, keep_bindings) -> int:
    """Buffer the deletions the copy needs, and return how many records they cover.

    `--overwrite` forgets everything the destination holds, which is what makes the target's
    state the source's and nothing else. Without it, the only record forgotten is a workspace
    binding the source does not carry: a store belongs to exactly one workspace, so leaving
    beside the copy's binding would be the state `get_workspace` has to refuse. Read from the
    destination's own documents rather than through `records()`, because a destination whose
    documents were written by an older arbite must still be replaceable."""
    forgotten = 0
    for record_type in COPY_ORDER:
        for record_id, _ in target.raw_documents(record_type):
            if not overwrite and not (
                record_type == "workspace" and record_id not in keep_bindings
            ):
                continue
            txn.delete_record(record_type, record_id)
            forgotten += 1
    return forgotten


def summary_lines(report: TransferReport) -> list:
    """The lines `arbite migrate` prints about the coordination half of a switch.

    Written from what was actually stored, so a reader can check the counts against
    `arbite workspace show` afterwards."""
    if report.total == 0:
        return [f"coordination: nothing to move (the {report.source_kind} store holds none)"]
    shape = ", ".join(
        f"{count} {record_type}{'' if count == 1 else 's'}"
        for record_type, count in report.documents.items()
        if count
    )
    lines = [
        f"coordination: copied {report.total} record(s) from the {report.source_kind} store "
        f"into the {report.target_kind} store ({shape})",
    ]
    if report.workspace:
        lines.append(
            f"coordination: the workspace binding {report.workspace} travelled with the records, "
            "so claims and attempts still name the workspace they were created in"
        )
    if report.upgraded:
        lines.append(
            f"coordination: {report.upgraded} record(s) written by an older arbite were upgraded "
            "to this build's schema revision"
        )
    if report.pending:
        lines.append(
            f"coordination: {report.pending} pending operation(s) travelled with their receipts "
            "-- 'arbite doctor' judges them against the bytes on disk"
        )
    if report.replaced:
        lines.append(
            f"coordination: replaced {report.replaced} record(s) the destination held "
            "(--overwrite)"
        )
    if report.artifacts:
        lines.append(
            f"evidence: {report.artifacts} version(s) ({human_size(report.artifact_bytes)}) "
            "carried; arbite never prunes evidence, so the store grows with the work"
        )
    return lines


def plan_lines(plan_result: TransferPlan) -> list:
    """What `--dry-run` prints about the coordination half, having changed nothing."""
    if not plan_result.has_state:
        return [f"coordination: nothing to move (the {plan_result.source_kind} store holds none)"]
    lines = [
        f"coordination: would copy {plan_result.total} record(s) from the "
        f"{plan_result.source_kind} store into the {plan_result.target_kind} store"
        + (
            f", replacing {plan_result.target_records} record(s) it holds"
            if plan_result.target_records
            else ""
        )
    ]
    if plan_result.upgradable:
        lines.append(
            f"coordination: {plan_result.upgradable} record(s) written by an older arbite would "
            "be upgraded to this build's schema revision"
        )
    if plan_result.artifacts:
        lines.append(
            f"evidence: would carry {plan_result.artifacts} version(s) "
            f"({human_size(plan_result.artifact_bytes)})"
        )
    return lines
