"""Removal and rename: `arbite file remove` and `arbite file rename`.

The engine (`coordination.mutations`) performs both operations, and the evidence is what it
already keeps: a receipt naming every path with the version before and after it, and the
bytes themselves, content-addressed. What a *command* adds is what the frozen RN blocks pin
-- the path rules, the destination's version rule, the claim ownership each path needs, the
ownership move a rename makes, and the report.

Three rules are the whole of this slice, and each is a refusal rather than a repair:

- **A rename needs both paths.** Source and destination have to be claimed by this attempt
  in *one* acquisition, which is what gives them one generation and what makes one read
  token authorise the whole move. A destination that is not held at all is refused with the
  command that claims both together, because acquiring them separately is precisely what
  cannot work; two generations are refused as stale, because the paths then did not change
  hands together.
- **A destination that exists needs its version.** Moving onto bytes somebody else might
  have written is deliberate or it does not happen: without `--expect-dest` the refusal
  names the version that is there, and a version that no longer matches the path is stale.
  Both change nothing. The comparison accepts what the text prints (a 12-character digest
  prefix), because a hint whose command cannot be pasted back is not a hint.
- **No recursive deletion.** A directory is refused with the manual alternative stated (the
  frozen RN4 block), and no mutation creates a directory: a destination whose parent is not
  there is refused by the path rules rather than by a surprising `mkdir`, so a create or a
  rename can only ever land inside directories the root already has.

A removal is one operation and nothing else: the bytes go into the receipt, and the claim on
the path stays active -- a path with no bytes is the state FC4 claims and creations are
authorised from, so the same attempt can create it again from a fresh probe, and
`file release` is how it gives the path up. Both commands then reconcile the ownership
records with what their paths hold, inside the same critical section as the operation: a
removal's claim records `absent`, and a rename ends the source's claim (the record keeps the
history, the release event and the receipt keep the version that moved) while the
destination's records the version it now owns. "Ownership moves to the destination" needs no
more than that, because the destination was claimed already -- a rename onto a path this
attempt does not hold is refused -- and writing it as one commit is what stops another
arbite process from seeing the bytes moved with the source still owned, or the reverse.
"""

from __future__ import annotations

import os
from dataclasses import replace

from ..errors import (
    ArbiteError,
    Busy,
    CoordinationError,
    NoClaim,
    PathRefused,
    Stale,
    UsageRefused,
)
from .claims import RELEASE_FILE
from .mutations import (
    FileMutations,
    MutationRequest,
    NO_BYTES_CHANGED,
    PathChange,
    REASON_NO_CLAIM,
    REASON_STALE_TOKEN_SPENT,
    REASON_STALE_VERSION,
    read_command,
    spent_message,
)
from .paths import canonical_relative, modified_clock, probe, refuse_if_excluded
from .records import ABSENT, CLAIM_RELEASED, short_digest, utc_now
from .results import REFUSAL_INDENT, OperationResult, succeeded
from .writes import (
    active_claim,
    evidence,
    mutation_target,
    record_claim_probe,
    require_mutation_context,
    require_read_token,
    version_facts,
)

#: The success lines. RN1 and RN3 are frozen and asserted byte for byte by
#: `tests/test_move_examples.py`: a rename names both paths, where the claim went and the
#: receipt; a removal names the version that went and where its bytes are kept. A rename
#: that replaced a destination adds one line first -- the version it replaced is the
#: difference between a move onto a free name and an overwrite, and RN1 has none.
REMOVED = "removed {path} (was {digest}, {shape}; bytes kept in receipt {receipt})"
RENAMED = "renamed {source} -> {dest}  {digest}"
REPLACED = "replaced {dest}  was {digest}  {shape}"
CLAIM_MOVED = "claim: moved to {dest} (generation {generation}); {source} released"
RECEIPT_MUTATION = "receipt: {receipt} · {ticket} / {attempt}"

#: RN4's refusal: the directory rule and the manual alternative, in full. Deleting a tree is
#: the one thing this slice deliberately does not do, so the sentence names what to do
#: instead -- `rmdir` is not arbite's, and saying so is more honest than implying the command
#: covers it.
DIRECTORY_REFUSAL = "'{path}' is a directory; recursive deletion is not supported"
DIRECTORY_HINT = "next: remove the files individually ('{list_command}'), then 'rmdir {path}'"

#: RN2's refusal: a destination that exists is replaced deliberately or not at all, and the
#: version to name is the one that is there -- printed in the short form every report uses.
DESTINATION_EXISTS = (
    "destination {dest} exists ({digest}) and no destination version was given"
)
DESTINATION_HINT = (
    "next: re-run with '--expect-dest {digest}' to replace it deliberately, or pick "
    "another name"
)

#: What `--expect-dest` accepts: a whole digest or the 12-character prefix a report prints.
#: Both, because the refusals above hand back the short spelling and a caller has to be able
#: to paste its own hint back in.
EXPECT_DEST_PREFIX = "sha256:"
EXPECT_DEST_SHORTEST = 12
EXPECT_DEST_HEX = "0123456789abcdef"
EXPECT_DEST_REFUSAL = (
    "--expect-dest takes a whole-file digest, either full ('sha256:<64 hex>') or the "
    "12-character prefix a report prints; got '{value}'"
)

#: The claim states a move leaves behind, spelled where they are decided: the source's claim
#: is released under this reason, so "where did these bytes go" is answerable from the record.
MOVE_REASON = "renamed to {dest}"


class FileMoves:
    """Removal and rename, for one ticket sink and one coordination store."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root
        self.mutations = FileMutations(sink, lifecycle)

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    def remove(
        self, raw_path, ticket_id: str = None, attempt_id: str = None, read_token: str = None
    ) -> OperationResult:
        """Remove one file's bytes, keeping them and their version in the receipt.

        The order of the checks is the order a caller can act on them. The directory
        refusal comes first of all -- it is a fact about *the path this command was asked
        about*, and the frozen RN4 block names neither a ticket nor an attempt and still
        gets that answer -- then the path policy, then the attribution a mutation needs,
        then the bytes, and finally the read that authorises changing them.

        The whole operation happens inside the store's operation lock, as a rename's does:
        the removal, the receipt that records it and the claim record that describes the
        path afterwards are one critical section, so no other arbite process can see the
        bytes gone while the claim still says they are there."""
        with self.store.operation_lock():
            path = canonical_relative(raw_path, self.project_root)
            self._refuse_directory(path)
            refuse_if_excluded(path)
            require_mutation_context(ticket_id, attempt_id)
            before = probe(self.project_root, path)
            if before.is_absent:
                self._refuse_absent(path, read_token)
            if not read_token:
                require_read_token(path, ticket_id, attempt_id)
            claim = active_claim(self.store, path)
            outcome = self.mutations.apply(
                MutationRequest(
                    kind="remove",
                    ticket_id=ticket_id,
                    attempt_id=attempt_id,
                    actor=self._actor(attempt_id),
                    changes=(self._change(path, read_token, before, ABSENT),),
                )
            )
            if claim is not None:
                self._reconcile_claim(claim, ABSENT)
        return self._remove_report(path, read_token, outcome)

    def _remove_report(self, path, read_token, outcome) -> OperationResult:
        """RN3's one line: what went, the version it was, and where its bytes are kept.

        The bytes are read back from the receipt's artifact rather than from the path --
        which is, by definition, not there any more. That is the point of keeping the
        evidence: the sentence stays true and reproducible after the file is gone."""
        receipt = outcome.receipt
        before, _ = evidence(self.store, receipt.before[path])
        return succeeded(
            lines=[
                REMOVED.format(
                    path=path,
                    digest=short_digest(before.digest),
                    shape=before.describe(),
                    receipt=receipt.id,
                )
            ],
            data={
                "path": path,
                "removed": True,
                "receipt": receipt.id,
                "ticket": receipt.ticket_id,
                "attempt": receipt.attempt_id,
                "claim_generation": receipt.claim_generation,
                "before": version_facts(before),
                "after": None,
                "token": {"id": read_token, "spent_by": receipt.id},
            },
        )

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def rename(
        self,
        raw_source,
        raw_dest,
        ticket_id: str = None,
        attempt_id: str = None,
        read_token: str = None,
        expect_dest: str = None,
    ) -> OperationResult:
        """Move one file's bytes to another path this attempt also holds.

        Everything happens inside the store's operation lock, and that is a requirement
        rather than tidiness: the operation, the receipt it records and the ownership it
        moves are one critical section, so no other arbite process can acquire the
        destination, read either path or judge the receipt in the middle of the move."""
        root = self.project_root
        with self.store.operation_lock():
            source = mutation_target(raw_source, root)
            dest = mutation_target(raw_dest, root)
            self._refuse_same_path(source, dest)
            require_mutation_context(ticket_id, attempt_id)
            source_before = probe(root, source)
            if source_before.is_absent:
                raise PathRefused(
                    f"no such path '{source}'; `file rename` moves bytes that exist",
                    text_hint=(
                        f"next: 'arbite file list {self._directory(source)}' to see what is there"
                    ),
                )
            if not read_token:
                require_read_token(source, ticket_id, attempt_id)
            source_claim = self._require_claim(source, ticket_id, attempt_id)
            dest_before = probe(root, dest)
            dest_claim = self._require_destination_claim(
                source, dest, ticket_id, attempt_id
            )
            self._require_one_generation(
                source, source_claim, dest, dest_claim, ticket_id, attempt_id
            )
            self._require_destination_version(
                source, dest, dest_before, expect_dest, read_token, ticket_id, attempt_id
            )
            # The destination's own observation: the version the caller stated for it
            # (absence, or the digest it named) recorded under the claim that holds it, so
            # the engine checks it against the bytes exactly as it checks a read token.
            dest_token = record_claim_probe(
                self.store,
                self.app.derived_workspace().id,
                dest,
                ticket_id,
                attempt_id,
                self._actor(attempt_id),
                dest_claim.generation,
                dest_before.digest,
            )
            outcome = self.mutations.apply(
                MutationRequest(
                    kind="rename",
                    ticket_id=ticket_id,
                    attempt_id=attempt_id,
                    actor=self._actor(attempt_id),
                    changes=(
                        self._change(source, read_token, source_before, ABSENT),
                        PathChange(
                            path=dest,
                            expect=dest_before.digest,
                            becomes=source_before.digest,
                            token=dest_token.id,
                        ),
                    ),
                )
            )
            self._move_claims(
                source_claim,
                dest_claim,
                dest,
                ticket_id,
                attempt_id,
                source_before.digest,
                outcome.receipt,
            )
        return self._rename_report(
            source, dest, source_before, dest_before, read_token, outcome
        )

    def _rename_report(
        self, source, dest, source_before, dest_before, read_token, outcome
    ) -> OperationResult:
        """RN1's three lines, plus what a replacing rename has to say.

        A move onto an existing path prints one extra line before the claim line: the
        version it replaced is the difference between the two renames, and its bytes are in
        the receipt like any other evidence. The versions are read back from the receipt's
        artifacts, so the report describes what the operation recorded rather than what the
        tree happens to hold now."""
        receipt = outcome.receipt
        moved, _ = evidence(self.store, receipt.after[dest])
        lines = []
        if not dest_before.is_absent:
            lines.append(
                REPLACED.format(
                    dest=dest,
                    digest=short_digest(dest_before.digest),
                    shape=dest_before.describe(),
                )
            )
        lines.append(
            RENAMED.format(source=source, dest=dest, digest=short_digest(moved.digest))
        )
        lines.append(
            CLAIM_MOVED.format(
                dest=dest, generation=receipt.claim_generation, source=source
            )
        )
        lines.append(
            RECEIPT_MUTATION.format(
                receipt=receipt.id, ticket=receipt.ticket_id, attempt=receipt.attempt_id
            )
        )
        return succeeded(
            lines=lines,
            data={
                "source": source,
                "destination": dest,
                "receipt": receipt.id,
                "ticket": receipt.ticket_id,
                "attempt": receipt.attempt_id,
                "claim_generation": receipt.claim_generation,
                "moved": version_facts(moved),
                "source_before": version_facts(source_before),
                "replaced": None if dest_before.is_absent else version_facts(dest_before),
                "claim": {
                    "held": dest,
                    "released": source,
                    "generation": receipt.claim_generation,
                },
                "token": {"id": read_token, "spent_by": receipt.id},
            },
        )

    # ------------------------------------------------------------------
    # The rules a caller has to satisfy
    # ------------------------------------------------------------------

    def _refuse_directory(self, path: str) -> None:
        """Refuse a directory, with the manual alternative stated (RN4).

        Deliberately the *first* check a removal makes: a tree is not a file, arbite has no
        recursive deletion, and an agent that asked for one needs to hear that rather than
        which flag it forgot -- so the answer comes before the ticket, the claim or the
        token, exactly as the frozen block shows. A symlink is not this refusal to make:
        `probe` refuses to follow one by name, and falling through to it keeps that rule in
        one place."""
        target = self.project_root / path
        if not target.is_dir() or target.is_symlink():
            return
        list_command = f"arbite file list {path}"
        raise PathRefused(
            DIRECTORY_REFUSAL.format(path=path),
            [list_command, f"rmdir {path}"],
            text_hint=DIRECTORY_HINT.format(list_command=list_command, path=path),
        )

    def _refuse_absent(self, path: str, read_token) -> None:
        """The refusal for a path that is not there, unless the token says why.

        A name with no bytes under it is normally an error -- `file remove` removes bytes
        that exist, and `file list` is how a caller sees what does -- which is the answer a
        `file read` of the same path gives (RD5's rule, one command over). The exception is
        a token a mutation has already spent *on this very path*: that is a replay of a
        removal that happened, and the fact the caller needs is which operation it was, not
        that the bytes are gone. Its usual repair -- re-read the path -- cannot be run here,
        so the hint says what is true instead of naming a command that would refuse. A spent
        token for some *other* path is not this case: the answer then belongs to the name the
        caller asked about, which has nothing under it."""
        observation = (
            None if not read_token else self.store.find_record("observation", read_token)
        )
        if observation is not None and observation.is_spent and observation.path == path:
            raise Stale(
                spent_message(observation),
                reason=REASON_STALE_TOKEN_SPENT,
                next_actions=["arbite file claims"],
                text_hint=(
                    "next: nothing to remove -- this replay changed nothing; "
                    "'arbite file claims' shows what the attempt still holds"
                ),
            )
        raise PathRefused(
            f"no such path '{path}'; `file remove` removes bytes that exist",
            text_hint=(
                f"next: 'arbite file list {self._directory(path)}' to see what is there"
            ),
        )

    def _refuse_same_path(self, source: str, dest: str) -> None:
        """Refuse a rename onto itself, which would record a move of nothing.

        Compared with the filesystem's case rules, so `Base.py` -> `base.py` is one path on
        a machine that says those two names are one file."""
        if os.path.normcase(source) != os.path.normcase(dest):
            return
        raise PathRefused(
            f"source and destination are the same path ('{source}'); a rename moves bytes "
            "to a different path"
        )

    def _require_claim(self, path: str, ticket_id: str, attempt_id: str):
        """The claim this attempt holds on a path, or the refusal that says why it cannot.

        The same two outcomes the engine produces for a write -- busy (4) when another
        attempt holds the path, an error (1) when nobody does -- asked here as well, so a
        rename can answer about *both* of its paths before it looks at either version."""
        claim = active_claim(self.store, path)
        if claim is None:
            command = (
                f"arbite file claim {path} --ticket {ticket_id} --attempt {attempt_id}"
            )
            raise NoClaim(
                f"{ticket_id} / {attempt_id} does not hold a claim on {path} "
                f"({REASON_NO_CLAIM});\n"
                f"{REFUSAL_INDENT}a read does not authorize a change",
                [command],
                text_hint=f"next: '{command}'",
            )
        self._require_own(claim, path, attempt_id)
        return claim

    def _require_destination_claim(self, source, dest, ticket_id, attempt_id):
        """The same check for the destination, with the one acquisition spelled out.

        A rename is the one operation whose two paths have to be *one* acquisition, so the
        command that fixes a missing destination claim names both paths: acquiring the
        destination on its own would give it a generation of its own and leave the move
        unauthorisable."""
        claim = active_claim(self.store, dest)
        if claim is None:
            command = (
                f"arbite file claim {source} {dest} --ticket {ticket_id} "
                f"--attempt {attempt_id}"
            )
            raise NoClaim(
                f"{ticket_id} / {attempt_id} does not hold a claim on {dest} "
                f"({REASON_NO_CLAIM});\n"
                f"{REFUSAL_INDENT}a rename claims both paths in one acquisition, so one "
                "read authorises the whole move",
                [command],
                text_hint=f"next: '{command}'",
            )
        self._require_own(claim, dest, attempt_id)
        return claim

    @staticmethod
    def _require_own(claim, path: str, attempt_id: str) -> None:
        """Refuse a path another attempt holds: outcome 4, with nothing changed."""
        if claim.held_by(attempt_id):
            return
        raise Busy(
            f"{path} is held by {claim.ticket_id} / {claim.attempt_id} "
            f"(generation {claim.generation})\n{NO_BYTES_CHANGED}",
            reason="file_busy",
        )

    def _require_one_generation(
        self, source, source_claim, dest, dest_claim, ticket_id, attempt_id
    ) -> None:
        """Refuse a rename whose two paths were not claimed together.

        One acquisition is what gives two paths one generation, and one generation is what a
        single read token authorises: a rename whose destination was claimed separately
        would spend a token that covers only half of the move. The engine checks the same
        thing; asking here means the answer can name the fix, which for this case is a
        re-acquisition rather than the fresh read a stale version wants."""
        if source_claim.generation == dest_claim.generation:
            return
        release = (
            f'arbite file release {dest} --ticket {ticket_id} --attempt {attempt_id} '
            '--reason "<why>"'
        )
        claim = (
            f"arbite file claim {source} {dest} --ticket {ticket_id} --attempt {attempt_id}"
        )
        mark = f"arbite file read {source} --ticket {ticket_id} --attempt {attempt_id}"
        raise Stale(
            f"{source} is claimed at generation {source_claim.generation} and {dest} at "
            f"generation {dest_claim.generation}, so the two paths did not change hands "
            f"together;\n{NO_BYTES_CHANGED}",
            reason=REASON_STALE_VERSION,
            next_actions=[release, claim, mark],
            text_hint=(
                f"next: '{release}', then '{claim}' to hold both at one generation, then "
                f"'{mark}' for a token that authorises the move"
            ),
        )

    def _require_destination_version(
        self, source, dest, dest_before, expect_dest, read_token, ticket_id, attempt_id
    ) -> None:
        """Rule two: an existing destination is replaced deliberately, or not at all.

        The destination's version is the difference between a move and an overwrite of
        somebody's bytes, so `--expect-dest` is required as soon as there is something to
        replace. It is also *checked* here, not only by the engine: "the version you named is
        not the one there" is a repair -- read the path, then name the version that is there
        now -- whereas the engine's message for a destination that arrived after a read is
        about a read this caller never took."""
        if dest_before.is_absent:
            if expect_dest:
                raise PathRefused(
                    f"destination {dest} is not there, so there is no version to replace",
                    text_hint=(
                        "next: re-run without '--expect-dest' to move onto the name that is "
                        f"free, or '{self._list_command(dest)}' to see what is there"
                    ),
                )
            return
        if not expect_dest:
            digest = short_digest(dest_before.digest)
            raise UsageRefused(
                DESTINATION_EXISTS.format(dest=dest, digest=digest),
                [self._rename_command(source, dest, ticket_id, attempt_id, read_token, digest)],
                text_hint=DESTINATION_HINT.format(digest=digest),
            )
        if self._matches(dest_before.digest, self._digest_prefix(expect_dest)):
            return
        command = read_command(ticket_id, attempt_id, dest)
        raise Stale(
            f"destination {dest} is now {short_digest(dest_before.digest)}"
            f"{self._changed_at(dest)}, not {expect_dest}\n{NO_BYTES_CHANGED}",
            reason=REASON_STALE_VERSION,
            next_actions=[command],
            text_hint=(
                f"next: '{command}' to read the version that is there now, or pick "
                "another name"
            ),
        )

    @staticmethod
    def _digest_prefix(value) -> str:
        """The hex part of a stated destination version, or the refusal for a value that
        cannot be one.

        Both spellings are accepted because both are printed: every report shortens a digest
        to twelve characters, so a caller pasting a refusal's own `--expect-dest` back has to
        be understood, while JSON and the stored records carry all 64."""
        text = value if isinstance(value, str) else str(value)
        hexdigits = (
            text[len(EXPECT_DEST_PREFIX) :]
            if text.startswith(EXPECT_DEST_PREFIX)
            else ""
        )
        usable = (
            EXPECT_DEST_SHORTEST <= len(hexdigits) <= 64
            and all(character in EXPECT_DEST_HEX for character in hexdigits)
        )
        if not usable:
            raise UsageRefused(EXPECT_DEST_REFUSAL.format(value=value))
        return hexdigits

    @staticmethod
    def _matches(stored: str, hexdigits: str) -> bool:
        """Whether a stated version names the digest on disk: whole, or as its prefix."""
        body = stored[len(EXPECT_DEST_PREFIX) :]
        if len(hexdigits) == 64:
            return body == hexdigits
        return body.startswith(hexdigits)

    # ------------------------------------------------------------------
    # The ownership move
    # ------------------------------------------------------------------

    def _move_claims(
        self, source_claim, dest_claim, dest, ticket_id, attempt_id, moved_digest, receipt
    ) -> None:
        """Move ownership: the source's claim ends and the destination's records the bytes.

        The destination was *already* claimed -- a rename onto a path this attempt does not
        hold is refused -- so "ownership moves to the destination" means two records in one
        commit: the source's claim is released (with the reason saying where the bytes went),
        and the destination's claim is refreshed to the version it now owns, which is what
        keeps `file claims` and a later read's drift note describing the path truthfully.
        The released record keeps the state it was released in, so the history of the
        acquisition is not rewritten: its observed version becomes `absent`, and the version
        that moved away is in the event below and in the receipt. A plain `file release` is
        different in exactly that respect, which is why the two do not share a sentence.

        If this commit cannot land, the answer is deliberately *not* a "nothing changed"
        refusal: the bytes did move, and saying otherwise is the one thing a caller cannot
        act on -- so it names the operation, the paths and the repair."""
        reason = MOVE_REASON.format(dest=dest)
        released = utc_now()
        actor = self._actor(attempt_id)
        try:
            with self.store.transaction() as txn:
                txn.replace_record(
                    replace(
                        source_claim,
                        state=CLAIM_RELEASED,
                        released=released,
                        release_reason=reason,
                        observed_version=ABSENT,
                    ),
                    expect_revision=txn.revision("claim", source_claim.id),
                )
                txn.replace_record(
                    replace(dest_claim, observed_version=moved_digest),
                    expect_revision=txn.revision("claim", dest_claim.id),
                )
                txn.append_event(
                    RELEASE_FILE,
                    "claim",
                    subject=source_claim.path,
                    result="released",
                    ticket_id=ticket_id,
                    attempt_id=attempt_id,
                    actor=actor,
                    operation_id=receipt.id,
                    payload={
                        "generation": source_claim.generation,
                        "version": moved_digest,
                        "reason": reason,
                        "moved_to": dest,
                    },
                )
        except ArbiteError as failure:
            raise CoordinationError(
                f"the rename to {dest} happened and receipt {receipt.id} records it, but its "
                f"ownership could not be moved ({failure}); the bytes have moved, so this is "
                "not a refusal -- finish it with "
                f"'arbite file release {source_claim.path} --ticket {ticket_id} "
                f'--attempt {attempt_id} --reason "{reason}"\''
            )

    def _reconcile_claim(self, claim, digest: str) -> None:
        """Point a claim record at what its path holds now, after the owner changed it.

        A removal leaves the path owned but empty, which is exactly the state FC4 claims and
        creations are authorised from -- so the claim stays active and its observed version
        becomes `absent`, which is what `file claims` should print for a path with no bytes.
        Failure here is not a refusal either: the bytes are gone and the receipt records it,
        so the message says so and names the command that repairs the record."""
        try:
            with self.store.transaction() as txn:
                txn.replace_record(
                    replace(claim, observed_version=digest),
                    expect_revision=txn.revision("claim", claim.id),
                )
        except ArbiteError as failure:
            raise CoordinationError(
                f"the removal happened, but the claim on {claim.path} still records the "
                f"version it replaced ({failure}); the bytes are gone, so this is not a "
                "refusal -- 'arbite file release "
                f'{claim.path} --ticket {claim.ticket_id} --attempt {claim.attempt_id} '
                '--reason "removed"\' finishes the record'
            )

    # ------------------------------------------------------------------
    # Small shared pieces
    # ------------------------------------------------------------------

    def _change(self, path, read_token, before, becomes) -> PathChange:
        """One path's change, with the version the *token* observed as its expectation.

        `expect` is not the version just probed: the two differ exactly when the file moved
        after the read, and that is the case the engine reports with both digests (WR2's
        repair path, one command further on). A token this store does not know is the
        engine's refusal to make -- "does not exist in this store" -- so the probed version
        stands in and is never compared."""
        observation = (
            None if not read_token else self.store.find_record("observation", read_token)
        )
        expect = observation.digest if observation is not None else before.digest
        return PathChange(path=path, expect=expect, becomes=becomes, token=read_token)

    def _actor(self, attempt_id: str) -> str:
        """The attempt's worker when the store knows it, the attempt id otherwise.

        Attribution, not authentication: an attempt that does not exist is refused by the
        engine before anything is written, so no receipt is ever attributed to a guess."""
        attempt = self.store.get_attempt(attempt_id)
        return attempt.worker_id if attempt is not None else attempt_id

    def _changed_at(self, path: str) -> str:
        """` (changed HH:MM:SS)` for the version on disk, or nothing when it cannot be
        stat'ed -- a sentence is better than a failure at that point."""
        when = modified_clock(self.project_root, path)
        return "" if when is None else f" (changed {when})"

    @staticmethod
    def _directory(path: str) -> str:
        return os.path.dirname(path) or "."

    @classmethod
    def _list_command(cls, path: str) -> str:
        return f"arbite file list {cls._directory(path)}"

    @staticmethod
    def _rename_command(
        source, dest, ticket_id, attempt_id, read_token, expect_dest=None
    ) -> str:
        """The rename again, with every flag filled in: the command a refusal hands back.

        The read token is repeated deliberately -- a refusal spends nothing, so the token
        the caller presented still authorises the move -- and `--expect-dest` is printed in
        the short form the message itself used, which this command accepts."""
        parts = [
            "arbite file rename",
            source,
            dest,
            "--ticket",
            ticket_id,
            "--attempt",
            attempt_id,
        ]
        if read_token:
            parts += ["--read-token", read_token]
        if expect_dest:
            parts += ["--expect-dest", expect_dest]
        return " ".join(parts)
