"""The frozen dependency-edge transcripts this slice owns: DP1, DP2.

No other family runs `arbite depend`, so DP covers the two outcomes of dropping one
edge: the removal that lands as a single write, and the removal that is refused without
writing anything at all. Each block is asserted against its transcript in
`.arbite/planning/interaction-examples.md` -- command, the stream its body belongs to,
exit code -- with ids normalised on both sides (`examples.py`), and the project each
block starts from is built here through the sink, which is the only way a fixture can
hold the document's own ticket ids.

The views are asserted beside the transcript, because a printed sentence is not the
contract: the edge has to be gone from the topological order and the tree, and the
ticket that was depended on has to be the document it already was.
"""

from __future__ import annotations

import json

import examples
import lifecycle_state as state

#: The document's illustrative ids, the same ones the claim scenarios use, so the DP
#: blocks read as one world with the blocks above them.
TICKET = "tic-cf9f"
FIRST = "tic-9b57"
SECOND = "tic-e9ed"

#: `make_ticket`'s stamp, which is what an unwritten ticket still carries.
STAMP = "2026-01-01T00:00:00"


def _mesh(tmp_path, sink_kind: str, depends_on: list) -> "object":
    """`tic-cf9f` holding `depends_on` in that order, with both candidate dependencies
    present in the store so every id a transcript names resolves. Urgency is
    `tic-cf9f` most urgent, so a topological order says exactly who waits on whom."""
    project = state.initialise(tmp_path, sink_kind)
    state.put(
        project,
        TICKET,
        sink_kind,
        title=state.C03_TITLE,
        priority=1,
        depends_on=list(depends_on),
        **state.epic_ticket(),
    )
    for tid, title, priority in (
        (FIRST, state.C04_TITLE, 5),
        (SECOND, "Cascade ticket lifecycle through file ownership and receipts", 3),
    ):
        state.put(project, tid, sink_kind, title=title, priority=priority, **state.epic_ticket())
    return project


def _json(project, *args):
    return json.loads(examples.run_cli(project, *args).stdout)


# --- DP1: the removal that lands -------------------------------------------


def test_DP1_dropping_one_dependency_is_the_frozen_transcript(tmp_path, kind):
    project = _mesh(tmp_path, kind, [FIRST, SECOND])
    examples.assert_scenario(examples.scenario_block("DP1"), project, sink=kind)


def test_DP1_the_dropped_edge_is_gone_from_every_view(tmp_path, kind):
    """One dependency goes, the other keeps its place, and the order the edges were made
    in survives -- `tic-cf9f` is the most urgent ticket, so its place in the topological
    order only moves when nothing holds it back any more."""
    project = _mesh(tmp_path, kind, [FIRST, SECOND])
    dependency_before = _json(project, "show", FIRST, "--json")
    assert [row["id"] for row in _json(project, "list", "--topo", "--json")] == [
        SECOND,
        FIRST,
        TICKET,
    ]

    examples.assert_scenario(examples.scenario_block("DP1"), project, sink=kind)

    ticket = _json(project, "show", TICKET, "--json")
    assert ticket["depends_on"] == [SECOND]
    assert ticket["updated"] > STAMP, "the write re-stamped the ticket it changed"
    assert _json(project, "show", FIRST, "--json") == dependency_before
    assert [row["id"] for row in _json(project, "list", "--topo", "--json")] == [
        SECOND,
        TICKET,
        FIRST,
    ]
    tree = _json(project, "list", "--tree", "--json")
    node = next(root for root in tree if root["id"] == TICKET)
    assert [child["id"] for child in node["depends"]] == [SECOND]
    assert FIRST in {root["id"] for root in tree}, "it is a dependency-free ticket now"


# --- DP2: the removal that is refused --------------------------------------


def test_DP2_removing_an_absent_dependency_is_refused_and_writes_nothing(tmp_path, kind):
    """The refusal is the transcript -- it names the id the ticket does not depend on and
    the dependency it does have -- and it writes nothing, which is what lets the caller
    retry without wondering whether half the command happened."""
    project = _mesh(tmp_path, kind, [FIRST])
    before = _json(project, "show", TICKET, "--json")

    refused = examples.assert_scenario(examples.scenario_block("DP2"), project, sink=kind)

    assert refused.startswith("error: ")
    ticket = _json(project, "show", TICKET, "--json")
    assert ticket == before, "an unchanged ticket, stamp included"
    assert ticket["depends_on"] == [FIRST]
