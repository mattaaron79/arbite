"""The recovery engine: what a staged operation left behind, and the one honest answer.

A file operation is not one write. It is an *intent* -- a receipt with its before and
after digests, the bytes kept as evidence -- then a staged copy of the new content
beside the target, then the filesystem change itself, then the finalisation of the
receipt. A process can die between any two of those, so this module decides, for every
operation left pending, which of three true statements applies:

- the recorded **after** version is on disk: the operation did happen, and the bytes are
  already right. Finalise the receipt as succeeded. Nothing is applied twice.
- the recorded **before** version is on disk: the operation did not happen. Finalise the
  receipt as failed and discard the staged copy. Nothing was changed.
- **neither**: the bytes are drift -- somebody else wrote the file, or wrote it
  differently. No version is "correct" without a human, so the bytes are left exactly
  where they are, the receipt stays pending, and the finding says so. The file backend
  additionally archives the observed bytes as an artifact; a backend that cannot store
  content leaves them in place, which is the strongest preservation there is.

Two rules shape everything here. **Nothing is guessed**: a half-applied operation is
never completed, reverted or "resolved", which is why the drift case is reported rather
than repaired. And **nothing is done on a schedule**: there is no daemon and no watcher,
so reconciliation happens when somebody asks -- `arbite doctor --fix`, `arbite doctor`
(report only, writing nothing) or the next file operation, which reconciles before it
stages anything of its own.

The findings render in a fixed order, because the report is meant to be acted on from
the top: a live claim nobody can use, then an unfinished mutation, then the records that
name something missing. `Problem`s are what `doctor` prints; `OperationJudgement`s are
what callers needing the detail work from (the journal's retry path, later receipt views).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from ..errors import PathRefused, RecordError, Stale
from ..sinks.base import Problem
from .paths import probe
from .records import (
    ABSENT,
    ATTEMPT_ACTIVE,
    CLAIM_ACTIVE,
    CLAIM_RELEASED,
    RECEIPT_FAILED,
    RECEIPT_SUCCEEDED,
    Artifact,
    FileClaim,
    OperationReceipt,
    WorkAttempt,
    new_id,
    utc_now,
)

# ---------------------------------------------------------------------------
# What a pending operation turned out to be
# ---------------------------------------------------------------------------

#: The operation's recorded after version is on disk: it happened.
COMPLETED = "completed"
#: The operation's recorded before version is on disk: it did not happen.
NOT_APPLIED = "not_applied"
#: Neither version is on disk: somebody else's bytes, and a decision for a human.
DRIFT = "drift"
#: The bytes could not be observed at all (no project root, a path arbite will not look
#: through). Also a decision for a human, and never a reason to touch anything.
UNKNOWN = "unknown"

VERDICTS = (COMPLETED, NOT_APPLIED, DRIFT, UNKNOWN)

#: The event kinds a reconciliation appends, so a poll sees recovery happen rather than
#: having to compare receipts before and after.
RECOVERY_FINALIZED = "recover.finalized"
RECOVERY_DRIFT = "recover.drift"

#: The staged copy of an operation's new content is kept beside its target: same
#: directory, so replacing the target with it is one atomic `os.replace`, and same
#: filesystem, so that replace cannot fail across a device boundary. The suffix is how a
#: leftover is recognisable by name (see `is_stage_file`), and the leading dot keeps it
#: out of an ordinary listing.
STAGE_SUFFIX = ".arbite-stage"

#: What the continuation of a wrapped problem line is indented with. The doctor prints a
#: finding as `problem [tic-XXXX] kind: detail`, and the frozen DR1/DR2 blocks continue
#: under the word `problem`, so the indent is exactly as wide as that word plus its space.
PROBLEM_CONTINUATION = " " * len("problem ")

#: The attempt outcomes that name a *ticket* transition rather than an attempt state, with
#: the words a finding uses. A claim orphaned by a closed ticket reads "ticket closed
#: 2026-09-21T13:20:03Z" (the frozen DR1 block); an attempt that ended some other way
#: falls back to its own state, because saying more than the record does would be
#: inventing a reason.
ATTEMPT_OUTCOME_WORDS = {
    "closed": "ticket closed",
    "released": "ticket released",
    "shelved": "ticket shelved",
    "blocked": "ticket blocked",
    "reopened": "ticket reopened",
    "taken_over": "ticket taken over",
}

#: The two claim findings a repair may act on. Both are states nobody can use: the
#: attempt is over, or it never existed. A claim held by a *live* attempt is never
#: released here -- deciding that an agent is stale is exactly the invention the plan
#: forbids, and a stopped worker is never inferred from a timestamp.
ORPHANED_CLAIM = "orphaned_claim"
CLAIM_WITHOUT_ATTEMPT = "claim_without_attempt"


def stage_path(root, path: str, operation_id: str) -> Path:
    """Where an operation stages the new content for `path`.

    Beside the target, with a name built from the target, the operation and a fixed
    suffix: the same directory is what makes the replacement atomic, and the operation id
    keeps two operations for one path from sharing a stage file."""
    target = Path(root) / path
    return target.parent / f".{target.name}.{operation_id}{STAGE_SUFFIX}"


def is_stage_file(name: str) -> bool:
    """Whether a filename is a stage file, for the callers that must skip one.

    A stage file is transport -- the bytes an operation is about to write -- so it is
    never a managed path, never a payload and never a ticket. It exists only between
    staging and the atomic replacement, or until the recovery that discards it."""
    return name.endswith(STAGE_SUFFIX) and name.startswith(".")


# ---------------------------------------------------------------------------
# Judging one pending operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationJudgement:
    """What one pending operation turned out to be, and what was done about it.

    `observed` maps every path the receipt names to the version on disk now -- a digest,
    `ABSENT`, or `None` when arbite will not look through what is there (a symlink, a
    directory). `finalised` is the receipt result a reconciliation wrote, or None;
    `preserved` is the digest whose bytes were archived as evidence, when the backend can
    store content; `stage_removed` records whether a leftover stage file was discarded."""

    receipt: OperationReceipt
    verdict: str
    observed: dict
    reason: str = ""
    finalised: Optional[str] = None
    preserved: Optional[str] = None
    stage_removed: bool = False

    @property
    def is_problem(self) -> bool:
        """Whether this operation needs a human: drift, or something unjudgeable.

        A reconcilable operation is *not* a problem: its repair is unambiguous and free,
        so reporting it would be noise, and `doctor` without `--fix` writes nothing. The
        count of pending operations `doctor` prints -- the store's own numbers -- still
        names it as unfinished, because that is what the records say."""
        return self.verdict in (DRIFT, UNKNOWN)

    @property
    def paths(self) -> tuple:
        return tuple(self.receipt.paths)


def observe(root, path: str) -> Optional[str]:
    """The version on disk for `path` under `root`, or None when arbite will not judge it.

    `ABSENT` covers a path that is not there *and* a path whose parent directory is not
    there: in both cases the recorded file does not exist, which is the question the
    comparison asks. Anything arbite refuses to look through -- a symlinked component, a
    directory, a special file, a hard-linked target -- is None, because reporting "the
    bytes are not what was recorded" about something arbite did not read would be a
    guess."""
    if root is None:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    target = base / path
    if target.is_symlink():
        return None
    if not target.exists():
        return ABSENT
    try:
        return probe(root, path).digest
    except PathRefused:
        return None


def judge(receipt: OperationReceipt, root) -> OperationJudgement:
    """Decide what `receipt`'s staged operation left behind, changing nothing.

    The comparison is per path and all-or-nothing: an operation "happened" only when
    *every* path it names is at its recorded after version, and "did not happen" only when
    every path is at its before version. A rename that got half way -- the source gone,
    the destination not yet written -- is therefore drift, not a guess about which half to
    finish. An operation whose before and after versions are the same (a write of
    identical bytes) reads as completed: the bytes on disk are the version it wanted. An
    operation naming no path has nothing to compare, so it is unjudgeable rather than
    silently "done"."""
    if not receipt.paths:
        return OperationJudgement(
            receipt=receipt,
            verdict=UNKNOWN,
            observed={},
            reason="the receipt names no path, so there are no bytes to compare",
        )
    observed = {path: observe(root, path) for path in receipt.paths}
    if any(version is None for version in observed.values()):
        return OperationJudgement(
            receipt=receipt,
            verdict=UNKNOWN,
            observed=observed,
            reason="arbite could not read every path this operation names",
        )
    if all(observed[path] == receipt.after[path] for path in receipt.paths):
        return OperationJudgement(
            receipt=receipt,
            verdict=COMPLETED,
            observed=observed,
            reason="the recorded after version is on disk",
        )
    if all(observed[path] == receipt.before[path] for path in receipt.paths):
        return OperationJudgement(
            receipt=receipt,
            verdict=NOT_APPLIED,
            observed=observed,
            reason="the recorded before version is on disk, so nothing was applied",
        )
    return OperationJudgement(
        receipt=receipt,
        verdict=DRIFT,
        observed=observed,
        reason=_drift_reason(receipt, observed),
    )


def judgements(store, root=None) -> list:
    """Every pending operation, judged, in the store's order. Reads only."""
    root = root if root is not None else workspace_root(store)
    return [judge(receipt, root) for receipt in store.pending_operations()]


def workspace_root(store) -> Optional[str]:
    """The project root the bytes live under, from the store's own workspace binding.

    A record of where the workspace is, not a search for one: the store was handed to
    arbite by a project, and its binding says which. A store with no binding -- or one
    holding two, the ambiguity `get_workspace` refuses -- has no answer, so the judgement
    is *unknown* rather than a guess about a directory."""
    try:
        workspace = store.get_workspace()
    except RecordError:
        return None
    return workspace.root if workspace is not None else None


def _drift_reason(receipt: OperationReceipt, observed: dict) -> str:
    """Why the bytes are drift, in the words the finding uses (no path: the finding prints
    that beside the versions)."""
    for path in receipt.paths:
        if observed[path] == receipt.after[path]:
            continue
        if observed[path] == receipt.before[path]:
            return (
                "some of this operation's paths are applied and others are not, so it did "
                "not complete as one change"
            )
        return (
            "the bytes on disk are neither the recorded before nor the recorded after "
            "version"
        )
    return "the bytes on disk are neither the recorded before nor the recorded after version"


# ---------------------------------------------------------------------------
# Reconciling: the writes
# ---------------------------------------------------------------------------


def settle(store, receipt: OperationReceipt, root=None) -> OperationJudgement:
    """Judge one pending receipt and write what is unambiguous about it.

    The single-receipt form of `reconcile`, for a caller that has a reason to look at one
    operation first -- a retry presenting the id it means to re-run asks what its own
    receipt left behind before deciding whether to run it again."""
    root = root if root is not None else workspace_root(store)
    return _finish(store, judge(receipt, root))


def reconcile(store, root=None, skip=()) -> list:
    """Bring every pending operation to an honest, recorded end.

    Each operation is judged and the two unambiguous answers are written: completed
    operations are finalised as succeeded (their bytes are already the recorded after
    version, so nothing is applied), unapplied ones as failed (nothing was applied, and
    the staged copy is discarded). Drift and unjudgeable operations are *left pending*:
    their receipt is the only record of what was intended, and no automatic pass may
    decide which version is right. Each reconciliation appends a `recover.*` event, so the
    stream says what happened even though the receipt keeps the intent's own time.

    `skip` names operation ids the caller is about to finish itself: a retry presents the
    id it means to re-run, and finalising that receipt as failed before re-running it
    would turn an interrupted write into a refused one.

    Returns one `OperationJudgement` per operation looked at, in store order.
    """
    root = root if root is not None else workspace_root(store)
    return [
        _finish(store, judge(receipt, root))
        for receipt in store.pending_operations()
        if receipt.id not in skip
    ]


def _finish(store, outcome: OperationJudgement) -> OperationJudgement:
    """Write what is unambiguous about one judged operation, and nothing else."""
    receipt = outcome.receipt
    if outcome.verdict == COMPLETED:
        return replace(
            outcome,
            finalised=(
                RECEIPT_SUCCEEDED
                if _finalise(store, receipt, RECEIPT_SUCCEEDED, outcome.reason)
                else None
            ),
            stage_removed=_discard_stage(store, outcome),
        )
    if outcome.verdict == NOT_APPLIED:
        return replace(
            outcome,
            finalised=(
                RECEIPT_FAILED
                if _finalise(store, receipt, RECEIPT_FAILED, outcome.reason)
                else None
            ),
            stage_removed=_discard_stage(store, outcome),
        )
    if outcome.verdict == DRIFT:
        preserved = archive_evidence(store, outcome)
        _record_drift(store, outcome, preserved)
        return replace(outcome, preserved=preserved)
    return outcome


def _finalise(store, receipt: OperationReceipt, result: str, reason: str) -> bool:
    """Finalise a pending receipt, and say whether this caller was the one that did it.

    A compare-and-swap on the receipt's own revision, in one commit with the event that
    records the reconciliation, so two recoveries racing (a retry and a doctor run, say)
    produce exactly one finalisation. A receipt that is no longer pending was finalised by
    somebody else, which is not an error: it is the answer this call wanted."""
    try:
        with store.transaction() as txn:
            current = txn.get_record("receipt", receipt.id)
            if not current.is_pending:
                return False
            txn.replace_record(
                replace(current, result=result),
                expect_revision=txn.revision("receipt", receipt.id),
            )
            txn.append_event(
                RECOVERY_FINALIZED,
                "recovery",
                subject=", ".join(current.paths),
                result=result,
                ticket_id=current.ticket_id,
                attempt_id=current.attempt_id,
                operation_id=current.id,
                payload={
                    "verdict": (
                        "completed" if result == RECEIPT_SUCCEEDED else "not_applied"
                    ),
                    "reason": reason,
                },
            )
    except Stale:
        # Somebody moved the receipt between the read and the write: they decided, so this
        # one does not. Nothing was written.
        return False
    return True


def _discard_stage(store, outcome: OperationJudgement) -> bool:
    """Remove the staged copy of an operation that is over.

    The stage file is transport: once the operation has been finalised -- applied, or
    never applied -- the bytes are either the target's own or recorded as an artifact, so
    a copy beside the target would only be a second, unmanaged version of one file. Drift
    keeps it: there it is evidence, and nothing about a drifted path is touched."""
    root = workspace_root(store)
    if root is None:
        return False
    removed = False
    for path in outcome.receipt.paths:
        stage = stage_path(root, path, outcome.receipt.id)
        if stage.is_file():
            stage.unlink()
            removed = True
    return removed


def archive_evidence(store, outcome: OperationJudgement) -> Optional[str]:
    """Keep the bytes a drifted operation found on disk, and return their digest.

    Content-addressed, so two findings about one version share the bytes. Deliberately
    best-effort: a backend that cannot store artifact content (SQLite, until tic-7c42
    decides how content lives in a database) leaves the bytes exactly where they are --
    arbite never changes a drifted file, so the evidence survives anyway, and the digests
    the finding prints are what a human compares. Nothing is claimed about a store that
    could not archive: the caller sees `preserved=None`."""
    root = workspace_root(store)
    if root is None:
        return None
    for path in outcome.receipt.paths:
        observed = outcome.observed.get(path)
        if observed is None or observed == ABSENT:
            continue
        if observed in (outcome.receipt.before[path], outcome.receipt.after[path]):
            continue
        try:
            data = (Path(root) / path).read_bytes()
        except OSError:
            continue
        try:
            store.put_artifact_bytes(observed, data)
        except NotImplementedError:
            return None
        _record_artifact(store, observed, len(data), outcome.receipt)
        return observed
    return None


def _record_artifact(store, digest: str, size: int, receipt: OperationReceipt) -> None:
    """Index archived evidence once per version, naming the operation that recorded it.

    One artifact record per distinct digest, the same rule the journal follows: the bytes are
    stored once, and every later operation that meets the same version names the same record,
    which is what keeps "how much evidence is there" a count of versions rather than of
    events."""
    artifacts = store.records("artifact")
    if any(artifact.digest == digest for artifact in artifacts):
        return
    store.put_record(
        Artifact(
            id=new_id("artifact", {artifact.id for artifact in artifacts}),
            digest=digest,
            size=size,
            created=utc_now(),
            operation_id=receipt.id,
        )
    )


def _record_drift(store, outcome: OperationJudgement, preserved: Optional[str]) -> None:
    """Append the one event a drift needs: what arbite found, and that it changed nothing.

    Appended under the operation id, so the event and the still-pending receipt point at
    each other, and idempotent by (kind, operation id): a second reconciliation of the
    same drift appends no second event."""
    receipt = outcome.receipt
    with store.transaction() as txn:
        txn.append_event(
            RECOVERY_DRIFT,
            "recovery",
            subject=", ".join(receipt.paths),
            result="drift",
            ticket_id=receipt.ticket_id,
            attempt_id=receipt.attempt_id,
            operation_id=receipt.id,
            payload={
                "reason": outcome.reason,
                # Whole digests: this is the one place the question is exactly "which
                # bytes are these".
                "before": receipt.before,
                "after": receipt.after,
                "observed": {
                    path: str(outcome.observed.get(path)) for path in receipt.paths
                },
                "evidence": preserved or "",
            },
        )


# ---------------------------------------------------------------------------
# What `doctor` reports, and what `--fix` repairs
# ---------------------------------------------------------------------------


def findings(store, ticket_ids=None, operation_findings=None) -> list:
    """Every coordination finding, in the order a reader acts on them.

    Three phases, because the correct response follows the phase: a claim a live holder
    cannot use (release it), an unfinished mutation (judge it), then the records that name
    something missing (inspect them). `operation_findings` lets a caller that has already
    judged (and perhaps reconciled) the pending operations hand its rendering of phase two
    in, instead of judging them again with a possibly different answer.
    """
    ownership, dangling = _claim_findings(store)
    operations = (
        operation_findings if operation_findings is not None else operation_problems(store)
    )
    return [*ownership, *operations, *dangling, *_other_findings(store, ticket_ids)]


def operation_problems(store, root=None) -> list:
    """Phase two: the unfinished operations that need a human, judged from the bytes."""
    return [
        problem
        for problem in (operation_problem(outcome) for outcome in judgements(store, root))
        if problem is not None
    ]


def operation_problem(
    outcome: OperationJudgement, known_command=None, fix=False
) -> Optional[Problem]:
    """One unfinished operation as `doctor` reports it, or None when it needs no report.

    A reconcilable operation returns None: `doctor` without `--fix` changes nothing, and
    its repair is unambiguous and free, so a report about it would be noise. Drift is what
    the report exists for, and its wording is the frozen DR1 block's -- including the
    `--fix` form, which replaces the diagnosis with what a human should do instead, naming
    the receipt and change views only when this arbite actually has them."""
    receipt = outcome.receipt
    if outcome.verdict == DRIFT:
        return Problem(
            "pending_operation",
            _drift_detail(outcome, known_command=known_command) if fix else _diagnosis(outcome),
            ticket_id=receipt.ticket_id,
        )
    if outcome.verdict == UNKNOWN:
        return Problem(
            "pending_operation",
            f"{receipt.id} staged a {receipt.kind} of "
            f"{', '.join(receipt.paths) or '(no path)'} and was not finalized; arbite could "
            f"not judge it ({outcome.reason}), so nothing was changed -- inspect the "
            "receipt and the bytes on disk by hand",
            ticket_id=receipt.ticket_id,
        )
    return None


def _diagnosis(outcome: OperationJudgement) -> str:
    """The DR1 detail: what was staged, and the three versions that disagree.

    The line breaks are part of the message rather than a wrapper's choice, which is why
    it reads the same whatever length a digest happens to be printed at."""
    receipt = outcome.receipt
    path = _first_mismatch(outcome)
    observed = outcome.observed.get(path)
    return (
        f"{receipt.id} staged a {receipt.kind} of {', '.join(receipt.paths)} and was\n"
        f"{PROBLEM_CONTINUATION}not finalized; on-disk bytes "
        f"({observed if observed is not None else 'unreadable'}) match neither before\n"
        f"{PROBLEM_CONTINUATION}({receipt.before[path]}) nor after ({receipt.after[path]})"
    )


def _drift_detail(outcome: OperationJudgement, known_command=None) -> str:
    """The DR2 detail: the same finding, with what to do instead of what arbite saw.

    The receipt and change views it points at are named only when this arbite has them
    (`known_command`), because guidance naming a command nobody can run is a capability
    claimed on paper only -- and the sentence has to stay true in a build where the receipt
    slice (tic-7c42) has not landed yet."""
    receipt = outcome.receipt
    return (
        f"{receipt.id} ... (not fixed: arbite will not guess which version is\n"
        f"{PROBLEM_CONTINUATION}correct; {_inspect_clause(receipt, known_command)}, then "
        "restore or re-apply by hand)"
    )


def _inspect_clause(receipt: OperationReceipt, known_command=None) -> str:
    """How to look at the versions a drift left behind, naming only real commands."""
    named = []
    if known_command is not None and known_command("receipt"):
        named.append(f"'arbite receipt {receipt.id}'")
    if known_command is not None and known_command("changes") and receipt.ticket_id:
        named.append(f"'arbite changes {receipt.ticket_id}'")
    if len(named) == 2:
        return f"inspect {named[0]} and {named[1]}"
    if named:
        return f"inspect {named[0]}"
    return f"inspect the receipt {receipt.id} and the ticket's recorded change history"


def _first_mismatch(outcome: OperationJudgement) -> str:
    """The path whose bytes are the reason this is drift (the first, in receipt order)."""
    receipt = outcome.receipt
    for path in receipt.paths:
        observed = outcome.observed.get(path)
        if observed is None or observed != receipt.after[path]:
            return path
    return receipt.paths[0]


def _claim_findings(store) -> tuple:
    """The claim findings, as `(live but unusable, naming something missing)`.

    Split because the responses differ: the first group is a claim a live holder cannot
    use (its attempt is over), the second is a claim naming an attempt or a workspace this
    store does not have. Within a group the order is by path, so two runs agree and a
    human reads the paths in one direction."""
    workspace_id = _workspace_id(store)
    attempts = {attempt.id: attempt for attempt in store.records("attempt")}
    orphaned, dangling = [], []
    for claim in sorted(store.records("claim"), key=lambda claim: (claim.path, claim.id)):
        if claim.state != CLAIM_ACTIVE:
            continue
        attempt = attempts.get(claim.attempt_id)
        if attempt is None:
            dangling.append(_claim_problem(store, claim, None))
        elif attempt.state != ATTEMPT_ACTIVE:
            orphaned.append(_claim_problem(store, claim, attempt))
        if workspace_id is not None and claim.workspace_id != workspace_id:
            dangling.append(
                Problem(
                    "claim_for_another_workspace",
                    f"claim on {claim.path} names workspace {claim.workspace_id}, but this "
                    f"store records {workspace_id}",
                    ticket_id=claim.ticket_id,
                )
            )
    return orphaned, dangling


def _ended_reason(attempt: WorkAttempt) -> str:
    """Why an attempt is no longer active, in its own words.

    The attempt records an `outcome` when a lifecycle command ends it; when that outcome
    names a ticket transition the finding prints it (the frozen DR1 block reads "ticket
    closed <time>"), and otherwise the attempt's own state is the whole truth."""
    words = ATTEMPT_OUTCOME_WORDS.get(attempt.outcome or "")
    if words is not None:
        return f"{words} {attempt.ended}"
    return f"attempt {attempt.state} {attempt.ended}"


def _other_findings(store, ticket_ids=None) -> list:
    """The record findings that are about neither claims nor unfinished operations."""
    problems = []
    ambiguous = multiple_workspace_problem(store)
    if ambiguous is not None:
        problems.append(ambiguous)
    workspace_id = _workspace_id(store)
    known_tickets = set(ticket_ids) if ticket_ids is not None else None
    for attempt in store.records("attempt"):
        if workspace_id is not None and attempt.workspace_id != workspace_id:
            problems.append(
                Problem(
                    "attempt_for_another_workspace",
                    f"attempt {attempt.id} names workspace {attempt.workspace_id}, but this "
                    f"store records {workspace_id}",
                    ticket_id=attempt.ticket_id,
                )
            )
        if known_tickets is not None and attempt.ticket_id not in known_tickets:
            problems.append(
                Problem(
                    "attempt_without_ticket",
                    f"attempt {attempt.id} names ticket {attempt.ticket_id}, which is not in "
                    "the ticket store",
                    ticket_id=attempt.ticket_id,
                )
            )
    cursors = {}
    for event in store.records("event"):
        cursors.setdefault(event.cursor, []).append(event.id)
    for cursor, event_ids in sorted(cursors.items()):
        if len(event_ids) > 1:
            problems.append(
                Problem(
                    "duplicate_event_cursor",
                    f"cursor {cursor} is claimed by {', '.join(sorted(event_ids))} -- a "
                    "resumed poll would skip or replay one of them",
                )
            )
    return problems


def _workspace_id(store) -> Optional[str]:
    """The recorded workspace id, or None when there is not exactly one."""
    try:
        workspace = store.get_workspace()
    except RecordError:
        return None
    return workspace.id if workspace is not None else None


def multiple_workspace_problem(store) -> Optional[Problem]:
    """The finding a store holding two workspace bindings needs, or None.

    Asked before the id is (see `_workspace_id`), because the id cannot be answered for
    such a store: two bindings is the state `get_workspace` refuses, and the finding
    repeats what that refusal said."""
    try:
        store.get_workspace()
    except RecordError as e:
        return Problem("multiple_workspaces", str(e))
    return None


def repair(store, ticket_ids=None, root=None, known_command=None) -> list:
    """`doctor --fix`: repair what is unambiguous, report what is not.

    The two unambiguous repairs are releasing a claim nobody can use (its attempt is over
    or never existed -- never a live one) and finalising an unfinished operation whose
    bytes are exactly one of the two recorded versions. Drift stays a problem, and the
    report says why rather than guessing; a claim somebody else moved while the repair ran
    is re-reported from the fresh state rather than forced."""
    root = root if root is not None else workspace_root(store)
    outcomes = reconcile(store, root)
    phase_two = [
        problem
        for problem in (
            operation_problem(outcome, known_command=known_command, fix=True)
            for outcome in outcomes
        )
        if problem is not None
    ]
    phase_two += [
        _finalised_problem(outcome) for outcome in outcomes if outcome.finalised is not None
    ]
    ownership, dangling = _repair_claims(store)
    return [*ownership, *phase_two, *dangling, *_other_findings(store, ticket_ids)]


def _finalised_problem(outcome: OperationJudgement) -> Problem:
    """The `fixed` line for an operation a reconciliation finalised."""
    receipt = outcome.receipt
    if outcome.finalised == RECEIPT_SUCCEEDED:
        detail = (
            f"finalized {receipt.id} as succeeded ({outcome.reason}; this repair changed no "
            "bytes)"
        )
    else:
        detail = (
            f"finalized {receipt.id} as failed ({outcome.reason}; the staged copy was "
            "discarded)"
        )
    return Problem("pending_operation", detail, ticket_id=receipt.ticket_id, fixed=True)


def _repair_claims(store) -> tuple:
    """Release the claims nobody can use, and render both claim phases.

    Release is a claim record write plus the `release.file` event the release command
    appends, compare-and-swapped on the claim's own revision, so a claim that changed
    under this run is reported from its fresh state instead of being overwritten. The bytes
    and the receipts are untouched: revoking ownership is not a change to the work."""
    workspace_id = _workspace_id(store)
    attempts = {attempt.id: attempt for attempt in store.records("attempt")}
    ownership, dangling = [], []
    for claim in sorted(store.records("claim"), key=lambda claim: (claim.path, claim.id)):
        if claim.state != CLAIM_ACTIVE:
            continue
        attempt = attempts.get(claim.attempt_id)
        if attempt is not None and attempt.state == ATTEMPT_ACTIVE:
            continue
        if workspace_id is not None and claim.workspace_id != workspace_id:
            # A claim from another workspace is a finding, not something to release: this
            # store is not the one it belongs to.
            dangling.append(_claim_problem(store, claim, attempt))
            continue
        if not _release_claim(store, claim, attempt):
            # The claim moved while this run was repairing it: report what is there now.
            dangling.append(_claim_problem(store, claim, attempts.get(claim.attempt_id)))
            continue
        (ownership if attempt is not None else dangling).append(
            Problem(
                ORPHANED_CLAIM if attempt is not None else CLAIM_WITHOUT_ATTEMPT,
                _released_detail(claim, attempt),
                ticket_id=claim.ticket_id,
                fixed=True,
            )
        )
    return ownership, dangling


def orphaned_claim_detail(claim: FileClaim, attempt: WorkAttempt) -> str:
    """The DR1 wording for a claim a live holder cannot use, line breaks included.

    The breaks are part of the message rather than a wrapper's decision, exactly as the
    diagnosis of a drifted operation is: the frozen DR1 block prints this finding as two
    lines, and the wrapped sentence is what a reader compares against the store."""
    return (
        f"claim on {claim.path} names attempt {claim.attempt_id},\n"
        f"{PROBLEM_CONTINUATION}which is not active ({_ended_reason(attempt)})"
    )


def dangling_claim_detail(claim: FileClaim) -> str:
    """The DR1 wording for a claim naming an attempt this store does not have (see above)."""
    return (
        f"claim on {claim.path} names {claim.attempt_id}, which\n"
        f"{PROBLEM_CONTINUATION}does not exist in this store"
    )


def _claim_problem(store, claim: FileClaim, attempt: Optional[WorkAttempt]) -> Problem:
    """The finding for one claim, rendered from the records as they are now."""
    if attempt is None:
        return Problem(
            CLAIM_WITHOUT_ATTEMPT, dangling_claim_detail(claim), ticket_id=claim.ticket_id
        )
    if attempt.state != ATTEMPT_ACTIVE:
        return Problem(
            ORPHANED_CLAIM,
            orphaned_claim_detail(claim, attempt),
            ticket_id=claim.ticket_id,
        )
    return Problem(
        "claim_for_another_workspace",
        f"claim on {claim.path} names workspace {claim.workspace_id}, but this store records "
        f"{_workspace_id(store)}",
        ticket_id=claim.ticket_id,
    )


def _released_detail(claim: FileClaim, attempt: Optional[WorkAttempt]) -> str:
    """What a repaired claim line says.

    The orphaned case adds that the work is untouched, because a reader who sees a claim
    released may reasonably wonder whether the bytes moved with it; a claim naming no
    attempt has no such work to reassure anybody about (the frozen DR2 block prints them
    this way)."""
    if attempt is not None:
        return f"released the claim on {claim.path} (bytes and receipts unchanged)"
    return f"released the claim on {claim.path}"


def _release_claim(store, claim: FileClaim, attempt: Optional[WorkAttempt]) -> bool:
    """Revoke one claim's ownership, keeping the record as the path's history."""
    if attempt is None:
        reason = f"attempt {claim.attempt_id} does not exist in this store"
    else:
        reason = f"attempt {claim.attempt_id} is not active ({_ended_reason(attempt)})"
    try:
        with store.transaction() as txn:
            current = txn.find_record("claim", claim.id)
            if current is None or current.state != CLAIM_ACTIVE:
                return False
            txn.replace_record(
                replace(
                    current,
                    state=CLAIM_RELEASED,
                    released=utc_now(),
                    release_reason=f"doctor --fix: {reason}",
                ),
                expect_revision=txn.revision("claim", claim.id),
            )
            txn.append_event(
                "release.file",
                "claim",
                subject=current.path,
                result="released",
                ticket_id=current.ticket_id,
                attempt_id=current.attempt_id,
                payload={
                    "generation": current.generation,
                    "version": current.observed_version,
                    "reason": f"doctor --fix: {reason}",
                },
            )
    except Stale:
        return False
    return True
