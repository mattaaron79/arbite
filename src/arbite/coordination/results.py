"""The outcome vocabulary: exit codes, the `next:` line, and the JSON result shape.

Every command in the coordination surface answers in one shape, whether it is
reporting a success or refusing: an `OperationResult` carrying text lines, the same
facts as JSON, and the next actions that follow from the outcome. Two rules from
the handoff are built in rather than left to each caller:

- **The exit code and the `next:` line come from one key.** An outcome is a
  `(kind, reason)` pair; `EXIT_CODES` maps the kind to 0-5 and `next_actions_for`
  maps the reason (falling back to the kind) to the commands that follow. That is
  what stops the CLI, the tests and the generated guide from drifting apart about
  what an outcome means.
- **Text is primary and JSON carries the same facts.** `to_json()` mirrors
  `next_actions`, because a machine consumer branches on the exit code and a
  language agent reads the lines; a fact that exists only in one of them is a bug.

Busy (4) and stale (5) are first-class outcomes rather than errors, because they
need a different caller response from a genuine failure: pick other work, or
re-read and retry. The hints rendered for them obey the handoff's guardrails -- a
hint is computed from the state at failure time, may only name tokens the failing
command itself printed, and never suggests waiting, retrying a busy path, or
working around arbite with the shell.

The hint table is keyed by *reason* because the same kind has different correct
answers (`not_ready` and `lost_race` are both errors). Slices that own an outcome
register its hints with `register_next_actions`, so the wording lives with the
behaviour that produces it instead of being guessed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..errors import ArbiteError, Busy, RecordError, Stale

# Exit codes. 0-3 predate this module and keep their meaning exactly; 4 and 5 join
# them so a caller can distinguish "held by someone" and "your token is stale" --
# both of which changed nothing -- from a real error.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_EMPTY = 2
EXIT_PROBLEMS = 3
EXIT_BUSY = 4
EXIT_STALE = 5

# Passthrough is the one deliberate exception to the table above: a wrapped tool's
# own exit code must survive untouched, and 0-5 are all reachable that way. So
# `arbite cmd` refuses with its own codes, and says so in words a caller can read.
EXIT_PASSTHROUGH_REFUSED = 125
EXIT_PASSTHROUGH_UNSUPPORTED = 126
EXIT_PASSTHROUGH_NOT_FOUND = 127

OK = "ok"
ERROR = "error"
EMPTY = "empty"
PROBLEMS = "problems"
BUSY = "busy"
STALE = "stale"

#: The outcome kinds, in exit-code order. The kind is what chooses the exit code.
OUTCOME_KINDS = (OK, ERROR, EMPTY, PROBLEMS, BUSY, STALE)

EXIT_CODES = {
    OK: EXIT_OK,
    ERROR: EXIT_ERROR,
    EMPTY: EXIT_EMPTY,
    PROBLEMS: EXIT_PROBLEMS,
    BUSY: EXIT_BUSY,
    STALE: EXIT_STALE,
}

#: What the correct caller response is, in words -- the same column the handoff's
#: exit-code table states, kept beside the codes so the two cannot drift.
OUTCOME_RESPONSES = {
    OK: "continue",
    ERROR: "fix the command",
    EMPTY: "nothing to do",
    PROBLEMS: "repair",
    BUSY: "pick other work; do not retry blindly",
    STALE: "re-read, then retry",
}

#: The label a non-success outcome prints in front of its message. `error` is the
#: default (the CLI has printed `error: <message>` since before this vocabulary
#: existed, and that text is part of the contract); busy and stale print their own
#: word so a caller reading stderr sees the same branch key the exit code carries.
OUTCOME_LABELS = {
    OK: "",
    ERROR: "error",
    EMPTY: "empty",
    PROBLEMS: "problems",
    BUSY: "busy",
    STALE: "stale_read",
}

#: The continuation used when an outcome has more than one next action. The
#: examples document renders two hints as one `next:` line continued under it.
NEXT_CONTINUATION = ",\n      or "


class _Hints:
    """Reason-keyed next actions, registered by the slice that owns the outcome."""

    def __init__(self):
        self._by_reason: dict = {}
        self._by_kind: dict = {}

    def register(self, key: str, actions) -> None:
        """Record the next actions for an outcome key.

        `key` is a reason (`stale_read`, `file_busy`, ...) or a bare kind. A reason
        wins over its kind, so a specific answer (re-read *this* path) is never
        buried under a generic one."""
        actions = list(actions)
        for action in actions:
            if not isinstance(action, str) or not action.strip():
                raise RecordError(f"a next action must be a non-empty string, got {action!r}")
        if key in OUTCOME_KINDS:
            self._by_kind[key] = actions
        else:
            self._by_reason[key] = actions

    def for_outcome(self, kind: str, reason: Optional[str] = None) -> list:
        if reason and reason in self._by_reason:
            return list(self._by_reason[reason])
        return list(self._by_kind.get(kind, ()))

    def known_reasons(self) -> list:
        return sorted(self._by_reason)


_HINTS = _Hints()


def register_next_actions(key: str, actions) -> None:
    """Publish the next actions for an outcome key (see `_Hints.register`)."""
    _HINTS.register(key, actions)


def next_actions_for(kind: str, reason: Optional[str] = None) -> list:
    """The next actions for an outcome, or [] when the outcome has none."""
    if kind not in OUTCOME_KINDS:
        raise RecordError(f"unknown outcome kind '{kind}' (known: {', '.join(OUTCOME_KINDS)})")
    return _HINTS.for_outcome(kind, reason)


def render_next_line(actions) -> str:
    """The `next:` line for `actions`, or '' when there are none.

    One hint per line unless there are several, in which case they continue under
    the first -- the shape the frozen transcripts use."""
    if not actions:
        return ""
    return "next: " + NEXT_CONTINUATION.join(actions)


@dataclass(frozen=True)
class Outcome:
    """A `(kind, reason)` pair: the kind chooses the exit code, the reason refines
    the correct next action. `reason` is optional and falls back to the kind."""

    kind: str
    reason: Optional[str] = None

    def __post_init__(self):
        if self.kind not in OUTCOME_KINDS:
            raise RecordError(
                f"unknown outcome kind '{self.kind}' (known: {', '.join(OUTCOME_KINDS)})"
            )

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.kind]

    @property
    def label(self) -> str:
        return OUTCOME_LABELS[self.kind]

    @property
    def ok(self) -> bool:
        return self.kind == OK

    @property
    def response(self) -> str:
        return OUTCOME_RESPONSES[self.kind]


@dataclass
class OperationResult:
    """What a command reports: the outcome, the lines it printed, the same facts as
    JSON, and the actions that follow.

    `lines` is the text a language agent reads; `data` is the branchable form and
    is expected to carry the same facts (JSON may carry more -- never less).
    `next_actions` are exact commands, and their rendering is `render_next_line`.

    `text_hint` is the one case where the printed sentence is not that rendering: a
    *successful* claim hands back the command to run next and a sentence explaining
    why (the frozen CL1 transcript prints both), while JSON publishes the bare
    command. Set, it is the complete `next:` line -- including the word `next:` -- and
    it must name the very commands `next_actions` carries, so the two cannot describe
    different next steps."""

    outcome: Outcome
    lines: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    next_actions: list = field(default_factory=list)
    text_hint: Optional[str] = None

    @property
    def kind(self) -> str:
        return self.outcome.kind

    @property
    def exit_code(self) -> int:
        return self.outcome.exit_code

    def to_text(self) -> str:
        """The whole text output, ending with the `next:` line when there is one."""
        parts = list(self.lines)
        hint = self.text_hint if self.text_hint is not None else render_next_line(self.next_actions)
        if hint:
            parts.append(hint)
        return "\n".join(parts)

    def to_json(self) -> dict:
        """The `--json` payload: the facts plus the mirrored next actions.

        An outcome's own `next_actions` win. When it has none, whatever the
        operation already published under that key survives: the events view prints
        its continuation inline (the `cursor:` line) and publishes the same command
        for a poll, and a mirror that overwrote it with `[]` would drop a fact the
        text carries."""
        payload = dict(self.data)
        if self.next_actions or "next_actions" not in payload:
            payload["next_actions"] = list(self.next_actions)
        return payload

    def to_stderr_text(self) -> str:
        """How a non-success outcome reads on stderr: the label, then the message.

        Kept beside `to_text()` so the label, the message and the exit code come
        from one outcome instead of being re-derived at the call site."""
        first = self.lines[0] if self.lines else ""
        label = self.outcome.label or "error"
        return f"{label}: {first}"


def succeeded(lines=None, data=None, next_actions=None, text_hint=None) -> OperationResult:
    """A plain success.

    `next_actions` is not decoration: a *success* can have a next step that is only
    true because it succeeded -- claiming a ticket hands back the attempt id a file
    command needs -- and the frozen CL1 transcript prints that `next:` line after a
    successful claim, so the success path has to be able to carry one. `text_hint` is
    how that line reads in text when it is more than the command (see
    `OperationResult`)."""
    return OperationResult(
        Outcome(OK),
        list(lines or ()),
        dict(data or {}),
        list(next_actions or ()),
        text_hint,
    )


def failed(message: str, reason: Optional[str] = None, data=None, lines=None) -> OperationResult:
    """An error outcome: bad input, not found, a policy refusal."""
    text_lines = list(lines) if lines is not None else [message]
    return OperationResult(
        Outcome(ERROR, reason), text_lines, dict(data or {}), next_actions_for(ERROR, reason)
    )


def refused_busy(message: str, reason: str = "file_busy", data=None) -> OperationResult:
    """Outcome 4: something live holds what was asked for, and nothing changed."""
    return OperationResult(
        Outcome(BUSY, reason), [message], dict(data or {}), next_actions_for(BUSY, reason)
    )


def refused_stale(message: str, reason: str = "stale_read", data=None) -> OperationResult:
    """Outcome 5: a token, digest or generation is no longer current, nothing changed."""
    return OperationResult(
        Outcome(STALE, reason), [message], dict(data or {}), next_actions_for(STALE, reason)
    )


def outcome_of(exc: ArbiteError) -> Outcome:
    """The outcome an exception carries, so the CLI has one place that turns a
    failure into a branch key, a label and an exit code."""
    if isinstance(exc, Busy):
        return Outcome(BUSY, getattr(exc, "reason", None) or "busy")
    if isinstance(exc, Stale):
        return Outcome(STALE, getattr(exc, "reason", None) or "stale_read")
    return Outcome(ERROR, getattr(exc, "reason", None))


def next_actions_of(exc: ArbiteError) -> list:
    """The next actions an exception carries, or its kind's own hints."""
    actions = list(getattr(exc, "next_actions", ()) or ())
    if actions:
        return actions
    outcome = outcome_of(exc)
    return next_actions_for(outcome.kind, outcome.reason)


# Hints that belong to the vocabulary itself rather than to one command. Each is a
# fallback that is true whatever caused the outcome, and none of them suggests
# waiting, retrying a busy path, or reaching around arbite with a shell. A slice
# that owns an outcome registers the concrete hint for its reason
# (`register_next_actions("file_busy", [...])`), which then wins over these.
register_next_actions(
    BUSY,
    ["'arbite list next --claim <agent-id>' to take other workable work instead"],
)
register_next_actions(
    STALE,
    ["'arbite show <id>' to re-read the current state, then retry the change"],
)
register_next_actions(
    EMPTY,
    ["relax the filters and re-run, or 'arbite list next' for workable tickets"],
)
register_next_actions(
    PROBLEMS,
    ["re-run 'arbite doctor --fix' to repair what arbite can correct automatically"],
)
