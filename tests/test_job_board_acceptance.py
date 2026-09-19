"""B08: end-to-end acceptance for manual multi-provider job-board coordination.

This is the epic's acceptance slice. It drives the real CLI in a subprocess --
the way a manually started worker does -- against both shipped sinks, and asserts
the recipe in `.arbite/planning/multi-provider-job-board.md` ("Acceptance
demonstration") plus the extra scenarios the plan's final paragraph names.

In the plan's order: two differently labelled workers mixing ad-hoc work with a
coordinator's reservation; a direct assignment and a two-ticket continuity
package picked up by the other worker; unauthorised claims (reservation, direct
assignment, package order, dependency) refused; structured progress and a board
query answered with no resident orchestrator; separate attempts and proxy change
receipts per package member, with file claims released at each ticket boundary
and a fresh read required for the next; a capacity-limited batch and a
simultaneous offer acceptance with exactly one winner; withdraw-vs-claim and
reserve-vs-claim races with defined serial outcomes; incremental event queries
across separate process invocations; partial package completion with an explicit
handoff; a disabled profile that keeps its history; capability/tier/cost
unknowns; releasing a reservation while an attempt is active; a file-sink
journalled event reconciled exactly once after a crash; and a quiescent transfer
between both sinks that preserves board state.

Nothing here starts a daemon, watcher, scheduler or model loop, and no test
touches the checkout's own `.arbite/` store.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from arbite import coordination as c
from arbite.coordination_storage import CRASH_AFTER_JOURNAL
from arbite.sinks.coordination_file import FileCoordinationStore

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

TICKET_RE = re.compile(r"tic-[0-9a-f]{4,}")

WORKER_A = "claude.opus-5.901"      # provider label: anthropic
WORKER_B = "openai.gpt-5.902"       # provider label: openai
COORD = "coord.human.903"


def other_kind(kind: str) -> str:
    return "sqlite" if kind == "file" else "file"


def _env(kind: str, sink: str | None) -> dict:
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    # The store under test is always stated, never inherited from the shell.
    environment.pop("ARBITE_SINK", None)
    if sink:
        environment["ARBITE_SINK"] = sink
    return environment


@pytest.fixture
def cli(tmp_project, kind):
    """Run the checkout's CLI in a throwaway project, for one sink."""

    def run(*args, expect=0, sink=kind, input=None):
        proc = subprocess.run(
            [sys.executable, "-m", "arbite.cli", *args],
            cwd=str(tmp_project),
            env=_env(kind, sink),
            input=input,
            capture_output=True,
            text=True,
        )
        if expect is not None:
            assert proc.returncode == expect, (
                f"arbite {' '.join(args)} -> exit {proc.returncode}, expected {expect}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    run("init")
    return run


def spawn(kind: str, project: Path, *args) -> subprocess.Popen:
    """A concurrently running CLI invocation, for the race scenarios."""
    return subprocess.Popen(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(project),
        env=_env(kind, kind),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ---------------------------------------------------------------------------
# CLI helpers -- every fact comes from the documented output
# ---------------------------------------------------------------------------


def jdata(proc) -> dict:
    return json.loads(proc.stdout)["data"]


def created_id(proc) -> str:
    match = TICKET_RE.search(proc.stdout)
    assert match is not None, f"no ticket id in create output: {proc.stdout!r}"
    return match.group()


def make(cli, title: str, **extra) -> str:
    args = ["create", "--title", title, "--type", "chore", "--tier", "medium",
            "--domain", "python"]
    for name, value in extra.items():
        args += [f"--{name.replace('_', '-')}", str(value)]
    return created_id(cli(*args))


def attempts(cli, ticket: str | None = None) -> list:
    """Work attempts, read the documented way (`export --scope coordination`)."""
    document = json.loads(cli("export", "--scope", "coordination", "--no-artifacts").stdout)
    records = document["records"]["work_attempts"]
    return records if ticket is None else [a for a in records if a["ticket_id"] == ticket]


def active_attempt(cli, ticket: str) -> str:
    live = [a["id"] for a in attempts(cli, ticket) if a["state"] == "active"]
    assert len(live) == 1, f"expected exactly one active attempt for {ticket}: {live}"
    return live[0]


def open_ids(cli) -> list:
    return [t["id"] for t in json.loads(cli("list", "--status", "open", "--json").stdout)]


def board(cli, worker: str, *extra, expect=0, **kwargs) -> dict:
    return jdata(cli("board", "--worker", worker, "--json", *extra, expect=expect, **kwargs))


def ready_ids(data) -> list:
    return [ticket["id"] for ticket in data["ready"]]


def excluded_codes(data, ticket_id: str) -> list:
    entry = [ticket for ticket in data["excluded"] if ticket["id"] == ticket_id][0]
    return [reason["code"] for reason in entry["readiness"]["reasons"]]


def page(cli, *extra, expect=0, **kwargs) -> dict:
    return jdata(cli("events", "--json", *extra, expect=expect, **kwargs))


def all_events(cli, *extra) -> list:
    """Every matching event, walked page by page from the beginning of the stream."""
    seen: list = []
    token = "0"
    for _ in range(30):
        proc = cli("events", "--after", token, "--json", *extra, expect=None)
        if proc.returncode == 2:
            return seen
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)["data"]
        seen.extend(payload["events"])
        if not payload["has_more"]:
            return seen
        token = payload["next_cursor"]
    raise AssertionError("walking the event stream did not terminate")


def operations(cli, ticket: str) -> list:
    return jdata(cli("changes", ticket, "--json"))["evidence"]["operations"]


def register_two_providers(cli) -> None:
    cli("worker", "register", WORKER_A, "--tier", "high", "--provider", "anthropic",
        "--model", "claude-opus-5", "--runtime", "claude-code", "--capability", "python",
        "--locality", "local", "--cost-class", "paid", "--capacity", "2", "--json")
    cli("worker", "register", WORKER_B, "--tier", "medium", "--provider", "openai",
        "--model", "gpt-5", "--runtime", "codex", "--capability", "python",
        "--locality", "remote", "--cost-class", "paid", "--capacity", "2", "--json")


def seed_board(cli) -> SimpleNamespace:
    """The acceptance demonstration's starting point: two labelled workers, one
    unreserved ticket, and a coordinator reservation of three -- one directly
    assigned to A, two packaged and published for pickup."""
    register_two_providers(cli)
    adhoc = make(cli, "unreserved: either worker", priority=1)
    direct = make(cli, "reserved: assigned to A")
    first = make(cli, "reserved: package member one")
    second = make(cli, "reserved: package member two")
    reservation = jdata(
        cli("reserve", "create", direct, first, second, "--agent", COORD, "--json")
    )["reservation"]
    assigned = jdata(
        cli("offer", "assign", direct, "--worker", WORKER_A, "--agent", COORD, "--json")
    )["offer"]
    package = jdata(
        cli("package", "create", first, second, "--agent", COORD, "--json")
    )["package"]
    offer = jdata(
        cli("offer", "publish", "--package", package["id"], "--agent", COORD,
            "--min-tier", "medium", "--require-capability", "python", "--json")
    )["offer"]
    return SimpleNamespace(adhoc=adhoc, direct=direct, first=first, second=second,
                           reservation=reservation, assigned=assigned,
                           package=package, offer=offer)


# ---------------------------------------------------------------------------
# 1. The acceptance demonstration: two providers, ad-hoc work plus a
#    reservation with a direct assignment and a public package
# ---------------------------------------------------------------------------


def test_two_providers_mix_ad_hoc_work_with_assignment_and_package(cli):
    state = seed_board(cli)

    labels = {p["worker_id"]: p["provider"] for p in jdata(cli("worker", "list", "--json"))["workers"]}
    assert labels == {WORKER_A: "anthropic", WORKER_B: "openai"}
    assert jdata(cli("worker", "show", WORKER_A, "--json"))["profile"]["model"] == "claude-opus-5"

    # An unreserved ticket is ready for either worker, and the board says so.
    assert state.adhoc in ready_ids(board(cli, WORKER_A))
    assert state.adhoc in ready_ids(board(cli, WORKER_B))

    # A reservation is a hold, not work: still open, no attempt, nothing started.
    reserved = {state.direct, state.first, state.second}
    assert reserved <= set(open_ids(cli))
    assert [a for a in attempts(cli) if a["ticket_id"] in reserved] == []

    # A takes the unreserved work in one step (the race-free form).
    pulled = json.loads(cli("list", "next", "--claim", WORKER_A, "--count", "1", "--json").stdout)
    assert [ticket["id"] for ticket in pulled] == [state.adhoc]

    # B may not take A's direct assignment.
    refused = cli("claim", state.direct, "--agent", WORKER_B, expect=1)
    assert "directly assigned to" in refused.stderr and WORKER_A in refused.stderr

    # B accepts the package: acceptance starts the attempt and binds every member.
    accepted = jdata(cli("offer", "claim", state.offer["id"], "--agent", WORKER_B, "--json"))
    assert accepted["ticket_id"] == state.first
    assert [a["id"] for a in attempts(cli, state.first) if a["state"] == "active"]
    package = jdata(cli("package", "show", state.package["id"], "--json"))["package"]
    assert package["bound_worker"] == WORKER_B and package["current"] == state.first

    # A cannot steal B's second member -- even administratively.
    for extra in ((), ("--force", "--reason", "steal it")):
        stolen = cli("claim", state.second, "--agent", WORKER_A, *extra, expect=1)
        assert "worked in order" in stolen.stderr

    # A claims its own direct assignment.
    cli("claim", state.direct, "--agent", WORKER_A)
    assert {a["ticket_id"] for a in attempts(cli) if a["state"] == "active"} == {
        state.adhoc, state.direct, state.first,
    }

    # Structured progress, readable by any external caller with no resident process.
    progress = jdata(cli("reserve", "progress", state.reservation["id"], "--json"))
    assert progress["counts"]["active"] == 2 and progress["counts"]["completed"] == 0
    members = {m["ticket_id"]: m for m in progress["reservations"][0]["members"]}
    assert members[state.direct]["worker_id"] == WORKER_A
    assert members[state.first]["worker_id"] == WORKER_B
    assert members[state.second]["classification"] == "unavailable"

    # The board names the continuity hold and the capacity actually consumed;
    # nothing is ready for B, which is an exit code -- not something to wait on.
    explained = board(cli, WORKER_B, expect=2)
    assert "package_order" in excluded_codes(explained, state.second)
    assert explained["capacity"]["active"] == 1


# ---------------------------------------------------------------------------
# 2. Continuity packages: a separate attempt and proxy receipts per member,
#    file claims released and files reread between members
# ---------------------------------------------------------------------------


def test_package_members_get_separate_attempts_and_proxy_receipts(cli, tmp_project):
    register_two_providers(cli)
    first = make(cli, "package member one")
    second = make(cli, "package member two")
    package = jdata(cli("package", "create", first, second, "--agent", COORD, "--json"))["package"]
    offer = jdata(cli("offer", "publish", "--package", package["id"], "--agent", COORD,
                      "--require-capability", "python", "--json"))["offer"]
    jdata(cli("offer", "claim", offer["id"], "--agent", WORKER_B, "--json"))

    attempt_one = active_attempt(cli, first)
    (tmp_project / "src").mkdir(exist_ok=True)
    # A file that does not exist yet is claimed as an absent path and created
    # with no read token, then read again before any replacement.
    cli("file", "claim", "src/one.py", "--ticket", first, "--attempt", attempt_one, "--json")
    cli("file", "write", "src/one.py", "--ticket", first, "--attempt", attempt_one,
        "--input", "-", "--json", input="one = 0\n")
    token = jdata(cli("file", "read", "src/one.py", "--ticket", first,
                      "--attempt", attempt_one, "--json"))["read_token"]
    cli("file", "write", "src/one.py", "--ticket", first, "--attempt", attempt_one,
        "--read-token", token, "--input", "-", "--json", input="one = 1\n")
    assert (tmp_project / "src" / "one.py").read_text() == "one = 1\n"

    # The token is consumed by the mutation it authorized: no reuse, no bytes moved.
    reused = json.loads(cli("file", "write", "src/one.py", "--ticket", first,
                            "--attempt", attempt_one, "--read-token", token,
                            "--input", "-", "--json", expect=1, input="one = 2\n").stdout)
    assert reused["code"] == "stale_read"
    assert (tmp_project / "src" / "one.py").read_text() == "one = 1\n"

    writes = [op for op in operations(cli, first) if op["operation_kind"] == "write"]
    assert [op["attempt_id"] for op in writes] == [attempt_one, attempt_one]
    assert all(op["result"] == "ok" for op in writes)
    assert {op["paths"][0] for op in writes} == {"src/one.py"}

    # Closing the member releases its file claims at the ticket boundary.
    cli("close", first, "--agent", WORKER_B)
    assert jdata(cli("package", "show", package["id"], "--json"))["package"]["current"] == second
    other = make(cli, "unrelated work")
    cli("claim", other, "--agent", WORKER_A)
    other_attempt = active_attempt(cli, other)
    cli("file", "claim", "src/one.py", "--ticket", other, "--attempt", other_attempt, "--json")

    # The next member is its own attempt and must read its file fresh.
    jdata(cli("package", "claim", package["id"], "--agent", WORKER_B, "--json"))
    attempt_two = active_attempt(cli, second)
    assert attempt_two != attempt_one
    cli("file", "claim", "src/two.py", "--ticket", second, "--attempt", attempt_two, "--json")
    cli("file", "write", "src/two.py", "--ticket", second, "--attempt", attempt_two,
        "--input", "-", "--json", input="two = 0\n")
    token_two = jdata(cli("file", "read", "src/two.py", "--ticket", second,
                          "--attempt", attempt_two, "--json"))["read_token"]
    cli("file", "write", "src/two.py", "--ticket", second, "--attempt", attempt_two,
        "--read-token", token_two, "--input", "-", "--json", input="two = 2\n")
    assert (tmp_project / "src" / "two.py").read_text() == "two = 2\n"

    second_writes = [op for op in operations(cli, second) if op["operation_kind"] == "write"]
    assert [op["attempt_id"] for op in second_writes] == [attempt_two, attempt_two]
    assert {op["ticket_id"] for op in second_writes} == {second}

    cli("close", second, "--agent", WORKER_B)
    completed = jdata(cli("package", "show", package["id"], "--json"))["package"]
    assert completed["state"] == "completed" and completed["current"] is None
    assert completed["tickets"] == [first, second]


# ---------------------------------------------------------------------------
# 3. Capacity, and one winner when two workers race for the same offer
# ---------------------------------------------------------------------------


def test_capacity_limits_a_batch_and_simultaneous_acceptance_has_one_winner(cli, kind,
                                                                           tmp_project):
    cli("worker", "register", "local.small.940", "--tier", "high", "--provider", "local",
        "--model", "llama-3", "--capacity", "2", "--json")
    tickets = [make(cli, f"batch {index}", priority=index + 1) for index in range(3)]

    batch = cli("list", "next", "--claim", "local.small.940", "--count", "3", "--json")
    assert [ticket["id"] for ticket in json.loads(batch.stdout)] == tickets[:2]
    assert "declared capacity of 2" in batch.stderr and "claimed 2" in batch.stderr

    full = cli("list", "next", "--claim", "local.small.940", "--count", "3", expect=2)
    assert "declared capacity of 2" in full.stderr
    active = [a for a in attempts(cli) if a["worker_id"] == "local.small.940"
              and a["state"] == "active"]
    assert len(active) == 2  # a capacity-limited worker cannot overclaim via a batch

    contested = tickets[2]
    offer = jdata(cli("offer", "publish", contested, "--agent", COORD, "--json"))["offer"]
    procs = [spawn(kind, tmp_project, "offer", "claim", offer["id"], "--agent", worker, "--json")
             for worker in (WORKER_A, WORKER_B, "third.worker.941")]
    outcomes = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        outcomes.append((proc.returncode, out, err))

    winners = [outcome for outcome in outcomes if outcome[0] == 0]
    assert len(winners) == 1, outcomes
    for code, out, _ in outcomes:
        if code:
            assert json.loads(out)["code"] == "offer_conflict"

    live = [a for a in attempts(cli, contested) if a["state"] == "active"]
    assert len(live) == 1
    accepted = [event for event in all_events(cli, "--kind", "offer_accepted")
                if offer["id"] in event["subject_ids"]]
    assert len(accepted) == 1


# ---------------------------------------------------------------------------
# 4. A direct claim cannot bypass a reservation or a dependency, and the
#    withdraw/reserve races have defined serial outcomes
# ---------------------------------------------------------------------------


def test_direct_claim_cannot_bypass_reservation_or_dependencies(cli):
    register_two_providers(cli)

    held = make(cli, "the coordinator's")
    reservation = jdata(cli("reserve", "create", held, "--agent", COORD, "--json"))["reservation"]
    refused = cli("claim", held, "--agent", WORKER_B, expect=1)
    assert "reserved by" in refused.stderr and reservation["id"] in refused.stderr
    forced = cli("claim", held, "--agent", WORKER_B, "--force", "--reason", "override", expect=1)
    assert "reserved by" in forced.stderr and reservation["id"] in forced.stderr
    assert jdata(cli("reserve", "show", reservation["id"], "--json"))["reservation"]["state"] == "active"
    assert attempts(cli, held) == []

    earlier = make(cli, "the dependency")
    later = make(cli, "waits for the dependency")
    cli("depend", later, earlier)
    unmet = cli("claim", later, "--agent", WORKER_A, expect=1)
    assert "unmet dependencies" in unmet.stderr and earlier in unmet.stderr
    assert attempts(cli, later) == []
    assert "dependencies_unmet" in excluded_codes(board(cli, WORKER_A), later)

    cli("claim", earlier, "--agent", WORKER_A)
    cli("close", earlier, "--agent", WORKER_A)
    cli("claim", later, "--agent", WORKER_A)


def test_withdraw_and_reserve_races_have_defined_serial_outcomes(cli, kind, tmp_project):
    register_two_providers(cli)

    # withdraw-vs-claim on a reserved, published ticket.
    held = make(cli, "contested withdraw")
    reservation = jdata(cli("reserve", "create", held, "--agent", COORD, "--json"))["reservation"]
    offer = jdata(cli("offer", "publish", held, "--agent", COORD, "--json"))["offer"]
    withdraw = spawn(kind, tmp_project, "offer", "withdraw", offer["id"], "--agent", COORD, "--json")
    claim = spawn(kind, tmp_project, "claim", held, "--agent", WORKER_A)
    withdraw_out, _ = withdraw.communicate(timeout=120)
    _, claim_err = claim.communicate(timeout=120)
    assert withdraw.returncode != claim.returncode, (withdraw.returncode, claim.returncode)

    live = [a for a in attempts(cli, held) if a["state"] == "active"]
    if claim.returncode == 0:                      # the claim won the lock
        assert len(live) == 1
        assert json.loads(withdraw_out)["code"] == "offer_conflict"
        assert json.loads(withdraw_out)["details"]["reason"] == "accepted"
    else:                                          # withdrawal won: back to reservation-only
        assert live == []
        assert "reserved by" in claim_err
        assert json.loads(withdraw_out)["data"]["offer"]["state"] == "withdrawn"
    assert jdata(cli("reserve", "show", reservation["id"], "--json"))["reservation"]["state"] == "active"

    # reserve-vs-claim on an unreserved ticket.
    loose = make(cli, "contested reserve")
    reserve = spawn(kind, tmp_project, "reserve", "create", loose, "--agent", COORD, "--json")
    claim = spawn(kind, tmp_project, "claim", loose, "--agent", WORKER_A)
    reserve_out, _ = reserve.communicate(timeout=120)
    _, claim_err = claim.communicate(timeout=120)
    assert reserve.returncode != claim.returncode, (reserve.returncode, claim.returncode)

    live = [a for a in attempts(cli, loose) if a["state"] == "active"]
    if claim.returncode == 0:                      # the claim won the lock
        assert len(live) == 1
        assert json.loads(reserve_out)["code"] == "reservation_conflict"
    else:                                          # the reservation won
        assert live == []
        assert "reserved by" in claim_err
        assert json.loads(reserve_out)["data"]["reservation"]["state"] == "active"


# ---------------------------------------------------------------------------
# 5. An external caller inspects progress, exits, and resumes querying later
# ---------------------------------------------------------------------------


def test_an_external_caller_can_exit_and_resume_the_event_stream(cli):
    register_two_providers(cli)
    ticket = make(cli, "something to do")
    cli("claim", ticket, "--agent", WORKER_A)

    first = page(cli, "--after", "0", "--limit", "2")
    assert first["cursor_namespace"]
    assert first["next_cursor"].startswith(first["cursor_namespace"] + "#")
    assert [event["cursor"] for event in first["events"]] == [1, 2]
    assert first["has_more"] is True

    # This process is gone; a fresh invocation resumes from the stored token.
    resumed = page(cli, "--after", first["next_cursor"], "--limit", "2")
    assert [event["cursor"] for event in resumed["events"]] == [3, 4]
    assert resumed["cursor_namespace"] == first["cursor_namespace"]

    # A retry re-delivers the same ids, so a consumer deduplicates instead of acting twice.
    again = page(cli, "--after", "0", "--limit", "2")
    assert [event["id"] for event in again["events"]] == [event["id"] for event in first["events"]]

    # A filtered stream is filtered before the limit, and stays in cursor order.
    lifecycle = page(cli, "--after", "0", "--category", "lifecycle")
    assert lifecycle["events"] and all(e["category"] == "lifecycle" for e in lifecycle["events"])

    # A cursor the store cannot trust is refused, never silently restarted.
    assert json.loads(cli("events", "--after", "7", "--json", expect=1).stdout)["code"] == "invalid_cursor"
    assert json.loads(cli("events", "--after", "file:/elsewhere/coordination#3",
                          "--json", expect=1).stdout)["code"] == "cursor_foreign_store"

    # "Nothing ready" is an exit code, so no resident model loop is needed to wait.
    idle = board(cli, "unregistered.worker.950", "--epic", "no-such-epic", expect=2)
    assert idle["ready"] == [] and idle["excluded"] == []
    newest = page(cli, "--after", "0")
    assert newest["has_more"] is False and newest["next_cursor"] != "0"
    empty = page(cli, "--after", newest["next_cursor"], expect=2)
    assert empty["events"] == [] and empty["next_cursor"] == newest["next_cursor"]


# ---------------------------------------------------------------------------
# 6. Partial package completion, explicit handoff, and a disabled profile
# ---------------------------------------------------------------------------


def test_partial_package_completion_handoff_and_disabled_profile_history(cli):
    state = seed_board(cli)
    # (state.first / state.second are the package; finish the first member.)
    jdata(cli("offer", "claim", state.offer["id"], "--agent", WORKER_B, "--json"))
    cli("close", state.first, "--agent", WORKER_B)
    jdata(cli("package", "claim", state.package["id"], "--agent", WORKER_B, "--json"))

    # An explicit handoff of the remainder is refused while its attempt is live.
    busy = json.loads(cli("package", "handoff", state.package["id"], "--agent", COORD,
                          "--reason", "replan", "--release", "--json", expect=1).stdout)
    assert busy["code"] == "package_conflict" and busy["details"]["reason"] == "active_attempt"

    cli("package", "note", state.package["id"], "member one done; member two in progress",
        "--agent", WORKER_B)

    released = jdata(cli("package", "handoff", state.package["id"], "--agent", COORD,
                         "--reason", "hand the rest back", "--release", "--interrupt",
                         "--json"))["package"]
    assert released["state"] == "released"
    # Completed members stay completed; the remaining member is ad-hoc available again.
    closed = [t["id"] for t in json.loads(cli("list", "--status", "closed", "--json").stdout)]
    assert state.first in closed
    fresh = board(cli, "fresh.worker.960")
    assert "package_order" not in excluded_codes(fresh, state.second)
    assert "reserved_elsewhere" in excluded_codes(fresh, state.second)
    assert state.second in open_ids(cli)
    assert json.loads(cli("show", state.second, "--json").stdout)["assignee"] is None
    # Only the continuity binding was released: the coordinator's reservation holds.
    assert state.second in ready_ids(board(cli, COORD))
    assert [a for a in attempts(cli, state.second) if a["state"] == "active"] == []

    # Disabling a profile stops new work but never deletes history.
    cli("worker", "disable", WORKER_B, "--reason", "parked", "--json")
    profile = jdata(cli("worker", "show", WORKER_B, "--json"))["profile"]
    assert profile["state"] == "disabled" and profile["disabled_reason"] == "parked"
    document = json.loads(cli("export", "--scope", "coordination", "--no-artifacts").stdout)
    assert any(p["worker_id"] == WORKER_B for p in document["records"]["worker_profiles"])
    assert any(a["worker_id"] == WORKER_B for a in document["records"]["work_attempts"])
    assert "worker_disabled" in {event["event_kind"] for event in all_events(cli)}


# ---------------------------------------------------------------------------
# 7. Unknown capability/tier/cost, and releasing a reservation while active
# ---------------------------------------------------------------------------


def test_unknown_worker_values_and_reservation_release_while_active(cli):
    register_two_providers(cli)
    restricted = make(cli, "restricted offer")
    jdata(cli("offer", "publish", restricted, "--agent", COORD, "--min-tier", "medium",
              "--require-capability", "python", "--json"))

    # An unknown worker value fails an explicit constraint, naming what is missing.
    refused = cli("claim", restricted, "--agent", "unknown.worker.970", expect=1)
    assert "no known tier" in refused.stderr and "no declared capabilities" in refused.stderr
    check = jdata(cli("worker", "check", "unknown.worker.970", "--min-tier", "low",
                      "--require-capability", "python", "--local-only", "--max-cost", "5",
                      "--max-cost-unit", "USD/ticket", "--json"))
    assert check["eligible"] is False
    codes = {reason["code"] for reason in check["reasons"]}
    assert {"capabilities_unknown", "locality_unknown", "cost_unknown"} <= codes

    # A registered worker whose tier is below the requirement is refused too.
    cli("worker", "register", "local.small.971", "--tier", "low", "--provider", "local",
        "--capability", "python", "--json")
    below = cli("claim", restricted, "--agent", "local.small.971", expect=1)
    assert "below the required tier" in below.stderr or "tier" in below.stderr

    # Releasing a reservation while a member has a live attempt is refused ...
    held = make(cli, "held and worked")
    reservation = jdata(cli("reserve", "create", held, "--agent", COORD, "--json"))["reservation"]
    cli("claim", held, "--agent", COORD)
    refused = json.loads(cli("reserve", "release", reservation["id"], "--agent", COORD,
                             "--json", expect=1).stdout)
    assert refused["code"] == "reservation_conflict"
    assert refused["details"]["reason"] == "active_attempts"
    assert jdata(cli("reserve", "show", reservation["id"], "--json"))["reservation"]["state"] == "active"

    # ... and an explicit interruption is what ends it, returning the ticket to open.
    released = jdata(cli("reserve", "release", reservation["id"], "--agent", COORD,
                         "--interrupt", "--reason", "replan", "--json"))["reservation"]
    assert released["state"] == "released"
    assert held in open_ids(cli)
    assert [a for a in attempts(cli, held) if a["state"] == "active"] == []


# ---------------------------------------------------------------------------
# 8. Event replay after a crash: a journalled file-sink event becomes visible
#    exactly once, through the CLI
# ---------------------------------------------------------------------------


def _crash_with_a_journalled_event(root: str, payload: dict) -> None:
    """Journal one event with the documented hook, then die before applying it."""
    store = FileCoordinationStore(Path(root), crash_point=CRASH_AFTER_JOURNAL)
    with store.transaction() as tx:
        tx.append_event(c.Event.from_dict(payload))
    os._exit(97)  # pragma: no cover - the crash point exits first


def test_event_replay_after_a_crash_is_reconciled_exactly_once(tmp_project):
    environment = _env("file", "file")

    def run(*args, expect=0):
        proc = subprocess.run([sys.executable, "-m", "arbite.cli", *args],
                              cwd=str(tmp_project), env=environment,
                              capture_output=True, text=True)
        assert proc.returncode == expect, f"{args} -> {proc.returncode}\n{proc.stderr}"
        return proc

    run("init")
    coordination_dir = tmp_project / ".arbite" / "coordination"
    planned = c.Event(
        id=c.new_record_id("event"),
        kind_="ticket_claimed",
        category="lifecycle",
        timestamp=c.utc_now(),
        subject_ids=["tic-a1b2"],
        payload={"ticket_id": "tic-a1b2"},
    )
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_with_a_journalled_event,
                              args=(str(coordination_dir), planned.to_dict()))
    process.start()
    process.join(60)
    assert process.exitcode == 97

    store = FileCoordinationStore(coordination_dir)
    assert len(store.pending_journals()) == 1   # journalled ...
    assert store.inspect_events() == []         # ... and therefore not visible yet

    payload = json.loads(run("events", "--after", "0", "--json").stdout)["data"]
    delivered = [event for event in payload["events"] if event["id"] == planned.id]
    assert len(delivered) == 1                  # visible now, exactly once
    assert delivered[0]["event_kind"] == "ticket_claimed"

    resumed = json.loads(run("events", "--after", payload["next_cursor"], "--json",
                             expect=2).stdout)["data"]
    assert resumed["events"] == []              # reconciled, so it never repeats
    assert FileCoordinationStore(coordination_dir).pending_journals() == []


# ---------------------------------------------------------------------------
# 9. A quiescent transfer moves the board, not just the tickets
# ---------------------------------------------------------------------------


def test_a_quiescent_transfer_between_both_sinks_preserves_board_state(cli, tmp_project, kind):
    register_two_providers(cli)
    finished = make(cli, "finished work")
    handed = make(cli, "handed back")
    cli("claim", finished, "--agent", WORKER_A)
    cli("close", finished, "--agent", WORKER_A)
    reservation = jdata(cli("reserve", "create", handed, "--agent", COORD, "--json"))["reservation"]
    offer = jdata(cli("offer", "publish", handed, "--agent", COORD, "--json"))["offer"]
    cli("offer", "withdraw", offer["id"], "--agent", COORD, "--json")
    cli("reserve", "release", reservation["id"], "--agent", COORD, "--json")
    cli("worker", "disable", WORKER_B, "--reason", "parked", "--json")

    before = json.loads(cli("export", "--scope", "coordination", "--no-artifacts").stdout)
    ticket_ids = [t["id"] for t in json.loads(cli("list", "--json").stdout)]

    # Make the source store this project's committed choice, so the transfer's own
    # config write is what a later flagless command reads.
    cli("init", "--sink", kind, sink=None)
    target = other_kind(kind)
    migrated = cli("migrate", "--to", target, sink=None)
    assert "not quiescent" not in migrated.stderr
    assert f"sink: {target}" in (tmp_project / "arbite.yaml").read_text()

    # The destination is now the store a plain command reads (no --sink flag).
    after = json.loads(cli("export", "--scope", "coordination", "--no-artifacts", sink=None).stdout)
    assert [t["id"] for t in json.loads(cli("list", "--json", sink=None).stdout)] == ticket_ids
    assert len(after["records"]["worker_profiles"]) == len(before["records"]["worker_profiles"]) == 2
    assert {p["worker_id"] for p in after["records"]["worker_profiles"]} == {WORKER_A, WORKER_B}
    assert [r["state"] for r in after["records"]["reservations"]] == ["released"]
    assert [o["state"] for o in after["records"]["offers"]] == ["withdrawn"]
    assert [a["state"] for a in after["records"]["work_attempts"]] == ["finished"]

    # And the board surfaces still answer from the destination.
    explained = board(cli, WORKER_A, sink=None)
    assert explained["worker"]["source"] == "profile"
    profiles = {p["worker_id"]: p["state"] for p in jdata(cli("worker", "list", "--json", sink=None))["workers"]}
    assert profiles == {WORKER_A: "enabled", WORKER_B: "disabled"}
    assert jdata(cli("reserve", "list", "--state", "all", "--json", sink=None))["count"] == 1
    assert page(cli, "--after", "0", "--json", sink=None)["events"]
    assert cli("doctor", sink=None, expect=0)
