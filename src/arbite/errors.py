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


class SinkError(ArbiteError):
    """The storage layer failed: an IO error, a database error, a corrupt store."""


class SinkNotInitialised(SinkError):
    """The selected sink has no store yet. The CLI reports this as 'run `arbite
    init` first' rather than as a crash."""
