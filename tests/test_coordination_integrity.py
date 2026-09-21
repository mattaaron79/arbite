"""What `doctor` finds in the coordination records, and which half reports it.

The refinement this slice owns is the *split*: a finding that means the same thing whichever
store holds the records is computed once (claims nobody can use, unfinished operations judged
against the bytes on disk, records naming something missing, an attempt still active on a closed
ticket), while a finding about a backend's own bookkeeping -- the file backend's revision
counters and commit journal, the SQLite backend's revision rows -- stays with the backend that
can have it. Both halves are asserted here, on real stores, with the damage done in each
backend's own terms.

The frozen DR1/DR2 blocks live in `test_recovery_examples.py`; this module covers the finding
classes they do not, and the two refusals the examples document describes as "without guessing":
nothing here is repaired except the bookkeeping a repair can drop.
"""

from __future__ import annotations

import json

import pytest

import migration_state as state
from arbite.coordination import recovery

SINKS = state.SINKS


def report(project, *args, sink_kind=None, expect=3) -> dict:
    """A `doctor --json` run, as the payload: the kinds, the details and the counts."""
    proc = state.run(project, "doctor", *args, "--json", sink_kind=sink_kind, expect=expect)
    return json.loads(proc.stdout)


def kinds(payload: dict) -> list:
    return [problem["kind"] for problem in payload["problems"]]


# ---------------------------------------------------------------------------
# An attempt that outlived the ticket it belongs to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink_kind", SINKS)
def test_C12_doctor_reports_an_attempt_still_active_on_a_closed_ticket(tmp_path, sink_kind):
    """The finding that names an attempt no lifecycle command can use.

    A closed ticket ends its attempt and releases its claims, so an attempt that is *still*
    active on one is an inconsistency: it holds ownership it cannot use and cannot mutate
    anything. It is reported, with the two honest ways out, on both sinks."""
    project, attempt = state.attempt_on_closed_ticket(tmp_path, sink_kind)
    payload = report(project, sink_kind=sink_kind)

    assert kinds(payload) == [recovery.ATTEMPT_ON_CLOSED_TICKET]
    assert payload["problems"][0]["id"] == state.CLOSED_TICKET
    assert payload["remaining"] == 1 and payload["fixed"] == 0

    text = state.run(project, "doctor", sink_kind=sink_kind, expect=3).stdout
    assert attempt in text and state.CLOSED_TICKET in text
    assert f"'arbite release {state.CLOSED_TICKET}" in text, "the release that ends it is named"
    assert "reopen" in text, "and so is the way back"


@pytest.mark.parametrize("sink_kind", SINKS)
def test_C12_a_repair_does_not_end_an_attempt_on_a_closed_ticket(tmp_path, sink_kind):
    """Reported, never repaired: the attempt's own `ended` time was never recorded.

    Writing "ended now" would claim the attempt ran until the repair, which is not what happened
    -- the same reason a stopped worker is never inferred from a timestamp. A `--fix` run
    therefore reports it exactly as a report run does, and the record is untouched."""
    project, attempt = state.attempt_on_closed_ticket(tmp_path, sink_kind)
    payload = report(project, "--fix", sink_kind=sink_kind)

    assert kinds(payload) == [recovery.ATTEMPT_ON_CLOSED_TICKET]
    assert payload["fixed"] == 0
    stored = state.store_for(project, sink_kind).get_attempt(attempt)
    assert stored.is_active and stored.ended is None


# ---------------------------------------------------------------------------
# Shared findings stay shared, per-sink ones stay per sink
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sink_kind", SINKS)
def test_C12_the_same_damage_reports_the_same_shared_finding_on_both_sinks(tmp_path, sink_kind):
    """The property both backends exist for: one meaning, whichever store holds the records."""
    project, path = state.orphaned_claim(tmp_path, sink_kind)
    payload = report(project, sink_kind=sink_kind)
    assert kinds(payload) == [recovery.ORPHANED_CLAIM]
    assert path in payload["problems"][0]["detail"]

    store = state.store_for(project, sink_kind)
    assert [problem.kind for problem in store.record_problems()] == [recovery.ORPHANED_CLAIM]


def test_C12_the_file_backend_reports_and_drops_orphan_revision_counters(tmp_path):
    """A counter for a record that is not there: the file backend's own bookkeeping.

    Nothing shared can see it and nothing but this backend can repair it, which is what makes it
    a per-sink finding rather than a copy of a shared one."""
    project, path = state.orphaned_claim(tmp_path, "file")
    report(project, "--fix", expect=0)
    key = state.orphan_revision_counter(project, "file")

    reported = report(project)
    assert kinds(reported) == ["orphan_revision_counters"]
    assert key in reported["problems"][0]["detail"]

    fixed = report(project, "--fix", expect=0)
    assert kinds(fixed) == ["orphan_revision_counters"] and fixed["fixed"] == 1
    counters = (project / ".arbite" / "coordination" / "revisions.json").read_text(
        encoding="utf-8"
    )
    assert key not in counters, "the counter a repair drops is gone"
    assert report(project, expect=0)["problems"] == []


def test_C12_the_sqlite_backend_reports_and_drops_orphan_revision_rows(tmp_path):
    """The same finding in the other backend's terms: a row with no record beside it.

    The mirror of the ticket sink's orphaned index rows, and reported the same way: the records
    are authoritative and the counter is bookkeeping, so the repair drops the row."""
    project, path = state.orphaned_claim(tmp_path, "sqlite")
    report(project, "--fix", sink_kind="sqlite", expect=0)
    key = state.orphan_revision_counter(project, "sqlite")

    reported = report(project, sink_kind="sqlite")
    assert kinds(reported) == ["orphan_revision_rows"]
    assert key in reported["problems"][0]["detail"]

    fixed = report(project, "--fix", sink_kind="sqlite", expect=0)
    assert kinds(fixed) == ["orphan_revision_rows"] and fixed["fixed"] == 1
    assert report(project, sink_kind="sqlite", expect=0)["problems"] == []


def test_C12_the_file_backend_reports_an_outstanding_commit_journal(tmp_path):
    """A commit a dead process staged: reported by `doctor`, replayed by the next write.

    `--fix` does not replay it, because replaying is a *write*'s job and decides nothing (the
    journal names the documents and their absolute revisions), while a report must not change
    the store it is reporting on."""
    project, path = state.orphaned_claim(tmp_path, "file")
    report(project, "--fix", expect=0)
    journal = state.commit_journal(project)

    reported = report(project)
    assert kinds(reported) == ["pending_commit"]
    assert "replays it" in reported["problems"][0]["detail"]

    fixed = report(project, "--fix")
    assert kinds(fixed) == ["pending_commit"], "a repair does not replay a commit journal"
    assert journal.exists()


# ---------------------------------------------------------------------------
# The sink's own index, and the coordination store's unfinished operation
# ---------------------------------------------------------------------------


def test_C12_a_drifted_note_index_and_a_pending_operation_are_reported_together(tmp_path):
    """One report, two families, two responses -- and neither guesses.

    The SQLite sink's derived note index is repaired from the body it is derived from (the body
    is authoritative); the coordination store's unfinished operation whose bytes match neither
    recorded version is reported with all three versions and left alone. The exit code is 3
    before and after, because the drift remains."""
    project = state.quiescent(tmp_path, "sqlite")
    operation = state.drifted_pending_operation(
        project, state.HOLDER_TICKET, state.DRIFT_PY, "sqlite"
    )
    state.damaged_note_index(project, state.RIVAL_TICKET)

    reported = report(project, sink_kind="sqlite")
    # The sink's own checks come first in `doctor`'s list, then the coordination store's, so a
    # reader meets the report in the order it was assembled.
    assert kinds(reported) == ["notes_index_drift", "pending_operation"]
    assert reported["problems"][-1]["id"] == state.HOLDER_TICKET

    text = state.run(project, "doctor", sink_kind="sqlite", expect=3).stdout
    assert operation in text
    assert "re-run with --fix to rebuild the index" in text

    fixed = report(project, "--fix", sink_kind="sqlite")
    assert kinds(fixed) == ["notes_index_drift", "pending_operation"]
    assert [problem["fixed"] for problem in fixed["problems"]] == [True, False]
    repair_text = state.run(project, "doctor", "--fix", sink_kind="sqlite", expect=3).stdout
    assert "will not guess which version" in repair_text

    # The index was rebuilt from the body; the drifted bytes were not touched.
    assert kinds(report(project, sink_kind="sqlite")) == ["pending_operation"]
    assert state.store_for(project, "sqlite").get_record("receipt", operation).is_pending
    assert (project / state.DRIFT_PY).read_bytes() == state.DRIFTED


def test_C12_a_report_reads_and_changes_nothing(tmp_path):
    """`doctor` without `--fix` writes nothing, so an identical second run is identical.

    Checked against the store's whole document set: a report that quietly reconciled an
    unfinished operation would change a receipt here."""
    project = state.quiescent(tmp_path, "file")
    state.drifted_pending_operation(project, state.HOLDER_TICKET, state.DRIFT_PY, "file")
    store = state.store_for(project, "file")
    before = state.snapshot(store)

    text = state.run(project, "doctor", expect=3).stdout
    assert state.snapshot(store) == before, "a report changed no record"
    assert state.run(project, "doctor", expect=3).stdout == text
    assert kinds(report(project)) == ["pending_operation"]
