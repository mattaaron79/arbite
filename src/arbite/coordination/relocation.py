"""Bringing a moved project's coordination records under the workspace it derives now.

A workspace id is a digest of the project root and the store (see
`records.derived_workspace_id`), so moving a project on disk derives a new id while the
store still holds attempts, claims and a binding stamped with the old one. There is one
store per project, so every foreign stamp in a store came from an earlier location of
that same store: restamping it is a repair, not a guess (tic-9969).

Claims are the delicate part: a claim's id is derived from its workspace and path, so
restamping one *re-keys* it. A foreign claim whose path already has a record under the
current workspace is resolved by state -- the active one wins, a released foreign record
is dropped as superseded history, and two active owners are left alone and reported.

`reset` is the heavier tool for a stale project (tic-cf80): it ends every active attempt
and releases every active claim, then restamps, keeping receipts, events and released
records as history.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Optional

from ..sinks.base import Problem
from .records import (
    ATTEMPT_ACTIVE,
    CLAIM_ACTIVE,
    CLAIM_RELEASED,
    FileClaim,
    Workspace,
    claim_id_for,
    utc_now,
)

STALE_BINDING = "stale_workspace_binding"
FOREIGN_ATTEMPTS = "attempt_for_another_workspace"
FOREIGN_CLAIMS = "claim_for_another_workspace"

WORKSPACE_RESTAMPED = "workspace.restamped"
WORKSPACE_RESET = "workspace.reset"

#: The attempt outcome `reset` records; `recovery.ATTEMPT_OUTCOME_WORDS` renders it.
RESET_OUTCOME = "reset"


@dataclass
class Restamp:
    """What one restamp changed, and the claims it had to leave alone."""

    workspace_id: str
    previous_bindings: list = field(default_factory=list)
    attempts: Counter = field(default_factory=Counter)
    claims: Counter = field(default_factory=Counter)
    superseded: Counter = field(default_factory=Counter)
    conflicts: list = field(default_factory=list)
    #: claim id -> the workspace a record re-keyed onto it came from, this run.
    origins: dict = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(
            self.previous_bindings or self.attempts or self.claims or self.superseded
        )


@dataclass
class Reset:
    """What `reset` changed: the restamp, then the attempts ended and claims released."""

    restamp: Restamp
    attempts: list = field(default_factory=list)
    claims: list = field(default_factory=list)


def _bindings(store) -> list:
    return store.records("workspace")


def _moved(binding: Workspace, workspace: Workspace) -> bool:
    return os.path.normcase(binding.root) != os.path.normcase(workspace.root)


def target_workspace(store, workspace: Optional[Workspace] = None) -> Optional[Workspace]:
    """The workspace every record should name.

    The recorded binding, because that is what new attempts are stamped with -- unless
    `workspace` (the one the project derives now) shows the binding was recorded at
    another root, or there is not exactly one binding: then the derived workspace. A
    binding that differs only in its store (a migration) stays authoritative."""
    bindings = _bindings(store)
    if workspace is not None and (
        len(bindings) != 1 or _moved(bindings[0], workspace)
    ):
        return workspace
    return bindings[0] if len(bindings) == 1 else None


def stale_bindings(store, target: Workspace) -> list:
    """The recorded bindings that are not `target`."""
    return [binding for binding in _bindings(store) if binding.id != target.id]


def findings(store, workspace: Optional[Workspace] = None) -> list:
    """Report-only findings: a stale binding, and the foreign stamps grouped by id."""
    target = target_workspace(store, workspace)
    if target is None:
        return []
    current = target.id
    problems = [
        Problem(
            STALE_BINDING,
            f"this store records workspace {binding.id} at {binding.root}, but the project "
            f"is now {current} at {target.root}; 'arbite doctor --fix' re-records it",
        )
        for binding in stale_bindings(store, target)
    ]
    attempts = Counter(
        a.workspace_id for a in store.records("attempt") if a.workspace_id != current
    )
    for old, count in sorted(attempts.items()):
        problems.append(
            Problem(
                FOREIGN_ATTEMPTS,
                f"{count} attempt(s) name workspace {old}, but this project is {current} "
                "(recorded before the project moved?); 'arbite doctor --fix' restamps them",
            )
        )
    claims = {}
    for claim in store.records("claim"):
        if claim.workspace_id != current:
            claims.setdefault(claim.workspace_id, []).append(claim)
    for old, group in sorted(claims.items()):
        active = sum(1 for claim in group if claim.state == CLAIM_ACTIVE)
        problems.append(
            Problem(
                FOREIGN_CLAIMS,
                f"{len(group)} claim(s) ({active} active) name workspace {old}, but this "
                f"project is {current}; 'arbite doctor --fix' restamps them",
            )
        )
    return problems


def restamp(store, workspace: Workspace) -> Restamp:
    """Re-record the binding as `workspace` and restamp every foreign attempt and claim.

    One commit under the operation lock, so a concurrent claim cannot land between the
    re-key of a path and the check that decided it."""
    result = Restamp(workspace.id)
    with store.operation_lock():
        with store.transaction() as txn:
            result.previous_bindings = stale_bindings(store, workspace)
            if result.previous_bindings or not _bindings(store):
                store.put_workspace(workspace, txn=txn)
            for attempt in store.records("attempt"):
                if attempt.workspace_id == workspace.id:
                    continue
                txn.replace_record(
                    replace(attempt, workspace_id=workspace.id),
                    expect_revision=store.revision("attempt", attempt.id),
                )
                result.attempts[attempt.workspace_id] += 1
            foreign = [c for c in store.records("claim") if c.workspace_id != workspace.id]
            # Newest first, then active first: of two records for one path, the one kept
            # is the active one, else the most recently acquired.
            foreign.sort(key=lambda c: c.acquired, reverse=True)
            foreign.sort(key=lambda c: (c.state != CLAIM_ACTIVE, c.path))
            for claim in foreign:
                _rekey_claim(store, txn, claim, workspace.id, result)
            if result.changed:
                txn.append_event(
                    WORKSPACE_RESTAMPED,
                    "recovery",
                    subject=workspace.id,
                    result="restamped",
                    payload={
                        "previous_bindings": [b.id for b in result.previous_bindings],
                        "attempts": dict(result.attempts),
                        "claims": dict(result.claims),
                        "superseded": dict(result.superseded),
                    },
                )
    return result


def _rekey_claim(store, txn, claim: FileClaim, workspace_id: str, result: Restamp) -> None:
    target = claim_id_for(workspace_id, claim.path)
    moved = replace(claim, id=target, workspace_id=workspace_id)
    existing = txn.find_record("claim", target)
    if existing is None:
        txn.delete_record("claim", claim.id)
        txn.put_record(moved)
        result.claims[claim.workspace_id] += 1
        result.origins[target] = claim.workspace_id
        return
    if existing.path != claim.path or (
        claim.state == CLAIM_ACTIVE and existing.state == CLAIM_ACTIVE
    ):
        result.conflicts.append(claim)
        return
    txn.delete_record("claim", claim.id)
    newer = existing.state != CLAIM_ACTIVE and claim.acquired > existing.acquired
    if claim.state == CLAIM_ACTIVE or newer:
        txn.replace_record(moved, expect_revision=txn.revision("claim", target))
        result.claims[claim.workspace_id] += 1
        displaced = result.origins.get(target)
        if displaced is not None:
            result.claims[displaced] -= 1
            result.superseded[displaced] += 1
        result.origins[target] = claim.workspace_id
    else:
        result.superseded[claim.workspace_id] += 1


def restamp_problems(result: Restamp) -> list:
    """The `fixed` lines for a restamp, plus one unfixed line per claim left alone."""
    problems = [
        Problem(
            STALE_BINDING,
            f"re-recorded the workspace binding as {result.workspace_id} "
            f"(was {binding.id} at {binding.root})",
            fixed=True,
        )
        for binding in result.previous_bindings
    ]
    for old, count in sorted(result.attempts.items()):
        problems.append(
            Problem(
                FOREIGN_ATTEMPTS,
                f"restamped {count} attempt(s) from {old} to {result.workspace_id}",
                fixed=True,
            )
        )
    for old in sorted(set(result.claims) | set(result.superseded)):
        detail = f"restamped {result.claims[old]} claim(s) from {old} to {result.workspace_id}"
        if result.superseded[old]:
            detail += (
                f" and dropped {result.superseded[old]} released record(s) superseded by "
                "a newer record for the same path"
            )
        problems.append(Problem(FOREIGN_CLAIMS, detail, fixed=True))
    for claim in result.conflicts:
        problems.append(
            Problem(
                FOREIGN_CLAIMS,
                f"claim on {claim.path} names workspace {claim.workspace_id}, and "
                f"{result.workspace_id} already has a record for that path that it cannot "
                "replace; left alone ('arbite workspace reset' releases both)",
                ticket_id=claim.ticket_id,
            )
        )
    return problems


def plan_reset(store) -> tuple:
    """The active attempts and active claims a reset would end, in a stable order."""
    attempts = sorted(
        (a for a in store.records("attempt") if a.state == ATTEMPT_ACTIVE),
        key=lambda a: (a.ticket_id, a.id),
    )
    claims = sorted(
        (c for c in store.records("claim") if c.state == CLAIM_ACTIVE),
        key=lambda c: (c.path, c.id),
    )
    return attempts, claims


def reset(store, workspace: Workspace, actor: Optional[str] = None) -> Reset:
    """End every active attempt and release every active claim, then restamp.

    Ended attempts are `interrupted` with outcome `reset`; released claims keep their
    records as history. Releasing first means the restamp meets no two active owners
    of one path, so it can always settle them. Receipts, events, artifacts and scratch
    payloads are untouched."""
    # Lazy: lifecycle imports recovery, which imports this module.
    from .lifecycle import ATTEMPT_ENDED, RELEASE_FILE, STATE_INTERRUPTED

    reason = "workspace reset"
    with store.operation_lock():
        attempts, claims = plan_reset(store)
        now = utc_now()
        ended, released = [], []
        with store.transaction() as txn:
            for attempt in attempts:
                done = replace(
                    attempt,
                    state=STATE_INTERRUPTED,
                    outcome=RESET_OUTCOME,
                    ended=now,
                    last_activity=now,
                )
                txn.replace_record(done, expect_revision=store.revision("attempt", attempt.id))
                txn.append_event(
                    ATTEMPT_ENDED,
                    "attempt",
                    subject=attempt.id,
                    result=STATE_INTERRUPTED,
                    ticket_id=attempt.ticket_id,
                    attempt_id=attempt.id,
                    actor=actor,
                    payload={"outcome": RESET_OUTCOME, "handoff": "", "reason": reason},
                )
                ended.append(done)
            for claim in claims:
                done = replace(
                    claim, state=CLAIM_RELEASED, released=now, release_reason=reason
                )
                txn.replace_record(done, expect_revision=store.revision("claim", claim.id))
                txn.append_event(
                    RELEASE_FILE,
                    "claim",
                    subject=claim.path,
                    result="released",
                    ticket_id=claim.ticket_id,
                    attempt_id=claim.attempt_id,
                    actor=actor,
                    payload={
                        "generation": claim.generation,
                        "version": claim.observed_version,
                        "reason": reason,
                    },
                )
                released.append(done)
            txn.append_event(
                WORKSPACE_RESET,
                "recovery",
                subject=workspace.id,
                result="reset",
                actor=actor,
                payload={"attempts_ended": len(ended), "claims_released": len(released)},
            )
        stamped = restamp(store, workspace)
    return Reset(stamped, ended, released)
