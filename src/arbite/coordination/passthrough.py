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
Four rules shape it:

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
  125, 126 and 127. Guarded mode's escape report is the one place arbite answers with a
  code of its own for a command that really ran -- 1, for a finding the caller has to act
  on -- and even there `exit:` and `exit_code` carry the tool's own code.
- **Guarded mode is a different mode, not a stronger observation.** `--claim PATH...`
  acquires the declared paths all-or-nothing *before* the command starts, refuses (125,
  nothing claimed and nothing run) when one of them is busy, verifies every change against
  the set it holds, reports an escape as `unclaimed_write` and leaves those bytes exactly
  where the tool put them, and releases the claims when the run ends -- including when the
  tool failed.

What arbite cannot do is stated rather than implied, and it is the reason an *observed*
report is short on claims:

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

Guarded mode adds ownership rather than fidelity: it cannot make a `sed` visible to the
manifest any better than observation can, and it does not try. What it adds is the claim,
and with it the two things observation cannot say -- that the declared paths were held
while the command ran, and that a change outside the declaration escaped rather than
merely happened.
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from ..errors import ArbiteError
from .claims import CLAIM_COLUMN_GAP, FileClaims
from .lifecycle import local_time
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
    BUSY,
    EXIT_ERROR,
    EXIT_PASSTHROUGH_NOT_FOUND,
    EXIT_PASSTHROUGH_REFUSED,
    EXIT_PASSTHROUGH_UNSUPPORTED,
    OUTCOME_LABELS,
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
REASON_NO_STORE = "no_coordination_store"
REASON_ATTEMPT = "attempt_not_current"
REASON_NO_COMMAND = "no_command"
REASON_ATTEMPT_PAIR = "attempt_pair"
#: Guarded mode's refusals. A busy declared path uses the claim layer's own reason
#: (`file_busy`), so a consumer branching on it reads the same key `file claim` publishes.
REASON_CLAIM_BUSY = "file_busy"
REASON_CLAIM_REFUSED = "claim_refused"
REASON_CLAIM_ATTEMPT = "claim_needs_attempt"

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

#: A declaration with nobody to own it. Claiming a path is claiming it *for an attempt*,
#: so `--claim` without the pair is an invocation arbite cannot carry out rather than a
#: policy refusal.
CLAIM_ATTEMPT_MESSAGE = (
    "guarded mode claims paths for one attempt, so '--claim' needs both '--ticket' and "
    "'--attempt'"
)
CLAIM_ATTEMPT_HINT = (
    "next: name the ticket and its current attempt, or drop '--claim' to run the command "
    "in observed mode"
)

#: The busy refusal (PC3): what happened, then the request as a table. The held rows name
#: the holder, and the free ones say `free`, because the caller's next decision is which
#: of the declared paths it can work on instead.
CLAIM_BUSY_HEADING = (
    "{held} of {total} declared {noun} {verb} held; nothing was claimed and the command "
    "did not run"
)
CLAIM_BUSY_FREE = "free"
CLAIM_BUSY_HELD = "held by {holder} ({actor}) since {since}, gen {generation}"

#: What a finished guarded run says about its claims. The tool's own outcome is on the
#: `exit:` line; these lines are about ownership, which does not outlive the run.
RELEASE_REASON = "work complete for this command"
RELEASED = "claims released (work complete for this command)"
RELEASED_FAILED = "claims released (the command exited {code})"
RELEASED_ESCAPED = "claims released (the run is over; the unclaimed write is left in place)"
RELEASE_UNRECORDED = (
    "note: the claims could not be released ({error}); 'arbite file claims' shows who "
    "holds them now"
)

#: The escape report (PC4). The continuation is indented to the label's own width so the
#: two lines read as one sentence, and the message is deliberately about *this run's*
#: claim: an escape is a write arbite held no authorisation for, not a write nobody owns.
ESCAPE_LABEL = "unclaimed_write:"
ESCAPE_LINE = (
    ESCAPE_LABEL
    + " {path} was modified without being claimed; the bytes are recorded"
)
ESCAPE_TAIL = (
    " " * (len(ESCAPE_LABEL) + 1)
    + "and left as they are (arbite does not undo a command it did not perform)"
)
ESCAPE_HOLDER = (
    " " * (len(ESCAPE_LABEL) + 1)
    + "note: {holder} holds it at generation {generation}; this run had no claim of its own"
)
ESCAPE_HINT = (
    "'arbite file claim {paths} --ticket {ticket} --attempt {attempt}' and re-read {them},\n"
    "      or 'arbite changes {ticket}' and correct by hand"
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

#: How a run is labelled. `observed` is the honest word for a run that claimed nothing
#: (another writer can interleave), and `guarded` is the honest word for one that held the
#: paths it declared -- which is also why only that mode may say `exclusive`.
MODE = "observed"
MODE_GUARDED = "guarded"
NO_EXCLUSIVITY = "(no exclusivity claimed)"
MODE_OBSERVED = f"mode: {MODE}"
MODE_GUARDED_LINE = f"mode: {MODE_GUARDED}"
EXCLUSIVE = "(exclusive on {count} {noun})"
SHELL_NOTE = "note: redirections happen in the shell and are visible only after the fact"

ECHO_PREFIX = "arbite cmd: "
EXIT_LINE = "exit: {code} ({ms} ms)  {mode}{tail}"
CHANGED_HEADING = "changed {count} {noun}:"
#: Guarded mode's headings. The aggregate is what a reader needs first, so the heading
#: states it and the rows carry the per-path column only when the rows disagree (see
#: `Change.row`): a run where nothing escaped says `all inside the claimed set`.
CHANGED_GUARDED = "changed {count} {noun}, all inside the claimed set:"
CHANGED_ESCAPED = "changed {count} {noun}, {escaped} OUTSIDE the claimed set:"

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
#: A guarded run that stayed inside its claim has nothing left to claim: the review is the
#: only next step, and naming `--claim` again would be advice the caller has already taken.
NEXT_GUARDED_REVIEW = "'arbite changes {ticket}' to review"
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
#: The same row with the claimed-set column (PC4), between the detail and the operation id.
ROW_WITH_CLAIM = "  {status} {path}  {before} -> {after}  {detail}  {claim}  ({operation})"
CLAIM_INSIDE = "claimed"
CLAIM_OUTSIDE = "NOT claimed"
#: What a guarded run answers when its own verification found a change outside the set it
#: holds. It is arbite's own outcome, so it is arbite's own error code -- and it is the one
#: code this command produces that is not the wrapped tool's.
ESCAPE_EXIT = EXIT_ERROR

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

    `label` is the word the text puts in front of the message -- `error` for a policy
    refusal, `busy` for one the exit-code vocabulary calls busy (a claimed path is held) --
    and `rows` are the lines that continue it, which is how the busy table reaches a
    template whose first line is pinned. `data` adds the structured half JSON carries and
    the sentence cannot.
    """

    message: str
    reason: str
    exit_code: int
    text_hint: str = ""
    confirmation: str = ""
    actions: tuple = ()
    label: str = OUTCOME_LABELS["error"]
    mode: str = MODE
    rows: tuple = ()
    data: dict = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [f"{self.label}: {self.message}", *self.rows]
        if self.confirmation:
            lines.append(self.confirmation)
        if self.text_hint:
            lines.append(self.text_hint)
        return "\n".join(lines)

    def to_json(self) -> dict:
        payload = dict(self.data)
        payload.update(
            {
                "error": self.message,
                "reason": self.reason,
                "exit_code": self.exit_code,
                "ran": False,
                "mode": self.mode,
                "next_actions": list(self.actions),
            }
        )
        return payload


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


@dataclass(frozen=True)
class Claimed:
    """What a guarded run acquired, before its command starts (tic-42d2 / C14).

    The paths are the canonical ones the claim layer recorded, so "inside the claimed set"
    is a fact about ownership rather than about the spelling the caller typed. `lines` is
    the acquisition as `arbite file claim` prints it -- the heading, a row per path, and
    the note a re-acquisition carries -- because the run reports the claim it holds in the
    words the claim command already uses.
    """

    paths: tuple
    generation: int
    lines: tuple


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
    #: Whether this change falls inside the set the run claimed, set by guarded mode's
    #: verification; None in observed mode, where there is no declared set to compare with.
    part_of_claim: Optional[bool] = None

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

    def row(self, width: int = 0, show_claim: bool = False) -> str:
        """The row as the report prints it, with the claimed-set column when it is needed.

        `show_claim` is on only for a guarded run that had an escape: there the rows
        disagree with each other, and a reader has to see which side of the claim each one
        fell on (PC4). A guarded run whose changes are all inside the set says so in the
        heading, and printing `claimed` on every row of it would be a word repeated rather
        than a fact told (PC2). `width` pads the path column so a multi-row block lines its
        digests up; a single row is unaffected by it.
        """
        template = ROW_WITH_CLAIM if show_claim else ROW
        return template.format(
            status=self.status,
            path=self.path.ljust(width),
            before=short_digest(self.before_digest),
            after=short_digest(self.after_digest),
            detail=self.detail,
            claim=CLAIM_INSIDE if self.part_of_claim else CLAIM_OUTSIDE,
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
            # Guarded mode answers the question its own row asks -- was this inside the
            # declared set -- so text and JSON cannot disagree about the same run.
            "claimed": (
                self.part_of_claim
                if self.part_of_claim is not None
                else bool(self.claim and self.claim.get("mine"))
            ),
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
    #: The canonical paths this run holds, empty for an observed run. Non-empty is what
    #: makes the run guarded: it was acquired by `plan` before the command could start, and
    #: the release at the end goes back through the same claim layer.
    claim_paths: tuple = ()
    claim_generation: int = 0
    #: The acquisition as `arbite file claim` prints it, which the report repeats so the
    #: caller can see what was held before it sees what the command did with it.
    claims_lines: tuple = ()
    #: The claim layer the acquisition and the release go through. None for an observed run,
    #: which holds nothing and therefore has nothing to release.
    claims: Optional[object] = None

    @property
    def guarded(self) -> bool:
        """Whether this run holds declared paths. One word for one question, so no caller
        has to re-derive "guarded" from a list that happens to be empty."""
        return bool(self.claim_paths)

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def execute(self) -> Report:
        """Run the command, diff the manifest, verify it, record it and report it.

        The order is the whole of guarded mode's honesty: the changes are read out of the
        manifest while the claim is still held (so each receipt can say which generation it
        happened under), the run is recorded, and only then are the claims released -- after
        the evidence that they were held, because a release rewrites the claim records the
        receipts were read against.
        """
        before = _manifest(self.project_root)
        started = time.monotonic()
        process, stdout, stderr = self._run()
        duration_ms = int(round((time.monotonic() - started) * 1000))
        code = _exit_code(process.returncode)
        after = _manifest(self.project_root)
        changes = self._changes(before, after)
        escaped = ()
        if self.guarded:
            changes, escaped = self._verify(changes)
        recorded = self._record(changes, escaped, code, duration_ms, stdout, stderr)
        release = self._release() if self.guarded else None
        return self._report(
            changes, code, duration_ms, stdout, stderr, recorded, escaped, release
        )

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
    # Verification and release
    # ------------------------------------------------------------------

    def _verify(self, changes) -> tuple:
        """Every change against the claimed set: which are inside, and which escaped.

        The set is what the acquisition holds -- the declared paths, canonicalised by the
        claim layer -- so "inside" is a fact about ownership rather than about what happens
        to be on disk now. Each change is stamped with its answer, which is what the row's
        column and the JSON's `claimed` both read, and the escaped paths come back in
        canonical order for the report and the next actions to name.

        Nothing is undone here, and that is the point rather than an omission: arbite did
        not perform the write, cannot know what the tool meant by it, and rolling back
        bytes it never held would destroy work it was asked only to run.
        """
        claimed = set(self.claim_paths)
        verified = []
        escaped = []
        for change in changes:
            inside = change.path in claimed
            if not inside:
                escaped.append(change.path)
            verified.append(replace(change, part_of_claim=inside))
        return verified, tuple(escaped)

    def _release(self) -> tuple:
        """Release this run's claims, and answer honestly whether it worked.

        The command has already run, so a failure here cannot become a refusal: the claim
        keeps whatever the store says about it, the report names the problem instead of
        pretending the path is free, and `doctor --fix` is what releases a claim whose
        attempt is over. Release is what keeps a guarded run's ownership from outliving the
        run itself.
        """
        try:
            self.claims.release(
                self.ticket_id, self.attempt_id, list(self.claim_paths), RELEASE_REASON
            )
        except ArbiteError as failed:
            return False, str(failed)
        return True, None

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, changes, escaped, code: int, duration_ms: int, stdout, stderr) -> dict:
        """Persist the evidence: one receipt per changed path, then the run's event.

        Everything commits in one transaction, so a run cannot be half recorded. The
        only thing outside it is the after-version's *bytes* (an artifact, best effort):
        a version arbite cannot keep -- it is over the store's size limit, or the backend
        has no artifact store -- does not fail the recording, because the tool already
        ran and a receipt naming the digest is still true. `artifacts` says which
        versions have an image behind them.

        A guarded run's escape is recorded on the change's own event (the `claimed` flag)
        and on the run's (`unclaimed_write`, with the claimed set beside it) rather than as
        a receipt kind of its own: the evidence is the same passthrough receipt either way,
        and the question "did this change fall inside the claim" is a fact about the run,
        which is where the stream asks it.
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
            "mode": MODE_GUARDED if self.guarded else MODE,
            "exclusive": self.guarded,
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
        if self.guarded:
            payload["claim_paths"] = list(self.claim_paths)
            payload["claim_generation"] = self.claim_generation
            payload["unclaimed_write"] = list(escaped)
        with self.store.transaction() as txn:
            for change in changes:
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
                        # Only a guarded run had a set to be inside or outside of; an
                        # observed run's null would be a question it never asked.
                        **({"claimed": change.part_of_claim} if self.guarded else {}),
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

    def _report(
        self, changes, code, duration_ms, stdout, stderr, recorded, escaped, release
    ) -> Report:
        """The report: the claim it held, what ran, what changed, and what escaped.

        A guarded run opens with the acquisition -- so the caller sees what was held
        before it sees what the command did with it -- states an escape once instead of
        once per row, and closes by saying what happened to the claims. Observed mode's
        lines are unchanged: the two modes differ in what they can honestly say, not in
        the shape of the report.
        """
        lines = list(self.claims_lines) if self.guarded else []
        lines.append(ECHO_PREFIX + self.command_line())
        lines.extend(_captured_lines(stdout))
        lines.append(
            EXIT_LINE.format(
                code=code, ms=duration_ms, mode=self._mode_line(), tail=self._mode_tail()
            )
        )
        if changes:
            lines.append(self._change_heading(changes, escaped))
            lines.extend(self._change_rows(changes, escaped))
        lines.append(self._event_line())
        lines.extend(self._escape_lines(escaped, changes))
        if self.guarded:
            lines.append(self._release_line(code, escaped, release))
        for note in recorded["notes"]:
            lines.append(note)
        for name, stream in (("stdout", stdout), ("stderr", stderr)):
            if stream.open:
                lines.append(STILL_OPEN.format(stream=name))
            if stream.truncated:
                lines.append(stream.note(name))

        actions, text_hint = self._next(changes, code, escaped)
        # An escape is arbite's own finding, so it is arbite's own code -- the one outcome
        # this command answers for itself. The tool's code is still what `exit:` and the
        # JSON's `exit_code` carry, so nothing about the wrapped command is rewritten.
        exit_code = ESCAPE_EXIT if escaped else code
        return Report(
            exit_code=exit_code,
            lines=lines,
            data=self._facts(
                changes, code, exit_code, duration_ms, stdout, stderr, recorded, escaped, release
            ),
            next_actions=actions,
            text_hint=text_hint,
            stderr_text=stderr.text,
        )

    def _mode_line(self) -> str:
        """The mode the `exit:` line names: what this run could claim, in one phrase."""
        if not self.guarded:
            return MODE_OBSERVED
        count = len(self.claim_paths)
        noun = "path" if count == 1 else "paths"
        return f"{MODE_GUARDED_LINE} {EXCLUSIVE.format(count=count, noun=noun)}"

    def _mode_tail(self) -> str:
        """The note the exit line adds.

        Observed mode has to say what it did *not* claim, which is the only honest thing
        it can say about ownership; guarded mode has nothing to disclaim. A shell run says
        where its redirections went in either mode, because that stays true either way.
        """
        if self.shell:
            return f"  {SHELL_NOTE}"
        return "" if self.guarded else f" {NO_EXCLUSIVITY}"

    def _change_heading(self, changes, escaped) -> str:
        """What changed, and whether the claimed set covered it."""
        count = len(changes)
        noun = "path" if count == 1 else "paths"
        if not self.guarded:
            return CHANGED_HEADING.format(count=count, noun=noun)
        if not escaped:
            return CHANGED_GUARDED.format(count=count, noun=noun)
        return CHANGED_ESCAPED.format(count=count, noun=noun, escaped=len(escaped))

    @staticmethod
    def _change_rows(changes, escaped) -> list:
        """The changed rows, with the path column padded so a multi-row block lines up.

        The claimed column is printed only when the rows disagree with each other (see
        `Change.row`), which is the difference between PC2's block and PC4's.
        """
        width = max(len(change.path) for change in changes)
        return [change.row(width, show_claim=bool(escaped)) for change in changes]

    @staticmethod
    def _escape_lines(escaped, changes) -> list:
        """The `unclaimed_write` block: one entry per escaped path, in canonical order.

        Each says what escaped and that its bytes were kept, and -- when somebody *else*
        holds the path -- who, because "without being claimed" must not read as "nobody
        has a claim on it" when a claim record says otherwise.
        """
        if not escaped:
            return []
        by_path = {change.path: change for change in changes}
        lines = []
        for path in escaped:
            lines.append(ESCAPE_LINE.format(path=path))
            lines.append(ESCAPE_TAIL)
            claim = by_path[path].claim
            if claim and not claim["mine"]:
                lines.append(
                    ESCAPE_HOLDER.format(holder=claim["holder"], generation=claim["generation"])
                )
        return lines

    @staticmethod
    def _release_line(code, escaped, release) -> str:
        """What happened to the claims, said rather than assumed.

        The release runs whether the tool succeeded, failed or escaped, so the line says
        which of the three it was. A release that could not be recorded is reported with
        the command that follows it named, never as a silent success.
        """
        released, error = release
        if not released:
            return RELEASE_UNRECORDED.format(error=error)
        if escaped:
            return RELEASED_ESCAPED
        return RELEASED if code == 0 else RELEASED_FAILED.format(code=code)

    def _event_line(self) -> str:
        line = EVENT_PREFIX + EVENT_TOOL.format(tool=self.tool)
        if self.ticket_id and self.attempt_id:
            line += EVENT_WHERE.format(ticket=self.ticket_id, attempt=self.attempt_id)
        if self.actor:
            line += EVENT_ACTOR.format(actor=self.actor)
        return line

    def _next(self, changes, code, escaped) -> tuple:
        """The next actions, and the sentence they print as.

        The hint is computed from the state at the end of the run and names only tokens
        this command printed: the ticket, the paths that changed, and the receipts. A
        run that changed nothing has no next step (the frozen PC5 block prints none),
        which is the honest answer -- there is nothing to review.

        An escape comes first because it is the only outcome here that leaves a decision
        for the caller: the path has to be claimed properly (and re-read, since a
        pre-claim read authorises nothing) or corrected by hand, and the review is offered
        beside it because arbite will not decide which. A guarded run that stayed inside
        its claim has already taken the only step observed mode's hint suggests, so its
        hint names the review alone.
        """
        if not changes:
            return (), ""
        if escaped:
            paths = " ".join(escaped)
            claim = (
                f"arbite file claim {paths} --ticket {self.ticket_id} "
                f"--attempt {self.attempt_id}"
            )
            review = f"arbite changes {self.ticket_id}"
            text = ESCAPE_HINT.format(
                paths=paths,
                ticket=self.ticket_id,
                attempt=self.attempt_id,
                them="it" if len(escaped) == 1 else "them",
            )
            return (claim, review), f"next: {text}"
        if code != 0:
            first = changes[0].operation
            if self.ticket_id:
                action = f"arbite changes {self.ticket_id}"
                text = f"next: {NEXT_FAILED.format(ticket=self.ticket_id)}"
            else:
                action = f"arbite receipt {first}"
                text = f"next: {NEXT_FAILED_NO_TICKET.format(operation=first)}"
            return (action,), text
        if self.guarded:
            review = f"arbite changes {self.ticket_id}"
            return (review,), f"next: {NEXT_GUARDED_REVIEW.format(ticket=self.ticket_id)}"
        claim = "--claim " + " ".join(change.path for change in changes)
        if not self.ticket_id:
            return (), f"next: {NEXT_CLAIM_ONLY.format(claim=claim)}"
        review = f"arbite changes {self.ticket_id}"
        return (review,), f"next: {NEXT_REVIEW.format(ticket=self.ticket_id, claim=claim)}"

    def _facts(
        self, changes, code, exit_code, duration_ms, stdout, stderr, recorded, escaped, release
    ) -> dict:
        """The JSON form: the same facts as the text, plus the branchable ones.

        `exclusive` is what this run actually held -- False for an observation, True for a
        guarded run that acquired its declared paths -- while `exclusivity` describes the
        seam itself: `available` is True now, so an observed report's hint may be read as
        something arbite really does.

        A guarded report adds `claims` (what it held, and whether the release worked),
        `unclaimed_write` (the paths that escaped), `escaped` and `arbite_exit_code`. That
        last one is the honest split `exit_code` cannot carry alone: `exit_code` is always
        the wrapped tool's own, and `arbite_exit_code` is what this process returns, which
        is 1 when the verification found an escape.
        """
        facts = {
            "command": list(self.argv),
            "tool": self.tool,
            "cmdline": self.command_line(),
            "shell": self.shell,
            "mode": MODE_GUARDED if self.guarded else MODE,
            "exclusive": self.guarded,
            "exclusivity": self._exclusivity(changes),
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
        if self.guarded:
            released, error = release
            facts["claims"] = {
                "paths": list(self.claim_paths),
                "generation": self.claim_generation,
                "released": released,
                "release_reason": RELEASE_REASON if released else None,
            }
            if error:
                facts["claims"]["release_error"] = error
            facts["unclaimed_write"] = list(escaped)
            facts["escaped"] = bool(escaped)
            facts["arbite_exit_code"] = exit_code
        return facts

    def _exclusivity(self, changes) -> dict:
        """The `--claim` seam as a fact rather than as a promise.

        The same four keys in both modes: what *this* run held (`claimed`), whether the flag
        exists at all (`available`), the suggestion an observed run can still be given, and
        an exception to `available` if one ever applies (None today). An observed run's hint
        is a command that now works, which is exactly why `available` had to stop saying
        otherwise the moment guarded mode landed.
        """
        hint = None
        if not self.guarded and changes:
            hint = (
                "arbite cmd --claim "
                + " ".join(change.path for change in changes)
                + " -- <command>"
            )
        return {
            "claimed": list(self.claim_paths),
            "available": True,
            "hint": hint,
            "reason": None,
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
        #: The claim layer. Guarded mode goes through exactly the operation `arbite file
        #: claim` presents, which is what makes "passthrough cannot bypass ownership" a
        #: property of the shared rules rather than of a second implementation of them.
        self.claims = FileClaims(sink, lifecycle)

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

        The order is the order a caller can act on it: what was asked for at all, the
        pair that attributes a run, the declaration that needs one, the invocation's
        shape, the tool, the state that has to hold for the run to be recorded and
        attributed, and -- last, and only for a guarded run -- the acquisition itself.
        Every one of them returns a value, so there is no path from this function to a
        started process that skipped a check.

        Acquisition is last on purpose: it is the only step here that *changes* state, so
        every refusal that could have been made without it has already been made, and a
        refused invocation never leaves a claim behind it.
        """
        argv = tuple(arg for arg in argv if arg is not None)
        if not argv:
            return _unsupported(NO_COMMAND_MESSAGE, REASON_NO_COMMAND, NO_COMMAND_HINT)
        if bool(ticket_id) != bool(attempt_id):
            return _unsupported(ATTEMPT_PAIR_MESSAGE, REASON_ATTEMPT_PAIR, ATTEMPT_PAIR_HINT)
        if claim_paths and not (ticket_id and attempt_id):
            return _unsupported(
                CLAIM_ATTEMPT_MESSAGE, REASON_CLAIM_ATTEMPT, CLAIM_ATTEMPT_HINT
            )

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

        claimed = None
        if claim_paths:
            outcome = self._acquire(claim_paths, ticket_id, attempt_id, actor)
            if isinstance(outcome, Refusal):
                return outcome
            claimed = outcome

        return PassthroughRun(
            store=self.store,
            project_root=self.project_root,
            argv=argv,
            tool=tool,
            shell=shell,
            ticket_id=ticket_id,
            attempt_id=attempt_id,
            actor=actor,
            claim_paths=claimed.paths if claimed else (),
            claim_generation=claimed.generation if claimed else 0,
            claims_lines=claimed.lines if claimed else (),
            claims=self.claims if claimed else None,
        )

    # ------------------------------------------------------------------
    # Guarded mode: the acquisition a run starts with
    # ------------------------------------------------------------------

    def _acquire(self, raw_paths, ticket_id, attempt_id, actor):
        """The all-or-nothing acquisition a guarded run begins with (PC2).

        It is the claim layer's own operation, so a guarded run cannot hold anything a
        plain `arbite file claim` would refuse, and the two acquire in the same canonical
        order with the same generation numbering. A path another attempt holds comes back
        as the claim layer's `file_busy` outcome with the *whole* request listed, and that
        becomes a refusal: nothing is claimed, and the command has not started.
        """
        try:
            result = self.claims.claim(ticket_id, attempt_id, raw_paths)
        except ArbiteError as refused:
            return Refusal(
                message=str(refused),
                reason=REASON_CLAIM_REFUSED,
                exit_code=REFUSED,
                text_hint=text_hint_of(refused),
                confirmation=NO_RUN,
                actions=tuple(getattr(refused, "next_actions", ()) or ()),
                mode=MODE_GUARDED,
            )
        if result.kind == BUSY:
            return self._busy_refusal(result, ticket_id, attempt_id, actor)
        lines = list(result.lines)
        if result.data.get("note"):
            # A re-acquisition's note ("this is a new claim generation (N); any token
            # from generation M is dead") is part of what the caller needs to see before
            # the command runs, exactly as it is for a plain claim (FC8).
            lines.append(result.data["note"])
        return Claimed(
            paths=tuple(result.data["canonical_order"]),
            generation=int(result.data["generation"]),
            lines=tuple(lines),
        )

    def _busy_refusal(self, result, ticket_id, attempt_id, actor) -> Refusal:
        """PC3: a declared path is held, so nothing is claimed and nothing runs.

        The rows are the shape `file claim` prints for its own refusal -- the held paths
        with their holder, the free ones as `free` -- because the caller's next decision is
        the same one, and the *free* ones matter here more than they do there: they are the
        paths this run would have had. The code is 125 rather than 4, because 0-5 belong to
        the wrapped tool in this command; the refusal is still arbite's own, it says the
        command did not run, and JSON carries `ran: false` plus the same structured table.
        """
        held = {entry["path"]: entry for entry in result.data["held"]}
        free = list(result.data["free"])
        paths = sorted([*held, *free])
        width = max(len(path) for path in paths) + CLAIM_COLUMN_GAP
        rows = []
        for path in paths:
            entry = held.get(path)
            if entry is None:
                rows.append(f"  {path.ljust(width)}{CLAIM_BUSY_FREE}")
                continue
            rows.append(
                f"  {path.ljust(width)}"
                + CLAIM_BUSY_HELD.format(
                    holder=f"{entry['ticket']} / {entry['attempt']}",
                    actor=self._holder_actor(entry["attempt"]),
                    since=local_time(entry["since"]),
                    generation=entry["generation"],
                )
            )
        total = len(paths)
        noun = "path" if total == 1 else "paths"
        verb = "is" if len(held) == 1 else "are"
        holder = result.data["held"][0]
        actions = (
            f"arbite list next --claim {actor}",
            f"arbite changes {holder['ticket']}",
        )
        spoken = (
            f"work a different ticket ('{actions[0]}')",
            f"'{actions[1]}' to see whether the holder has finished",
        )
        return Refusal(
            message=CLAIM_BUSY_HEADING.format(held=len(held), total=total, noun=noun, verb=verb),
            reason=REASON_CLAIM_BUSY,
            exit_code=REFUSED,
            rows=tuple(rows),
            confirmation=NO_RUN,
            # The joining word goes at the end of the previous hint, the way the frozen
            # claim refusals print it (FC3), not at the start of the next line.
            text_hint="next: " + ", or\n      ".join(spoken),
            actions=actions,
            label=OUTCOME_LABELS[BUSY],
            mode=MODE_GUARDED,
            data={
                "held": result.data["held"],
                "free": free,
                "claimed": [],
                "ticket": ticket_id,
                "attempt": attempt_id,
            },
        )

    def _holder_actor(self, attempt_id: str) -> str:
        """The worker the holding attempt belongs to, as the busy rows name it.

        Read from the attempt rather than carried on the claim, for the same reason the
        claim report does it: a worker id is attribution, and a second copy is a second
        thing that can disagree."""
        attempt = self.store.get_attempt(attempt_id)
        return attempt.worker_id if attempt is not None else "(unknown worker)"


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
    "Claimed",
    "ManifestEntry",
    "PassthroughRun",
    "PassthroughRuns",
    "Refusal",
    "Report",
    "observed_line_delta",
    "shell_syntax",
]
