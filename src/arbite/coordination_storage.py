"""Storage-neutral helpers shared by the two coordination sinks.

This module is the *envelope* layer for shared-directory coordination storage:
the small, pure functions that turn a coordination record plus its store-local
revision into a JSON-serialisable mapping and back, check an expected revision,
and describe a write-ahead journal intent. It deliberately contains **no**
filesystem and **no** SQL code so that both sinks -- the file sink's journal and
SQLite's real transactions -- can share one definition of what a stored
coordination record *is*, and so the whole layer is unit-testable without a
store to hand.

Two envelopes exist, and they are different things on purpose:

- a **record envelope** (`encode_record`) is the durable form of one record at
  one revision. The revision is store-local and monotonic per
  `(kind, record_id)`: 1 on first store, +1 on every successful put. It is kept
  *outside* the record's own payload so a record's `to_dict()` round-trip (and
  therefore the C01 JSON contract) is untouched.
- a **journal intent** (`journal_intent`) is the write-ahead description of one
  multi-record transaction: the resolved record writes *and* the resolved events
  with their pre-allocated cursors. A crash mid-apply leaves the intent behind and
  the next transaction replays it, which is what lets the file sink commit
  related state and events together without a database.

Nothing here retries, sleeps, scans or repairs: it is data plus pure helpers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from . import coordination
from .errors import CoordinationConflict, InvalidRecord

#: Version of a stored record envelope. Stored on every envelope so a later
#: migration can tell which layout a payload used.
RECORD_ENVELOPE_VERSION = 1

#: Version of a write-ahead journal intent.
JOURNAL_VERSION = 1

#: Journal intent state. The only state a journal file is ever written in by
#: `commit`; replay removes it once applied, so a leftover file *is* "pending".
JOURNAL_STATE_PENDING = "pending"

#: Prefix for the temp files the file sink stages before `os.replace`, kept
#: distinct from the ticket sink's `TMP_PREFIX` so `arbite doctor` does not report
#: a coordination temp file as a stranded ticket write.
COORDINATION_TMP_PREFIX = ".arbite-coord-tmp-"

#: Crash-injection phases for the file sink's commit protocol. A test may pass one
#: of these as `crash_point=`; the store then exits the process at that boundary
#: so a journal / partially-applied state can be produced on purpose. Never set
#: outside a crash-injection test.
CRASH_AFTER_JOURNAL = "after_journal"
CRASH_AFTER_RECORDS = "after_records"
CRASH_AFTER_EVENTS = "after_events"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def as_mapping(payload: Any, *, what: str) -> dict:
    """The mapping inside `payload`, which may be a JSON string/bytes or a dict.

    Raises `InvalidRecord` for anything that is not a mapping, or a mapping whose
    `schema_version` is missing/not a positive integer -- a stored payload we
    cannot version is a payload we cannot honestly interpret.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as e:
            raise InvalidRecord(f"{what} is not valid JSON: {e}")
    if not isinstance(payload, dict):
        raise InvalidRecord(f"{what} must be a JSON object, got {type(payload).__name__}")
    version = payload.get("schema_version")
    if not _is_int(version) or version < 1:
        raise InvalidRecord(
            f"{what} has no usable schema_version (got {version!r}); a stored "
            "payload that cannot be versioned cannot be interpreted"
        )
    return payload


# ---------------------------------------------------------------------------
# Record envelopes
# ---------------------------------------------------------------------------


def encode_record(record, revision: int) -> dict:
    """The durable envelope for `record` at `revision`.

    `revision` is the store-local revision being written (>= 1). The record's own
    payload comes from `record.to_dict()`, so the C01 JSON contract is unchanged.
    """
    if not _is_int(revision) or revision < 1:
        raise InvalidRecord(f"a stored revision must be an integer >= 1, got {revision!r}")
    return {
        "schema_version": RECORD_ENVELOPE_VERSION,
        "revision": int(revision),
        "record": record.to_dict(),
    }


def decode_record(payload: Any) -> Tuple[Any, int]:
    """`(record, revision)` from an envelope produced by `encode_record`."""
    envelope = as_mapping(payload, what="stored record envelope")
    revision = envelope.get("revision")
    if not _is_int(revision) or revision < 1:
        raise InvalidRecord(
            f"stored record envelope has no usable revision (got {revision!r})"
        )
    return coordination.record_from_dict(envelope.get("record")), int(revision)


# ---------------------------------------------------------------------------
# Revision checking
# ---------------------------------------------------------------------------


def check_revision(
    kind: str,
    record_id: str,
    expect_revision: Optional[int],
    current: int,
) -> int:
    """`current` when the expectation holds, else raise `CoordinationConflict`.

    A `None` expectation means "no check" -- last-write-wins, exactly as the
    unguarded ticket path is documented. An explicit expectation that does not
    equal the stored revision raises *before* anything is written, and the error
    carries the numbers a caller needs to re-read and retry.
    """
    current = int(current)
    if expect_revision is None:
        return current
    if not _is_int(expect_revision):
        raise InvalidRecord(
            f"expect_revision must be an integer or None, got {expect_revision!r}"
        )
    expected = int(expect_revision)
    if expected != current:
        raise CoordinationConflict(
            f"{kind} {record_id} is at revision {current}, not {expected}; "
            "re-read it and retry",
            details={
                "kind": kind,
                "record_id": record_id,
                "expected": expected,
                "current": current,
            },
        )
    return current


# ---------------------------------------------------------------------------
# Event envelopes
# ---------------------------------------------------------------------------


def encode_event(event, cursor: int) -> dict:
    """The durable envelope for an *append-only* event at its assigned `cursor`."""
    if not _is_int(cursor) or cursor < 1:
        raise InvalidRecord(f"an event cursor must be an integer >= 1, got {cursor!r}")
    event.cursor = int(cursor)
    return {
        "schema_version": RECORD_ENVELOPE_VERSION,
        "cursor": int(cursor),
        "event": event.to_dict(),
    }


def decode_event(payload: Any) -> Any:
    """The `coordination.Event` from an envelope produced by `encode_event`."""
    envelope = as_mapping(payload, what="stored event envelope")
    cursor = envelope.get("cursor")
    if not _is_int(cursor) or cursor < 1:
        raise InvalidRecord(f"stored event envelope has no usable cursor (got {cursor!r})")
    event = coordination.record_from_dict(envelope.get("event"))
    event.cursor = int(cursor)
    return event


# ---------------------------------------------------------------------------
# Journal intents (the file sink's write-ahead protocol)
# ---------------------------------------------------------------------------


def journal_intent(
    operation_id: str,
    *,
    workspace_ids: Optional[List[str]] = None,
    records: Optional[List[dict]] = None,
    events: Optional[List[dict]] = None,
) -> dict:
    """A write-ahead intent describing one resolved multi-record transaction.

    `records` entries are `{"kind", "record_id", "revision", "payload"}` and
    `events` entries are `{"cursor", "payload"}` -- both already resolved, so
    replay is a pure re-application and never re-decides anything.
    """
    return {
        "schema_version": JOURNAL_VERSION,
        "state": JOURNAL_STATE_PENDING,
        "operation_id": operation_id,
        "workspace_ids": list(workspace_ids or []),
        "records": list(records or []),
        "events": list(events or []),
    }


def decode_journal(payload: Any) -> dict:
    """A validated journal intent from `journal_intent` output."""
    intent = as_mapping(payload, what="coordination journal")
    if not isinstance(intent.get("operation_id"), str) or not intent["operation_id"]:
        raise InvalidRecord("coordination journal has no operation_id")
    for key in ("workspace_ids", "records", "events"):
        if not isinstance(intent.get(key), list):
            raise InvalidRecord(f"coordination journal field {key!r} must be a list")
    return intent


# ---------------------------------------------------------------------------
# Namespace registry entries (planning key C11)
# ---------------------------------------------------------------------------
#
# An export that merges several stores' event streams has to say which store
# each store-local cursor came from. A *namespace entry* is that statement: for
# one imported namespace it records how many events came in, which source cursor
# mapped to which destination cursor, and under which contract version. The
# shape is deliberately flat -- five keys, no nested envelope -- so it is obvious
# in a diff and cheap to decode, and because it is built and validated in one
# place the two sinks cannot drift on what a valid entry is.


def _cursor_key(value: Any, what: str) -> str:
    """A source cursor normalised to its canonical string key.

    JSON cannot preserve integer object keys, so a stored entry always reads back
    with string keys; accepting an int here too means a caller building an entry
    in memory and a decoder reading one back agree on the same shape.
    """
    if _is_int(value) and value >= 1:
        return str(int(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() and int(text) >= 1:
            return text
    raise InvalidRecord(f"{what} must be a positive integer, got {value!r}")


def _cursor_number(value: Any, what: str) -> int:
    if not _is_int(value) or value < 1:
        raise InvalidRecord(f"{what} must be an integer >= 1, got {value!r}")
    return int(value)


def namespace_record(
    namespace: Any,
    *,
    imported_at: Any,
    event_count: Any,
    cursor_map: Any,
    source_contract_version: Any,
) -> dict:
    """Build and validate one namespace-registry entry.

    The entry is exactly::

        {"namespace": str,
         "imported_at": <UTC timestamp>,
         "event_count": int,
         "cursor_map": {str(source_cursor): int(destination_cursor)},
         "source_contract_version": int}

    `cursor_map` source keys are normalised to strings (a JSON round-trip cannot
    keep integer keys) while destination cursors stay integers. Every field is
    validated here, and `InvalidRecord` is raised for bad input, so the file sink
    and the SQLite sink share one definition of a valid entry.
    """
    if not isinstance(namespace, str) or not namespace:
        raise InvalidRecord(f"a namespace must be a non-empty string, got {namespace!r}")
    if not coordination.is_utc_timestamp(imported_at):
        raise InvalidRecord(
            "a namespace imported_at must be a YYYY-MM-DDTHH:MM:SSZ UTC timestamp, "
            f"got {imported_at!r}"
        )
    if not _is_int(event_count) or event_count < 0:
        raise InvalidRecord(
            f"a namespace event_count must be an integer >= 0, got {event_count!r}"
        )
    if not isinstance(cursor_map, Mapping):
        raise InvalidRecord(
            f"a namespace cursor_map must be a mapping, got {type(cursor_map).__name__}"
        )
    clean_cursor_map: Dict[str, int] = {}
    for source, destination in cursor_map.items():
        clean_cursor_map[_cursor_key(source, "a cursor_map source cursor")] = _cursor_number(
            destination, "a cursor_map destination cursor"
        )
    if not _is_int(source_contract_version) or source_contract_version < 1:
        raise InvalidRecord(
            "a namespace source_contract_version must be an integer >= 1, got "
            f"{source_contract_version!r}"
        )
    return {
        "namespace": namespace,
        "imported_at": imported_at,
        "event_count": int(event_count),
        "cursor_map": clean_cursor_map,
        "source_contract_version": int(source_contract_version),
    }


def decode_namespace(payload: Any) -> dict:
    """A validated namespace-registry entry from its stored form.

    `payload` may be a JSON string/bytes, a mapping, or a mapping whose
    `cursor_map` is itself a JSON string -- the SQLite sink stores the map in
    its own column, and this keeps one decoder for both. Anything malformed
    raises `InvalidRecord`.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as e:
            raise InvalidRecord(f"a stored namespace entry is not valid JSON: {e}")
    if not isinstance(payload, Mapping):
        raise InvalidRecord(
            f"a stored namespace entry must be a JSON object, got {type(payload).__name__}"
        )
    cursor_map = payload.get("cursor_map")
    if isinstance(cursor_map, (bytes, bytearray)):
        cursor_map = cursor_map.decode("utf-8")
    if isinstance(cursor_map, str):
        try:
            cursor_map = json.loads(cursor_map)
        except ValueError as e:
            raise InvalidRecord(f"a stored namespace cursor_map is not valid JSON: {e}")
    return namespace_record(
        payload.get("namespace"),
        imported_at=payload.get("imported_at"),
        event_count=payload.get("event_count"),
        cursor_map=cursor_map,
        source_contract_version=payload.get("source_contract_version"),
    )


def safe_component(value: Any, *, what: str) -> str:
    """A path component safe to use as a filename under the store root.

    Record kinds and opaque record ids are the storage keys of the file sink; a
    value containing a separator or `..` is refused rather than normalised, so a
    caller can never address a file outside the coordination directory.
    """
    if not isinstance(value, str) or not value:
        raise InvalidRecord(f"{what} must be a non-empty string, got {value!r}")
    if value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
        raise InvalidRecord(f"{what} {value!r} is not a usable store key")
    return value
