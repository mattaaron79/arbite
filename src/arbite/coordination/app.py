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
- `steps` are the record writes, in a defined order, with the *last* one the commit
  point. Ordering is what makes an interrupted operation recoverable today: the
  prefix left behind is a state a re-run completes, never a state that claims
  something the store cannot support. Making those steps atomic -- one transaction,
  a recoverable journal, a real lock -- is tic-1a75 (C02), which replaces this
  ordering with a transaction rather than replacing the operations.

The operations this slice ships are the workspace ones, because the workspace is
the fact every later operation is anchored to: `workspace_show` reports the derived
workspace, and `record_workspace` (run by `arbite init`) records the binding.
Claiming, reads, writes, receipts, events and passthrough arrive in their own
tickets as further operations here; none of them is implemented ahead of its slice.

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
from .records import Workspace, derived_workspace_id
from .results import OperationResult, succeeded
from .scratch import scratch_root, scratch_summary
from .store import CoordinationStore, open_coordination_store


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
        """Evaluate every guard, then perform every step, returning their names.

        A step that raises leaves the store holding the steps that already ran:
        that is the recoverable prefix, and it exists because the operations are
        ordered so the *last* step is the one that makes the state mean something."""
        for guard in self.guards:
            try:
                guard.check()
            except CoordinationError:
                raise
            except OSError as e:
                raise CoordinationError(f"{self.kind}: {guard.name}: {e}")
        completed = []
        for name, step in self.steps:
            step(context)
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

        Both steps are named, in order, because the order is what makes the
        operation recoverable without a transaction: the layout is created first and
        the workspace record last, so an interruption between them leaves an empty
        layout (a re-run completes it) and never a record whose store does not
        exist. `check_layout` runs the same guards without running the steps."""
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
                ("layout", lambda app: app.store.init()),
                ("workspace", lambda app: app.store.put_workspace(workspace)),
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

        No event is appended. Appending one with a stable cursor -- and committing it
        together with the state it describes -- is tic-1a75 (C02); a second
        implementation of the cursor here would be one the transaction slice would
        have to unpick."""
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
