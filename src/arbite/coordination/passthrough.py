"""Passthrough command observation: `arbite cmd` in *observed* mode.

Agents are trained on `grep`, `sed`, `mv` and friends, so this command wraps those
invocations instead of asking anyone to change habits: the command runs, arbite
snapshots a digest manifest of the managed paths before and after, and the paths that
differ get a receipt, a `passthrough.changed` entry in the file stream, and one
`passthrough.exec` event carrying the tool, the argv hash, the exit code and the
duration. Those events are the evidence a later policy question is decided from: which
tools agents actually reach for, how often a change lands outside a claimed set, and
how often a claim would have blocked real work.

It is deliberately the lowest-fidelity path in the proxy, and its output says so.
Three rules shape it:

- **Observation is not exclusivity.** Nothing is claimed, so another writer can
  interleave; the report prints `observed (no exclusivity claimed)` and each receipt
  records the claim generation the attempt *had* (0 when it had none, which is the
  normal case here) rather than an authorisation nobody granted.
- **A refusal never runs the command.** Shell syntax without `--shell`, an interactive
  tool, a watcher, a tool that is not on PATH, an attempt that is no longer current and
  a project with no coordination store are all judged *before* the process is started,
  and each refusal says `command did not run` (in JSON: `ran: false`). They exit 125,
  126 or 127, because 0-5 are all reachable as a wrapped tool's own exit code and a
  caller that cannot tell "the tool failed" from "arbite refused" has lost the ability
  to branch.
- **The wrapped tool's exit code survives untouched**, including the codes 0-5 the rest
  of arbite reserves (a tool really can exit 4). Only arbite's own pre-run refusals use
  125, 126 and 127.

What arbite cannot do is stated rather than implied, and it is the reason the report is
short on claims:

- It cannot know which files a program *read*, so a read-only run is an execution event
  and nothing else.
- It cannot keep the *before* bytes of a path the tool rewrote: the manifest holds
  digests and line identities, not copies, so a receipt keeps the version arbite can
  still read back -- the one the tool left -- and the version it replaced is a digest
  with no image behind it.
- It cannot see a change made and reverted inside the run (a before/after manifest has
  no ordered log), nor one made outside the manifest: arbite's own state (`.git`, the
  coordination tree, the store files, scratch, the project config) and the generated or
  build output it refuses to record a proxy write for are not managed paths.

Guarded mode (`--claim PATH...`) is the *next* slice (tic-42d2, C14) and is deliberately
not implemented here: the flag is parsed, so the shape later tickets need already
exists, and any use of it today is refused by name.
"""

from __future__ import annotations

import difflib
import os
import posixpath
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..errors import ArbiteError
from .paths import (
    STATE_CONFIG,
    STATE_COORDINATION,
    STATE_GIT,
    STATE_SCRATCH,
    STATE_STORE,
    arbite_state,
    policy_exclusion,
)
from .records import (
    ABSENT,
    RECEIPT_SUCCEEDED,
    Artifact,
    OperationReceipt,
    digest_bytes,
    new_id,
    short_digest,
    utc_now,
)
from .results import (
    EXIT_PASSTHROUGH_NOT_FOUND,
    EXIT_PASSTHROUGH_REFUSED,
    EXIT_PASSTHROUGH_UNSUPPORTED,
    render_next_line,
    text_hint_of,
)
from .store import require_storable_artifact

# ---------------------------------------------------------------------------
# Exit codes and the words the refusals use
# ---------------------------------------------------------------------------

#: 125, 126 and 127 are arbite's own, and nothing else here uses them: a *refusal* is
#: the one outcome a caller must never confuse with the wrapped tool's result.
REFUSED = EXIT_PASSTHROUGH_REFUSED
UNSUPPORTED = EXIT_PASSTHROUGH_UNSUPPORTED
NOT_FOUND = EXIT_PASSTHROUGH_NOT_FOUND

#: The sentence the frozen PC6 blocks print for the two refusals that name it. A
#: refusal that says this says the process was never started -- which is also why no
#: `passthrough.exec` event exists for it (JSON carries the same fact as `ran: false`).
NO_RUN = "command did not run"

#: The reason keys, so the JSON branch key and the printed sentence come from one
#: place. They are strings rather than an enum because they are also payload keys.
REASON_SHELL_SYNTAX = "shell_syntax"
REASON_INTERACTIVE = "interactive"
REASON_LONG_RUNNING = "long_running"
REASON_BACKGROUND = "background"
REASON_NOT_FOUND = "tool_not_found"
REASON_GUARDED = "guarded_not_implemented"
REASON_NO_STORE = "no_coordination_store"
REASON_ATTEMPT = "attempt_not_current"
REASON_NO_COMMAND = "no_command"
REASON_ATTEMPT_PAIR = "attempt_pair"

#: The refusals whose sentence is frozen: PC6 prints these three literally, so the
#: words live here rather than at the call site.
SHELL_SYNTAX_MESSAGE = (
    "'{token}' style shell syntax needs '--shell'; without it arbite executes argv directly"
)
SHELL_SYNTAX_HINT = (
    "next: re-run with '--shell -- \"<command>\"', or pass arguments without shell syntax"
)
INTERACTIVE_MESSAGE = "interactive commands are not supported (no terminal is provided)"
INTERACTIVE_HINT = (
    "next: edit through 'arbite file edit', or run the editor outside arbite and accept "
    "that the change is unattributed"
)
NOT_FOUND_MESSAGE = "'{tool}' was not found on PATH"

#: The tokens that *are* shell syntax rather than an argument to the program. A token
#: that merely contains one of these characters -- a sed substitution's `|`, a regex's
#: `*` -- is a character, not an operator, and is passed through as argv.
SHELL_OPERATORS = (
    ">", ">>", ">|", ">>|", "<", "<<", "<<<", "|", "||", "&&", "&", ";", ";;", "&>", "&>>",
    "1>", "1>>", "2>", "2>>", "2>&1", "1>&2",
)
#: An expansion the caller almost certainly expected the shell to perform. These are
#: refused anywhere in a token, because `$HOME` in an argv is never what was meant.
SHELL_EXPANSIONS = ("$(", "${", "`")

#: Tools that need a terminal. arbite gives the command no terminal and an empty stdin
#: (see `PassthroughRun._popen`), which is what makes this refusal true rather than
#: a guess about the tool's behaviour.
INTERACTIVE_TOOLS = (
    "vim", "vi", "nvim", "nano", "pico", "emacs", "less", "more", "most", "man",
    "top", "htop", "btop", "screen", "tmux", "ssh", "mutt", "alpine", "irssi", "w3m",
)

#: Commands that do not return on their own, and the follower flags of the tools that
#: gain a "never returns" mode. arbite runs one command to completion in the
#: foreground, so these are refused before they start; a tool that is long-running
#: without looking like one cannot be detected in advance, and that limit is stated in
#: the refusal's own words rather than guessed at.
LONG_RUNNING_TOOLS = ("watch", "inotifywait", "entr", "tailf")
FOLLOW_FLAGS = {
    "tail": ("-f", "-F", "--follow"),
    "journalctl": ("-f", "--follow"),
    "docker": ("-f", "--follow"),
    "kubectl": ("-f", "--follow"),
}

LONG_RUNNING_MESSAGE = (
    "a long-running command is out of scope: '{tool}' does not return on its own"
)
LONG_RUNNING_HINT = (
    "next: run it outside arbite and accept that the change is unattributed, or use a "
    "form of the command that returns"
)
BACKGROUND_MESSAGE = (
    "a background command is out of scope: arbite does not leave a process running "
    "behind it"
)
BACKGROUND_HINT = (
    "next: run the command in the foreground, or run it outside arbite and accept that "
    "the change is unattributed"
)

#: The guarded-mode seam. C14 replaces the refusal with the acquisition, the busy
#: refusal and the verification; today the flag exists so the caller-facing shape does.
GUARDED_MESSAGE = (
    "guarded mode ('--claim') is not implemented yet, so this run would claim no "
    "exclusivity"
)
GUARDED_HINT = (
    "next: 'arbite cmd -- <command>' to run it in observed mode, which records what it "
    "changed without claiming anything"
)

#: What a version arbite cannot keep is reported as: the receipt still names the digest,
#: and the report says the image is not held. A refusal is not available here, because
#: the command that wrote those bytes has already run.
OVERSIZE_NOTE = (
    "note: {path} is recorded as a digest only (the version is larger than arbite keeps, "
    "and the command it described has already run)"
)

NO_STORE_MESSAGE = (
    "this project has no coordination store, so the run could not be recorded"
)
NO_STORE_HINT = "next: 'arbite init' to create the store, then re-run the command"

NO_COMMAND_MESSAGE = "a command is required"
NO_COMMAND_HINT = "next: 'arbite cmd -- <command> [args...]', or '--shell -- \"<command>\"'"

ATTEMPT_PAIR_MESSAGE = (
    "an attempt is what attributes a passthrough run, so --ticket and --attempt are "
    "given together or not at all"
)
ATTEMPT_PAIR_HINT = (
    "next: add the missing flag, or drop both and let the run be recorded without "
    "attribution"
)

# ---------------------------------------------------------------------------
# The report's literals
# ---------------------------------------------------------------------------

#: How an observed run is labelled. `observed` is the honest word: another writer can
#: interleave, and C14's guarded mode is what earns the word `exclusive`.
MODE = "observed"
NO_EXCLUSIVITY = "(no exclusivity claimed)"
MODE_OBSERVED = f"mode: {MODE}"
SHELL_NOTE = "note: redirections happen in the shell and are visible only after the fact"

ECHO_PREFIX = "arbite cmd: "
EXIT_LINE = "exit: {code} ({ms} ms)  {mode}{tail}"
CHANGED_HEADING = "changed {count} {noun}:"

#: The event line of the frozen PC1/PC5 blocks: the kind, the tool, the ticket and
#: attempt it is attributed to, and the actor the store names for that attempt. The
#: gaps are the frozen block's (three, three, two spaces), not a column layout.
EVENT_PREFIX = "event: passthrough.exec"
EVENT_TOOL = "   tool: {tool}"
EVENT_WHERE = "   ticket: {ticket} / {attempt}"
EVENT_ACTOR = "  actor: {actor}"

#: The success hint of the frozen PC1 block. The command to review is named with the
#: ticket already filled in, and the claim half is a *flag* on this command, exactly as
#: the block writes it -- it is the seam C14 fills in.
NEXT_REVIEW = (
    "'arbite changes {ticket}' to review, or claim paths next time ('{claim}') for exclusivity"
)
NEXT_CLAIM_ONLY = "claim paths next time ('{claim}') for exclusivity"
NEXT_FAILED = "'arbite changes {ticket}' to review what the failed command left behind"
NEXT_FAILED_NO_TICKET = "'arbite receipt {operation}' to read the change it left behind"

#: How a row describes its two endpoints. `absent` is the version a create replaced or
#: a removal produced (`records.ABSENT`), and a delta of lines needs both sides read as
#: text: bytes arbite cannot read print their two sizes instead, because a line count
#: there would be a number nobody could reproduce.
STATUS_CREATED = "A"
STATUS_CHANGED = "M"
STATUS_REMOVED = "D"
DETAIL_CREATED = "created"
DETAIL_REMOVED = "removed"
DETAIL_DELTA = "+{added} -{removed}"
DETAIL_BINARY = "{before} -> {after} bytes (binary)"

ROW = "  {status} {path}  {before} -> {after}  {detail}  ({operation})"

#: The odd names the receipts' `result` field can have. An observed change is recorded,
#: not performed, and `succeeded` is the store's word for "this receipt is final"; the
#: *tool's* outcome lives in the event's payload, where it cannot be read as arbite's
#: own opinion of the command.
RECEIPT_RESULT = RECEIPT_SUCCEEDED

#: The event kinds and categories this slice appends. `passthrough.exec` is the run, in
#: the `passthrough` category the records module already reserves; `passthrough.changed`
#: is one changed path, in the ordinary file stream, so a passthrough change sits in
#: `arbite events --tail` beside a proxy write and in the same log `arbite changes`
#: orders by cursor.
EXEC_KIND = "passthrough.exec"
EXEC_CATEGORY = "passthrough"
CHANGED_KIND = "passthrough.changed"
CHANGED_CATEGORY = "file"

# ---------------------------------------------------------------------------
# Bounded capture
# ---------------------------------------------------------------------------

#: How much of the command's own output is kept for the report. Both bounds are applied
#: while the pipe is drained, so a command that prints a gigabyte cannot exhaust memory:
#: the bytes beyond the byte bound are read and discarded. The output is never stored --
#: arbite records versions, not prose -- which the truncation line says out loud.
CAPTURE_LINES = 40
CAPTURE_BYTES = 8 * 1024
TRUNCATION = (
    "{stream} truncated: {shown} of {total} lines shown (the command's output is "
    "captured, not stored)"
)
STILL_OPEN = (
    "{stream} was still open when the command exited (a process it started holds it); "
    "arbite does not wait for that, so this is what was read"
)
DRAIN_CHUNK = 64 * 1024

#: How long the pipe readers get after the command itself has exited. Normally they are
#: already done; the grace period is what a background child costs, and it is bounded
#: because arbite is one-shot.
PIPE_GRACE_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """A refusal that happens *before* anything runs, as a value rather than a raise.

    Keeping it a value is what makes "refusals never run the command" checkable: the
    caller either has a refusal or a run, and only a run can start a process or append
    an event. `confirmation` is the sentence the frozen blocks print for the refusals
    that name it; JSON always carries `ran: false`, so the fact is not lost where the
    text is pinned by a transcript.
    """

    message: str
    reason: str
    exit_code: int
    text_hint: str = ""
    confirmation: str = ""
    actions: tuple = ()

    def to_text(self) -> str:
        lines = [f"error: {self.message}"]
        if self.confirmation:
            lines.append(self.confirmation)
        if self.text_hint:
            lines.append(self.text_hint)
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "error": self.message,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "ran": False,
            "mode": MODE,
            "next_actions": list(self.actions),
        }


def _refused(message: str, reason: str, hint: str = "", actions=()) -> Refusal:
    """A policy refusal: arbite declined, so the code is 125 and the command never ran."""
    return Refusal(
        message=message,
        reason=reason,
        exit_code=REFUSED,
        text_hint=hint,
        confirmation=NO_RUN,
        actions=tuple(actions),
    )


def _unsupported(message: str, reason: str, hint: str, confirmation: bool = False) -> Refusal:
    """An invocation arbite does not support: 126, judged before the process starts.

    `confirmation` is off for the two refusals the frozen PC6 block writes without the
    sentence -- their words already say the invocation was refused, and the absent
    `passthrough.exec` event is the proof it never ran.
    """
    return Refusal(
        message=message,
        reason=reason,
        exit_code=UNSUPPORTED,
        text_hint=hint,
        confirmation=NO_RUN if confirmation else "",
    )


# ---------------------------------------------------------------------------
# What a run found
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One managed path in the before/after manifest.

    `digest` decides whether the path changed; `lines` is what makes a line delta
    possible for a path whose *before* bytes are already gone. It holds one identity
    per line (Python's own string hash) rather than a copy of the content, so the
    manifest is proportional to the number of lines rather than to the size of the
    tree, and nothing about a project's bytes is held in memory or written anywhere.
    The identities are compared inside this one process and never printed or stored,
    which is why the per-process hash salt does not matter.
    """

    digest: str
    size: int
    lines: Optional[tuple] = None

    @property
    def is_text(self) -> bool:
        return self.lines is not None

    def to_dict(self) -> dict:
        return {"digest": self.digest, "bytes": self.size}


@dataclass(frozen=True)
class Change:
    """One path an observed command changed, and what arbite can honestly say about it."""

    path: str
    status: str
    before: Optional[ManifestEntry]
    after: Optional[ManifestEntry]
    operation: str
    claim: Optional[dict] = None

    @property
    def before_digest(self) -> str:
        return ABSENT if self.before is None else self.before.digest

    @property
    def after_digest(self) -> str:
        return ABSENT if self.after is None else self.after.digest

    @property
    def generation(self) -> int:
        """The claim generation this attempt held on the path, or 0 for none.

        A claim held by *somebody else* does not make 0 wrong: this run happened under
        no claim of its own, and saying otherwise would dress an observation up as an
        authorisation. The holder is reported separately, for the questions the
        `passthrough.exec` stream exists to answer.
        """
        if self.claim and self.claim.get("mine"):
            return int(self.claim.get("generation") or 0)
        return 0

    @property
    def detail(self) -> str:
        if self.before is None:
            return DETAIL_CREATED
        if self.after is None:
            return DETAIL_REMOVED
        if self.before.is_text and self.after.is_text:
            added, removed = _line_counts(self.before.lines, self.after.lines)
            return DETAIL_DELTA.format(added=added, removed=removed)
        return DETAIL_BINARY.format(before=self.before.size, after=self.after.size)

    def row(self) -> str:
        # TODO(tic-42d2 / C14): guarded mode inserts its `claimed` / `NOT claimed` column
        # between the detail and the operation id (PC4) once every change is verified
        # against the claimed set; observed mode has no set to compare against.
        return ROW.format(
            status=self.status,
            path=self.path,
            before=short_digest(self.before_digest),
            after=short_digest(self.after_digest),
            detail=self.detail,
            operation=self.operation,
        )

    def facts(self) -> dict:
        return {
            "path": self.path,
            "status": self.status,
            "before": None if self.before is None else self.before.to_dict(),
            "after": None if self.after is None else self.after.to_dict(),
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "detail": self.detail,
            "operation": self.operation,
            "claimed": bool(self.claim and self.claim.get("mine")),
            "held_by": (
                None if not self.claim or self.claim.get("mine") else self.claim.get("holder")
            ),
        }


@dataclass(frozen=True)
class Captured:
    """One stream of the command's own output, bounded while it was drained.

    `bytes` is what the command really produced and `text` is the bounded prefix the
    report shows, so "there was more" is a fact rather than an inference. `open` says the
    stream was still being written when arbite stopped waiting -- a process the command
    started outlived it -- which is reported rather than waited for.
    """

    text: str
    lines: int
    shown: int
    bytes: int
    truncated: bool
    open: bool = False

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "lines": self.lines,
            "shown": self.shown,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "open": self.open,
        }

    def note(self, stream: str) -> str:
        return TRUNCATION.format(stream=stream, shown=self.shown, total=self.lines)


@dataclass(frozen=True)
class Report:
    """What an observed run reports: the lines, the same facts as JSON, and the code.

    `exit_code` is the wrapped command's own -- never arbite's -- so `next_actions` and
    the text carry the advice while the code carries the tool's result.
    """

    exit_code: int
    lines: list
    data: dict = field(default_factory=dict)
    next_actions: tuple = ()
    text_hint: str = ""
    stderr_text: str = ""

    def to_text(self) -> str:
        parts = list(self.lines)
        hint = self.text_hint or render_next_line(self.next_actions)
        if hint:
            parts.append(hint)
        return "\n".join(parts)

    def to_json(self) -> dict:
        payload = dict(self.data)
        payload["next_actions"] = list(self.next_actions)
        return payload


def _line_counts(before: tuple, after: tuple) -> tuple:
    """`(added, removed)` for two line-identity sequences.

    The same opcode walk `coordination.writes.line_delta` performs on the text itself,
    and the same numbers: two lines are equal exactly when their identities are, so the
    equalities SequenceMatcher sees are the ones the text would present.
    """
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, before, after)
    for tag, start, end, new_start, new_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += end - start
        if tag in ("replace", "insert"):
            added += new_end - new_start
    return added, removed


# ---------------------------------------------------------------------------
# The invocation, prepared
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassthroughRun:
    """A validated invocation, ready to start: everything a refusal would have stopped.

    Built by `PassthroughRuns.plan`, which is the *only* thing that decides whether a
    command may run. Nothing here re-checks policy at execution time, so "the refusal
    happened before anything started" is a property of the shape rather than of care.
    """

    store: object
    project_root: Path
    argv: tuple
    tool: str
    shell: bool
    ticket_id: Optional[str] = None
    attempt_id: Optional[str] = None
    actor: Optional[str] = None
    #: The paths a guarded run would hold (C14). Empty by construction here: `plan`
    #: refuses `--claim` before a run exists, so no observed run can carry one.
    claim_paths: tuple = ()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def execute(self) -> Report:
        """Run the command, diff the manifest, record what changed and report it."""
        before = _manifest(self.project_root)
        started = time.monotonic()
        process, stdout, stderr = self._run()
        duration_ms = int(round((time.monotonic() - started) * 1000))
        code = _exit_code(process.returncode)
        after = _manifest(self.project_root)
        changes = self._changes(before, after)
        recorded = self._record(changes, code, duration_ms, stdout, stderr)
        return self._report(changes, code, duration_ms, stdout, stderr, recorded)

    def command_line(self) -> str:
        if self.shell:
            return f"sh -c '{self.argv[0]}'"
        return " ".join(self.argv)

    def _run(self):
        """Start the command, drain both pipes bounded, and stop once it has finished.

        The child gets an empty stdin and no terminal: that is what makes "interactive
        commands are not supported" true rather than a preference, and it is what stops
        a tool that decides to prompt from hanging arbite. Each pipe is read by its own
        daemon thread and the bytes past the bound are discarded -- a pipe nobody drains
        would block the child, and buffering the whole output is exactly the unbounded
        memory this bound exists to avoid.

        The wait is bounded on purpose. The command is waited for (that is the whole
        point of running it), but a pipe is *not*: a process the command started can
        inherit them, outlive it and hold them open, and arbite is one-shot -- it will not
        sit there for a daemon. So after the command exits, the readers get a grace period
        and whatever is still open is reported as still open.
        """
        process = subprocess.Popen(  # noqa: S603 - the caller asked for this command
            self._popen_argv(),
            cwd=str(self.project_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = _PipeCapture(CAPTURE_BYTES), _PipeCapture(CAPTURE_BYTES)
        readers = [
            _reader(process.stdout, stdout),
            _reader(process.stderr, stderr),
        ]
        for reader in readers:
            reader.start()
        process.wait()
        deadline = time.monotonic() + PIPE_GRACE_SECONDS
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
        return process, stdout.captured(), stderr.captured()

    def _popen_argv(self) -> list:
        return ["sh", "-c", self.argv[0]] if self.shell else list(self.argv)

    # ------------------------------------------------------------------
    # The diff
    # ------------------------------------------------------------------

    def _changes(self, before: dict, after: dict) -> list:
        """The changed paths, in canonical order, each with the claim facts around it."""
        changes = []
        for path in sorted(set(before) | set(after)):
            old, new = before.get(path), after.get(path)
            if old is not None and new is not None and old.digest == new.digest:
                continue
            if old is None:
                status = STATUS_CREATED
            elif new is None:
                status = STATUS_REMOVED
            else:
                status = STATUS_CHANGED
            changes.append(
                Change(
                    path=path,
                    status=status,
                    before=old,
                    after=new,
                    operation=self._mint_operation(),
                    claim=self._claim_facts(path),
                )
            )
        return changes

    def _mint_operation(self) -> str:
        """A fresh receipt id, absent from the receipts the store already holds."""
        return new_id("receipt", {receipt.id for receipt in self.store.records("receipt")})

    def _claim_facts(self, path: str) -> Optional[dict]:
        """Any live claim on `path`: whose it is, at which generation, and whether mine."""
        active = self.store.claims_for_path(path)
        if not active:
            return None
        holder = active[0]
        mine = bool(self.attempt_id) and holder.held_by(self.attempt_id)
        return {
            "mine": mine,
            "generation": holder.generation,
            "holder": None if mine else f"{holder.ticket_id}/{holder.attempt_id}",
        }

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, changes, code: int, duration_ms: int, stdout, stderr) -> dict:
        """Persist the evidence: one receipt per changed path, then the run's event.

        Everything commits in one transaction, so a run cannot be half recorded. The
        only thing outside it is the after-version's *bytes* (an artifact, best effort):
        a version arbite cannot keep -- it is over the store's size limit, or the backend
        has no artifact store -- does not fail the recording, because the tool already
        ran and a receipt naming the digest is still true. `artifacts` says which
        versions have an image behind them.
        """
        artifacts: dict = {}
        notes: list = []
        for change in changes:
            if change.after is None:
                continue
            data = _read_bytes(self.project_root, change.path)
            if data is None:
                continue
            try:
                require_storable_artifact(change.after.digest, data)
            except ArbiteError:
                # The mutation engine refuses to *start* on a version it cannot keep;
                # here the tool has already run, so the honest answer is a receipt that
                # names the digest and says the bytes are not held.
                notes.append(OVERSIZE_NOTE.format(path=change.path))
                continue
            artifacts[change.after.digest] = data

        # Which change stored each version, so an artifact record names the operation it
        # belongs to: two paths that end with identical bytes share one artifact, and the
        # first change that named it is the one recorded.
        by_digest = {
            change.after.digest: change.operation
            for change in changes
            if change.after is not None
        }
        stored = self._store_artifacts(artifacts, notes, by_digest)
        payload = {
            "tool": self.tool,
            "argv": list(self.argv),
            "argv_hash": digest_bytes(_argv_bytes(self.argv, self.shell)),
            "shell": self.shell,
            "mode": MODE,
            "exclusive": False,
            "exit_code": code,
            "duration_ms": duration_ms,
            "changed": [change.path for change in changes],
            "receipts": [change.operation for change in changes],
            "claims": {
                change.path: change.claim for change in changes if change.claim is not None
            },
            "unclaimed": [change.path for change in changes if change.generation == 0],
            "stdout_bytes": stdout.bytes,
            "stderr_bytes": stderr.bytes,
        }
        with self.store.transaction() as txn:
            for change in changes:
                # TODO(tic-42d2 / C14): a guarded run verifies every change against the
                # claimed set here and records an unclaimed write as `unclaimed_write`
                # instead of the plain observed receipt this slice writes.
                txn.put_record(
                    OperationReceipt(
                        id=change.operation,
                        kind="passthrough",
                        paths=[change.path],
                        result=RECEIPT_RESULT,
                        recorded_at=utc_now(),
                        ticket_id=self.ticket_id,
                        attempt_id=self.attempt_id,
                        actor=self.actor,
                        before={change.path: change.before_digest},
                        after={change.path: change.after_digest},
                        artifacts=sorted(
                            artifact.id
                            for digest, artifact in stored.items()
                            if digest == change.after_digest
                        ),
                        claim_generation=change.generation,
                    )
                )
                txn.append_event(
                    CHANGED_KIND,
                    CHANGED_CATEGORY,
                    subject=change.path,
                    result=change.detail,
                    ticket_id=self.ticket_id,
                    attempt_id=self.attempt_id,
                    actor=self.actor,
                    operation_id=change.operation,
                    payload={
                        "before": change.before_digest,
                        "after": change.after_digest,
                        "generation": change.generation,
                    },
                )
            txn.append_event(
                EXEC_KIND,
                EXEC_CATEGORY,
                subject=self.tool,
                result=f"exit {code} ({duration_ms} ms)",
                ticket_id=self.ticket_id,
                attempt_id=self.attempt_id,
                actor=self.actor,
                payload=payload,
            )
        return {"tool": self.tool, "argv_hash": payload["argv_hash"], "notes": notes}

    def _store_artifacts(self, data: dict, notes: list, by_digest: dict) -> dict:
        """Store the after-versions' bytes once per digest, and return `digest -> record`."""
        existing = {artifact.digest: artifact for artifact in self.store.records("artifact")}
        stored = {}
        for digest, content in sorted(data.items()):
            artifact = existing.get(digest)
            if artifact is None:
                try:
                    self.store.put_artifact_bytes(digest, content)
                except (ArbiteError, NotImplementedError) as exc:
                    notes.append(
                        f"note: this store cannot keep the bytes of "
                        f"{short_digest(digest)} ({exc}), so the version the command left "
                        "is recorded as a digest"
                    )
                    continue
                artifact = Artifact(
                    id=new_id("artifact", {a.id for a in existing.values()}),
                    digest=digest,
                    size=len(content),
                    created=utc_now(),
                    operation_id=by_digest.get(digest),
                )
                self.store.put_record(artifact)
                existing[digest] = artifact
            stored[digest] = artifact
        return stored

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _report(self, changes, code: int, duration_ms: int, stdout, stderr, recorded) -> Report:
        """The report: what ran, what it cost, what changed, and what is *not* claimed."""
        lines = [ECHO_PREFIX + self.command_line()]
        lines.extend(_captured_lines(stdout))
        lines.append(
            EXIT_LINE.format(
                code=code,
                ms=duration_ms,
                mode=MODE_OBSERVED,
                tail=f"  {SHELL_NOTE}" if self.shell else f" {NO_EXCLUSIVITY}",
            )
        )
        if changes:
            lines.append(
                CHANGED_HEADING.format(
                    count=len(changes), noun="path" if len(changes) == 1 else "paths"
                )
            )
            lines.extend(change.row() for change in changes)
        lines.append(self._event_line())
        for note in recorded["notes"]:
            lines.append(note)
        for name, stream in (("stdout", stdout), ("stderr", stderr)):
            if stream.open:
                lines.append(STILL_OPEN.format(stream=name))
            if stream.truncated:
                lines.append(stream.note(name))

        actions, text_hint = self._next(changes, code)
        return Report(
            exit_code=code,
            lines=lines,
            data=self._facts(changes, code, duration_ms, stdout, stderr, recorded),
            next_actions=actions,
            text_hint=text_hint,
            stderr_text=stderr.text,
        )

    def _event_line(self) -> str:
        line = EVENT_PREFIX + EVENT_TOOL.format(tool=self.tool)
        if self.ticket_id and self.attempt_id:
            line += EVENT_WHERE.format(ticket=self.ticket_id, attempt=self.attempt_id)
        if self.actor:
            line += EVENT_ACTOR.format(actor=self.actor)
        return line

    def _next(self, changes, code) -> tuple:
        """The next actions, and the sentence they print as.

        The hint is computed from the state at the end of the run and names only tokens
        this command printed: the ticket, the paths that changed, and the receipts. A
        run that changed nothing has no next step (the frozen PC5 block prints none),
        which is the honest answer -- there is nothing to review.
        """
        if not changes:
            return (), ""
        if code != 0:
            first = changes[0].operation
            if self.ticket_id:
                action = f"arbite changes {self.ticket_id}"
                text = f"next: {NEXT_FAILED.format(ticket=self.ticket_id)}"
            else:
                action = f"arbite receipt {first}"
                text = f"next: {NEXT_FAILED_NO_TICKET.format(operation=first)}"
            return (action,), text
        claim = "--claim " + " ".join(change.path for change in changes)
        if not self.ticket_id:
            return (), f"next: {NEXT_CLAIM_ONLY.format(claim=claim)}"
        review = f"arbite changes {self.ticket_id}"
        return (review,), f"next: {NEXT_REVIEW.format(ticket=self.ticket_id, claim=claim)}"

    def _facts(self, changes, code, duration_ms, stdout, stderr, recorded) -> dict:
        """The JSON form: the same facts as the text, plus the branchable ones.

        `exclusive` is False and stays False in this slice; `exclusivity` names the seam
        (the `--claim` flag) *and* says it is not available yet, so a machine consumer
        cannot read the frozen hint as a promise arbite does not keep.
        """
        return {
            "command": list(self.argv),
            "tool": self.tool,
            "cmdline": self.command_line(),
            "shell": self.shell,
            "mode": MODE,
            "exclusive": False,
            "exclusivity": {
                "claimed": [],
                "available": False,
                "hint": (
                    None
                    if not changes
                    else "arbite cmd --claim "
                    + " ".join(change.path for change in changes)
                    + " -- <command>"
                ),
                "reason": REASON_GUARDED,
            },
            "argv_hash": recorded["argv_hash"],
            "exit_code": code,
            "duration_ms": duration_ms,
            "output": {"stdout": stdout.to_dict(), "stderr": stderr.to_dict()},
            "changed": [change.facts() for change in changes],
            "event": EXEC_KIND,
            "ticket": self.ticket_id,
            "attempt": self.attempt_id,
            "actor": self.actor,
            "receipts": [change.operation for change in changes],
            "ran": True,
        }


# ---------------------------------------------------------------------------
# The command surface
# ---------------------------------------------------------------------------


class PassthroughRuns:
    """`arbite cmd`, for one sink and one coordination store: plan, then run."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = Path(self.app.project_root)

    def plan(
        self,
        argv,
        *,
        ticket_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        shell: bool = False,
        claim_paths=(),
    ):
        """Everything that can refuse, before anything can run: a `Refusal` or a run.

        The order is the order a caller can act on it: what was asked for at all, then
        the seam that is not implemented, then the invocation's shape, then the tool,
        then the state that has to hold for the run to be recorded and attributed. Every
        one of them returns a value, so there is no path from this function to a started
        process that skipped a check.
        """
        argv = tuple(arg for arg in argv if arg is not None)
        if not argv:
            return _unsupported(NO_COMMAND_MESSAGE, REASON_NO_COMMAND, NO_COMMAND_HINT)
        if claim_paths:
            # TODO(tic-42d2 / C14): guarded mode replaces this refusal with the
            # all-or-nothing claim, its busy refusal, and the verification of every
            # observed change against the claimed set. The flag is parsed here so the
            # caller-facing shape that slice extends already exists.
            return _refused(GUARDED_MESSAGE, REASON_GUARDED, GUARDED_HINT)
        if bool(ticket_id) != bool(attempt_id):
            return _unsupported(ATTEMPT_PAIR_MESSAGE, REASON_ATTEMPT_PAIR, ATTEMPT_PAIR_HINT)

        tool = "sh" if shell else _tool_name(argv[0])
        if shell:
            refusal = _shell_line_refusal(argv[0], tool)
            if refusal is not None:
                return refusal
            if shutil.which("sh") is None:
                return _not_found("sh")
        else:
            refusal = _argv_refusal(argv, tool)
            if refusal is not None:
                return refusal
            if _tool_path(argv[0], self.project_root) is None:
                return _not_found(_tool_name(argv[0]))

        actor = None
        if ticket_id:
            try:
                attempt = self.lifecycle.require_attempt(ticket_id, attempt_id)
            except ArbiteError as refused:
                return Refusal(
                    message=str(refused),
                    reason=REASON_ATTEMPT,
                    exit_code=REFUSED,
                    text_hint=text_hint_of(refused),
                    confirmation=NO_RUN,
                    actions=tuple(getattr(refused, "next_actions", ()) or ()),
                )
            actor = attempt.worker_id
        try:
            workspace = self.store.get_workspace()
        except ArbiteError:
            # A backend whose store does not exist yet (the SQLite file, before `init`)
            # answers by refusing to describe what is not there. Either way the run has
            # nowhere to be recorded, so it is refused rather than run unrecorded.
            workspace = None
        if workspace is None:
            return _refused(NO_STORE_MESSAGE, REASON_NO_STORE, NO_STORE_HINT)

        return PassthroughRun(
            store=self.store,
            project_root=self.project_root,
            argv=argv,
            tool=tool,
            shell=shell,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            actor=actor,
            claim_paths=tuple(claim_paths),
        )


def _not_found(tool: str) -> Refusal:
    """The tool is not on PATH: 127, the code a shell uses for the same fact.

    No next action, and the frozen PC6 block prints none: what to install is not
    something arbite can name, and inventing a command here would be advice it cannot
    stand behind.
    """
    return Refusal(
        message=NOT_FOUND_MESSAGE.format(tool=tool),
        reason=REASON_NOT_FOUND,
        exit_code=NOT_FOUND,
        confirmation=NO_RUN,
    )


def _argv_refusal(argv, tool: str) -> Optional[Refusal]:
    """The refusals an argv invocation can trip: shell syntax, a terminal, a watcher."""
    token = shell_syntax(argv)
    if token is not None:
        return _unsupported(
            SHELL_SYNTAX_MESSAGE.format(token=token), REASON_SHELL_SYNTAX, SHELL_SYNTAX_HINT
        )
    if (refusal := _interactive_refusal(tool)) is not None:
        return refusal
    return _long_running_refusal(tool, argv)


def _shell_line_refusal(line: str, tool: str) -> Optional[Refusal]:
    """The refusals a `sh -c` line can trip: a background process, a terminal, a watcher.

    Shell syntax is *the point* of `--shell`, so the operators are left to the shell --
    except backgrounding, which would leave a process running behind a one-shot command,
    and the same terminal and watcher questions the argv form asks.
    """
    if (refusal := _interactive_refusal(tool)) is not None:
        return refusal
    tokens = _shell_tokens(line)
    if "&" in tokens or line.rstrip().endswith("&"):
        return _unsupported(BACKGROUND_MESSAGE, REASON_BACKGROUND, BACKGROUND_HINT)
    return _long_running_refusal(tool, tokens)


def _interactive_refusal(tool: str) -> Optional[Refusal]:
    if tool in INTERACTIVE_TOOLS:
        return _unsupported(INTERACTIVE_MESSAGE, REASON_INTERACTIVE, INTERACTIVE_HINT)
    return None


def _long_running_refusal(tool: str, tokens) -> Optional[Refusal]:
    """A command that does not return on its own, refused by name.

    Only the shapes arbite can recognise are here: a watcher, or a follower flag on a
    tool that gains a "never returns" mode. Anything else that happens to run for a long
    time cannot be detected before it starts, and the refusal's own wording says what
    arbite is not: a supervisor.
    """
    if tool in LONG_RUNNING_TOOLS:
        return _unsupported(
            LONG_RUNNING_MESSAGE.format(tool=tool), REASON_LONG_RUNNING, LONG_RUNNING_HINT
        )
    for flag in FOLLOW_FLAGS.get(tool, ()):
        if flag in tokens:
            return _unsupported(
                LONG_RUNNING_MESSAGE.format(tool=tool), REASON_LONG_RUNNING, LONG_RUNNING_HINT
            )
    return None


def shell_syntax(argv) -> Optional[str]:
    """The first token that *is* shell syntax rather than an argument, or None.

    A whole token that is an operator (`>`, `>>`, `|`, `&`, `;`, `2>&1`), or a token
    containing a substitution (`$(...)`, `${...}`, a backtick), is syntax the caller
    expected a shell to act on: arbite refuses it and says `--shell` is how to get that.
    An operator *inside* a token is a character a program asked for -- a sed
    substitution's `|`, a regex's `*` -- and is passed through exactly as given, which is
    why `sed -i 's/a/a|b/'` runs and `grep -c '>' file` does not.
    """
    for token in argv:
        if token in SHELL_OPERATORS:
            return token
        for expansion in SHELL_EXPANSIONS:
            if expansion in token:
                return expansion
    return None


def _shell_tokens(line: str) -> tuple:
    """The line's words, or an empty tuple when it does not split (the shell's problem)."""
    try:
        return tuple(shlex.split(line))
    except ValueError:
        return ()


def _tool_name(word: str) -> str:
    """The program's name as the report prints it: the basename of a path, or the word."""
    return posixpath.basename(word.replace("\\", "/")) or word


def _tool_path(word: str, project_root) -> Optional[str]:
    """Where `word` would be found, or None.

    A word that names a path is resolved against the project root, because that is the
    directory the command will run in -- resolving it against arbite's own working
    directory would check a different file than the one about to be executed.
    """
    if "/" in word or "\\" in word:
        candidate = Path(word) if os.path.isabs(word) else Path(project_root) / word
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(word)


def _argv_bytes(argv, shell: bool) -> bytes:
    """The exact bytes an argv hash covers: NUL-separated, so no join is ambiguous."""
    return ("\0".join(argv) + ("\0shell" if shell else "")).encode("utf-8")


def _exit_code(returncode: int) -> int:
    """The code arbite reports for a finished process, mapped the way a shell would.

    A process killed by a signal has no exit code of its own, so the shell's convention
    (128 + signal) is used rather than a negative number that could be mistaken for one.
    """
    return returncode if returncode >= 0 else 128 - returncode


class _PipeCapture:
    """One pipe, drained by its own thread into a bounded buffer.

    Shared with the main thread rather than returned by the reader, because a reader
    that is still blocked when the grace period ends must not take its bytes with it:
    whatever arrived before that moment is still what the command printed.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.kept = bytearray()
        self.total = 0
        self.finished = False

    def drain(self, stream) -> None:
        while True:
            chunk = stream.read(DRAIN_CHUNK)
            if not chunk:
                self.finished = True
                return
            self.total += len(chunk)
            if len(self.kept) < self.limit:
                self.kept.extend(chunk[: self.limit - len(self.kept)])

    def captured(self) -> Captured:
        """This stream as the report prints it: whole lines, and whether they were cut.

        Neither bound may print half a line: when the byte bound cut the tail, the
        incomplete last line is dropped rather than shown as if the command had printed
        it, and `open` records the other case -- a process the command started still
        holding the pipe when arbite stopped waiting.
        """
        data = bytes(self.kept)
        lines = data.decode("utf-8", errors="replace").splitlines()
        cut_bytes = self.total > len(data)
        if cut_bytes and lines:
            lines = lines[:-1]
        shown = lines[:CAPTURE_LINES]
        return Captured(
            text="\n".join(shown),
            lines=len(lines),
            shown=len(shown),
            bytes=self.total,
            truncated=len(lines) > len(shown) or cut_bytes,
            open=not self.finished,
        )


def _reader(stream, capture: "_PipeCapture"):
    """A daemon thread draining one pipe (see `_PipeCapture.drain`)."""
    return threading.Thread(target=capture.drain, args=(stream,), daemon=True)


def _captured_lines(captured: Captured) -> list:
    """The command's own stdout, printed in place: the line the tool printed, verbatim."""
    return captured.text.splitlines() if captured.text else []


def _read_bytes(root, relative: str) -> Optional[bytes]:
    """The bytes at `relative`, or None when they cannot be read any more."""
    try:
        return (Path(root) / relative).read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def _manifest(root) -> dict:
    """`path -> ManifestEntry` for every managed path, in canonical order.

    "Managed" is the same answer the rest of the proxy gives: the files discovery offers
    as claimable rows, reached without following a link, minus the generated and build
    output a mutation refuses to record. arbite's own state is not in it: `.git`, the
    coordination tree, the store files, the scratch area and the project config are not
    paths a mutation may target, so a change there is not an observed write arbite can
    attribute.

    Taking it is proportional to the tree (one read per file, twice per command) and it
    holds digests and line identities rather than content, so nothing is copied.
    """
    found: dict = {}
    _walk(Path(root), "", found)
    return found


def _walk(directory: Path, relative_dir: str, found: dict) -> None:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for name in names:
        child = f"{relative_dir}/{name}" if relative_dir else name
        full = directory / name
        if full.is_symlink():
            continue
        if arbite_state(child) in (
            STATE_GIT,
            STATE_COORDINATION,
            STATE_STORE,
            STATE_SCRATCH,
            STATE_CONFIG,
        ):
            continue
        if policy_exclusion(child):
            continue
        if full.is_dir():
            _walk(full, child, found)
            continue
        if not full.is_file():
            continue
        entry = _entry(full)
        if entry is not None:
            found[child] = entry


def _entry(full: Path) -> Optional[ManifestEntry]:
    """One file's manifest entry, or None for a file arbite will not manage.

    A hard-linked target is refused by `probe` for the same reason a claim refuses it --
    a second name for the same bytes is how an alias would evade ownership -- and this
    walk matches that answer rather than inventing a second one.
    """
    try:
        if full.stat().st_nlink > 1:
            return None
        data = full.read_bytes()
    except OSError:
        return None
    return ManifestEntry(
        digest=digest_bytes(data),
        size=len(data),
        lines=_line_identities(data),
    )


def _line_identities(data: bytes) -> Optional[tuple]:
    """One identity per line, or None for bytes that are not UTF-8 text.

    The identity is the line's own string hash: equal lines are equal identities, which
    is all a line delta needs, and it costs a fraction of the bytes the text would.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return tuple(hash(line) for line in text.splitlines())


def observed_line_delta(before: ManifestEntry, after: ManifestEntry) -> Optional[tuple]:
    """`(added, removed)` between two manifest entries, or None when either is binary.

    Exposed for the tests that hold this up against `coordination.writes.line_delta`,
    because the two must agree: a report that told a different story from the engine's
    own line counts would make the same change read two ways.
    """
    if not (before.is_text and after.is_text):
        return None
    return _line_counts(before.lines, after.lines)


__all__ = [
    "CAPTURE_BYTES",
    "CAPTURE_LINES",
    "NO_RUN",
    "Change",
    "Captured",
    "ManifestEntry",
    "PassthroughRun",
    "PassthroughRuns",
    "Refusal",
    "Report",
    "observed_line_delta",
    "shell_syntax",
]
