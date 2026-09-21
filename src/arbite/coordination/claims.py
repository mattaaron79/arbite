"""Exclusive file claims: who holds which path, and how a path changes hands.

A claim is durable writer ownership of one whole file by one work attempt. It is not
a lock: no lock is ever held for a ticket's duration, and a claim is a *record* that
a lifecycle command releases (see the handoff's two-kinds-of-mutex rule). What this
module implements is the whole of v1's ownership surface -- `file claim`, `file
release` and `file claims` -- and the rules the frozen FC transcripts pin:

- **One claim record per path.** Its id is derived from the workspace and the path
  (`records.claim_id_for`), so the claim set *is* the current-state index the handoff
  asks for, and acquiring a path is replacing its record. Nobody has to replay a log
  to answer "is this free".
- **All-or-nothing, in canonical path order.** A request is validated and sorted
  first, then every claim it needs is written in **one** commit. A conflict anywhere
  leaves nothing behind: the paths are written inside a single transaction whose
  compare-and-swap is the claim records' own revisions, so a second process that read
  the same "free" answer loses at commit time with nothing written -- which is the
  deterministic order that stops two agents from each holding half of a pair.
- **Generations are minted, never reused.** Each acquisition takes the attempt's next
  generation (one past every claim the attempt has ever held), and the acquisition is
  stamped with it. Re-acquiring a released path mints a new generation, so a read
  token taken under the old one is dead (a read observation authorises a write only
  when it matches the live claim's generation -- see `records.ReadObservation`).
- **Release is history, not deletion.** Releasing revokes the generation, keeps the
  record as the path's released state, and leaves the attempt active: partial work
  stays on disk and is visible to the next worker, because release never silently
  reverts bytes.

Reads, writes, edits, renames and removes are *not* here: they arrive with their own
slices (tic-1c4f for discovery and reads, tic-60c7 for mutations), and the version a
claim records is the input they will check against. Ticket closure releasing a whole
attempt's claims is the close cascade's (tic-e9ed).
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import replace
from typing import Optional

from ..errors import CoordinationError, NotOwner, Stale
from .lifecycle import CLAIM_FILE, RELEASE_FILE, TicketLifecycle, local_time
from .paths import canonical_relative, probe
from .records import (
    ABSENT,
    CLAIM_RELEASED,
    FileClaim,
    claim_id_for,
    short_digest,
    utc_now,
)
from .results import BUSY, EMPTY, OK, OUTCOME_LABELS, OperationResult, Outcome, succeeded

#: The event kinds a claim change appends (`CLAIM_FILE` on acquisition, `RELEASE_FILE`
#: on release) live with the lifecycle, because the cascade that ends an attempt appends
#: the same `release.file` this module's explicit release does: one literal, one kind, so
#: `arbite events` cannot show two spellings of "this path changed hands". `claim.file` is
#: the kind the frozen event stream uses for an acquisition (`arbite events --tail`, EV2),
#: and its subject is the path with the generation as the one-line result -- so "who
#: acquired what, at which generation" is answerable after the claim record has moved on.

#: The `file claims` table's columns, pinned by the frozen FC6 transcript: each text
#: column is as wide as its widest value plus one gap, and the generation is a *number*
#: right-aligned in its own field so the digits line up down the column.
CLAIM_COLUMN_GAP = 2
CLAIM_GENERATION_WIDTH = 7

#: What a claim row prints when the path does not exist: creating a file is a claimable
#: act, and the parenthetical says what authorises it (a probe receipt, tic-1c4f/tic-60c7).
ABSENT_NOTE = "ABSENT (creation is authorized by a probe receipt)"

#: The `next:` line a successful release ends with. A sentence rather than a command:
#: the command it implies needs a path and a fresh read token this command has not
#: issued, so naming a runnable one would mean printing a token that does not exist yet.
RELEASE_HINT = "next: another mutation of this path needs a fresh claim and a fresh read"

CLAIM_RESULT_ORDER_HINT = (
    "acquired in canonical path order; all-or-nothing, so a conflict leaves no partial claims"
)


class FileClaims:
    """Exclusive file claims, for one ticket sink and one coordination store."""

    def __init__(self, sink, lifecycle: TicketLifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def claim(self, ticket_id: str, attempt_id: str, raw_paths) -> OperationResult:
        """Acquire every requested path for one attempt, or none of them.

        The order of the checks is the order a caller can act on them: the paths have
        to be paths arbite will manage at all, the attempt has to own the ticket, and
        then -- the only question that needs the store -- the paths have to be free.
        The write itself is one commit, in canonical path order, and it fails whole.
        """
        canonical = canonical_paths(raw_paths, self.project_root)
        ticket = self.sink.get(ticket_id, unique=True)
        attempt = self._owning_attempt(ticket, attempt_id, canonical)

        # The compare-and-swap token is read **before** the ownership check, never after
        # it, and that order is the guarantee rather than a detail: a commit that lands
        # between the two is then caught *either* by the check (it is seen) or by the
        # commit's revision check (it is not expected). Reading the token second would
        # leave a window in which a path claimed a moment ago looks free and is already
        # at the revision this write would accept -- two active claims on one path.
        expectations = {
            path: self.store.revision("claim", claim_id_for(attempt.workspace_id, path))
            for path in canonical
        }
        held = self._held_by_others(canonical, attempt.id)
        if held:
            return self._busy_result(ticket, attempt, canonical, held)

        versions = {path: probe(self.project_root, path) for path in canonical}
        generation = self._next_generation(attempt.id)
        records = {}
        for path in canonical:
            records[path] = FileClaim(
                id=claim_id_for(attempt.workspace_id, path),
                workspace_id=attempt.workspace_id,
                path=path,
                ticket_id=ticket.id,
                attempt_id=attempt.id,
                generation=generation,
                acquired=utc_now(),
                observed_version=versions[path].digest,
            )
        previous = self._existing_claims(records, canonical)
        now = utc_now()
        try:
            with self.store.transaction() as txn:
                # Canonical order, so two processes that want overlapping sets always
                # contend in the same sequence -- the rule that keeps an acquisition
                # loop from deadlocking itself.
                for path in canonical:
                    txn.replace_record(records[path], expect_revision=expectations[path])
                for path in canonical:
                    txn.append_event(
                        CLAIM_FILE,
                        "claim",
                        subject=path,
                        result=f"gen {generation}",
                        ticket_id=ticket.id,
                        attempt_id=attempt.id,
                        actor=attempt.worker_id,
                        payload={
                            "generation": generation,
                            "version": versions[path].digest,
                            "acquired": now,
                        },
                    )
        except Stale:
            # Somebody else's commit landed between this read and this write: the
            # compare-and-swap held, so nothing was written, and the honest answer is
            # who has it now.
            racers = self._held_by_others(canonical, attempt.id)
            if not racers:
                raise
            return self._busy_result(ticket, attempt, canonical, racers)
        return self._claimed_result(
            ticket, attempt, canonical, versions, generation, previous
        )

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    def release(self, ticket_id: str, attempt_id: str, raw_paths, reason: str) -> OperationResult:
        """Revoke this attempt's claim on each requested path, keeping the bytes.

        The attempt stays active: releasing a file is a decision about *this* file,
        not about the ticket's work. The record stays too, in its released state, so
        the path keeps a history a re-acquisition can be seen against -- and the new
        generation a re-acquisition mints is what makes any token from this one dead.
        """
        canonical = canonical_paths(raw_paths, self.project_root)
        ticket = self.sink.get(ticket_id, unique=True)
        attempt = self._owning_attempt(ticket, attempt_id, canonical)

        claims = {}
        expectations = {}
        for path in canonical:
            # The same ordering rule as acquisition, for the same reason: the revision
            # that will guard this write is read *before* the claim it describes, so a
            # re-acquisition landing in between cannot be silently released under a
            # generation number this command never saw (see `claim`).
            expectations[path] = self.store.revision(
                "claim", claim_id_for(attempt.workspace_id, path)
            )
            claim = self._record_for(attempt.workspace_id, path)
            if claim is None or claim.path != path:
                raise CoordinationError(
                    f"{path} is not claimed by attempt {attempt.id}, so there is nothing "
                    "to release ('arbite file claims' to see what is held)"
                )
            if not claim.held_by(attempt.id):
                raise CoordinationError(
                    f"{path} is held by {claim.ticket_id} / {claim.attempt_id}, not by your "
                    f"attempt {attempt.id}; only the holder releases a claim"
                )
            claims[path] = claim

        released_at = utc_now()
        with self.store.transaction() as txn:
            for path in canonical:
                claim = claims[path]
                txn.replace_record(
                    replace(
                        claim,
                        state=CLAIM_RELEASED,
                        released=released_at,
                        release_reason=reason,
                    ),
                    expect_revision=expectations[path],
                )
                txn.append_event(
                    RELEASE_FILE,
                    "claim",
                    subject=path,
                    result="released",
                    ticket_id=ticket.id,
                    attempt_id=attempt.id,
                    actor=attempt.worker_id,
                    payload={
                        "generation": claim.generation,
                        "version": claim.observed_version,
                        "reason": reason,
                    },
                )

        lines = []
        for path in canonical:
            claim = claims[path]
            lines.append(
                f"released {path} (claim generation {claim.generation} revoked; the "
                "attempt stays active)"
            )
            if claim.observed_version == ABSENT:
                lines.append(
                    f"nothing had been created at {path} under the claim, so there are no "
                    "bytes to keep"
                )
            else:
                lines.append(
                    "bytes on disk are unchanged and stay visible to the next worker "
                    f"({short_digest(claim.observed_version)})"
                )
        return succeeded(
            lines=lines,
            data={
                "ticket": ticket.id,
                "attempt": attempt.id,
                "reason": reason,
                "released": [
                    {
                        "path": path,
                        "generation": claims[path].generation,
                        "version": claims[path].observed_version,
                    }
                    for path in canonical
                ],
            },
            text_hint=RELEASE_HINT,
        )

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def claims(self, include_released: bool = False) -> OperationResult:
        """What is held right now, in canonical path order.

        The active claims by default; `include_released` adds the released records, so
        "what was held, and by whom" stays answerable from the store rather than from
        the event stream alone. Read-only: no locks, no writes."""
        workspace = self.app.derived_workspace()
        stored = [
            claim
            for claim in self.store.records("claim")
            if include_released or claim.is_active
        ]
        stored.sort(key=lambda claim: claim.path)
        active = sum(1 for claim in stored if claim.is_active)
        released = len(stored) - active
        data = {
            "workspace": workspace.id,
            "active": active,
            "released": released,
            "claims": [
                {
                    "path": claim.path,
                    "ticket": claim.ticket_id,
                    "attempt": claim.attempt_id,
                    "actor": self._actor(claim),
                    "generation": claim.generation,
                    "state": claim.state,
                    "since": claim.acquired,
                    "released": claim.released,
                    "reason": claim.release_reason,
                    "version": claim.observed_version,
                }
                for claim in stored
            ],
        }
        if not stored:
            wording = "claims" if include_released else "active claims"
            return OperationResult(
                Outcome(EMPTY), [f"no {wording} in {workspace.id}"], data, []
            )
        if include_released:
            noun = "claim" if len(stored) == 1 else "claims"
            heading = (
                f"{len(stored)} {noun} in {workspace.id} "
                f"({active} active, {released} released):"
            )
        else:
            noun = "active claim" if active == 1 else "active claims"
            heading = f"{active} {noun} in {workspace.id}:"
        rows = [heading, *self._claim_rows(stored)]
        return OperationResult(Outcome(OK), rows, data, [])

    # ------------------------------------------------------------------
    # Ownership of the attempt
    # ------------------------------------------------------------------

    def _owning_attempt(self, ticket, attempt_id: str, canonical):
        """The attempt a file operation names, verified to own the ticket.

        The ticket is read first, because the interesting refusal is the frozen FC5 one:
        a *different* attempt holds the ticket, and the caller needs the holder's id --
        not a "no such attempt" that would send it looking for the wrong problem. Only
        then is the shared guard asked (`require_attempt`), which is what refuses an
        attempt that does not exist, is no longer current, or was revoked by a takeover.
        """
        holder = self.lifecycle.active_attempt(ticket.id)
        if holder is not None and holder.id != attempt_id:
            raise NotOwner(
                f"{ticket.id} is {ticket.status} for {holder.id}; attempt {attempt_id} "
                "does not own this ticket",
                [
                    f"'arbite file claim {' '.join(canonical)} --ticket {ticket.id} "
                    f"--attempt {holder.id}'",
                    f"'arbite claim {ticket.id} --agent <your-id> --force --reason "
                    '"<why>"\' to take it over',
                ],
            )
        return self.lifecycle.require_attempt(ticket.id, attempt_id)

    # ------------------------------------------------------------------
    # The claim index
    # ------------------------------------------------------------------

    def _record_for(self, workspace_id: str, path: str) -> Optional[FileClaim]:
        """The one claim record that speaks for `path`, or None.

        A record found under this path's derived id that names a *different* path is a
        digest collision, and is refused rather than written over: clobbering it would
        silently release somebody else's file."""
        claim = self.store.find_record("claim", claim_id_for(workspace_id, path))
        if claim is not None and claim.path != path:
            raise CoordinationError(
                f"claim id {claim.id} (derived from '{path}') is already held by a claim on "
                f"'{claim.path}': the derived ids collided, so this claim cannot be recorded "
                "without releasing the other path; nothing was changed"
            )
        return claim

    def _existing_claims(self, records, canonical) -> dict:
        """The records this acquisition replaces, one per path (see `_record_for`).

        Read by the report rather than by the write: the frozen rows differ for a path
        whose released record already carried this version (FC8) against one that was
        simply free (FC1), and this is the only place that distinction is used."""
        existing = {}
        for path in canonical:
            claim = self._record_for(records[path].workspace_id, path)
            if claim is not None:
                existing[path] = claim
        return existing

    def _held_by_others(self, canonical, attempt_id: str) -> list:
        """The active claims that make this request busy, in canonical order.

        Ownership is per *attempt*, never per worker id: two attempts of one worker are
        still two owners, and treating them as one would let a stale attempt keep using
        bytes a takeover revoked."""
        wanted = {_key(path): path for path in canonical}
        held = {}
        for claim in self.store.active_claims():
            key = _key(claim.path)
            if key in wanted and not claim.held_by(attempt_id):
                held[key] = claim
        return [held[_key(path)] for path in canonical if _key(path) in held]

    def _next_generation(self, attempt_id: str) -> int:
        """The attempt's next generation: one past every claim it has ever held.

        Attempt-scoped rather than per path, which is what the frozen transcripts show
        (the same attempt's second acquisition prints generation 2 for both of its
        paths, and a re-acquisition after release prints the next one): a generation
        identifies *an acquisition*, so every claim taken together carries a number no
        earlier token of that attempt uses."""
        generations = [
            claim.generation
            for claim in self.store.records("claim")
            if claim.attempt_id == attempt_id
        ]
        return 1 + max(generations, default=0)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _busy_result(self, ticket, attempt, canonical, held) -> OperationResult:
        """The `file_busy` outcome: nothing changed, and who holds what (FC3).

        Outcome 4, and all-or-nothing: the *whole* request is listed, free paths
        included, because the caller's next decision is which paths it can work on
        instead -- and the hint offers the two moves that are honest here (plan against
        what is free, or hand the ticket back deliberately), never a retry of a busy
        path and never a shell workaround."""
        held_by_key = {_key(claim.path): claim for claim in held}
        width = max(len(path) for path in canonical) + CLAIM_COLUMN_GAP
        rows = []
        free_paths = []
        for path in canonical:
            claim = held_by_key.get(_key(path))
            if claim is None:
                rows.append(f"  {path.ljust(width)}free")
                free_paths.append(path)
                continue
            rows.append(
                f"  {path.ljust(width)}held by {claim.ticket_id} / {claim.attempt_id} "
                f"({self._actor(claim)}) since {local_time(claim.acquired)}, "
                f"gen {claim.generation}"
            )
        total = len(canonical)
        noun = "path" if total == 1 else "paths"
        verb = "is" if len(held) == 1 else "are"
        heading = (
            f"{len(held)} of {total} {noun} {verb} held; nothing was claimed (all-or-nothing)"
        )

        holder = held[0]
        release = (
            f"arbite release {ticket.id} --agent {attempt.worker_id} --reason "
            f'"needs {holder.path}"'
        )
        # The printed hint carries the explanation, the JSON carries the bare command --
        # and the frozen block joins the two at the end of the previous line, which is a
        # printing difference, not a different set of next steps (see `results.text_hint_of`).
        spoken = []
        if free_paths:
            directory = posixpath.dirname(free_paths[0]) or "."
            spoken.append(
                (f"arbite file list {directory}", f"'arbite file list {directory}' to plan against what is free")
            )
        spoken.append(
            (
                f"arbite changes {holder.ticket_id}",
                f"'arbite changes {holder.ticket_id}' to see what the holder has done",
            )
        )
        spoken.append((release, f"'{release}'"))
        data = {
            "error": "file_busy",
            "held": [self._held_entry(claim) for claim in held],
            "free": free_paths,
            "claimed": [],
            "ticket": ticket.id,
            "attempt": attempt.id,
        }
        return OperationResult(
            Outcome(BUSY, "file_busy"),
            [f"{OUTCOME_LABELS[BUSY]}: {heading}", *rows, self._requester_line(attempt)],
            data,
            [command for command, _ in spoken],
            text_hint="next: " + ", or\n      ".join(prose for _, prose in spoken),
        )

    def _claimed_result(self, ticket, attempt, canonical, versions, generation, previous):
        """What a successful acquisition prints (FC1, FC2, FC4, FC8).

        A row per path, in canonical order. The row describes the version the claim now
        owns, and prints the file's shape when arbite had to read it to establish that
        version -- a path whose released record already carried this version prints the
        version alone, because the bytes were recorded and the release line said they
        stay visible (the frozen FC8 row, against FC1's and FC2's)."""
        count = len(canonical)
        noun = "path" if count == 1 else "paths"
        lines = [
            f"claimed {count} {noun} for {ticket.id} / {attempt.id} (generation {generation}):"
        ]
        for path in canonical:
            version = versions[path]
            if version.is_absent:
                lines.append(f"  {path}   {ABSENT_NOTE}")
            elif self._reacquired(previous, path):
                lines.append(f"  {path}   {short_digest(version.digest)}")
            else:
                lines.append(
                    f"  {path}   {short_digest(version.digest)}  {version.describe()}"
                )
        if count > 1:
            lines.append(CLAIM_RESULT_ORDER_HINT)
        hint, actions, note = self._claim_hint(ticket, attempt, canonical, versions, previous, generation)
        data = {
            "ticket": ticket.id,
            "attempt": attempt.id,
            "generation": generation,
            "canonical_order": list(canonical),
            "all_or_nothing": True,
            "claimed": [
                {"path": path, "generation": generation, **versions[path].to_dict()}
                for path in canonical
            ],
        }
        if note:
            data["note"] = note
        return succeeded(lines=lines, data=data, next_actions=actions, text_hint=hint)

    def _claim_hint(self, ticket, attempt, canonical, versions, previous, generation):
        """The extra line a single-path acquisition ends with, if it has one.

        Three cases, and each says something the caller did not already have: read the
        path again under the claim before writing it (FC1, because a pre-claim read
        authorises nothing), be told that the new generation kills the tokens of the old
        one (FC8), or nothing at all -- a creation needs no read, and a multi-path claim
        already ends with the ordering rule (FC2, FC4)."""
        if len(canonical) != 1:
            return None, [], None
        path = canonical[0]
        version = versions[path]
        if version.is_absent:
            return None, [], None
        if self._reacquired(previous, path):
            note = (
                f"note: this is a new claim generation ({generation}); any token from "
                f"generation {previous[path].generation} is dead"
            )
            return note, [], note
        command = f"arbite file read {path} --ticket {ticket.id} --attempt {attempt.id}"
        hint = f"next: '{command}'\n      -- a pre-claim read does not authorize a write"
        return hint, [command], None

    def _requester_line(self, attempt) -> str:
        """Whether the refused caller still holds work, stated rather than implied."""
        held = [claim for claim in self.store.active_claims() if claim.held_by(attempt.id)]
        if not held:
            return f"your attempt {attempt.id} holds no claims and is still active"
        noun = "claim" if len(held) == 1 else "claims"
        return (
            f"your attempt {attempt.id} keeps its {len(held)} existing {noun}; nothing was "
            "claimed here"
        )

    def _claim_rows(self, claims) -> list:
        """The rows of `file claims` (FC6): path, holder, actor, generation and version.

        Laid out once for the whole table, so the columns line up whichever claims this
        store happens to hold, and two runs of the command on unchanged state print
        identical text. A released row says when it was released rather than when it was
        taken, because that is the fact that explains why the path is free."""
        widest_path = max(len(claim.path) for claim in claims) + CLAIM_COLUMN_GAP
        widest_where = max(len(_where(claim)) for claim in claims) + CLAIM_COLUMN_GAP
        widest_actor = max(len(self._actor(claim)) for claim in claims) + CLAIM_COLUMN_GAP
        rows = []
        for claim in claims:
            when = (
                f"since {local_time(claim.acquired)}"
                if claim.is_active
                else f"released {local_time(claim.released)}"
            )
            version = claim.observed_version
            rendered = ABSENT if version == ABSENT else short_digest(version)
            rows.append(
                f"  {claim.path.ljust(widest_path)}"
                f"{_where(claim).ljust(widest_where)}"
                f"{self._actor(claim).ljust(widest_actor)}"
                f"{f'gen {claim.generation}':>{CLAIM_GENERATION_WIDTH}}"
                f"  {when}  {rendered}"
            )
        return rows

    def _actor(self, claim: FileClaim) -> str:
        """The worker the claim's attempt belongs to, as reports name it.

        Read from the attempt rather than stored twice on the claim: a worker id is
        attribution, and a second copy is a second thing that can disagree."""
        attempt = self.store.get_attempt(claim.attempt_id)
        return attempt.worker_id if attempt is not None else "(unknown worker)"

    @staticmethod
    def _reacquired(previous, path: str) -> bool:
        claim = previous.get(path)
        return claim is not None and not claim.is_active

    @staticmethod
    def _held_entry(claim: FileClaim) -> dict:
        return {
            "path": claim.path,
            "ticket": claim.ticket_id,
            "attempt": claim.attempt_id,
            "generation": claim.generation,
            "since": claim.acquired,
        }


def canonical_paths(raw_paths, root) -> list:
    """Every requested path, canonicalised, deduplicated and sorted.

    Deduplicated by the *filesystem's* case rules, so `Foo.py` and `foo.py` in one
    request are one claim on a case-insensitive machine rather than two records that
    would then contend with each other."""
    paths = []
    seen = set()
    for raw in raw_paths:
        path = canonical_relative(raw, root)
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return sorted(paths)


def _where(claim: FileClaim) -> str:
    """The holder column: the ticket and the attempt that owns the claim."""
    return f"{claim.ticket_id} / {claim.attempt_id}"


def _key(path: str) -> str:
    """A path's comparison key: the case-insensitive spelling on a case-insensitive
    filesystem, the path itself on a case-sensitive one."""
    return os.path.normcase(path)
