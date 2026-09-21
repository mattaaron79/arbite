"""Projects in the states the claim/attempt transcripts describe.

The CL, LC5 and RC transcripts are about what the *acquisition* paths do, so the state
in front of them matters more than usual: whether a dependency is closed, whether a
ticket already has an attempt, and what an attempt's state and generation are. These
builders make that state explicit -- through the real CLI where a command is supposed
to produce it (`claim` for a live attempt), and through the sink where the state is one
no command produces any more (a ticket left `in_progress` from before attempts
existed, which is what `attempt adopt` migrates).

Tickets are created with the *document's own ids* wherever the frozen command names one
(`tic-cf9f`, `tic-9b57`, `tic-e9ed`): the ids are four hex characters, so the real
fixture can hold exactly the ticket the transcript talks about, and the harness's
normalisation turns them into `tic-XXXX` on both sides anyway. Two fixtures deliberately
choose their own ids instead, and say why where they do it.
"""

from __future__ import annotations

from pathlib import Path

import examples
from arbite.sinks import SinkSpec, build_sink
from helpers import make_ticket

EPIC = "shared-directory-coordination"

#: The titles `arbite` itself uses for these tickets in the document's transcripts
#: (they are the real epic's tickets, so the rows and the prose agree).
C03_TITLE = "Create work attempts and guard every ticket acquisition path"
C04_TITLE = "Bind canonical paths and implement exclusive file claims"


def initialise(tmp_path: Path, sink_kind: str = "file") -> Path:
    """An initialised project whose committed choice is `sink_kind`."""
    project = tmp_path / "project"
    project.mkdir()
    arbite_dir = project / ".arbite"
    arbite_dir.mkdir()
    (arbite_dir / "project.yaml").write_text(f"sink: {sink_kind}\n", encoding="utf-8")
    proc = examples.run_cli(project, "init")
    assert proc.returncode == 0, proc.stderr
    return project


def sink_for(project: Path, sink_kind: str = "file"):
    """The project's ticket sink, as the CLI resolves it."""
    return build_sink(SinkSpec(kind=sink_kind), project / ".arbite")


def put(project: Path, ticket_id: str, sink_kind: str = "file", **fields):
    """One ticket, written straight to the sink with the fields a fixture needs."""
    sink_for(project, sink_kind).create(make_ticket(ticket_id, **fields))
    return ticket_id


def epic_ticket(**fields):
    """The classification every ticket in this epic shares in the transcripts."""
    return dict(type="feature", tier="high", domain="io", epic=EPIC, **fields)


# --- CL1: a fresh ticket, ready to be claimed ------------------------------------


def claimable(project: Path, ticket_id: str = "tic-cf9f", sink_kind: str = "file") -> str:
    """One open, unclassified-nothing-workable ticket: CL1's starting point."""
    return put(project, ticket_id, sink_kind, title=C03_TITLE, priority=1, **epic_ticket())


# --- CL2: a dependency that is not closed ---------------------------------------


def dependent_with_open_prerequisite(
    project: Path, sink_kind: str = "file"
) -> tuple:
    """`tic-cf9f` in progress and `tic-9b57` waiting on it: CL2's starting point.

    The prerequisite is claimed through the CLI, so the attempt that makes it
    "in_progress with a live owner" is real rather than a fixture's assertion."""
    prerequisite = put(
        project,
        "tic-cf9f",
        sink_kind,
        title=C03_TITLE,
        priority=1,
        **epic_ticket(),
    )
    claimed = examples.run_cli(project, "claim", prerequisite, "--agent", "claude.opus.001")
    assert claimed.returncode == 0, claimed.stderr
    dependent = put(
        project,
        "tic-9b57",
        sink_kind,
        title=C04_TITLE,
        priority=1,
        depends_on=[prerequisite],
        **epic_ticket(),
    )
    return dependent, prerequisite


# --- CL3: a claim that loses the race -------------------------------------------


def claimed_by_another(
    project: Path, ticket_id: str = "tic-cf9f", agent: str = "claude.opus.001"
) -> str:
    """Claim an existing ticket, leaving the attempt that holds it: CL3's starting point.

    The claim goes through the CLI because the transcript's second stderr line is the
    *holder's attempt*, and only the real acquisition creates one."""
    claimed = examples.run_cli(project, "claim", ticket_id, "--agent", agent)
    assert claimed.returncode == 0, claimed.stderr
    return ticket_id


# --- CL4: an epic whose open work is all blocked --------------------------------


#: `list next` reports how many matching open tickets dependencies hold back; CL4's
#: transcript counts nine.
CL4_BLOCKED = 9


def blocked_epic(project: Path, sink_kind: str = "file") -> str:
    """Nine open tickets of the epic waiting on one in-progress prerequisite."""
    prerequisite = put(project, "tic-cf9f", sink_kind, title=C03_TITLE, priority=1, **epic_ticket())
    claimed = examples.run_cli(project, "claim", prerequisite, "--agent", "claude.opus.001")
    assert claimed.returncode == 0, claimed.stderr
    for index in range(CL4_BLOCKED):
        put(
            project,
            f"tic-b{index:03x}",
            sink_kind,
            title=f"{C04_TITLE} ({index + 1})",
            priority=2,
            depends_on=[prerequisite],
            **epic_ticket(),
        )
    return prerequisite


# --- CL5: a batch claim that runs the queue dry ---------------------------------


def two_workable_in_order(project: Path, sink_kind: str = "file") -> tuple:
    """The two workable tickets CL5 claims, in the order its table prints them.

    `list next` orders candidates by `(priority, id)`, and the document's own two ids
    (`tic-cf9f`, `tic-9b57`) sort the *other* way round -- `tic-9b57` is lower than
    `tic-cf9f` character by character. So the second row's ticket is created under an
    id that sorts after the first one's, which is what makes the printed order match
    the frozen block; the ids are normalised on both sides, so the rows are identified
    by their titles, which are the real tickets' titles.
    """
    first = put(project, "tic-cf9f", sink_kind, title=C03_TITLE, priority=1, **epic_ticket())
    second = put(project, "tic-f0a1", sink_kind, title=C04_TITLE, priority=1, **epic_ticket())
    assert first < second, "the fixture's ids must sort the way the table prints"
    return first, second


# --- CL6: legacy in-progress work ----------------------------------------------


def legacy_in_progress(
    project: Path, ticket_id: str = "tic-e9ed", sink_kind: str = "file", assignee="claude.opus.001"
) -> str:
    """A ticket left `in_progress` before attempt tracking: no attempt, one assignee.

    Written straight to the sink, because that is what the state is: the result of
    work that started in an arbite that had no attempt records, which no current
    command can reproduce."""
    return put(
        project,
        ticket_id,
        sink_kind,
        title="Add coordination migrations, export and integrity recovery",
        status="in_progress",
        assignee=assignee,
        priority=1,
        **epic_ticket(),
    )
