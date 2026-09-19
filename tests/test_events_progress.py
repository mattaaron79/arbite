"""Resumable event queries and coordinator progress views (planning key B06).

Two layers are checked here:

- the store-level query API (`arbite.events` over
  `CoordinationStore.query_events`): ordering, cursor consistency under filters,
  limit boundaries, stable ids, and the file sink reconciling an interrupted
  commit *before* its events are reported, run against both sinks through the
  `sink` fixture;
- the `arbite events` and `arbite reserve progress` CLI surfaces, driven as real
  subprocesses in a throwaway project for each sink -- including a cursor that
  resumes across two separate process invocations.

Nothing here touches the checkout's own `.arbite/` store.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from arbite import coordination as c
from arbite import events, readiness
from arbite.coordination_storage import CRASH_AFTER_JOURNAL
from arbite.errors import ForeignCursor, InvalidCursor, InvalidRecord
from arbite.sinks.coordination_file import FileCoordinationStore
from conftest import make_sink
from helpers import make_ticket

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def append(sink, kind: str, category: str, *, subjects=(), payload=None) -> str:
    """Append one event through the store, the way an operation would."""
    with sink.coordination().transaction() as tx:
        event = c.Event(
            id=c.new_record_id("event"),
            kind_=kind,
            category=category,
            timestamp=c.utc_now(),
            subject_ids=list(subjects),
            payload=dict(payload or {}),
        )
        return tx.append_event(event).id


def walk(sink, **kwargs) -> list:
    """Every page of one query, each resumed by the token the previous page returned."""
    pages = []
    token = 0
    for _ in range(50):
        page = events.query(sink, after=token, **kwargs)
        pages.append(page)
        if not page["has_more"]:
            break
        token = page["next_cursor_token"]
    assert not pages[-1]["has_more"], "the walk did not terminate"
    return pages


def flat(pages) -> list:
    return [event for page in pages for event in page["events"]]


def crash_with_a_journalled_event(root: str, payload: dict) -> None:
    """Journal one event with the documented hook, then die before applying it."""
    store = FileCoordinationStore(Path(root), crash_point=CRASH_AFTER_JOURNAL)
    with store.transaction() as tx:
        tx.append_event(c.Event.from_dict(payload))
    os._exit(97)  # pragma: no cover - the crash point exits first


# ---------------------------------------------------------------------------
# cursor tokens
# ---------------------------------------------------------------------------


def test_a_cursor_token_binds_its_cursor_to_the_store_that_issued_it():
    namespace = "sqlite:/tmp/p/.arbite/arbite.db"
    assert events.cursor_token(namespace, 41) == "sqlite:/tmp/p/.arbite/arbite.db#41"

    # From the beginning, however a caller spells it.
    for start in (None, "", "  ", 0, "0"):
        assert events.parse_cursor(start, namespace) == 0
    assert events.parse_cursor(events.cursor_token(namespace, 41), namespace) == 41

    # A bare integer names no store, so it is refused rather than trusted: reading
    # another store's position silently is the failure the token exists to prevent.
    with pytest.raises(InvalidCursor) as bare:
        events.parse_cursor(7, namespace)
    assert bare.value.error_code == "invalid_cursor"
    assert "bare integer" in str(bare.value)
    assert bare.value.details["cursor_namespace"] == namespace

    for value in (True, "not-a-token", f"{namespace}#later"):
        with pytest.raises(InvalidCursor):
            events.parse_cursor(value, namespace)

    with pytest.raises(ForeignCursor) as foreign:
        events.parse_cursor("file:/elsewhere/coordination#3", namespace)
    assert foreign.value.error_code == "cursor_foreign_store"
    assert foreign.value.details["token_namespace"] == "file:/elsewhere/coordination"
    assert foreign.value.details["cursor_namespace"] == namespace

    # A namespace that itself contains the separator still round-trips: the
    # separator is the *last* one.
    odd = "file:/tmp/a#b/coordination"
    assert events.parse_cursor(events.cursor_token(odd, 5), odd) == 5


def test_member_classification_precedence_is_documented():
    ad_hoc = "w.a"
    ticket = make_ticket("tic-a1b2")
    state = readiness.BoardState(by_id={"tic-a1b2": ticket})
    verdict = readiness.evaluate(ticket, state, worker_id=ad_hoc)
    assert verdict.ready is True
    assert events.classify_member(ticket, verdict, None) == "ready"

    # A missing ticket cannot be evaluated at all.
    assert events.classify_member(None, None, None) == "unavailable"

    # Explicit operator statements outrank inferences, and a live attempt is a fact
    # about work in progress rather than a readiness verdict.
    closed = make_ticket("tic-a1b2", status="closed", closed="2026-02-01T00:00:00")
    assert events.classify_member(closed, None, object()) == "completed"
    blocked = make_ticket("tic-a1b2", status="blocked", blocked_by="waiting")
    assert events.classify_member(blocked, None, object()) == "blocked"
    assert events.classify_member(ticket, verdict, object()) == "active"

    waiting = make_ticket("tic-a1b2", depends_on=["tic-b2c3"])
    pair = {"tic-a1b2": waiting, "tic-b2c3": make_ticket("tic-b2c3")}
    waiting_verdict = readiness.evaluate(
        waiting, readiness.BoardState(by_id=pair), worker_id=ad_hoc
    )
    assert waiting_verdict.ready is False
    assert events.classify_member(waiting, waiting_verdict, None) == "dependency_waiting"

    assert events.MEMBER_STATES == (
        "completed", "blocked", "active", "dependency_waiting", "ready", "unavailable",
    )


# ---------------------------------------------------------------------------
# the store-level query API, on both sinks
# ---------------------------------------------------------------------------


def test_query_walks_the_whole_ordered_stream_in_pages(sink):
    for index in range(5):
        append(sink, "ticket_claimed", "lifecycle", subjects=["tic-0001"])
        append(sink, "read_observed", "read", subjects=[f"att-{index:04d}"])

    pages = walk(sink, limit=3)
    ordered = flat(pages)
    cursors = [event.cursor for event in ordered]
    assert len(ordered) == 10
    assert cursors == sorted(set(cursors))  # ordered by cursor, no repeats
    assert len({event.id for event in ordered}) == 10  # ... with stable unique ids
    assert pages[-1]["has_more"] is False
    assert pages[-1]["next_cursor"] == cursors[-1]  # the whole stream was consumed

    # A repeated query re-delivers exactly the same events: consumers deduplicate by id.
    again = events.query(sink, limit=3)
    assert [event.id for event in again["events"]] == [event.id for event in pages[0]["events"]]
    assert again["next_cursor"] == pages[0]["next_cursor"]

    # Nothing new after the newest cursor: an empty page that does not move backwards.
    empty = events.query(sink, after=pages[-1]["next_cursor_token"])
    assert empty["events"] == [] and empty["has_more"] is False
    assert empty["next_cursor"] == cursors[-1]


def test_query_filters_advance_the_cursor_consistently(sink):
    for _ in range(3):
        append(sink, "ticket_claimed", "lifecycle", subjects=["tic-0001"])
        append(sink, "read_observed", "read", subjects=["att-0001"])

    pages = walk(sink, limit=2, categories=("read",))
    ordered = flat(pages)
    assert [event.cursor for event in ordered] == [2, 4, 6]
    assert {event.category for event in ordered} == {"read"}
    assert len(pages) == 2 and pages[0]["has_more"] is True
    # The first page stopped after the second *matching* event, so the second page
    # starts right there: filtered-out traffic is neither skipped nor re-read.
    assert pages[0]["next_cursor"] == 4
    assert [event.cursor for event in pages[1]["events"]] == [6]
    assert pages[1]["next_cursor"] == 6

    # Kind and subject filters use the same machinery.
    assert [event.cursor for event in flat(walk(sink, kinds=("ticket_claimed",)))] == [1, 3, 5]
    assert [event.cursor for event in flat(walk(sink, subjects=("att-0001",)))] == [2, 4, 6]
    assert flat(walk(sink, subjects=("att-0001", "tic-0001"))) == flat(walk(sink))

    # An unknown filter value fails instead of quietly matching nothing.
    with pytest.raises(InvalidRecord):
        events.query(sink, categories=("nope",))
    with pytest.raises(InvalidRecord):
        events.query(sink, kinds=("not_a_kind",))
    with pytest.raises(InvalidRecord):
        events.query(sink, limit=0)
    with pytest.raises(InvalidRecord):
        sink.coordination().query_events(limit=0)

    # `limit=None` is the whole stream, and an empty subject list is no filter.
    assert len(events.query(sink, limit=None)["events"]) == 6


def test_query_answers_empty_and_creates_nothing_on_a_store_that_does_not_exist(
    kind, arbite_dir
):
    sink = make_sink(kind, arbite_dir, initialise=False)
    store = sink.coordination()
    assert store.is_initialised() is False

    page = events.query(sink)
    assert page["events"] == [] and page["count"] == 0 and page["has_more"] is False
    assert page["next_cursor"] == 0
    assert store.query_events(after_cursor=9, limit=5) == {
        "events": [], "next_cursor": 9, "has_more": False, "scanned": 0,
    }
    # A query is a query: it may not create a store, a directory or a database.
    assert list(arbite_dir.iterdir()) == []
    assert store.is_initialised() is False


def test_only_reconciled_events_are_reported_after_an_interrupted_commit(arbite_dir):
    """The file sink journals before applying, so a crash leaves an intent -- not a
    half-visible event. The next read reconciles it, exactly once, and then it is final."""
    coordination_dir = arbite_dir / "coordination"
    planned = c.Event(
        id=c.new_record_id("event"),
        kind_="ticket_claimed",
        category="lifecycle",
        timestamp=c.utc_now(),
        subject_ids=["tic-a1b2"],
        payload={"ticket_id": "tic-a1b2"},
    )
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=crash_with_a_journalled_event, args=(str(coordination_dir), planned.to_dict())
    )
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(coordination_dir)
    assert len(store.pending_journals()) == 1  # journalled ...
    assert store.inspect_events() == []        # ... and therefore not visible yet

    sink = make_sink("file", arbite_dir)
    page = events.query(sink)
    assert [event.id for event in page["events"]] == [planned.id]
    assert [event.cursor for event in page["events"]] == [1]
    assert store.pending_journals() == []      # the replay resolved the commit
    assert [entry["cursor"] for entry in store.inspect_events()] == [1]

    # Reconciled exactly once: resuming after it finds nothing, and the id is stable.
    resumed = events.query(sink, after=page["next_cursor_token"])
    assert resumed["events"] == [] and resumed["next_cursor"] == 1


# ---------------------------------------------------------------------------
# the CLI: `arbite events` and `arbite reserve progress`
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_project, kind):
    def run(*args, expect=0):
        environment = dict(os.environ, PYTHONPATH=str(SRC_DIR), ARBITE_SINK=kind)
        proc = subprocess.run(
            [sys.executable, "-m", "arbite.cli", *args],
            cwd=str(tmp_project), env=environment, capture_output=True, text=True,
        )
        if expect is not None:
            assert proc.returncode == expect, (
                f"arbite {' '.join(args)} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    run("init")
    return run


def created_id(proc) -> str:
    return re.search(r"(tic-[0-9a-f]{4})", proc.stdout).group(1)


def make(cli, title: str, **extra) -> str:
    args = ["create", "--title", title, "--type", "bug", "--tier", "low", "--domain", "py"]
    for name, value in extra.items():
        args += [f"--{name.replace('_', '-')}", str(value)]
    return created_id(cli(*args))


def page_of(cli, *extra, expect=0) -> dict:
    return json.loads(cli("events", "--json", *extra, expect=expect).stdout)["data"]


def all_events(cli, *extra) -> list:
    """Every matching event, walked page by page from the beginning of the stream."""
    seen: list = []
    token = "0"
    for _ in range(20):
        proc = cli("events", "--after", token, "--json", *extra, expect=None)
        if proc.returncode == 2:
            return seen
        assert proc.returncode == 0, proc.stderr
        page = json.loads(proc.stdout)["data"]
        seen.extend(page["events"])
        if not page["has_more"]:
            return seen
        token = page["next_cursor"]
    raise AssertionError("walking the event stream did not terminate")


def by_kind(cli, *extra) -> dict:
    grouped: dict = {}
    for event in all_events(cli, *extra):
        grouped.setdefault(event["event_kind"], []).append(event)
    return grouped


def active_attempt(cli, ticket: str) -> str:
    """The ticket's active attempt id, from the documented coordination export."""
    document = json.loads(
        cli("export", "--scope", "coordination", "--no-artifacts").stdout
    )
    return [
        attempt["id"] for attempt in document["records"]["work_attempts"]
        if attempt["ticket_id"] == ticket and attempt["state"] == "active"
    ][0]


def test_events_cli_pages_across_processes_and_is_dedupable(cli):
    first = make(cli, "the first", priority=1)
    second = make(cli, "the second", priority=2)
    cli("claim", first, "--agent", "worker.a")
    cli("close", first, "--agent", "worker.a")
    cli("claim", second, "--agent", "worker.b")

    page = page_of(cli, "--after", "0", "--limit", "2")
    assert page["count"] == 2 and page["has_more"] is True
    assert [event["cursor"] for event in page["events"]] == [1, 2]
    assert page["next_cursor"].startswith(page["cursor_namespace"] + "#")

    # A *separate process* resumes exactly where this one stopped.
    resumed = page_of(cli, "--after", page["next_cursor"], "--limit", "2")
    assert [event["cursor"] for event in resumed["events"]] == [3, 4]

    every = all_events(cli)
    cursors = [event["cursor"] for event in every]
    assert len(cursors) > 6
    assert cursors == sorted(set(cursors))  # ordered, and no event twice
    assert len({event["id"] for event in every}) == len(every)  # stable unique ids
    assert every[0]["event_kind"] == "workspace_bound"

    # A repeated query re-delivers the same events with the same ids, so a consumer
    # that deduplicates by id can retry safely.
    again = page_of(cli, "--after", "0", "--limit", "2")
    assert [event["id"] for event in again["events"]] == [event["id"] for event in page["events"]]
    assert again["next_cursor"] == page["next_cursor"]

    # Reading everything leaves a cursor that has not moved backwards: nothing new
    # after it is an empty page (exit 2 is "the query ran, nothing matched").
    newest = page_of(cli, "--after", "0")
    assert newest["has_more"] is False
    assert newest["next_cursor"] != "0"
    empty = page_of(cli, "--after", newest["next_cursor"], expect=2)
    assert empty["events"] == [] and empty["next_cursor"] == newest["next_cursor"]

    # --limit must be a positive integer, and the filters are validated.
    assert json.loads(
        cli("events", "--after", "0", "--limit", "0", "--json", expect=1).stdout
    )["code"] == "invalid_record"
    assert json.loads(
        cli("events", "--category", "nope", "--json", expect=1).stdout
    )["code"] == "invalid_record"

    # The human output states where to resume.
    text = cli("events", "--after", "0", "--limit", "2").stdout
    assert page["next_cursor"] in text and "(more available)" in text


def test_events_cli_refuses_a_cursor_it_cannot_trust(cli):
    ticket = make(cli, "something", priority=1)
    cli("claim", ticket, "--agent", "worker.a")

    bare = json.loads(cli("events", "--after", "7", "--json", expect=1).stdout)
    assert bare["ok"] is False and bare["code"] == "invalid_cursor"
    assert bare["details"]["cursor"] == 7
    assert "bare integer" in bare["message"]

    malformed = json.loads(cli("events", "--after", "not-a-token", "--json", expect=1).stdout)
    assert malformed["code"] == "invalid_cursor"

    foreign = json.loads(
        cli("events", "--after", "file:/somewhere/else/coordination#3", "--json", expect=1).stdout
    )
    assert foreign["code"] == "cursor_foreign_store"
    assert foreign["details"]["token_namespace"] == "file:/somewhere/else/coordination"
    assert foreign["details"]["cursor_namespace"]
    assert foreign["details"]["cursor_namespace"] != "file:/somewhere/else/coordination"

    # It never silently restarts from zero: the exit code is an error, not a result.
    human = cli("events", "--after", "7", expect=1)
    assert "bare integer" in human.stderr and human.stdout == ""


def test_events_cli_separates_read_observations_from_lifecycle_traffic(cli, tmp_project):
    (tmp_project / "work.py").write_text("print('hi')\n", encoding="utf-8")
    ticket = make(cli, "work", priority=1)
    cli("claim", ticket, "--agent", "worker.a")
    attempt = active_attempt(cli, ticket)
    cli("file", "claim", "work.py", "--ticket", ticket, "--attempt", attempt)
    cli("file", "read", "work.py", "--ticket", ticket, "--attempt", attempt, "--json")

    reads = page_of(cli, "--after", "0", "--category", "read")
    assert [event["event_kind"] for event in reads["events"]] == ["read_observed"]
    assert reads["events"][0]["payload"]["path"] == "work.py"
    assert reads["events"][0]["payload"]["attempt_id"] == attempt

    lifecycle = all_events(cli, "--category", "lifecycle")
    kinds = [event["event_kind"] for event in lifecycle]
    assert "ticket_claimed" in kinds and "read_observed" not in kinds

    # A subject filter names the affected work: the ticket's own transitions, and
    # (by attempt id) the read observations the ticket's attempt produced.
    by_ticket = all_events(cli, "--subject", ticket)
    assert all(ticket in event["subject_ids"] for event in by_ticket)
    ticket_kinds = [event["event_kind"] for event in by_ticket]
    assert "ticket_claimed" in ticket_kinds and "read_observed" not in ticket_kinds
    assert "read_observed" in [event["event_kind"] for event in all_events(
        cli, "--subject", attempt
    )]


def test_progress_cli_classifies_members_and_states_activity_is_not_liveness(cli):
    active = make(cli, "being worked", priority=1)
    stopped = make(cli, "explicitly blocked", priority=2)
    ready = make(cli, "ready to pick up", priority=3)
    waiting = make(cli, "waits for the blocked one", priority=4)
    done = make(cli, "already done", priority=5)
    reservation = json.loads(cli(
        "reserve", "create", active, stopped, ready, waiting, done,
        "--agent", "coord.one", "--json",
    ).stdout)["data"]["reservation"]["id"]
    cli("claim", active, "--agent", "coord.one")
    cli("block", stopped, "--reason", "waiting on the upstream fix", "--agent", "coord.one")
    cli("depend", waiting, stopped)
    cli("claim", done, "--agent", "coord.one")
    cli("close", done, "--agent", "coord.one")

    data = json.loads(cli("reserve", "progress", reservation, "--json").stdout)["data"]
    assert data["counts"] == {
        "completed": 1, "blocked": 1, "active": 1,
        "dependency_waiting": 1, "ready": 1, "unavailable": 0,
    }
    members = {m["ticket_id"]: m for m in data["reservations"][0]["members"]}
    assert set(members) == {active, stopped, ready, waiting, done}
    assert members[active]["classification"] == "active"
    assert members[stopped]["classification"] == "blocked"
    assert members[stopped]["blocked_by"] == "waiting on the upstream fix"
    assert members[ready]["classification"] == "ready"
    assert members[done]["classification"] == "completed"
    assert members[waiting]["classification"] == "dependency_waiting"
    assert members[waiting]["dependency"] == {"unmet": [stopped], "kind": "requery_hint"}

    # "Ready" is the shared evaluator's verdict for the reservation owner -- the
    # same evaluator `arbite board` reports, so the two cannot disagree.
    assert members[ready]["readiness"]["ready"] is True
    assert members[waiting]["readiness"]["reasons"][0]["code"] == "dependencies_unmet"
    assert members[active]["readiness"]["reasons"][0]["code"] == "ticket_not_open"
    board = json.loads(cli("board", "--worker", "coord.one", "--json").stdout)["data"]
    assert ready in [ticket["id"] for ticket in board["ready"]]

    # Current worker and latest recorded activity come from durable records only.
    assert members[active]["worker_id"] == "coord.one"
    assert members[active]["active_attempt"]["attempt_id"]
    assert members[done]["worker_id"] == "coord.one"
    naming = [event for event in all_events(cli) if active in event["subject_ids"]]
    assert members[active]["latest_activity"]["cursor"] == max(
        event["cursor"] for event in naming
    )
    assert "never a liveness guarantee" in data["notices"]["activity"]
    assert "derived from current state every time" in data["notices"]["activity"]

    # The coordinator's whole board, and the same caveats in the human output.
    owned = json.loads(cli(
        "reserve", "progress", "--owner", "coord.one", "--json"
    ).stdout)["data"]
    assert [row["reservation_id"] for row in owned["reservations"]] == [reservation]
    text = cli("reserve", "progress", reservation).stdout
    assert "dependency_waiting" in text
    assert "blocked by: waiting on the upstream fix" in text
    assert "never a liveness guarantee" in text

    # An owner with no reservations is an empty answer, and the two selectors are
    # mutually exclusive.
    assert json.loads(cli(
        "reserve", "progress", "--owner", "nobody.at.all", "--json", expect=2
    ).stdout)["data"]["reservations"] == []
    assert "not both" in cli(
        "reserve", "progress", reservation, "--owner", "coord.one", expect=1
    ).stderr
    assert "progress needs a reservation id" in cli(
        "reserve", "progress", expect=1
    ).stderr


def test_publish_accept_complete_and_package_events_name_the_ids_a_caller_needs(cli):
    offered = make(cli, "offered work", priority=1)
    packaged_first = make(cli, "packaged first", priority=2)
    packaged_second = make(cli, "packaged second", priority=3)
    reservation = json.loads(cli(
        "reserve", "create", offered, "--agent", "coord.one", "--json"
    ).stdout)["data"]["reservation"]["id"]
    offer = json.loads(cli(
        "offer", "publish", offered, "--agent", "coord.one", "--json"
    ).stdout)["data"]["offer"]["id"]

    published = by_kind(cli)["offer_published"][0]
    assert published["payload"]["offer_id"] == offer
    assert published["payload"]["reservation_id"] == reservation
    assert published["payload"]["ticket_ids"] == [offered]

    accepted = json.loads(cli(
        "offer", "claim", offer, "--agent", "worker.b", "--json"
    ).stdout)["data"]
    attempt = accepted["attempt_id"]
    grouped = by_kind(cli)
    started = grouped["attempt_started"][0]
    assert started["payload"]["attempt_id"] == attempt
    assert started["payload"]["offer_id"] == offer
    assert started["payload"]["ticket_id"] == offered
    assert started["payload"]["worker_id"] == "worker.b"
    claimed = grouped["ticket_claimed"][0]
    assert claimed["payload"]["attempt_id"] == attempt
    accepted_event = grouped["offer_accepted"][0]
    assert accepted_event["payload"]["offer_id"] == offer
    assert accepted_event["payload"]["reservation_id"] == reservation
    assert accepted_event["payload"]["worker_id"] == "worker.b"
    assert accepted_event["payload"]["attempt_id"] == attempt

    # Completion names the attempt that finished, so a caller can requery the work.
    cli("close", offered, "--agent", "worker.b")
    grouped = by_kind(cli)
    finished = grouped["attempt_finished"][0]
    assert finished["payload"]["attempt_id"] == attempt
    assert finished["payload"]["ticket_id"] == offered
    assert grouped["offer_completed"][0]["payload"]["offer_id"] == offer
    assert grouped["offer_completed"][0]["payload"]["attempt_id"] == attempt

    # A package advance names the package, the closed member's attempt and its worker.
    package = json.loads(cli(
        "package", "create", packaged_first, packaged_second,
        "--agent", "coord.one", "--json",
    ).stdout)["data"]["package"]["id"]
    package_claim = json.loads(cli(
        "package", "claim", package, "--agent", "worker.c", "--json"
    ).stdout)["data"]
    cli("close", packaged_first, "--agent", "worker.c")
    grouped = by_kind(cli)
    bound = grouped["package_bound"][0]
    assert bound["payload"]["package_id"] == package
    assert bound["payload"]["worker_id"] == "worker.c"
    advanced = grouped["package_advanced"][0]
    assert advanced["payload"]["package_id"] == package
    assert advanced["payload"]["attempt_id"] == package_claim["attempt_id"]
    assert advanced["payload"]["worker_id"] == "worker.c"
    assert advanced["payload"]["closed"] == packaged_first
    assert advanced["payload"]["next"] == packaged_second
