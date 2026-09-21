"""The application layer: guarded multi-record operations, and the workspace report.

This is where coordination *policy* lives, so that it lives neither in argparse nor
in SQL. Argparse must know nothing about claims (a flag's help is documentation,
not an invariant), and a rule expressed in SQL would exist only for one backend --
the file backend has no SQL, and the README's whole premise is that both sinks mean
the same thing. So an operation is a value here:

- `guards` are preconditions, checked in order, before anything is written. A guard
  that fails refuses the operation with an outcome (`error`, `busy`, `stale`) and
  changes nothing; the guard's name is what a caller reads to know which rule it
  tripped.
- `steps` are the record writes, in a defined order. They run inside *one*
  transaction (`store.transaction()`), so an operation's records and events commit
  together or not at all: the ordering is a reading order now, not a recovery
  mechanism, because with a real transaction the last step no longer has to be the
  commit point.

The operations this slice ships are the workspace ones, because the workspace is
the fact every later operation is anchored to: `workspace_show` reports the derived
workspace, `record_workspace` (run by `arbite init`) records the binding, and
`events` reads the stream those operations append to. Claiming, reads, writes,
receipts and passthrough arrive in their own tickets as further operations here;
none of them is implemented ahead of its slice.

**Identity is attribution, not authentication.** A worker id, an actor name and a
declared attempt are what they say they are: cooperating local processes sharing
one store. Nothing in this layer verifies who called it, and no record is evidence
of identity -- only of what was attributed at the time. Runtime enforcement is a
later, separate concern (see the deferred decisions in the handoff).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..errors import CoordinationError
from .records import READ_CATEGORY, Workspace, derived_workspace_id, parse_utc
from .results import (
    EMPTY,
    ERROR,
    OK,
    OUTCOME_LABELS,
    OperationResult,
    Outcome,
    next_actions_for,
    register_next_actions,
    succeeded,
)
from .scratch import scratch_root, scratch_summary
from .store import CoordinationStore, open_coordination_store

#: The event row's columns, pinned by the frozen transcripts (EV2, EV3, EV5): a
#: cursor, the kind, the subject, the operation, ticket/attempt, actor and time.
#: The subject column is the only computed one -- four wider than the widest subject
#: it has to hold (see `_subject_width`).
EVENT_CURSOR_WIDTH = 4
EVENT_KIND_WIDTH = 14
EVENT_OPERATION_WIDTH = 9
EVENT_WHERE_WIDTH = 19
EVENT_ACTOR_WIDTH = 18
EVENT_TIME_WIDTH = 10
EVENT_SUBJECT_PADDING = 4

#: How many events a bare `arbite events` prints. The same number the `--follow`
#: refusal suggests, so the hint and the default cannot disagree.
DEFAULT_EVENT_TAIL = 20

#: The outcome reason for a refused `--follow`, so the exit code, the printed word
#: and the hint all come from the vocabulary rather than from this function.
FOLLOW_REFUSED = "follow_refused"

FOLLOW_REFUSAL_MESSAGE = (
    "unknown argument '--follow'; arbite commands are one-shot and never block"
)

FOLLOW_REFUSAL_HINT = (
    f"'arbite events --tail {DEFAULT_EVENT_TAIL}' to read the end of the stream, or poll with\n"
    "      'arbite events --after <cursor>' and keep the cursor in your own loop"
)

register_next_actions(FOLLOW_REFUSED, [FOLLOW_REFUSAL_HINT])


def follow_refusal() -> OperationResult:
    """The refusal of `--follow`, as a result rather than a raised failure.

    A blocking watcher is a sleeping process, which this design deliberately does
    not have, so the flag is refused and the refusal teaches the pattern instead:
    read the tail, or poll with your own loop and keep the cursor. It is printed on
    **stdout** because that is where the frozen transcript puts it (EV7) -- unlike
    every other refusal, which the CLI reports on stderr."""
    return OperationResult(
        outcome=Outcome(ERROR, FOLLOW_REFUSED),
        lines=[f"{OUTCOME_LABELS[ERROR]}: {FOLLOW_REFUSAL_MESSAGE}"],
        data={"error": FOLLOW_REFUSAL_MESSAGE},
        next_actions=next_actions_for(ERROR, FOLLOW_REFUSED),
    )


@dataclass(frozen=True)
class Guard:
    """One precondition of an operation.

    `check` raises when the precondition does not hold; `name` is the stable word a
    caller branches on and the word the refusal message uses, so two operations
    cannot describe the same rule differently."""

    name: str
    description: str
    check: Callable[[], None]


@dataclass(frozen=True)
class GuardedOperation:
    """A named operation: its guards, then its writes in order.

    Kept as a value so the ordering and the guard set are inspectable (and testable)
    rather than implied by the sequence of statements in a command function."""

    kind: str
    guards: tuple = ()
    steps: tuple = ()

    def run(self, context: "CoordinationApp") -> list:
        """Evaluate every guard, then perform every step in one transaction.

        A step that raises rolls the whole transaction back, so a refused or failed
        operation leaves **nothing** behind rather than a prefix. What a *dead*
        process leaves behind is the store's business, and both backends make it
        whole without deciding anything: the file backend's journal is replayed by
        the next write, and SQLite discards an uncommitted transaction."""
        for guard in self.guards:
            try:
                guard.check()
            except CoordinationError:
                raise
            except OSError as e:
                raise CoordinationError(f"{self.kind}: {guard.name}: {e}")
        completed = []
        with context.store.transaction() as txn:
            for name, step in self.steps:
                step(context, txn)
                completed.append(name)
        return completed


def _relative(path, project_root) -> str:
    """`path` as the text a report prints: project-relative when it is inside the
    project, absolute otherwise (a configured store may live anywhere)."""
    try:
        return str(Path(path).relative_to(Path(project_root)))
    except ValueError:
        return str(path)


def _field(label: str, value: str) -> str:
    """One `label: value` line with the value column aligned.

    Presentation only, and pinned by the WS transcripts: labels shorter than the
    column are padded, and the longest label (`coordination`) gets a single
    separating space rather than being cut."""
    text = f"{label}:"
    prefix = text.ljust(11) if len(text) < 11 else f"{text} "
    return f"{prefix}{value}"


def _count(value: int, singular: str, plural: Optional[str] = None) -> str:
    noun = singular if value == 1 else (plural or f"{singular}s")
    return f"{value} {noun}"


class CoordinationApp:
    """The coordination operations, against one store, for one workspace."""

    def __init__(
        self,
        store: CoordinationStore,
        project_root,
        arbite_dir,
        sink_root,
        sink_kind: Optional[str] = None,
        store_source: str = "",
    ):
        self.store = store
        self.project_root = Path(project_root)
        self.arbite_dir = Path(arbite_dir)
        #: The *ticket* store's kind and root, neither of which is the coordination
        #: store's identity: a file sink keeps its tickets and its coordination state
        #: in different places, and the workspace record has to say which is which.
        #: Today the two kinds coincide (each backend pairs with its sink), but the
        #: record's fields mean the *ticket* store, so they are taken from it.
        self.sink_kind = sink_kind or store.kind
        self.sink_root = Path(sink_root)
        #: Where the sink selection came from, in words ("sink: file in
        #: .arbite/project.yaml", "default"). Built by the CLI, which is the layer
        #: that knows about flags and config; the app layer only displays it.
        self.store_source = store_source

    @classmethod
    def open(cls, sink, project_root, arbite_dir, store_source: str = "") -> "CoordinationApp":
        """The application layer for a resolved ticket sink.

        The coordination store comes from the sink itself (`open_coordination_store`),
        because coordination state must be visible to everyone holding that store,
        not to everyone holding a copy of the project directory."""
        return cls(
            store=open_coordination_store(sink),
            project_root=project_root,
            arbite_dir=arbite_dir,
            sink_root=sink.root,
            sink_kind=getattr(sink, "kind", None),
            store_source=store_source,
        )

    # ------------------------------------------------------------------
    # The workspace: derived, never bound
    # ------------------------------------------------------------------

    def derived_workspace(self) -> Workspace:
        """The workspace this project *is*, from the located directory and the sink.

        There is no bind command and no conflict path: the workspace is derived, so
        two agents in one checkout compute the same answer without coordinating and
        a relocated root or a repointed store is honestly a new workspace.

        Deriving deliberately does not *read* the store -- only its kind and its
        location, which are known before it exists. `arbite init` derives the
        workspace before it creates the store, and a derivation that needed the
        store to be readable could not do that."""
        return Workspace(
            id=derived_workspace_id(self.project_root, self.sink_kind, self.sink_root),
            root=str(self.project_root),
            store_kind=self.sink_kind,
            store_root=str(self.sink_root),
            coordination_kind=self.store.kind,
            coordination_root=str(self.store.root),
        )

    def _workspace_operation(self) -> GuardedOperation:
        """The one write operation this slice ships: record the workspace binding.

        Both steps are named, in order, and both run inside one transaction: the
        layout is created first and the binding written second, so the operation
        reads top to bottom -- and because the binding itself is a replacement (the
        previous record goes, the new one arrives) the transaction is what stops a
        store from ever holding two bindings. `check_layout` runs the same guards
        without running the steps."""
        workspace = self.derived_workspace()
        return GuardedOperation(
            kind="workspace.record",
            guards=(
                Guard(
                    "arbite_dir_present",
                    "the located .arbite/ directory must exist",
                    self._check_arbite_dir,
                ),
                Guard(
                    "layout_writable",
                    "the coordination layout must be creatable",
                    self.store.check_writable_layout,
                ),
            ),
            steps=(
                ("layout", lambda app, txn: app.store.init()),
                ("workspace", lambda app, txn: app.store.put_workspace(workspace, txn=txn)),
            ),
        )

    def check_layout(self) -> list:
        """Evaluate the workspace operation's guards, writing nothing, and return
        their names.

        `arbite init` runs this *before* creating the ticket store, so a layout that
        cannot be built (a file where a directory belongs) is refused with an
        instruction rather than discovered halfway through setup. Nothing here
        mutates: it is the same guard list the write operation runs, so the check
        and the write cannot disagree about what is acceptable."""
        operation = self._workspace_operation()
        for guard in operation.guards:
            guard.check()
        return [guard.name for guard in operation.guards]

    def record_workspace(self) -> OperationResult:
        """Record this workspace's binding, creating the coordination layout first.

        The write operation `arbite init` runs, and the only one this slice ships --
        it is a genuinely multi-record operation (a layout of directories plus a
        record, on the SQLite backend a table plus a row), and its guards are
        evaluated before any of it happens.

        No event is appended. The event stream's first real entries belong to the
        operations that follow this one (claiming, reads, writes), and they append
        through `transaction().append_event(...)` -- the cursor allocation and the
        commit machinery are in place, so no later slice has to invent a second
        cursor."""
        workspace = self.derived_workspace()
        created = self.store.get_workspace() is None
        self._workspace_operation().run(self)
        return succeeded(
            lines=[
                _field(
                    "coordination",
                    f"{self._display_root(self.store.root, self._is_directory_backend())} ready "
                    f"(workspace {workspace.id}, {workspace.coordination_kind} backend)",
                )
            ],
            data={
                "workspace": workspace.id,
                "created": created,
                "coordination": workspace.coordination,
            },
        )

    def _check_arbite_dir(self) -> None:
        if not self.arbite_dir.is_dir():
            raise CoordinationError(
                f"no arbite directory at {self.arbite_dir} (run 'arbite init' inside the project)"
            )

    def _is_directory_backend(self, kind: Optional[str] = None) -> bool:
        """Whether a coordination backend's root is a directory rather than a file.

        The distinction is display-only, but it is not cosmetic: a directory prints
        with a trailing slash so a reader can tell `.arbite/coordination/` (a tree)
        from `.arbite/arbite.db` (one file) at a glance."""
        return (kind or self.store.kind) == "file"

    def workspace_show(self) -> OperationResult:
        """Report the derived workspace: what it is, where its state lives, and how
        much of it is active right now.

        Read-only by construction -- it writes nothing, not even the workspace
        record -- so two runs on unchanged state print the same text, which is what
        lets the WS transcripts be compared byte for byte."""
        workspace = self.derived_workspace()
        info = self.store.info()
        scratch = scratch_summary(self.arbite_dir)
        coordination_path = self._display_root(info.root, directory=info.kind == "file")
        scratch_path = self._display_root(scratch_root(self.arbite_dir), directory=True)

        claims = (
            "no active claims"
            if info.claims_active == 0
            else _count(info.claims_active, "active claim")
        )
        coordination_summary = ", ".join(
            [claims, _count(info.events, "event"), _count(info.receipts, "receipt")]
        )
        lines = [
            _field("workspace", workspace.id),
            _field("root", workspace.root),
            _field("store", f"{self.sink_kind} ({self.store_source})"),
            _field("coordination", f"{coordination_path}  ({coordination_summary})"),
            _field("scratch", f"{scratch_path}  ({scratch.describe()})"),
        ]
        return succeeded(
            lines=lines,
            data={
                "id": workspace.id,
                "root": workspace.root,
                "store": {
                    "kind": self.store.kind,
                    "root": _relative(self.sink_root, self.project_root),
                    "source": self.store_source,
                },
                # JSON carries the plain path: the trailing slash the text line
                # prints is a reading aid for "this is a directory", not part of
                # the path, and a machine consumer must not have to strip it (the
                # DR4 transcript prints its roots without one).
                "coordination": {
                    **info.to_dict(),
                    "root": _relative(info.root, self.project_root),
                },
                "scratch": {
                    "root": _relative(scratch_root(self.arbite_dir), self.project_root),
                    **scratch.to_dict(),
                },
            },
        )

    # ------------------------------------------------------------------
    # The event stream
    # ------------------------------------------------------------------

    def events(self, after=None, tail=None, include_reads: bool = False) -> OperationResult:
        """Read the event stream: what happened, in cursor order, one line per event.

        The selection rules are the examples document's (EV2-EV5): read
        *observations* are their own category and are left out unless
        `--include-reads` asks for them, because a research-heavy agent emits dozens
        of reads per write; `--tail N` prints the last N of what is selected;
        `--after C` prints everything selected after `C`, which is how a poll
        resumes; and the report ends with the cursor to keep and the command that
        resumes from it.

        "Nothing new" is an answer with its own exit code (EV4), not an error, so a
        polling loop branches on the code instead of matching text."""
        if after is not None and tail is not None:
            raise CoordinationError(
                "--after and --tail are alternatives: resume with '--after <cursor>', "
                "or bootstrap with '--tail <count>'"
            )
        if tail is not None and tail < 1:
            raise CoordinationError(f"--tail must be at least 1, got {tail}")
        if after is not None and after < 0:
            raise CoordinationError(f"--after is a cursor, so it is 0 or more, got {after}")
        window_size = tail if tail is not None else (DEFAULT_EVENT_TAIL if after is None else None)

        selected = [
            event
            for event in self.store.events()
            if include_reads or event.category != READ_CATEGORY
        ]
        if window_size is not None:
            window = selected[-window_size:]
            # A tail asks for a fixed-size view of the end, so its columns are laid
            # out for exactly the rows it asked for.
            laid_out = window
        else:
            window = [event for event in selected if event.cursor > after]
            # A cursor resume asks "everything new", so it keeps the stream's own
            # width: the same event lines up whether it was read mid-stream or at the
            # end, which is what a parser following a log expects.
            laid_out = selected

        subject_width = _subject_width(laid_out)
        rows = [_event_row(event, subject_width) for event in window]
        cursor = window[-1].cursor if window else (after or 0)
        # The resume guidance is a *job stream* affordance: with reads included the
        # cursor may point at an observation, so the line states the cursor and stops
        # there. EV2/EV3 (the default view) carry the parenthetical; EV4 (nothing
        # returned) and EV5 (reads included) do not -- exactly as the transcripts
        # print them.
        resume = f"arbite events --after {cursor}"
        resumable = bool(window) and not include_reads
        cursor_line = f"cursor: {cursor}" + (f" (resume with '{resume}')" if resumable else "")
        data = {
            "events": [_event_json(event) for event in window],
            "cursor": cursor,
            "next_actions": [resume] if resumable else [],
        }
        if not window:
            message = (
                f"no events since cursor {after}" if after is not None else "no events recorded"
            )
            return OperationResult(Outcome(EMPTY), [message, cursor_line], data, [])
        return OperationResult(Outcome(OK), rows + [cursor_line], data, [])

    def doctor_facts(self) -> dict:
        """The coordination and scratch facts `doctor` reports.

        `doctor`'s JSON names the backend it is inspecting and the scratch area's
        size; its text keeps the existing report shape here, because the note lines
        that render scratch in text are the scratch slice's (tic-95c0) together with
        the guidance they carry, and the recovery slice (tic-b03b) owns reporting
        coordination findings as problems."""
        info = self.store.info()
        return {
            "coordination": {
                **info.doctor_dict(),
                "root": _relative(info.root, self.project_root),
            },
            "scratch": scratch_summary(self.arbite_dir).to_dict(),
        }

    def _display_root(self, root, directory: bool) -> str:
        """A store root as the text and JSON reports print it: project-relative
        where possible, and (for a directory) with the trailing slash the WS
        transcripts use -- shown whether or not it exists yet, because the layout
        is what the path *will* be."""
        text = _relative(root, self.project_root)
        if directory and not text.endswith("/"):
            text += "/"
        return text


def _subject_width(events) -> int:
    """The width of the event row's subject column.

    Four wider than the widest subject the laid-out rows have to hold, so the
    columns after it start at the same offset in every row. The rows passed in are
    the ones this command laid out, not necessarily the ones it prints: a tail lays
    out its own window, a cursor resume lays out the whole stream (see `events`)."""
    widest = max((len(event.subject) for event in events), default=0)
    return widest + EVENT_SUBJECT_PADDING


def _event_where(event) -> str:
    """The ticket/attempt column: both when both are known, the one there is
    otherwise, and an empty column when the event belongs to no ticket."""
    if event.ticket_id and event.attempt_id:
        return f"{event.ticket_id}/{event.attempt_id}"
    return event.ticket_id or event.attempt_id or ""


def _event_row(event, subject_width: int) -> str:
    """One event as a line: cursor, kind, subject, operation, ticket/attempt, actor,
    local time, and the event's own one-line outcome last.

    The stored timestamp is UTC and the printed one is local, because a human reads
    the clock on the wall while the record keeps the unambiguous value."""
    local_time = parse_utc(event.recorded_at).astimezone().strftime("%H:%M:%S")
    return (
        f"{event.cursor:<{EVENT_CURSOR_WIDTH}}"
        f"{event.kind:<{EVENT_KIND_WIDTH}}"
        f"{event.subject:<{subject_width}}"
        f"{event.operation_id or '':<{EVENT_OPERATION_WIDTH}}"
        f"{_event_where(event):<{EVENT_WHERE_WIDTH}}"
        f"{event.actor or '':<{EVENT_ACTOR_WIDTH}}"
        f"{local_time:<{EVENT_TIME_WIDTH}}"
        f"{event.result}"
    ).rstrip()


def _event_json(event) -> dict:
    """One event as the poll's JSON shape -- the fields, and the field names, the
    examples document fixes for an orchestrator polling the stream."""
    return {
        "cursor": event.cursor,
        "kind": event.kind,
        "subject": event.subject,
        "ticket": event.ticket_id,
        "attempt": event.attempt_id,
        "actor": event.actor,
        "operation": event.operation_id,
        "at": event.recorded_at,
        "result": event.result,
    }
