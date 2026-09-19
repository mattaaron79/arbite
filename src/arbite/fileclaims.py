"""Exclusive file claim sets and explicit release (planning key C04).

This is the application-layer operation the file proxy and the C05+ mutation
commands call. It sits above `arbite.application` (guards, one-shot guarded
transactions) and `arbite.paths` (canonical identity), and it owns one rule: a
whole file has at most one active writer, and a *set* of paths is acquired
all-or-nothing.

The planning contract, made executable here:

- **All-or-nothing.** Every requested path is validated and checked inside one
  transaction. If any path is held by another attempt the whole request is refused
  with `file_busy` and the holder's ticket/attempt/generation -- the paths that
  would have been free are not claimed.
- **Independent ownership.** A busy path only blocks its own request; unrelated
  paths are claimed normally.
- **Idempotent reentrancy.** Re-claiming a path the same attempt already holds
  returns the existing claim unchanged (same id, same generation) -- no new token
  is minted and any read taken under it stays valid.
- **No waiting, no stealing.** Contention is a prompt, structured refusal. Nothing
  here sleeps, retries, expires a claim or reassigns it.
- **Explicit release revokes one token.** `release()` keeps the work attempt but
  marks the claim released; the record is retained as history. Re-acquiring the
  path mints a *new* claim with a *new* generation, so any read observation taken
  under the old generation can no longer authorize a write
  (`application.require_write_authorization` refuses it).
- **Destinations are representable.** A creation or rename destination that does
  not exist yet is a valid claim whose `observed_version` is `ABSENT`; because it
  is keyed by the same canonical path, a second attempt cannot claim it either.

Everything is keyed by the canonical path from `arbite.paths`, so aliases
(traversal, protected state, symlink components, case variants on a
case-insensitive volume) cannot be used to acquire a second token for one file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import paths as path_policy
from .application import CoordinationService, require_active_attempt
from .coordination import (
    EVENT_PAYLOAD_VERSION,
    Event,
    FileClaim,
)
from .application import require_current_generation
from .errors import (
    ClaimConflict,
    CoordinationNotFound,
    FileBusy,
    StaleRead,
)


@dataclass(frozen=True)
class ClaimSetResult:
    """The outcome of one `claim()` call.

    `acquired` are freshly minted claims (new generation); `reentrant` are claims
    this attempt already held, returned unchanged. Ordering follows the canonical
    path order, so the result is deterministic.
    """

    workspace_id: str
    ticket_id: str
    attempt_id: str
    operation_id: str
    acquired: List[FileClaim]
    reentrant: List[FileClaim]

    @property
    def paths(self) -> List[str]:
        return [claim.path for claim in self.acquired] + [
            claim.path for claim in self.reentrant
        ]


@dataclass(frozen=True)
class ReleaseResult:
    """The outcome of one `release()` call."""

    workspace_id: str
    ticket_id: str
    attempt_id: str
    operation_id: str
    reason: str
    released: List[FileClaim]
    already_released: List[FileClaim]


class FileClaimService:
    """Claim and release whole-file ownership for one bound workspace.

    Construct with the workspace's `CoordinationService` (which owns the
    authoritative store and its binding). `root` defaults to the workspace root;
    `case_insensitive` overrides filesystem case detection and exists so both
    platform behaviours can be exercised on one host.
    """

    def __init__(
        self,
        service: CoordinationService,
        *,
        root: Optional[str] = None,
        case_insensitive: Optional[bool] = None,
    ) -> None:
        self.service = service
        self.workspace = service.workspace
        self.root = str(root) if root is not None else self.workspace.root
        self._case = case_insensitive

    # -- canonical identity -------------------------------------------------

    def resolve(self, path: str, *, mode: str = path_policy.MODE_CLAIM):
        """The canonical `ResolvedTarget` for `path` (validating the filesystem)."""
        return path_policy.resolve_target(
            self.root, path, mode=mode, case_insensitive=self._case
        )

    def canonical_key(self, path: str) -> str:
        """The canonical claim key for `path`, without requiring it to exist.

        Used by `release()`: a released claim is addressed by the string identity
        that was recorded when it was acquired, so the file need not still be
        present (a remove followed by a release must not be impossible).
        """
        return self.resolve(path, mode=path_policy.MODE_RELEASE).relative

    # -- claim --------------------------------------------------------------

    def claim(
        self,
        attempt,
        requested_paths,
        *,
        ticket_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> ClaimSetResult:
        """Acquire exclusive ownership of every path in `requested_paths`.

        All-or-nothing and deterministic: paths are validated and canonicalized
        first, then processed in sorted order inside one guarded transaction.
        Raises `FileBusy` (nothing claimed) when any path is held by another
        attempt; raises `UnsupportedCoordination` for an alias/escape/special file
        before a transaction is even opened.
        """
        require_active_attempt(attempt)
        ticket_id = ticket_id or attempt.ticket_id
        self._require_same_ticket(attempt, ticket_id)
        self._require_same_workspace(attempt)

        targets: dict = {}
        for requested in requested_paths:
            resolved = self.resolve(requested, mode=path_policy.MODE_CLAIM)
            targets.setdefault(resolved.relative, resolved)
        ordered = [targets[key] for key in sorted(targets)]

        with self.service.guarded(
            "claim",
            attempt=attempt,
            ticket_id=ticket_id,
            paths=[target.relative for target in ordered],
            operation_id=operation_id,
        ) as operation:
            transaction = operation.transaction
            acquired: List[FileClaim] = []
            reentrant: List[FileClaim] = []
            for target in ordered:
                history = transaction.find(
                    "file_claim", workspace_id=self.workspace.id, path=target.relative
                )
                holder = _active_holder(history)
                if holder is not None:
                    if self._is_mine(holder, attempt, ticket_id):
                        reentrant.append(holder)
                        continue
                    raise self._busy(holder, ticket_id=ticket_id, attempt_id=attempt.id)

                generation = max((claim.generation for claim in history), default=0) + 1
                claim = FileClaim(
                    id=self.service.new_record_id("file_claim"),
                    workspace_id=self.workspace.id,
                    path=target.relative,
                    ticket_id=ticket_id,
                    attempt_id=attempt.id,
                    generation=generation,
                    acquired=self.service.now(),
                    observed_version=target.digest,
                )
                transaction.put(claim)
                transaction.append_event(
                    self._claim_event(claim, creation=target.is_creation)
                )
                acquired.append(claim)
            result = ClaimSetResult(
                workspace_id=self.workspace.id,
                ticket_id=ticket_id,
                attempt_id=attempt.id,
                operation_id=operation.operation_id,
                acquired=acquired,
                reentrant=reentrant,
            )
        return result

    # -- release ------------------------------------------------------------

    def release(
        self,
        attempt,
        requested_paths,
        *,
        reason: str,
        expected_generation: Optional[int] = None,
        ticket_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> ReleaseResult:
        """Release this attempt's active claims, revoking those tokens.

        The attempt is retained (unlike a ticket-level release); only the file
        token dies. Releasing an already-released path this attempt owned is
        idempotent; releasing a path another attempt holds, or one this attempt
        never held, is refused. All-or-nothing in one guarded transaction.

        `expected_generation` (optional) is the claim generation the caller
        believes it holds: a mismatch raises `stale_read` and releases nothing, so
        a stale caller cannot revoke a token it no longer owns after a
        release/reacquire cycle.
        """
        require_active_attempt(attempt)
        ticket_id = ticket_id or attempt.ticket_id
        self._require_same_ticket(attempt, ticket_id)
        self._require_same_workspace(attempt)
        if not (reason and str(reason).strip()):
            raise ClaimConflict(
                "releasing a file claim requires a non-empty reason so the release "
                "is attributable in the event log",
                details={"ticket_id": ticket_id, "attempt_id": attempt.id},
            )

        keys = sorted({self.canonical_key(path) for path in requested_paths})
        with self.service.guarded(
            "release",
            attempt=attempt,
            ticket_id=ticket_id,
            paths=keys,
            operation_id=operation_id,
        ) as operation:
            transaction = operation.transaction
            released: List[FileClaim] = []
            already_released: List[FileClaim] = []
            for key in keys:
                history = transaction.find(
                    "file_claim", workspace_id=self.workspace.id, path=key
                )
                holder = _active_holder(history)
                if holder is not None:
                    if self._is_mine(holder, attempt, ticket_id):
                        if expected_generation is not None:
                            require_current_generation(
                                expected_generation, holder.generation
                            )
                        holder.release(timestamp=self.service.now())
                        transaction.put(holder)
                        transaction.append_event(self._release_event(holder, reason))
                        released.append(holder)
                        continue
                    raise ClaimConflict(
                        f"path {key!r} is held by ticket {holder.ticket_id} "
                        f"(attempt {holder.attempt_id}), not by attempt {attempt.id}; "
                        "only the holder may release it",
                        details={
                            "workspace_id": self.workspace.id,
                            "path": key,
                            "holder_claim": holder.id,
                            "holder_ticket": holder.ticket_id,
                            "holder_attempt": holder.attempt_id,
                            "holder_generation": holder.generation,
                            "caller_attempt": attempt.id,
                            "caller_ticket": ticket_id,
                        },
                    )
                prior = [
                    claim
                    for claim in history
                    if not claim.is_active and self._is_mine(claim, attempt, ticket_id)
                ]
                if prior:
                    already_released.append(max(prior, key=lambda claim: claim.generation))
                    continue
                raise CoordinationNotFound(
                    f"no claim on {key!r} is recorded for attempt {attempt.id}; there "
                    "is nothing to release",
                    details={
                        "workspace_id": self.workspace.id,
                        "path": key,
                        "attempt_id": attempt.id,
                    },
                )
            result = ReleaseResult(
                workspace_id=self.workspace.id,
                ticket_id=ticket_id,
                attempt_id=attempt.id,
                operation_id=operation.operation_id,
                reason=str(reason),
                released=released,
                already_released=already_released,
            )
        return result

    # -- queries ------------------------------------------------------------

    def active_claims(
        self, *, attempt_id: Optional[str] = None, workspace_id: Optional[str] = None
    ) -> List[FileClaim]:
        """Active claims for the workspace, optionally narrowed to one attempt."""
        workspace_id = workspace_id or self.workspace.id
        with self.service.store.transaction(write=False) as tx:
            claims = [
                claim
                for claim in tx.find("file_claim", workspace_id=workspace_id)
                if claim.is_active
            ]
        if attempt_id is not None:
            claims = [claim for claim in claims if claim.attempt_id == attempt_id]
        return sorted(claims, key=lambda claim: claim.path)

    def claim_for(self, path: str, *, workspace_id: Optional[str] = None) -> Optional[FileClaim]:
        """The current active claim on canonical `path`, or None."""
        workspace_id = workspace_id or self.workspace.id
        key = self.canonical_key(path)
        with self.service.store.transaction(write=False) as tx:
            history = tx.find("file_claim", workspace_id=workspace_id, path=key)
        return _active_holder(history)

    # -- internals ----------------------------------------------------------

    def _require_same_ticket(self, attempt, ticket_id: str) -> None:
        if attempt.ticket_id != ticket_id:
            raise ClaimConflict(
                f"attempt {attempt.id} belongs to ticket {attempt.ticket_id}, not "
                f"{ticket_id}",
                details={
                    "attempt_id": attempt.id,
                    "attempt_ticket": attempt.ticket_id,
                    "requested_ticket": ticket_id,
                },
            )

    def _require_same_workspace(self, attempt) -> None:
        if attempt.workspace_id != self.workspace.id:
            raise ClaimConflict(
                f"attempt {attempt.id} belongs to workspace {attempt.workspace_id}, "
                f"not {self.workspace.id}; claims are per workspace",
                details={
                    "attempt_id": attempt.id,
                    "attempt_workspace": attempt.workspace_id,
                    "workspace_id": self.workspace.id,
                },
            )

    @staticmethod
    def _is_mine(claim: FileClaim, attempt, ticket_id: str) -> bool:
        return (
            claim.attempt_id == attempt.id
            and claim.ticket_id == ticket_id
            and claim.workspace_id == attempt.workspace_id
        )

    def _busy(self, holder: FileClaim, *, ticket_id: str, attempt_id: str) -> FileBusy:
        return FileBusy(
            f"path {holder.path!r} is held by ticket {holder.ticket_id} "
            f"(attempt {holder.attempt_id}, generation {holder.generation}); arbite "
            "does not wait or steal a claim -- claim a different path, or retry after "
            "the holder releases it",
            details={
                "workspace_id": holder.workspace_id,
                "path": holder.path,
                "holder_claim": holder.id,
                "holder_ticket": holder.ticket_id,
                "holder_attempt": holder.attempt_id,
                "holder_generation": holder.generation,
                "holder_observed_version": holder.observed_version,
                "requested_ticket": ticket_id,
                "requested_attempt": attempt_id,
                "available_actions": [
                    "claim a different path",
                    "retry later once the holder releases the path",
                    "release your own ticket with a handoff and pick up other work",
                ],
            },
        )

    def _claim_event(self, claim: FileClaim, *, creation: bool) -> Event:
        return Event(
            id=self.service.new_record_id("event"),
            cursor=None,
            kind_="claim_acquired",
            category="claim",
            timestamp=claim.acquired,
            subject_ids=[claim.id, claim.attempt_id, claim.ticket_id],
            # Deliberately no operation_id: the guarded operation appends its own
            # `operation_recorded` event, and sharing an operation id would let the
            # store's dedup collapse the two distinct records into one.
            operation_id=None,
            payload={
                "workspace_id": claim.workspace_id,
                "path": claim.path,
                "ticket_id": claim.ticket_id,
                "attempt_id": claim.attempt_id,
                "generation": claim.generation,
                "observed_version": claim.observed_version,
                "creation": creation,
                "reentrant": False,
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )

    def _release_event(self, claim: FileClaim, reason: str) -> Event:
        return Event(
            id=self.service.new_record_id("event"),
            cursor=None,
            kind_="claim_released",
            category="claim",
            timestamp=claim.released,
            subject_ids=[claim.id, claim.attempt_id, claim.ticket_id],
            operation_id=None,
            payload={
                "workspace_id": claim.workspace_id,
                "path": claim.path,
                "ticket_id": claim.ticket_id,
                "attempt_id": claim.attempt_id,
                "generation": claim.generation,
                "reason": reason,
            },
            payload_version=EVENT_PAYLOAD_VERSION,
        )


def _active_holder(history) -> Optional[FileClaim]:
    """The highest-generation active claim in `history`, or None.

    The collection invariant guarantees at most one *active* claim per
    (workspace, path); taking the highest generation is defensive so a legacy
    duplicate cannot make "who holds this" ambiguous.
    """
    active = [claim for claim in history if claim.is_active]
    if not active:
        return None
    return max(active, key=lambda claim: claim.generation)


__all__ = ["ClaimSetResult", "FileClaimService", "ReleaseResult"]
