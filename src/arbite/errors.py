"""The exception hierarchy every arbite command and sink reports through.

One base class means the CLI has a single `except` that turns a failure into
`error: <message>` on stderr with exit code 1, so nothing a sink raises can
escape as a traceback. The subtypes exist so callers can distinguish "the user
asked for something impossible" (TicketError, AmbiguousTicketId, Conflict) from
"the storage layer broke" (SinkError) -- a distinction the CLI uses for exit
codes and a test suite uses to assert the *right* failure, not merely a failure.
"""

from __future__ import annotations


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


class CoordinationError(ArbiteError):
    """The coordination layer failed: a record that cannot be stored or read, a
    store whose backend cannot be reached.

    A separate branch from `SinkError` because coordination state is a different
    storage domain from tickets (local runtime state versus the git-tracked
    development record); a failure here must not read as one in the ticket store."""


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
    caller branches on and the word it reads come from one place. The correct
    caller response is to pick other work, not to retry the busy path."""

    label = "busy"


class Stale(ArbiteError):
    """Outcome 5: a token, digest or generation is no longer current, and
    **nothing changed**. The correct caller response is to re-read and retry."""

    label = "stale_read"


class SinkError(ArbiteError):
    """The storage layer failed: an IO error, a database error, a corrupt store."""


class SinkNotInitialised(SinkError):
    """The selected sink has no store yet. The CLI reports this as 'run `arbite
    init` first' rather than as a crash."""
