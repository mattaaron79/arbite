"""Versioned reads: `arbite file read`, and the token a later mutation presents.

A read serves bytes and records *that they were served* -- nothing more. Three
properties follow from that and are built in here rather than left to a caller:

- **The digest is over the whole file, even for a ranged read.** A range-scoped hash
  would authorise a range-scoped lie: a writer that changed a line outside the
  returned window would still pass a range check. So the observation records the
  whole-file digest, the range is recorded beside it, and nothing in the version a
  mutation is checked against is ever narrowed.
- **A read never transfers ownership.** Reading a path another attempt holds serves
  the bytes with the holder named and mints a token that cannot authorise a write;
  `--fail-if-busy` refuses instead, so a caller that already knows it cannot use the
  bytes does not spend context on them. Reading an unclaimed path is a *pre-claim*
  observation: it is genuinely useful (it is how a caller learns the file), and it
  authorises nothing, because the observation records no claim generation.
- **The token is the observation's id.** It is recorded in the store, so the check a
  mutation makes is "is this id a live observation of this path, taken by this
  attempt, under the claim generation that still holds it" -- a question with one
  answer, asked of one record, rather than a signature arbite has to trust.

The report's wording is the frozen RD blocks': `(spent after one mutation of this
path)` names the *token class* (a mutation token is spent by one mutation of the
path), while whether a particular observation may authorise one is the claim it was
taken under -- `ReadObservation.authorizes_write`, and `token.authorizes_write` in
JSON. `(read-only)` is printed when no mutation could ever use the token, which is
the held-by-another case where a caller could otherwise be misled about using it now.
"""

from __future__ import annotations

import os
from typing import Optional

from ..errors import NotOwner, PathRefused, TicketError
from .lifecycle import local_time
from .paths import canonical_relative, missing_path_refusal, probe
from .records import (
    ABSENT,
    READ_CATEGORY,
    ReadObservation,
    new_id,
    short_digest,
    utc_now,
)
from .results import BUSY, OperationResult, Outcome, succeeded
from .scratch import printable_size

#: The event kind a served read appends, and the category that keeps it out of the
#: ordinary job view. A read is an *observation*: `arbite events` shows it only with
#: `--include-reads`, because a research-heavy agent emits dozens of them per write.
#: (A read that is part of a mutation is ordinary file activity and is `read.file` in
#: the `file` category -- that is tic-60c7's, not this slice's.)
READ_OBSERVED = "read.observed"

#: The one-line results those events carry, matching the token's class.
READ_ONLY_RESULT = "read-only"
MUTATION_RESULT = "one mutation"

#: The claim sentence a read prints. Each one is a fact the caller can act on: the
#: path is free, the caller holds it (and so the token is usable), or somebody else
#: does (and so the bytes are readable but unusable for a change). A whole-file read
#: of a free path adds the reassuring parenthetical and the workspace; a ranged read
#: says `none` and spends the line on the range and the token instead (the frozen
#: RD1 and RD3 shapes).
CLAIM_NONE = "none"
CLAIM_NONE_READABLE = "none (readable by anyone)"
CLAIM_OWN = "HELD by your attempt {ticket} / {attempt}, generation {generation}"
CLAIM_OTHER = (
    "HELD by {ticket} / {attempt} ({actor}) since {since}, generation {generation}"
)
BANNER_OWN = "this read token authorizes one mutation of this path under this claim"
BANNER_OTHER = "bytes are served, but this read token cannot authorize a write"

#: The indents the frozen RD blocks print: a banner continues under its claim line
#: (`len("claim: ")`), and a drift note continues under its own label.
CLAIM_INDENT = " " * len("claim: ")
NOTE_INDENT = " " * len("note: ")

#: The token line, by class: a mutation token is spent by one mutation of the path,
#: a read-only one can never be spent because no mutation may use it.
TOKEN_MUTATION = "read token: {id} (spent after one mutation of this path)"
TOKEN_READ_ONLY = "read token: {id} (read-only)"

#: The refusal `--fail-if-busy` raises, and the hint it prints. The hint never
#: suggests waiting or reaching around arbite: it names the two honest moves -- plan
#: against something free, or look at what is held -- and the JSON mirror carries the
#: command.
BUSY_HINT = "next: plan against an unclaimed path, or 'arbite file claims' to see what is free"
BUSY_ACTIONS = ["arbite file claims"]


class FileReads:
    """Read whole files or ranges of them, and record what was served."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read(
        self,
        raw_path,
        ticket_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        lines=None,
        fail_if_busy: bool = False,
    ) -> OperationResult:
        """Serve a path's bytes, record the observation, and report its version.

        The order is a caller's order: the path has to be one arbite will read at all
        (a refusal before anything else, which is why the frozen LS6 block needs no
        `--ticket`), then the path has to exist (RD5), then the attempt -- when one is
        named -- has to own the ticket, then the claim decides what the token is worth,
        and only then are bytes read and recorded.
        """
        path = canonical_relative(raw_path, self.project_root)
        if not (self.project_root / path).exists():
            # Before the ticket and the claim, because "there is nothing there" is a
            # fact about the path this command was asked about, and it is the answer
            # that sends a caller to the other command (`file list`, `file claim`).
            raise missing_path_refusal(
                path,
                ticket_id or "<ticket-id>",
                attempt_id or "<attempt-id>",
            )
        attempt = self._attempt(ticket_id, attempt_id, path)
        version = probe(self.project_root, path)
        claim = self._active_claim(path)
        own = claim is not None and claim.held_by(None if attempt is None else attempt.id)
        if claim is not None and not own and fail_if_busy:
            return self._busy_refusal(path, claim)

        data = (self.project_root / path).read_bytes()
        start, end = self._range(lines, version, path)
        # The drift note is computed *before* this read is recorded: "the last version
        # arbite observed" is what the store knew a moment ago, and the observation this
        # command is about to store is the version just read, which never differs from
        # itself (RD1 and RD4 differ only in whether anything was known before).
        drift = self._drift_note(path, version)
        observation = self._record(path, version, attempt, claim, start, end)
        return self._report(path, version, data, observation, claim, own, start, end, drift)

    # ------------------------------------------------------------------
    # The observation
    # ------------------------------------------------------------------

    def _record(self, path, version, attempt, claim, start, end) -> ReadObservation:
        """Persist the observation and its event, in one transaction.

        One commit, because the token and the evidence of the read are the same fact:
        a token whose event was lost would be a receipt nothing could explain."""
        workspace = self.app.derived_workspace()
        observation = ReadObservation(
            id=self._next_observation_id(),
            path=path,
            # The whole file, whatever range is served (see the module docstring).
            digest=version.digest,
            observed_at=utc_now(),
            attempt_id=None if attempt is None else attempt.id,
            actor=None if attempt is None else attempt.worker_id,
            # The generation *observed*: it is what the claim line reports, and
            # `authorizes_write` is what refuses a foreign or pre-claim observation.
            claim_generation=0 if claim is None else claim.generation,
            line_start=start,
            line_end=end,
        )
        read_only = not observation.authorizes_write(claim)
        with self.store.transaction() as txn:
            txn.put_record(observation)
            txn.append_event(
                READ_OBSERVED,
                READ_CATEGORY,
                subject=path,
                result=READ_ONLY_RESULT if read_only else MUTATION_RESULT,
                ticket_id=None if claim is None else claim.ticket_id,
                attempt_id=observation.attempt_id,
                actor=observation.actor,
                operation_id=observation.id,
                payload={
                    "digest": version.digest,
                    "claim_generation": observation.claim_generation,
                    "read_only": read_only,
                    "workspace": workspace.id,
                },
            )
        return observation

    def _next_observation_id(self) -> str:
        """A fresh `op-` id, free in both the observation and the receipt space.

        The two share the prefix on purpose (a caller holds one token handle), so
        minting one has to look at both -- a token that collided with a receipt would
        be two records claiming one id."""
        taken = {record.id for record in self.store.records("observation")}
        taken.update(record.id for record in self.store.records("receipt"))
        return new_id("observation", taken)

    # ------------------------------------------------------------------
    # The report
    # ------------------------------------------------------------------

    def _report(self, path, version, data, observation, claim, own, start, end, drift):
        """The read report: the version, the claim, any drift note, the token, bytes."""
        lines = [self._header(path, version)]
        generation = 0 if claim is None else claim.generation
        token = observation.id
        ranges = "" if start is None else f"   lines {start}-{end} of {version.lines}"
        if start is not None:
            # A ranged read packs the claim, the range and the token; RD3 is the frozen
            # shape, and the capability is in JSON (`token.authorizes_write`).
            lines.append(
                f"claim: {self._claim_brief(claim, own, generation, ranged=True)}{ranges}   "
                f"read token: {token}"
            )
            if claim is not None and not own:
                lines.append(f"{CLAIM_INDENT}{BANNER_OTHER}")
        else:
            lines.append(self._claim_line(claim, own, generation))
            if claim is not None and not own:
                lines.append(f"{CLAIM_INDENT}{BANNER_OTHER}")
            elif own:
                lines.append(f"{CLAIM_INDENT}{BANNER_OWN}")
        if drift:
            lines.append(drift)
        if start is None:
            lines.append(
                (TOKEN_READ_ONLY if claim is not None and not own else TOKEN_MUTATION).format(
                    id=token
                )
            )
        lines.append("---")
        lines.extend(self._content(data, version, start, end))
        return succeeded(
            lines=lines,
            data={
                "path": path,
                "digest": version.digest,
                "bytes": version.size,
                "lines": version.lines,
                "range": None if start is None else {"start": start, "end": end},
                "workspace": self.app.derived_workspace().id,
                "claim": None
                if claim is None
                else {
                    "ticket": claim.ticket_id,
                    "attempt": claim.attempt_id,
                    "generation": claim.generation,
                    "held_by_you": own,
                },
                "token": {
                    "id": token,
                    "authorizes_write": observation.authorizes_write(claim),
                    "spent_by": "one mutation of this path",
                },
                "note": drift and drift.splitlines()[0][len("note: ") :],
            },
        )

    @staticmethod
    def _header(path, version) -> str:
        """`path  sha256:...  570 lines  26.4 KiB` -- the whole version, one line."""
        if version.lines is None:
            shape = f"{printable_size(version.size)} (binary)"
        else:
            noun = "line" if version.lines == 1 else "lines"
            shape = f"{version.lines} {noun}  {printable_size(version.size)}"
        return f"{path}  {short_digest(version.digest)}  {shape}"

    def _claim_line(self, claim, own, generation) -> str:
        """A whole-file read's claim line, naming the workspace when it is the reader's.

        A path somebody else holds prints the holder and nothing else (the frozen RD2
        row): the workspace is not a fact the reader can act on there, and the line
        already ends with the generation it cannot use. A free or own path prints it,
        because "which workspace was this read in" is part of what the token means."""
        brief = self._claim_brief(claim, own, generation)
        if claim is not None and not own:
            return f"claim: {brief}"
        return f"claim: {brief}   workspace: {self._workspace_id()}"

    def _claim_brief(self, claim, own, generation, ranged: bool = False) -> str:
        if claim is None:
            return CLAIM_NONE if ranged else CLAIM_NONE_READABLE
        if own:
            return CLAIM_OWN.format(
                ticket=claim.ticket_id, attempt=claim.attempt_id, generation=generation
            )
        return CLAIM_OTHER.format(
            ticket=claim.ticket_id,
            attempt=claim.attempt_id,
            actor=self._actor(claim),
            since=local_time(claim.acquired),
            generation=generation,
        )

    def _actor(self, claim) -> str:
        """The worker a claim's attempt belongs to, as reports name it.

        Read from the attempt rather than stored twice on the claim: a worker id is
        attribution, and a second copy is a second thing that can disagree."""
        attempt = self.store.get_attempt(claim.attempt_id)
        return attempt.worker_id if attempt is not None else "(unknown worker)"

    def _drift_note(self, path, version) -> str:
        """The note an unattributed external edit earns, or ''.

        "The last version arbite observed" is what the store already knows about this
        path: the newest earlier observation of it, or -- when nothing has been read
        yet -- the version the claim was taken against. A path arbite has never seen
        (RD1's first read) has nothing to differ from, so it earns no note.
        """
        previous = self._last_observed(path)
        if previous is None or previous == ABSENT or previous == version.digest:
            return ""
        return (
            f"note: on-disk bytes differ from the last version arbite observed "
            f"({short_digest(previous)});\n"
            f"{NOTE_INDENT}an external edit is attributable to no ticket"
        )

    def _last_observed(self, path: str) -> Optional[str]:
        """The most recent version the store holds for a path, or None."""
        candidates = [
            (observation.observed_at, observation.id, observation.digest)
            for observation in self.store.records("observation")
            if observation.path == path
        ]
        claim = self._claim_record(path)
        if claim is not None:
            candidates.append((claim.acquired, claim.id, claim.observed_version))
        if not candidates:
            return None
        return max(candidates)[2]

    @staticmethod
    def _content(data, version, start, end) -> list:
        """The bytes as rows: `1234 | text`, numbered as the file numbers them."""
        if version.lines is None:
            return [
                f"(binary file, {printable_size(version.size)}: the digest above is the "
                "version; the bytes are not printed as text)"
            ]
        lines = data.decode("utf-8").splitlines()
        first = 1 if start is None else start
        last = len(lines) if end is None else min(end, len(lines))
        return [f"{number} | {lines[number - 1]}" for number in range(first, last + 1)]

    def _range(self, lines, version, path):
        """`--lines START[:END]` as a served range, or `(None, None)` for the file."""
        if lines is None:
            return None, None
        text = str(lines).strip()
        head, _, tail = text.partition(":")
        if not head.isdigit() or (tail and not tail.isdigit()):
            raise PathRefused(
                f"--lines expects START or START:END, got '{text}' "
                "(e.g. '--lines 1254:1260')"
            )
        start = int(head)
        end = int(tail) if tail else start
        if start < 1 or end < start:
            raise PathRefused(
                f"--lines {text} is not a range: lines are numbered from 1 and a range "
                "may not end before it starts"
            )
        if version.lines is None:
            raise PathRefused(
                f"--lines cannot be used on '{path}': it is not UTF-8 text, so it has no "
                "line numbering to address"
            )
        if start > version.lines:
            raise PathRefused(
                f"--lines {text} starts past the end of '{path}' ({version.lines} lines)",
                [f"'arbite file read {path}' to read the whole file"],
            )
        return start, min(end, version.lines)

    # ------------------------------------------------------------------
    # The claim questions
    # ------------------------------------------------------------------

    def _active_claim(self, path: str):
        claims = self.store.active_claims()
        for claim in claims:
            if _key(claim.path) == _key(path):
                return claim
        return None

    def _claim_record(self, path: str):
        claims = [claim for claim in self.store.records("claim") if _key(claim.path) == _key(path)]
        return claims[0] if claims else None

    def _attempt(self, ticket_id, attempt_id, path):
        """The attempt a read is attributed to, or None for an unattributed read.

        A read may name no attempt at all -- attribution is optional, and the frozen
        LS6 block reads a path with neither flag -- but naming one has to be as
        honest as a claim does it: the ticket exists, and the attempt owns it. Both
        flags go together, because attribution with half the pair is a mistake, not
        an answer.
        """
        if not ticket_id and not attempt_id:
            return None
        if not (ticket_id and attempt_id):
            raise TicketError(
                "--ticket and --attempt go together: an observation is attributed to an "
                "attempt of a ticket, or to nobody at all"
            )
        ticket = self.sink.get(ticket_id, unique=True)
        holder = self.lifecycle.active_attempt(ticket.id)
        if holder is not None and holder.id != attempt_id:
            raise NotOwner(
                f"{ticket.id} is {ticket.status} for {holder.id}; attempt {attempt_id} "
                "does not own this ticket",
                [
                    f"'arbite file read {path} --ticket {ticket.id} --attempt {holder.id}'",
                    f"'arbite claim {ticket.id} --agent <your-id> --force --reason "
                    '"<why>"\' to take it over',
                ],
            )
        return self.lifecycle.require_attempt(ticket.id, attempt_id)

    def _busy_refusal(self, path, claim) -> OperationResult:
        """`--fail-if-busy`: no bytes served, and the holder named (frozen RD2)."""
        return OperationResult(
            Outcome(BUSY, "file_busy_read"),
            [
                f"busy: {path} is held by {claim.ticket_id} / {claim.attempt_id} "
                f"({self._actor(claim)}) since {local_time(claim.acquired)}",
                "no bytes were served",
            ],
            {
                "error": "file_busy",
                "path": path,
                "held": {
                    "ticket": claim.ticket_id,
                    "attempt": claim.attempt_id,
                    "actor": self._actor(claim),
                    "generation": claim.generation,
                    "since": claim.acquired,
                },
            },
            BUSY_ACTIONS,
            text_hint=BUSY_HINT,
        )

    def _workspace_id(self) -> str:
        return self.app.derived_workspace().id


def _key(path: str) -> str:
    """A path's comparison key: the case-insensitive spelling on a case-insensitive
    filesystem, the path itself on a case-sensitive one."""
    return os.path.normcase(path)
