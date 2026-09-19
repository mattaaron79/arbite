"""Resumable durable-event queries and coordinator progress views (planning key B06).

Three read-only pieces live here. Nothing in this module subscribes, watches,
polls, sleeps, retries or delivers anything: every function answers one question
about durable state and returns, and the caller decides when to ask again.

**Cursor tokens.** `Event.cursor` is a per-store monotonic integer, so the integer
alone is ambiguous -- cursor 7 in two stores names two unrelated events. A *token*
binds the cursor to the store that issued it, as
``<cursor_namespace>#<cursor>`` (e.g. ``sqlite:/srv/p/.arbite/arbite.db#41``), and
`parse_cursor` accepts nothing else: a malformed token, a token from another
store's namespace, or a bare non-zero integer (whose store cannot be checked at
all, so it could silently read the wrong position) all fail with a structured
error code. ``0`` means "from the beginning of this store's stream".

A stream that was *imported* here does not change that. The import records the
source -> destination cursor map in the namespace registry (evidence of where
those events landed), and a token from the source store is still
`cursor_foreign_store` here: the destination's cursors are its own, so reading a
source cursor as a position would silently return the wrong page. The refusal is
*enriched* instead -- when the registry knows that namespace it also names the
mapped destination cursor and the token to resume from, so the way forward is a
query this store can answer.

**Paging.** `query` returns one ordered page: `after_cursor`, `limit` and optional
category/kind/subject filters. The cursor a page hands back is the cursor of the
last event the page *consumed* -- not merely the last one it returned -- so a
filter can never lose position, and resuming after the page neither skips a
matching event nor re-reads the whole log. Events carry a globally unique, stable
`id`; a retry or an overlapping query may legitimately deliver the same event
again, so a consumer deduplicates by id rather than assuming exactly-once
delivery.

**Finality.** Only committed/reconciled events are reported. The file sink journals
an intent before applying it and replays a leftover journal forward at the start of
every transaction (see `sinks.coordination_file`), so an interrupted transaction's
events become final -- or stay absent -- before any reader sees them; the SQLite
sink commits atomically, so a half-applied transaction never exists to read. This
module reuses those mechanisms and invents no recovery of its own.

**Coordinator progress.** `reservation_progress` classifies each member as
completed / blocked / active / dependency-waiting / ready / unavailable and reports
its current worker(s) and latest recorded activity. Readiness is delegated to the
shared evaluator (`arbite.readiness`) -- the same one backing `arbite board`,
`list next` and acquisition -- so progress cannot contradict what a claim would do.
Recorded activity is an *observation timestamp*, never a liveness guarantee, and a
dependency-completion event is only a hint to requery readiness.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import readiness, workers
from .coordination import EVENT_CATEGORIES, EVENT_KINDS
from .errors import ForeignCursor, InvalidCursor, InvalidRecord, UnsupportedCoordination
from .query import TicketQuery

#: Separator between a store's cursor namespace and a store-local cursor number.
CURSOR_SEPARATOR = "#"

#: How many events `events` returns when the caller gives no limit. The log is
#: append-only and never pruned, so an unbounded default would grow with the
#: store; every page reports `has_more` and a resumable `next_cursor` instead.
DEFAULT_LIMIT = 200

#: Member classifications, in the order they are decided (first match wins).
MEMBER_STATES = (
    "completed",
    "blocked",
    "active",
    "dependency_waiting",
    "ready",
    "unavailable",
)

EVENTS_NOTICE = (
    "read-only one-shot query: arbite runs no watcher, subscription server, polling "
    "loop or notification delivery. Every event carries a globally unique, stable id "
    "(`id`), so a consumer deduplicates repeated delivery by id -- a retry or an "
    "overlapping query may legitimately return the same event again. Only "
    "committed/reconciled events are reported: an interrupted file-sink transaction "
    "is replayed forward (or stays absent) before its events are visible. Pass the "
    "returned `next_cursor` token back as --after to resume; a cursor is meaningful "
    "only within the store that issued it, so a token from a store whose events were "
    "imported here is still refused as foreign -- with the mapped destination cursor "
    "named when the import recorded one"
)

ACTIVITY_NOTICE = (
    "recorded activity is an observation timestamp read from durable records (the "
    "newest event naming the member or one of its attempts, plus the attempt's own "
    "last_activity); it is never a liveness guarantee -- arbite does not infer that a "
    "worker is alive or dead and no heartbeat expires anything. Readiness is derived "
    "from current state every time it is asked, so a dependency-completion or close "
    "event is only a hint to requery: it is not a promise that a specific ticket is "
    "now ready"
)


# ---------------------------------------------------------------------------
# Cursor tokens
# ---------------------------------------------------------------------------


def cursor_token(namespace: str, cursor: int) -> str:
    """The resumable token for `cursor` in `namespace` (see the module docstring)."""
    return f"{namespace}{CURSOR_SEPARATOR}{int(cursor)}"


def parse_cursor(token, namespace: str) -> int:
    """The store-local cursor a token names, or `InvalidCursor`/`ForeignCursor`.

    `None`/`""`/`0` mean "from the beginning". A bare non-zero integer is refused
    rather than trusted: nothing about it says which store issued it, and reading
    the wrong store's position silently is exactly the failure this token exists to
    prevent."""
    if token is None:
        return 0
    if isinstance(token, bool):
        raise InvalidCursor(
            f"invalid cursor {token!r}: expected a cursor token from a previous "
            f"query ({namespace}{CURSOR_SEPARATOR}<cursor>) or 0",
            details={"cursor": token, "cursor_namespace": namespace},
        )
    if isinstance(token, int):
        if token == 0:
            return 0
        raise InvalidCursor(
            f"cursor {token} is a bare integer, which does not say which store it "
            "belongs to; pass the exact `next_cursor` token a previous query "
            f"returned, e.g. {namespace}{CURSOR_SEPARATOR}{token}, or 0 to start from "
            "the beginning",
            details={"cursor": token, "cursor_namespace": namespace},
        )
    text = str(token).strip()
    if not text:
        return 0
    if text.isdigit():
        return parse_cursor(int(text), namespace)
    given_namespace, separator, given_cursor = text.rpartition(CURSOR_SEPARATOR)
    if not separator or not given_cursor.isdigit():
        raise InvalidCursor(
            f"malformed cursor {text!r}: expected "
            f"{namespace}{CURSOR_SEPARATOR}<cursor> or 0",
            details={"cursor": text, "cursor_namespace": namespace},
        )
    if given_namespace != namespace:
        raise ForeignCursor(
            f"cursor {text!r} was issued by a different store "
            f"({given_namespace}); cursors are store-local, so it cannot be used "
            f"here (this store's namespace is {namespace}). Query this store once "
            "with --after 0 and resume from the token it returns",
            details={
                "cursor": text,
                "cursor_namespace": namespace,
                "token_namespace": given_namespace,
            },
        )
    return int(given_cursor)


# ---------------------------------------------------------------------------
# The event query service
# ---------------------------------------------------------------------------


def _require_store(sink):
    store = sink.coordination()
    if store is None:
        raise UnsupportedCoordination(
            f"the {sink.kind} sink has no coordination store, so it has no durable "
            "event log to query"
        )
    return store


def _clean_terms(values, vocabulary, label: str) -> Tuple[str, ...]:
    """Normalise a comma-separated/repeated filter, refusing unknown values.

    An unknown category or kind fails rather than matching nothing: a filter that
    silently returns an empty page looks exactly like "no such events happened"."""
    terms: List[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if not part or part in terms:
                continue
            if part not in vocabulary:
                raise InvalidRecord(
                    f"unknown {label} {part!r} (valid: {', '.join(vocabulary)})",
                    details={"filter": label, "value": part, "valid": list(vocabulary)},
                )
            terms.append(part)
    return tuple(terms)


def event_view(event, namespace: str, *, include_token: bool = True) -> Dict[str, Any]:
    """One event as the documented JSON shape, with its resumable token."""
    data = event.to_dict()
    if include_token and event.cursor is not None:
        data["cursor_token"] = cursor_token(namespace, event.cursor)
    return data


def _foreign_cursor_detail(store, error: ForeignCursor, namespace: str) -> ForeignCursor:
    """`error`, enriched when the foreign namespace was imported into this store.

    Refusing is deliberate -- a token names the store that issued it, and this
    store's cursors are its own -- but a caller who asked here after moving a
    store deserves the mapping instead of a bare refusal. The namespace registry
    records where the import's events landed, so when it knows that namespace the
    refusal also names the destination cursor and the exact token to resume from.
    Reading the registry is best-effort and creates nothing.
    """
    details = dict(error.details)
    token_namespace = details.get("token_namespace")
    try:
        entries = store.namespaces()
    except Exception:
        entries = []
    entry = next(
        (
            candidate for candidate in entries
            if isinstance(candidate, dict) and candidate.get("namespace") == token_namespace
        ),
        None,
    )
    if entry is None:
        return error
    cursor = details.get("cursor")
    source = None
    if isinstance(cursor, str) and CURSOR_SEPARATOR in cursor:
        source = cursor.rpartition(CURSOR_SEPARATOR)[2]
    mapped = None
    cursor_map = entry.get("cursor_map")
    if source is not None and source.isdigit() and isinstance(cursor_map, dict):
        mapped = cursor_map.get(source)
        if mapped is None:
            mapped = cursor_map.get(int(source))
    details["imported_namespace"] = token_namespace
    details["imported_at"] = entry.get("imported_at")
    details["imported_event_count"] = entry.get("event_count")
    message = (
        f"{error} -- that namespace was imported into this store on "
        f"{entry.get('imported_at')} ({entry.get('event_count')} event(s)), which is "
        "why this store can say where its cursor landed but will not read it as its "
        "own position: "
    )
    if mapped is not None:
        details["mapped_cursor"] = int(mapped)
        details["mapped_cursor_token"] = cursor_token(namespace, int(mapped))
        message += (
            f"source cursor {source} became destination cursor {mapped} here, so "
            f"resume with --after {cursor_token(namespace, int(mapped))}"
        )
    else:
        message += (
            "this source cursor is not in the recorded map, so start from --after 0 "
            "and resume from the token this store returns"
        )
    return ForeignCursor(message, details=details)


def query(    sink,
    *,
    after=None,
    limit: Optional[int] = None,
    categories: Optional[Iterable[str]] = None,
    kinds: Optional[Iterable[str]] = None,
    subjects: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """One ordered page of the store's durable event stream.

    `next_cursor` is the cursor of the last event the page consumed: with
    `has_more` it is the cursor of the last event returned, and otherwise it is the
    store's newest cursor at read time. Either way, resuming after it neither skips
    a matching event nor rescans filtered-out traffic.
    """
    store = _require_store(sink)
    namespace = store.cursor_namespace()
    try:
        after_cursor = parse_cursor(after, namespace)
    except ForeignCursor as error:
        raise _foreign_cursor_detail(store, error, namespace) from None
    resolved_limit = DEFAULT_LIMIT if limit is None else int(limit)
    if resolved_limit < 1:
        raise InvalidRecord(f"--limit must be a positive integer, got {limit}")
    categories = _clean_terms(categories, EVENT_CATEGORIES, "event category")
    kinds = _clean_terms(kinds, EVENT_KINDS, "event kind")
    subject_terms = tuple(
        dict.fromkeys(str(s).strip() for s in (subjects or ()) if str(s).strip())
    )
    page = store.query_events(
        after_cursor=after_cursor,
        limit=resolved_limit,
        categories=categories or None,
        kinds=kinds or None,
        subject_ids=subject_terms or None,
    )
    return {
        "events": page["events"],
        "count": len(page["events"]),
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "next_cursor_token": cursor_token(namespace, page["next_cursor"]),
        "cursor_namespace": namespace,
        "limit": resolved_limit,
        "filters": {
            "after_cursor": after_cursor,
            "categories": list(categories),
            "kinds": list(kinds),
            "subjects": list(subject_terms),
        },
        "notice": EVENTS_NOTICE,
    }


def query_view(sink, **kwargs) -> Dict[str, Any]:
    """`query` with events rendered as JSON-ready dicts (used by the CLI)."""
    page = query(sink, **kwargs)
    namespace = page["cursor_namespace"]
    return dict(page, events=[event_view(e, namespace) for e in page["events"]])


# ---------------------------------------------------------------------------
# Reservation (coordinator) progress
# ---------------------------------------------------------------------------


def classify_member(ticket, verdict, active_attempt) -> str:
    """The classification for one member, in the documented precedence order.

    `verdict` is a `readiness.Readiness` (or None for a member whose ticket no
    longer exists, which is reported `unavailable`). Precedence is completed,
    blocked, active, dependency-waiting, ready, unavailable: an explicit operator
    statement (a closed or blocked ticket) outranks an inferred one, and a live
    attempt is a fact about work in progress rather than a readiness verdict."""
    if ticket is None:
        return "unavailable"
    if ticket.status == "closed":
        return "completed"
    if ticket.status == "blocked":
        return "blocked"
    if active_attempt is not None:
        return "active"
    if verdict is not None and verdict.has_axis("dependency"):
        return "dependency_waiting"
    if verdict is not None and verdict.ready:
        return "ready"
    return "unavailable"


def _initialised(store) -> bool:
    probe = getattr(store, "is_initialised", None)
    return True if not callable(probe) else bool(probe())


def _attempts_and_activity(store, by_id) -> Tuple[Dict[str, List[Any]], Dict[str, Any]]:
    """Per-ticket attempts and the newest event naming each subject (one read).

    The subject index is by event `subject_ids`, so a member's latest activity
    includes events that name one of its attempts (a read observation names the
    attempt) as well as events that name the ticket."""
    attempts: Dict[str, List[Any]] = {}
    newest: Dict[str, Any] = {}
    if store is None or not _initialised(store):
        return attempts, newest
    with store.transaction(write=False) as tx:
        for attempt in tx.find("work_attempt"):
            attempts.setdefault(attempt.ticket_id, []).append(attempt)
        events = list(tx.find("event"))
    for event in events:
        if event.cursor is None:
            continue
        for subject in event.subject_ids:
            current = newest.get(subject)
            if current is None or event.cursor > current.cursor:
                newest[subject] = event
    for rows in attempts.values():
        rows.sort(key=lambda a: (a.generation, a.started))
    return attempts, newest


def _member_activity(attempts: Sequence, newest: Dict[str, Any]):
    subjects = [a.id for a in attempts]
    best = None
    for subject in subjects:
        found = newest.get(subject)
        if found is not None and (best is None or found.cursor > best.cursor):
            best = found
    return best


def _attempt_view(attempt) -> Dict[str, Any]:
    return {
        "attempt_id": attempt.id,
        "worker_id": attempt.worker_id,
        "generation": attempt.generation,
        "state": attempt.state,
        "started": attempt.started,
        "last_activity": attempt.last_activity,
        "ended": attempt.ended,
        "outcome": attempt.outcome,
    }


def _member_view(
    ticket_id: str,
    ticket,
    verdict,
    attempts: Sequence,
    newest: Dict[str, Any],
    *,
    namespace: str,
    reservation_id: str,
    live_offer,
    package_id: Optional[str],
) -> Dict[str, Any]:
    active = next((a for a in attempts if a.state == "active"), None)
    classification = classify_member(ticket, verdict, active)
    latest = _member_activity(attempts, newest)
    if active is not None:
        worker_id, worker_source = active.worker_id, "active_attempt"
    elif attempts:
        worker_id, worker_source = attempts[-1].worker_id, "last_attempt"
    elif ticket is not None and ticket.assignee:
        worker_id, worker_source = ticket.assignee, "declared_assignee"
    else:
        worker_id, worker_source = None, None
    row: Dict[str, Any] = {
        "ticket_id": ticket_id,
        "title": None if ticket is None else ticket.title,
        "status": "missing" if ticket is None else ticket.status,
        "classification": classification,
        "reservation_id": reservation_id,
        "worker_id": worker_id,
        "worker_source": worker_source,
        "assignee": None if ticket is None else ticket.assignee,
        "blocked_by": None if ticket is None else ticket.blocked_by,
        "active_attempt": None if active is None else _attempt_view(active),
        "last_attempt": None if not attempts else _attempt_view(attempts[-1]),
        "latest_activity": None if latest is None else {
            "event_id": latest.id,
            "event_kind": latest.kind_,
            "category": latest.category,
            "timestamp": latest.timestamp,
            "cursor": latest.cursor,
            "cursor_token": cursor_token(namespace, latest.cursor),
        },
        "readiness": None if verdict is None else verdict.to_dict(),
        "package_id": package_id,
        "offer": None if live_offer is None else {
            "offer_id": live_offer.offer.id,
            "mode": live_offer.offer.mode,
            "state": live_offer.offer.state,
            "allowed_workers": list(live_offer.offer.allowed_workers),
        },
    }
    if ticket is None:
        row["reasons"] = [{
            "code": "ticket_missing",
            "axis": "status",
            "message": f"ticket {ticket_id} is a member of the reservation but no "
                       "longer exists in the ticket store; readiness cannot be evaluated",
            "hard": True,
            "details": {"ticket_id": ticket_id},
        }]
    else:
        row["reasons"] = [] if verdict is None else [
            reason.to_dict() for reason in verdict.reasons
        ]
        row["hints"] = [] if verdict is None else [h.to_dict() for h in verdict.hints]
    if classification == "dependency_waiting":
        unmet = sorted({
            dep for reason in row["reasons"] if reason["axis"] == "dependency"
            for dep in (reason["details"].get("unmet") or [])
        })
        row["dependency"] = {"unmet": unmet, "kind": "requery_hint"}
    return row


def reservation_progress(sink, rows, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """The coordinator's observation of each reservation in `rows`.

    `rows` are `reservations.StoredReservation` values. Readiness is evaluated for
    each reservation's *owner* (the worker who may acquire its members directly),
    through the shared evaluator; the activity map comes from the durable event log
    and the attempts themselves."""
    store = sink.coordination()
    every = sink.query(TicketQuery(buckets=("*",)))
    by_id = {ticket.id: ticket for ticket in every}
    if store is None:
        state = readiness.BoardState(by_id=by_id)
        namespace = "none"
    else:
        namespace = store.cursor_namespace()
        state = readiness.load(store, every)
    attempts_by_ticket, newest = _attempts_and_activity(store, by_id)

    declarations: Dict[str, Any] = {}
    reservations: List[Dict[str, Any]] = []
    totals: Dict[str, int] = dict.fromkeys(MEMBER_STATES, 0)
    for row in rows:
        reservation = row.reservation
        if reservation.owner not in declarations:
            declarations[reservation.owner] = workers.declaration_for(store, reservation.owner)
        declaration = declarations[reservation.owner]
        members = []
        counts: Dict[str, int] = dict.fromkeys(MEMBER_STATES, 0)
        for ticket_id in reservation.members:
            ticket = by_id.get(ticket_id)
            attempts = attempts_by_ticket.get(ticket_id, [])
            verdict = None
            if ticket is not None:
                verdict = readiness.evaluate(
                    ticket, state, worker_id=reservation.owner, declaration=declaration
                )
            member = _member_view(
                ticket_id, ticket, verdict, attempts, newest,
                namespace=namespace, reservation_id=reservation.id,
                live_offer=state.published_offers.get(ticket_id),
                package_id=None if ticket_id not in state.packages
                else state.packages[ticket_id].package.id,
            )
            counts[member["classification"]] += 1
            totals[member["classification"]] += 1
            members.append(member)
        reservations.append({
            "reservation_id": reservation.id,
            "owner": reservation.owner,
            "state": reservation.state,
            "revision": row.revision,
            "created": reservation.created,
            "updated": reservation.updated,
            "released": reservation.released,
            "release_reason": reservation.release_reason,
            "source": reservation.source,
            "note": reservation.note,
            "members": members,
            "counts": counts,
        })
    return {
        "owner": owner,
        "cursor_namespace": namespace,
        "reservations": reservations,
        "counts": totals,
        "notices": {
            "activity": ACTIVITY_NOTICE,
            "readiness": readiness.QUERY_NOTICE,
            "reservation": reservations_notice(),
        },
    }


def reservations_notice() -> str:
    """Imported lazily so this module has no import cycle with `reservations`."""
    from .reservations import RESERVATION_NOTICE

    return RESERVATION_NOTICE


__all__ = [
    "ACTIVITY_NOTICE",
    "CURSOR_SEPARATOR",
    "DEFAULT_LIMIT",
    "EVENTS_NOTICE",
    "MEMBER_STATES",
    "classify_member",
    "cursor_token",
    "event_view",
    "parse_cursor",
    "query",
    "query_view",
    "reservation_progress",
]
