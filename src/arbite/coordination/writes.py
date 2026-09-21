"""Whole-file writes and exact edit batches: `arbite file write`, `arbite file edit`.

Both commands are thin, and deliberately: the engine (`coordination.mutations`) verifies
the ticket, the attempt, the claim, the read token and the bytes, archives the evidence,
replaces the file once and finalises the receipt. What lives here is what a *command*
adds -- the target's policy check, the payload it read, the report, and the two refusals
that belong to the command's own input (a path that is generated output, a batch that
does not apply).

The three report shapes are the frozen transcripts', implemented literally because
`tests/test_write_examples.py` asserts them byte for byte:

- **a whole-file write of an existing text file** reports the line delta, the receipt
  (naming the version it replaced) and, when the payload came from scratch, the payload
  line, and it ends by naming the fresh read the token it just spent requires (WR1).
- **a binary write** and **a creation** print the two lines that describe the change and
  stop: there is no line diff to re-apply and no line-based continuation to offer
  (WR6, SC3).
- **an edit** reports each replacement by the line it was found on -- the line numbers
  are the ones the caller read, because the batch is matched against the read version --
  folds the consumed payload into its receipt line, and stops (ED1, ED3).

Every refusal happens before the engine stages anything, and says **no bytes were
changed**: a caller that cannot tell "refused" from "may have written half a file" is
never put in that position.

The few helpers a *command* needs rather than the engine -- the canonical mutation target,
the probe a mutation records for itself under its claim, the artifact read-back a report
renders, and the refusals for a change that names no ticket, no attempt or no read token --
live at module level here, because the removal and rename commands (`coordination.moves`)
need exactly the same ones: a second copy would be a second answer to "which path is this,
and what authorises a change".
"""

from __future__ import annotations

import difflib
from pathlib import Path

from ..errors import PathRefused
from . import edits as edit_batches
from .mutations import (
    FileMutations,
    MutationOutcome,
    MutationRequest,
    PathChange,
    REASON_ATTEMPT_NOT_CURRENT,
    REASON_NO_CLAIM,
    REASON_STALE_TOKEN_SPENT,
    REASON_STALE_VERSION,
    read_command,
)
from ..errors import UsageRefused
from .paths import Version, canonical_relative, probe, refuse_if_excluded, version_of
from .records import (
    ABSENT,
    ReadObservation,
    digest_bytes,
    new_id,
    short_digest,
    utc_now,
)
from .results import OperationResult, REFUSAL_INDENT, register_next_actions, succeeded
from .scratch import EDITS_SHAPE, Payload, WRITE_SHAPE, printable_size

#: The event a mutation's own read appends: `read.file` in the `file` category, which is the
#: kind `coordination.reads` documents as this slice's -- a read that is part of a change is
#: ordinary file activity, not an observation somebody else asked for.
READ_FILE = "read.file"
READ_FILE_CATEGORY = "file"
MUTATION_RESULT = "one mutation"

#: The report's first line, by verb, and the shapes it can carry: a text file that
#: replaced another (`570 -> 588 lines  +18 -0`), bytes (`1024 -> 1187 bytes (binary)`)
#: or a creation, which has no before to name (`created, 84 lines`).
WROTE = "wrote {path}  {digest}  {shape}"
EDITED = "edited {path}  {digest}  {shape}"
TEXT_SHAPE = "{before} -> {after} lines  +{added} -{removed}"
BINARY_SHAPE = "{before} -> {after} bytes (binary)"
CREATED_TEXT = "created, {lines} lines"
CREATED_BINARY = "created, {size} (binary)"

#: The receipt lines. A text write names the version it replaced; a write of bytes arbite
#: cannot render as text says so instead, because a receipt holding a byte payload is not
#: a text diff and printing it as one would claim evidence the receipt does not hold. A
#: creation has no before to name, and an edit names only the generation, which is the
#: shape the frozen ED1 and ED3 blocks print.
RECEIPT_WRITE = "receipt: {receipt} · {ticket} / {attempt} · claim gen {generation} · before {before}"
RECEIPT_BINARY = (
    "receipt: {receipt} · {ticket} / {attempt} · claim gen {generation} "
    "(binary receipt holds a byte payload, not a text diff)"
)
RECEIPT_CREATED = "receipt: {receipt} · {ticket} / {attempt} · claim gen {generation}"
RECEIPT_EDITED = "receipt: {receipt} · claim gen {generation}"
RECEIPT_PAYLOAD = " · payload {display} consumed and cleared"

#: The success hint a whole-file text write ends with: the token it just spent, and the
#: command that takes a fresh one. A caller that wants to keep going needs exactly that,
#: and it is the sentence the frozen WR1 block prints.
SPENT_NOTE = "the read token {token} is spent -- '{command}' before another change"

#: The hints the reason table falls back to when a refusal is raised without one built
#: from the state (the engine's own refusals carry the concrete command).
register_next_actions(
    REASON_NO_CLAIM,
    ["'arbite file claim <path>' to take the path, then read it again under that claim"],
)
register_next_actions(
    REASON_STALE_VERSION,
    ["'arbite file read <path>' to re-read the version that is there now"],
)
register_next_actions(
    REASON_STALE_TOKEN_SPENT,
    ["'arbite file read <path>' for a fresh token"],
)
register_next_actions(
    REASON_ATTEMPT_NOT_CURRENT,
    ["'arbite show <id>' to read the ticket's state, then reopen it or stop"],
)


def mutation_target(raw_path, root) -> str:
    """The canonical path a mutation names, or the refusal that stops it.

    Two rules, both before anything is read: the path has to be one arbite manages at
    all, and it may not be generated or build output -- a proxy write whose evidence
    could not be attributed to source is refused early rather than recorded late (BY2)."""
    path = canonical_relative(raw_path, root)
    refuse_if_excluded(path)
    return path


def active_claim(store, path: str):
    """The active claim on `path`, or None."""
    claims = store.claims_for_path(path)
    return claims[0] if claims else None


def next_observation_id(store) -> str:
    """A fresh `op-` id, free in both the observation and the receipt space.

    The two share the prefix on purpose (a caller holds one handle), so minting one has
    to look at both -- a token that collided with a receipt would be two records claiming
    one id."""
    taken = {record.id for record in store.records("observation")}
    taken.update(record.id for record in store.records("receipt"))
    return new_id("observation", taken)


def evidence(store, digest: str) -> tuple:
    """`(version, bytes)` for a version a receipt recorded.

    Read back from the artifact store rather than from disk: a report describes what
    the operation *recorded*, which is what makes its sentence reproducible from the
    receipt alone -- and checkable by a reviewer after the file has moved on."""
    if digest == ABSENT:
        return Version(ABSENT), b""
    data = store.get_artifact_bytes(digest)
    return version_of(data, digest), data


def version_facts(version: Version) -> dict:
    """A version as JSON reports one: the same keys `file read --json` prints."""
    return {"digest": version.digest, "bytes": version.size, "lines": version.lines}


def require_mutation_context(ticket_id, attempt_id) -> None:
    """Refuse a mutation that names no ticket, or no attempt (the frozen WR7 rule).

    An *outcome* rather than an argparse error, because the fix is a command to run: the
    refusal names the command that supplies the missing half, and the exit code says "fix
    the command" (1) rather than "nothing matched" (the argparse exit 2 would mean).

    The read token is a separate check (`require_read_token`), because whether one is
    required depends on the path: existing bytes need the read that authorises changing
    them, while a path that is not there is created from a probe the command records itself.
    """
    if not ticket_id:
        command = "arbite list next --claim <agent-id>"
        raise UsageRefused(
            "--ticket is required: every mutation is recorded against a ticket and an attempt",
            [command],
            text_hint=(
                f"next: '{command}' to take workable work, or name the ticket this change "
                "belongs to"
            ),
        )
    if not attempt_id:
        command = f"arbite claim {ticket_id} --agent <your-id>"
        raise UsageRefused(
            "--attempt is required: every mutation is attributed to a work attempt",
            [command],
            text_hint=f"next: '{command}' to start one",
        )


def require_read_token(path, ticket_id, attempt_id) -> None:
    """Refuse a change to existing bytes that names no read token.

    WR7's rule one argument further on: the claim says whose the path is, and the read
    taken under it is what authorises the change, so a caller that presents neither gets
    the command that takes one rather than a stale outcome it cannot act on."""
    command = read_command(ticket_id, attempt_id, path)
    raise UsageRefused(
        "--read-token is required: a claim authorises ownership, and the read taken "
        "under it authorises the change",
        [command],
        text_hint=f"next: '{command}' to take one, then retry",
    )


def record_claim_probe(
    store, workspace_id, path, ticket_id, attempt_id, actor, claim_generation, digest
) -> ReadObservation:
    """Record the observation a mutation takes for itself, under the claim it holds.

    A path that is not there cannot be read -- `file read` refuses it, because there are
    no bytes to serve -- so a creation's probe is recorded here instead: the same
    observation a read would have written, with `absent` as its version and the claim
    generation it was taken under. That is the "read/probe receipt for an absent path"
    the plan gives creations, and the write spends it exactly like any other token.

    A rename's destination is the same shape one step on: the version the caller *stated*
    for it (its absence, or the digest it gave as the one it expects to replace) is
    recorded as the observation the engine checks against the bytes, exactly as a read
    token's observed version is checked. Ownership is the claim's either way."""
    observation = ReadObservation(
        id=next_observation_id(store),
        path=path,
        digest=digest,
        observed_at=utc_now(),
        attempt_id=attempt_id,
        actor=actor,
        claim_generation=claim_generation,
    )
    with store.transaction() as txn:
        txn.put_record(observation)
        txn.append_event(
            READ_FILE,
            READ_FILE_CATEGORY,
            subject=path,
            result=MUTATION_RESULT,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            actor=actor,
            operation_id=observation.id,
            payload={
                "digest": digest,
                "claim_generation": claim_generation,
                "read_only": False,
                "workspace": workspace_id,
            },
        )
    return observation


class FileWrites:
    """Whole-file writes and exact edit batches, for one sink and one store."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root
        self.mutations = FileMutations(sink, lifecycle)

    # ------------------------------------------------------------------
    # Whole-file writes
    # ------------------------------------------------------------------

    def write(
        self,
        raw_path,
        ticket_id: str,
        attempt_id: str,
        read_token: str,
        payload: Payload,
    ) -> OperationResult:
        """Replace a path's bytes with the payload's, if the token still authorises it.

        Writing a path that does not exist is the *creation* case, and it needs the same
        two things: the claim, and a read (a probe, there) taken under it that found the
        path absent. Nothing else distinguishes it -- the evidence records `absent` as the
        version the write replaced."""
        path = mutation_target(raw_path, self.project_root)
        before = probe(self.project_root, path)
        token = self._write_token(path, ticket_id, attempt_id, read_token, before)
        outcome = self._apply("write", path, ticket_id, attempt_id, token, before, payload.data)
        return self._write_report(path, ticket_id, attempt_id, token, outcome, payload)

    def _write_token(self, path, ticket_id, attempt_id, read_token, before):
        """The token this write presents, which a creation has to take for itself.

        A change to bytes that exist is authorised by a read of them, so the caller's
        `--read-token` is required and is what the engine checks. A path that is *not* there
        cannot be read -- `file read` refuses it, because there are no bytes to serve -- so a
        creation's probe is recorded here instead: the same observation a read would have
        written, with `absent` as its version and the claim generation it was taken under.
        That is the "read/probe receipt for an absent path" the plan gives creations, and the
        write spends it exactly like any other token.

        A token the caller passes for an absent path is honoured when it *is* such a probe
        (a retry spends the handle it was given); otherwise the probe is taken now, and the
        report names the token that was used, so nothing about which handle paid is implicit."""
        if not before.is_absent:
            if not read_token:
                require_read_token(path, ticket_id, attempt_id)
            return read_token
        claim = active_claim(self.store, path)
        if claim is None or not claim.held_by(attempt_id):
            # No probe of the caller's own to record: the refusal is the engine's (WR4),
            # which names the claim to take.
            return read_token
        presented = None if not read_token else self.store.find_record("observation", read_token)
        if (
            presented is not None
            and presented.digest == ABSENT
            and presented.attempt_id == attempt_id
            and presented.claim_generation == claim.generation
        ):
            return read_token
        attempt = self.store.get_attempt(attempt_id)
        observation = record_claim_probe(
            self.store,
            self.app.derived_workspace().id,
            path,
            ticket_id,
            attempt_id,
            None if attempt is None else attempt.worker_id,
            claim.generation,
            ABSENT,
        )
        return observation.id

    def _write_report(
        self, path, ticket_id, attempt_id, read_token, outcome, payload
    ) -> OperationResult:
        """The frozen shape for a whole-file write, assembled from what was recorded."""
        receipt = outcome.receipt
        before, before_bytes = evidence(self.store, receipt.before[path])
        after, after_bytes = evidence(self.store, receipt.after[path])
        created = before.is_absent
        binary = before.lines is None or after.lines is None

        if created:
            shape = (
                CREATED_BINARY.format(size=printable_size(after.size))
                if binary
                else CREATED_TEXT.format(lines=after.lines)
            )
        elif binary:
            shape = BINARY_SHAPE.format(before=before.size, after=after.size)
        else:
            added, removed = line_delta(before_bytes, after_bytes)
            shape = TEXT_SHAPE.format(
                before=before.lines, after=after.lines, added=added, removed=removed
            )

        generation = receipt.claim_generation
        lines = []
        if payload.from_stdin:
            # A piped write says so first, with the shape of what arrived: for a caller
            # that never staged a file, this line is the only confirmation of what the
            # command actually received.
            lines.append(payload.stdin_note(_stdin_shape(payload.data)))
        lines.append(WROTE.format(path=path, digest=short_digest(after.digest), shape=shape))
        if created:
            lines.append(
                RECEIPT_CREATED.format(
                    receipt=receipt.id,
                    ticket=ticket_id,
                    attempt=attempt_id,
                    generation=generation,
                )
            )
        elif binary:
            lines.append(
                RECEIPT_BINARY.format(
                    receipt=receipt.id,
                    ticket=ticket_id,
                    attempt=attempt_id,
                    generation=generation,
                )
            )
        else:
            lines.append(
                RECEIPT_WRITE.format(
                    receipt=receipt.id,
                    ticket=ticket_id,
                    attempt=attempt_id,
                    generation=generation,
                    before=short_digest(before.digest),
                )
            )

        consumed = payload.consume()
        if not created and not binary and consumed:
            lines.append(payload.consume_note())

        data = self._payload(
            path, receipt, before, after, payload, read_token, consumed=consumed
        )
        if created or binary:
            # Nothing to re-apply line by line, so no line-based continuation: the two
            # lines that describe the change are the whole report (WR6, SC3).
            return succeeded(lines=lines, data=data)

        command = read_command(ticket_id, attempt_id, path)
        return succeeded(
            lines=lines,
            data=data,
            next_actions=[command],
            text_hint=f"next: {SPENT_NOTE.format(token=read_token, command=command)}",
        )

    # ------------------------------------------------------------------
    # Exact edits
    # ------------------------------------------------------------------

    def edit(
        self,
        raw_path,
        ticket_id: str,
        attempt_id: str,
        read_token: str,
        payload: Payload,
        flag: str = "--edits",
    ) -> OperationResult:
        """Apply an exact-substitution batch to a text file, or change nothing at all.

        The batch is parsed and matched *before* the mutation is attempted, against the
        bytes the read token served: a batch that cannot select one place per edit is
        refused as bad input, naming the lines the caller has to look at, and the engine
        is never reached. That ordering is what makes "no partial write" a property of the
        shape rather than a cleanup path -- the new text is assembled in memory and handed
        over as one version, replaced once."""
        path = mutation_target(raw_path, self.project_root)
        before = probe(self.project_root, path)
        if before.is_absent:
            raise PathRefused(
                f"no such path '{path}'; `file edit` changes text that exists",
                text_hint=(
                    f"next: 'arbite file claim {path} --ticket {ticket_id} --attempt "
                    f"{attempt_id}' then 'arbite file write {path} ...' to create it"
                ),
            )
        if before.lines is None:
            raise PathRefused(
                f"'{path}' is not UTF-8 text, so it has no lines to edit;\n"
                f"{REFUSAL_INDENT}`file write` sends bytes, and `file read` shows the digest"
            )
        if not read_token:
            require_read_token(path, ticket_id, attempt_id)
        substitutions = edit_batches.parse_batch(payload.data, flag)
        before_bytes = (Path(self.project_root) / path).read_bytes()
        batch = edit_batches.apply_batch(
            before_bytes.decode("utf-8"),
            substitutions,
            path,
            read_command(ticket_id, attempt_id, path),
        )
        after_bytes = batch.text.encode("utf-8")

        outcome = self._apply(
            "edit", path, ticket_id, attempt_id, read_token, before, after_bytes
        )
        return self._edit_report(
            path, attempt_id, read_token, outcome, payload, batch, before
        )

    def _edit_report(
        self, path, attempt_id, read_token, outcome, payload, batch, before
    ) -> OperationResult:
        """The frozen shape for an edit: the delta, one row per replacement, the receipt."""
        receipt = outcome.receipt
        after, after_bytes = evidence(self.store, receipt.after[path])
        _, before_bytes = evidence(self.store, receipt.before[path])
        added, removed = line_delta(before_bytes, after_bytes)

        lines = []
        if payload.from_stdin:
            # The one payload that leaves nothing behind says where the bytes came from,
            # which is how a caller can tell a piped batch from one it forgot to pipe.
            lines.append(payload.stdin_note(EDITS_SHAPE.format(edits=len(batch.applied))))
        lines.append(
            EDITED.format(
                path=path,
                digest=short_digest(after.digest),
                shape=TEXT_SHAPE.format(
                    before=before.lines, after=after.lines, added=added, removed=removed
                ),
            )
        )
        lines.extend(edit.row() for edit in batch.applied)
        receipt_line = RECEIPT_EDITED.format(
            receipt=receipt.id, generation=receipt.claim_generation
        )
        consumed = payload.consume()
        if consumed:
            receipt_line += RECEIPT_PAYLOAD.format(display=payload.display)
        lines.append(receipt_line)

        data = self._payload(
            path, receipt, before, after, payload, read_token, consumed=consumed
        )
        data["edits"] = [_edit_data(edit) for edit in batch.applied]
        return succeeded(lines=lines, data=data)

    # ------------------------------------------------------------------
    # The operation, and its evidence
    # ------------------------------------------------------------------

    def _apply(
        self, kind, path, ticket_id, attempt_id, read_token, before, after_bytes
    ) -> MutationOutcome:
        """Hand the operation to the engine, with the version the token observed.

        `expect` is the version the *token* observed, not the version just probed: those
        differ exactly when the file moved after the read, and that is the case the engine
        reports with both digests. The engine re-probes under the operation lock, so a
        change between this line and the write is caught rather than overlooked."""
        observation = self.store.find_record("observation", read_token)
        # A token that is not in this store is the engine's refusal to make ("does not
        # exist in this store"), so the probed version stands in as the expected one and
        # is never compared.
        expect = observation.digest if observation is not None else before.digest
        attempt = self.store.get_attempt(attempt_id)
        return self.mutations.apply(
            MutationRequest(
                kind=kind,
                ticket_id=ticket_id,
                attempt_id=attempt_id,
                # Attribution, not authentication: the attempt's worker when the store
                # knows it, and the attempt id the caller claimed otherwise -- an
                # operation in that case is refused, so no receipt is ever written.
                actor=attempt.worker_id if attempt is not None else attempt_id,
                changes=(
                    PathChange(
                        path=path,
                        expect=expect,
                        becomes=digest_bytes(after_bytes),
                        payload=after_bytes,
                        token=read_token,
                    ),
                ),
            )
        )

    def _payload(
        self,
        path: str,
        receipt,
        before: Version,
        after: Version,
        payload: Payload,
        token: str,
        consumed: bool = False,
    ) -> dict:
        """The `--json` facts of a mutation: the same story the text tells.

        The token is the id the caller presented, and `spent_by` is the operation that
        used it -- the two halves of "one token authorises one mutation", which the store
        holds and the report mirrors."""
        return {
            "path": path,
            "receipt": receipt.id,
            "ticket": receipt.ticket_id,
            "attempt": receipt.attempt_id,
            "claim_generation": receipt.claim_generation,
            "before": None if before.is_absent else version_facts(before),
            "after": version_facts(after),
            "created": before.is_absent,
            "binary": after.lines is None,
            "token": {"id": token, "spent_by": receipt.id},
            "payload": None
            if payload.from_stdin
            else {
                "name": payload.name,
                "source": payload.display,
                "consumed": consumed,
            },
        }


def _stdin_shape(data: bytes) -> str:
    """How a piped write's payload is described: `84 lines, 2.3 KiB`, or the size alone.

    The counts come from the bytes that arrived rather than from the version they became,
    because the line is about what the command read."""
    version = version_of(data)
    if version.lines is None:
        return f"{printable_size(version.size)} (binary)"
    return WRITE_SHAPE.format(lines=version.lines, size=printable_size(version.size))


def _edit_data(edit) -> dict:
    """One applied replacement as JSON: the row the text prints, as fields."""
    return {
        "index": edit.index,
        "total": edit.total,
        "line": edit.line,
        "old": edit.old,
        "new": edit.new,
    }


def line_delta(before: bytes, after: bytes) -> tuple:
    """`(added, removed)`: the line diff between two recorded versions.

    `+18 -0` for a file that grew by eighteen lines, `+2 -2` for two wordings that changed
    in place -- computed from the bytes the receipt holds rather than from the edits that
    were requested, because the number has to describe the change that happened. A
    replacement counts on both sides, which is what makes a one-line change visible as
    `+1 -1` instead of as nothing at all."""
    before_lines = before.decode("utf-8").splitlines()
    after_lines = after.decode("utf-8").splitlines()
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines)
    for tag, start, end, new_start, new_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += end - start
        if tag in ("replace", "insert"):
            added += new_end - new_start
    return added, removed
