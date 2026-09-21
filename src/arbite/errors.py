"""The exception hierarchy every arbite command and sink reports through.

One base class means the CLI has a single `except` that turns a failure into
`error: <message>` on stderr with exit code 1, so nothing a sink raises can
escape as a traceback. The subtypes exist so callers can distinguish "the user
asked for something impossible" (TicketError, AmbiguousTicketId, Conflict) from
"the storage layer broke" (SinkError) -- a distinction the CLI uses for exit
codes and a test suite uses to assert the *right* failure, not merely a failure.
"""

from __future__ import annotations

from typing import Optional


class ArbiteError(Exception):
    """Base for every expected arbite failure. Anything raised through this class
    is reported to the user as a one-line error, never as a traceback."""


class TicketError(ArbiteError):
    """A ticket is invalid, or an operation on one is refused: malformed
    frontmatter or YAML, a field value outside its controlled vocabulary, a
    state change that does not apply.

    The name predates the sink abstraction and is kept deliberately: `arbite
    doctor`'s report, the CLI's messages and the docs all refer to it."""


class TicketNotFound(ArbiteError):
    """No ticket matched the given id or wildcard term."""


class AmbiguousTicketId(ArbiteError):
    """An id term matched more than one ticket on a command that mutates one.

    Read-only commands keep the convenience of the first alphabetical match;
    mutations refuse and list the candidates, because guessing there writes to
    the wrong ticket."""


class Conflict(ArbiteError):
    """A compare-and-swap update lost its race.

    Raised when a ticket is not in the state the caller expected -- someone else
    claimed it first, or it moved on between the read and the write. This is the
    error a lost claim produces, so the caller can retry against a fresh read
    instead of silently overwriting the winner's work."""


# The lifecycle refusals below carry a `reason` and the exact commands that follow
# from them. `coordination.results.outcome_of` reads `reason` (which is what keys a
# `next:` line) and `next_actions_of` prefers the actions carried here over the
# registered ones, so a refusal computed from the state at failure time -- "the
# ticket you lost is held by att-XXXX, take other work" -- is stated once, where the
# rule lives, instead of being guessed back at the CLI.


class NotReady(ArbiteError):
    """A ticket cannot be claimed yet: something it depends on is not closed.

    The rule this protects is the one `arbite list next` only *filters* on: list-next
    is a convenience over the same acquisition operation, so readiness has to hold
    inside the claim as well, or a caller could bypass the queue by naming the ticket
    directly."""

    reason = "not_ready"

    def __init__(self, message: str, next_actions=()):
        super().__init__(message)
        self.next_actions = tuple(next_actions)


class LostRace(Conflict):
    """A ticket acquisition lost its race: it is no longer in the state a claim
    requires, because another worker (or another command) got there first.

    A `Conflict`, so a batch that is walking candidates recognises it as "skip this
    one and try the next" without knowing anything about claims."""

    reason = "lost_race"

    def __init__(self, message: str, next_actions=()):
        super().__init__(message)
        self.next_actions = tuple(next_actions)


class LifecycleRequired(ArbiteError):
    """A generic setter was asked to make a change that a lifecycle command owns.

    The plan's "no backdoor through force": `arbite set` may edit fields, but a
    change that must end or start an attempt -- closing claimed work, resurrecting a
    released ticket -- routes to the command that does that, or it is refused with
    the command named."""

    reason = "lifecycle_required"

    def __init__(self, message: str, next_actions=()):
        super().__init__(message)
        self.next_actions = tuple(next_actions)


class CoordinationError(ArbiteError):
    """The coordination layer failed: a record that cannot be stored or read, a
    store whose backend cannot be reached, or a refusal computed from the store's
    own state.

    A separate branch from `SinkError` because coordination state is a different
    storage domain from tickets (local runtime state versus the git-tracked
    development record); a failure here must not read as one in the ticket store.

    A raise site that knows the repair may carry `next_actions` and the `text_hint`
    they read as -- "this operation id names a read observation", "this receipt's
    evidence is gone, look at it by hand" -- exactly as `PathRefused` does, so the
    sentence is built where the state is known rather than guessed at the CLI."""

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        self.text_hint = text_hint


class RecordError(CoordinationError):
    """A coordination record is malformed, carries an unknown type, or was written
    by a newer schema revision than this arbite understands.

    Reported rather than coerced: a record whose fields cannot be trusted must not
    be silently half-read, because every later decision (claims, staleness,
    evidence) is built from these fields."""


class Busy(ArbiteError):
    """Outcome 4: a live claim or attempt already holds what was asked for, and
    **nothing changed** (see the exit-code table in the coordination handoff).

    `label` is the text the CLI prints in front of the message, so the outcome the
    caller branches on and the word it reads come from one place. `reason` refines
    *which* busy this is (`file_busy`, `store_locked`, ...), which is what chooses
    the next actions; it defaults to the bare kind. The correct caller response is
    to pick other work, not to retry the busy path."""

    label = "busy"

    def __init__(self, message: str, reason: Optional[str] = None):
        super().__init__(message)
        self.reason = reason or "busy"


class Stale(ArbiteError):
    """Outcome 5: a token, digest or generation is no longer current, and
    **nothing changed**. The correct caller response is to re-read and retry.

    `reason` refines which stale this is (`stale_read`, `stale_version`,
    `stale_token_spent`, `attempt_not_current`, ...); it defaults to the bare kind,
    and it is what keys the `next:` line: a file that moved, a token that was spent
    and a ticket that closed are all outcome 5 but want different repairs. A raise
    site that knows the repair (it knows the path, the ticket and the attempt) may
    carry `next_actions` and the `text_hint` they read as, exactly as `PathRefused`
    does, so the sentence is built where the state is known."""

    label = "stale_read"

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        next_actions=(),
        text_hint: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason = reason or "stale_read"
        self.next_actions = tuple(next_actions)
        #: The exact `next:` sentence to print, when the fixed wording matters more
        #: than `render_next_line`'s join (see `results.text_hint_of`).
        self.text_hint = text_hint


class EditRefused(ArbiteError):
    """An exact-substitution batch does not apply, and **nothing was written**.

    Ambiguous, absent and overlapping selections are all this one outcome because the
    caller's response is the same for them: re-read the lines the refusal names and
    re-send the edit with a selection that identifies one place. The batch is applied
    to the in-memory version and written in one replacement, so a batch that fails
    anywhere leaves the file exactly as it was -- the guarantee is structural rather
    than a cleanup path.
    """

    reason = "edit_refused"

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        self.text_hint = text_hint


class UsageRefused(ArbiteError):
    """A mutation command was not given what makes it attributable.

    An error (exit 1), because the fix is to correct the command rather than to re-read:
    a change named without an attempt cannot be attributed to anybody, and a change named
    without a read token is authorised by nothing -- ownership is the claim's, but the
    change itself is the read's (the frozen WR7 block, and the same rule one argument
    further on)."""

    reason = "usage"

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        self.text_hint = text_hint


class NoClaim(ArbiteError):
    """A mutation named a path this attempt does not hold.

    An error rather than stale: re-reading the path will not make it the caller's,
    so the fix is a command -- claim the path, then take the read a mutation needs
    under that claim -- and the exit code has to say "fix the command". A read
    observation never authorises a change on its own; the claim is the ownership
    half, and this is the refusal that says so (the frozen WR4 block).
    """

    reason = "no_claim"

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        self.text_hint = text_hint


class NotOwner(ArbiteError):
    """A file operation named an attempt that does not own the ticket.

    An error rather than a busy or stale outcome: the caller's attempt is not the one
    the ticket belongs to, so the *path's* ownership was never even reached, and the
    command has to be corrected -- name the attempt the ticket actually has, or take
    the ticket over deliberately with `claim --force --reason`.
    """

    reason = "not_owner"

    def __init__(self, message: str, next_actions=()):
        super().__init__(message)
        self.next_actions = tuple(next_actions)


class EvidenceRefused(ArbiteError):
    """An operation's evidence cannot be stored, so the operation did not happen.

    An error (exit 1), because the caller has to change what it is doing rather than
    re-read: a version this proxy will not keep -- larger than the size limit one
    stored version may have -- cannot be recorded, and a mutation whose evidence has
    nowhere to live is refused **before** any byte of the project changes. The message
    and the `next:` line are built where the rule lives (the store interface), so both
    backends and every mutation kind refuse in exactly the same words.
    """

    reason = "evidence_refused"

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        self.text_hint = text_hint


class PathRefused(ArbiteError):
    """A path is not one arbite will manage, so the command is refused before anything
    is written or read.

    One class for the whole family -- outside the project root, protected arbite state
    or `.git` metadata, a directory, a special file, a symlink component, a
    hard-linked mutation target, a path a read required to exist -- because the
    caller's response is the same for all of them: fix the command. The message and
    the `next:` line are built where the rule lives, which is what makes the escape
    and `.git` refusals print exactly the frozen LS6 text.
    """

    reason = "path_refused"

    def __init__(self, message: str, next_actions=(), text_hint: Optional[str] = None):
        super().__init__(message)
        self.next_actions = tuple(next_actions)
        #: The exact `next:` sentence to print, when the frozen block joins its hints its
        #: own way. `next_actions` stays the branchable list (see `results.text_hint_of`).
        self.text_hint = text_hint


class StaleGeneration(Stale):
    """Outcome 5: the attempt generation a command named is no longer current.

    The check every operation that carries an attempt id needs, and the reason a
    takeover cannot be papered over: a revoked generation stays revoked, and a
    command that presents an old one is told to re-read rather than being allowed to
    act under an owner that has been replaced. Nothing was changed."""

    reason = "stale_generation"

    def __init__(self, message: str, next_actions=()):
        super().__init__(message, reason="stale_generation")
        self.next_actions = tuple(next_actions)


class SinkError(ArbiteError):
    """The storage layer failed: an IO error, a database error, a corrupt store."""


class SinkNotInitialised(SinkError):
    """The selected sink has no store yet. The CLI reports this as 'run `arbite
    init` first' rather than as a crash."""
