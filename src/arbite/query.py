"""The query vocabulary: one set of semantics that every sink implements.

`TicketQuery` is how a command asks for tickets and `TextMatch` is how it asks
for text, so no command ever filters by hand -- the file sink filters in Python
and the SQLite sink builds SQL, and the conformance suite asserts they answer
identically. `TicketQuery.matches()` here is the *reference* implementation: any
sink that pushes a predicate down into storage must still agree with it.

Ordering is pinned here too, because `--json` consumers and the human table both
depend on the order, and two sinks that sort differently would be a bug that
only shows up in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Optional

from .errors import AmbiguousTicketId, TicketError, TicketNotFound
from .schema import SEARCH_FIELDS, Ticket

TEXT_MODES = ("substring", "wildcard", "regex")

# Canonical orderings, each with a name so a sink can translate rather than
# invent. `flat` groups by status then urgency; `next` is the work-queue order;
# `created_asc` is the triage queue; `id` is the stable inventory order.
ORDERS = ("flat", "next", "created_asc", "id")


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------


def ticket_field_value(ticket: Ticket, name: str) -> str:
    """String form of a ticket field for searching; 'body' is the markdown body."""
    if name == "body":
        return ticket.body or ""
    value = getattr(ticket, name, None)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def compile_matcher(pattern: str, mode: str = "substring", ignore_case: bool = True):
    """Build a text->bool matcher from the pattern and mode.

    Default is a case-insensitive substring match; 'wildcard' treats '*' as 'any
    text'; 'regex' uses the pattern as a regular expression. An invalid mode or
    regex raises TicketError, so a bad search is reported rather than silently
    matching nothing."""
    if mode not in TEXT_MODES:
        raise TicketError(f"invalid text match mode '{mode}' (valid: {', '.join(TEXT_MODES)})")
    flags = re.IGNORECASE if ignore_case else 0
    if mode == "regex":
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise TicketError(f"invalid regex '{pattern}': {e}")
        return lambda text: rx.search(text) is not None
    if mode == "wildcard":
        # Translate simple globs: everything is literal except '*' = any text (incl. empty).
        rx = re.compile(re.escape(pattern).replace(r"\*", ".*"), flags)
        return lambda text: rx.search(text) is not None
    needle = pattern.lower()
    return lambda text: needle in text.lower()


# NOTE: there is deliberately no "translate a substring search into LIKE" helper
# here. SQLite's LIKE folds case in ASCII only (and Python's str.lower folds a few
# non-ASCII characters such as the Kelvin sign to their ASCII lowercase), so a
# LIKE pre-filter can miss a row the reference matcher below accepts -- i.e. it is
# not provably equivalent. The SQLite sink therefore pushes *structured*
# predicates into SQL and runs text matching through `compile_matcher()` on the
# rows that come back, which is slower on paper and correct in fact.


@dataclass(frozen=True)
class TextMatch:
    """A text search: what to look for, how to interpret it, and where to look.

    `fields` names frontmatter fields (plus 'body'), or the single entry 'all'
    meaning every field plus the body."""

    pattern: str
    mode: str = "substring"
    fields: tuple = ("all",)

    def resolved_fields(self) -> tuple:
        if "all" in self.fields:
            return tuple(sorted(SEARCH_FIELDS))
        return tuple(self.fields)

    def matcher(self):
        return compile_matcher(self.pattern, self.mode)

    def matches(self, ticket: Ticket) -> bool:
        """True if any selected field matches. This is the reference behavior a
        sink's pushdown must reproduce."""
        matcher = self.matcher()
        return any(matcher(ticket_field_value(ticket, name)) for name in self.resolved_fields())

    def unknown_fields(self) -> list:
        if "all" in self.fields:
            return []
        return [f for f in self.fields if f not in SEARCH_FIELDS]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketQuery:
    """A read request, expressed in storage-neutral terms.

    Everything here except `buckets` is a ticket field and can therefore be
    evaluated by `matches()` alone. `buckets` names where the *sink* put the
    ticket (a folder for the file sink, a column for SQLite) and is deliberately
    not a ticket field: it is applied by the sink, so a ticket's own text never
    claims a location it doesn't have."""

    status: tuple = ()
    type: tuple = ()
    tier: Optional[str] = None
    domain: Optional[str] = None
    epic: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[int] = None
    ids: tuple = ()
    buckets: tuple = ()
    text: Optional[TextMatch] = None
    order: str = "flat"
    limit: Optional[int] = None

    # `buckets` reads as: () -- only tickets sitting at their status location
    # (the default, because a ticket filed away in a bucket is out of the status
    # workflow); ('wishlist',) -- tickets in those named buckets; ('*',) --
    # anywhere, bucketed or not, which is what a full sweep such as
    # `arbite migrate` or `doctor` asks for.

    def normalized(self) -> "TicketQuery":
        """Coerce scalars to tuples so a caller can pass `status='open'` or
        `status=['open', 'raw']` and get the same query."""
        def as_tuple(value):
            if value is None:
                return ()
            if isinstance(value, str):
                return (value,)
            return tuple(value)

        q = replace(
            self,
            status=as_tuple(self.status),
            type=as_tuple(self.type),
            ids=as_tuple(self.ids),
            buckets=as_tuple(self.buckets),
        )
        if q.order not in ORDERS:
            raise TicketError(f"invalid order '{q.order}' (valid: {', '.join(ORDERS)})")
        if self.text is not None:
            unknown = self.text.unknown_fields()
            if unknown:
                from .schema import FIELD_ORDER

                raise TicketError(
                    f"unknown ticket field(s) to search: {', '.join(unknown)} "
                    f"(valid: all, body, {', '.join(FIELD_ORDER)})"
                )
            # Fail fast on a bad mode/regex rather than scanning nothing.
            if self.text.mode not in TEXT_MODES:
                raise TicketError(
                    f"invalid text match mode '{self.text.mode}' "
                    f"(valid: {', '.join(TEXT_MODES)})"
                )
            if self.text.mode == "regex":
                compile_matcher(self.text.pattern, "regex")
        return q

    def evolve(self, **changes) -> "TicketQuery":
        return replace(self.normalized(), **changes)

    def matches(self, ticket: Ticket) -> bool:
        """The reference predicate: True if `ticket` satisfies every structured
        filter and the text match. Sinks may short-circuit internally but must
        return the same set."""
        if self.status and ticket.status not in self.status:
            return False
        if self.type and ticket.type not in self.type:
            return False
        if self.tier and ticket.tier != self.tier:
            return False
        if self.domain and ticket.domain != self.domain:
            return False
        if self.epic and ticket.epic != self.epic:
            return False
        if self.assignee and ticket.assignee != self.assignee:
            return False
        if self.priority is not None and ticket.priority != self.priority:
            return False
        if self.ids and ticket.id not in self.ids:
            return False
        if self.text is not None and not self.text.matches(ticket):
            return False
        return True


def bucket_matches(bucket: Optional[str], wanted: tuple) -> bool:
    """Whether a ticket filed in `bucket` (None == its status location) passes
    the bucket part of a query.

    Intentionally not part of `TicketQuery.matches()`: a bucket is a property of
    where the sink put the ticket, not of the ticket, and a ticket must never be
    able to claim a location it does not have."""
    if not wanted:
        return bucket is None
    if "*" in wanted:
        return True
    return bucket in wanted


def sort_key(order: str, ticket: Ticket):
    """The canonical sort key for `order`. A sink that sorts in SQL must produce
    the same sequence (see the conformance suite)."""
    if order == "flat":
        return (ticket.status, ticket.priority_sort_key(), ticket.id)
    if order == "next":
        return (ticket.priority_sort_key(), ticket.id)
    if order == "created_asc":
        return (ticket.created or "", ticket.id)
    if order == "id":
        return (ticket.id,)
    raise TicketError(f"invalid order '{order}' (valid: {', '.join(ORDERS)})")


def sort_tickets(rows: list, order: str = "flat") -> list:
    return sorted(rows, key=lambda t: sort_key(order, t))


def apply_limit(rows: list, limit: Optional[int]) -> list:
    """Cap a result list. None means no cap; rows are already in the view's own
    order, so this always keeps the most relevant ones (most urgent first for a
    flat list, dependencies first for a topological view)."""
    return rows if limit is None else rows[:limit]


# ---------------------------------------------------------------------------
# Id resolution
# ---------------------------------------------------------------------------


def wildcard_matches(ids, term: str) -> list:
    """Every id containing `term` as a case-insensitive substring, sorted.
    'f6' matches tic-f607; 'tic-' matches every ticket."""
    term_lower = term.lower()
    return sorted(tid for tid in ids if term_lower in tid.lower())


def resolve_id(ids, term: str, unique: bool = False) -> str:
    """Resolve an id term to a single ticket id.

    An exact id always wins outright, so a full id is never ambiguous even when
    it happens to be a substring of another id. Otherwise, with unique=True the
    caller gets an AmbiguousTicketId listing the candidates rather than a
    silently chosen one: commands that mutate a ticket pass unique=True, because
    guessing there means writing to the wrong ticket, while read-only commands
    keep the convenience of the first alphabetical match."""
    matches = wildcard_matches(ids, term)
    if not matches:
        raise TicketNotFound(f"no ticket found matching '{term}'")
    exact = [tid for tid in matches if tid.lower() == term.lower()]
    if exact:
        return exact[0]
    if unique and len(matches) > 1:
        raise AmbiguousTicketId(
            f"'{term}' is ambiguous -- it matches {len(matches)} tickets: "
            f"{', '.join(matches)}. Pass a full ticket id."
        )
    return matches[0]


def resolve_terms(ids, terms) -> list:
    """Expand each id term into every id it matches by wildcard search, e.g.
    'f6' resolves to tic-f607. Each term must match at least one ticket
    (TicketNotFound otherwise). Returns a sorted list of unique ids."""
    resolved = []
    for term in terms:
        matches = wildcard_matches(ids, term)
        if not matches:
            raise TicketNotFound(f"no ticket found matching '{term}'")
        resolved.extend(matches)
    return sorted(set(resolved))
