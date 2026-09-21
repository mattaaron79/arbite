"""Exact-substitution edit batches: what `arbite file edit` applies, and its refusals.

An edit batch is an ordered list of exact text replacements, parsed from the payload
`--edits` names:

```json
{"edits": [
  {"old": "The one write path", "new": "The single write path"},
  {"old": "Raises Conflict", "new": "Raises Conflict or StaleRead", "occurrence": 2}
]}
```

Four rules, all of which exist to make an edit checkable rather than clever:

- **A replacement is matched exactly**, against the text the read token served. No
  fuzzy patching, no normalised whitespace, no regular expressions: a caller that
  cannot say what it is replacing cannot be told what went wrong.
- **Every edit must select exactly one place.** `old` occurring once is a selection;
  occurring twice is not, and the refusal lists the lines so the next read and the
  next edit are both mechanical. `occurrence` (1-based) or `line` names one.
- **Selections may not overlap.** Overlapping replacements have no defined result
  (which one wins?) and are refused rather than ordered by luck.
- **The batch is matched against the version the caller read and written once.** Line
  numbers therefore mean what they meant in the read, and a batch that fails anywhere
  leaves the file byte-for-byte as it was: the text is assembled in memory and handed
  to the recoverable write protocol as one replacement, so "no partial write" is a
  property of the shape rather than a cleanup path.

Untouched bytes are untouched: the replacement is a substring splice on the decoded
text, so newline conventions, trailing whitespace and bytes outside every selection
survive exactly. A file that is not UTF-8 text is not editable this way (`file write`
handles bytes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ..errors import EditRefused
from .results import REFUSAL_INDENT

#: The keys one edit may carry.
OLD_KEY = "old"
NEW_KEY = "new"
OCCURRENCE_KEY = "occurrence"
LINE_KEY = "line"

#: How many lines of context the refusal's `--lines` window asks for. The frozen ED2
#: block asks for `80:90` around an occurrence on line 85, which is this window.
CONTEXT_LINES = 5

#: The refusal's second line: every failure of a batch is "nothing was written", and
#: saying so is what stops a caller re-applying a change that may already be there.
UNCHANGED = "no bytes were changed"


@dataclass(frozen=True)
class Substitution:
    """One requested replacement, and how it says which occurrence it means."""

    old: str
    new: str
    occurrence: Optional[int] = None
    line: Optional[int] = None

    @property
    def old_first_line(self) -> str:
        return _first_line(self.old)

    @property
    def new_first_line(self) -> str:
        return _first_line(self.new)


@dataclass(frozen=True)
class AppliedEdit:
    """One replacement that was applied, and where it was found."""

    index: int
    total: int
    line: int
    old: str
    new: str

    def row(self) -> str:
        """The report row: `  1/2 replace at line 222: "old" -> "new"` (the ED1 shape)."""
        return (
            f"  {self.index}/{self.total} replace at line {self.line}: "
            f'"{self.old}" -> "{self.new}"'
        )


@dataclass(frozen=True)
class EditBatch:
    """A batch's substitutions, and the text they produce."""

    substitutions: tuple
    applied: tuple
    text: str


def parse_batch(payload: bytes, flag: str = "--edits") -> tuple:
    """The substitutions in a payload, or a refusal naming what is wrong with it.

    A bare JSON list is accepted as well as `{"edits": [...]}`, because both are what
    a caller naturally emits, and the shape is checked field by field so a malformed
    batch is refused before a ticket, a claim or a token is consulted."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError:
        raise EditRefused(
            f"'{flag}' payload is not UTF-8 text, so it cannot be an edit batch;\n"
            f"{REFUSAL_INDENT}an edit batch is JSON (`file write` sends bytes)"
        )
    except ValueError as e:
        raise EditRefused(
            f"'{flag}' payload is not JSON ({e});\n"
            f'{REFUSAL_INDENT}send \'{{"edits": [{{"old": ..., "new": ...}}]}}\''
        )
    if isinstance(document, dict):
        entries = document.get("edits")
    else:
        entries = document
    if not isinstance(entries, list):
        raise EditRefused(
            f"'{flag}' expects a list of edits, or an object with an 'edits' list"
        )
    if not entries:
        raise EditRefused(
            f"'{flag}' holds no edits, so there is nothing to do;\n"
            f"{REFUSAL_INDENT}no bytes were changed"
        )
    substitutions = []
    for index, entry in enumerate(entries, start=1):
        substitutions.append(_substitution(entry, index, len(entries), flag))
    return tuple(substitutions)


def _substitution(entry, index: int, total: int, flag: str) -> Substitution:
    if not isinstance(entry, dict):
        raise EditRefused(
            f"edit {index}/{total} must be an object with '{OLD_KEY}' and '{NEW_KEY}'"
        )
    unknown = sorted(set(entry) - {OLD_KEY, NEW_KEY, OCCURRENCE_KEY, LINE_KEY})
    if unknown:
        raise EditRefused(
            f"edit {index}/{total} has unknown key(s): {', '.join(unknown)} "
            f"(known: {OLD_KEY}, {NEW_KEY}, {OCCURRENCE_KEY}, {LINE_KEY})"
        )
    old, new = entry.get(OLD_KEY), entry.get(NEW_KEY)
    if not isinstance(old, str) or not isinstance(new, str):
        raise EditRefused(
            f"edit {index}/{total} must be an object with string '{OLD_KEY}' and "
            f"'{NEW_KEY}'"
        )
    if not old:
        raise EditRefused(
            f"edit {index}/{total} has an empty '{OLD_KEY}': an edit names the text it "
            "replaces"
        )
    occurrence, line = entry.get(OCCURRENCE_KEY), entry.get(LINE_KEY)
    for label, value in ((OCCURRENCE_KEY, occurrence), (LINE_KEY, line)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise EditRefused(
                f"edit {index}/{total} '{label}' must be a whole number of 1 or more, "
                f"got {value!r}"
            )
    if occurrence is not None and line is not None:
        raise EditRefused(
            f"edit {index}/{total} names both '{OCCURRENCE_KEY}' and '{LINE_KEY}'; "
            "one occurrence is what an edit selects"
        )
    return Substitution(old=old, new=new, occurrence=occurrence, line=line)


def apply_batch(text: str, substitutions, path: str, read_command: str) -> EditBatch:
    """Apply `substitutions` to `text`, or refuse with nothing changed.

    `read_command` is the exact `arbite file read` command the refusals name, built by
    the caller because it knows the ticket and the attempt; every hint here is a
    command the failing command's own state produces, never a guess.
    """
    total = len(substitutions)
    selections = []
    for index, substitution in enumerate(substitutions, start=1):
        start, end = _select(text, substitution, index, total, path, read_command)
        for other_start, other_end, other_index in selections:
            if start < other_end and other_start < end:
                raise EditRefused(
                    f"edit {index}/{total} does not apply: it overlaps edit "
                    f'{other_index} ("{substitutions[other_index - 1].old_first_line}") '
                    f"in {path};\n{REFUSAL_INDENT}no bytes were changed",
                    text_hint="next: send a batch whose selections do not overlap, then retry",
                )
        selections.append((start, end, index))

    applied = [
        AppliedEdit(
            index=index,
            total=total,
            line=_line_of(text, start),
            old=substitutions[index - 1].old_first_line,
            new=substitutions[index - 1].new_first_line,
        )
        for start, _, index in sorted(selections)
    ]
    # Splice the selections from the end, so an earlier replacement never moves the
    # offsets of a later one. Every byte outside a selection is copied untouched.
    result = text
    for start, end, index in sorted(selections, reverse=True):
        result = result[:start] + substitutions[index - 1].new + result[end:]
    applied.sort(key=lambda edit: edit.index)
    return EditBatch(substitutions=tuple(substitutions), applied=tuple(applied), text=result)


def _select(text: str, substitution: Substitution, index: int, total: int, path: str, read_command: str):
    """The one span `substitution` selects, or a refusal that says how to fix it."""
    starts = _occurrences(text, substitution.old)
    what = f'edit {index}/{total} does not apply: "{substitution.old_first_line}"'
    if not starts:
        raise EditRefused(
            f"{what} does not occur in {path};\n{REFUSAL_INDENT}no bytes were changed",
            [read_command],
            text_hint=(
                f"next: read the file ('{read_command}'), then re-send the edit with "
                "text it contains"
            ),
        )
    lines = [_line_of(text, start) for start in starts]
    if substitution.line is not None:
        candidates = [start for start, line in zip(starts, lines) if line == substitution.line]
        if not candidates:
            raise EditRefused(
                f"{what} occurs on line(s) {_lines_text(lines)}, not line "
                f"{substitution.line};\n{REFUSAL_INDENT}no bytes were changed",
                [read_command],
                text_hint=(
                    f"next: read those lines ('{read_command} --lines "
                    f"{_window(lines)}'), then re-send the edit at the line it is on"
                ),
            )
        return candidates[0], candidates[0] + len(substitution.old)
    if substitution.occurrence is not None:
        if substitution.occurrence > len(starts):
            raise EditRefused(
                f"{what} occurs {len(starts)} time(s) (line(s) {_lines_text(lines)}), so "
                f"there is no occurrence {substitution.occurrence};\n"
                f"{REFUSAL_INDENT}no bytes were changed",
                [read_command],
                text_hint=(
                    f"next: read those lines ('{read_command} --lines "
                    f"{_window(lines)}'), then re-send the edit with an occurrence that exists"
                ),
            )
        start = starts[substitution.occurrence - 1]
        return start, start + len(substitution.old)
    if len(starts) > 1:
        # The ED2 refusal: the occurrences are named, so the next read and the next
        # edit are both mechanical.
        raise EditRefused(
            f"{what} occurs {len(starts)} times (lines {_lines_text(lines)});\n"
            f"{REFUSAL_INDENT}an edit must name one occurrence; no bytes were changed",
            [f"{read_command} --lines {_window(lines)}"],
            text_hint=(
                f"next: read those lines ('{read_command} --lines {_window(lines)}'),\n"
                f"      then re-send the edit with an explicit occurrence"
            ),
        )
    return starts[0], starts[0] + len(substitution.old)


def _occurrences(text: str, old: str) -> list:
    """Every start offset where `old` occurs in `text`, overlapping ones included.

    An occurrence count has to match what a caller can see: a text that overlaps
    itself (`"aa"` in `"aaa"`) really does occur twice, and hiding the second one
    would silently pick a placement the caller did not ask for."""
    starts, position = [], text.find(old)
    while position != -1:
        starts.append(position)
        position = text.find(old, position + 1)
    return starts


def _line_of(text: str, offset: int) -> int:
    """The 1-based line number `offset` falls on."""
    return text.count("\n", 0, offset) + 1


def _lines_text(lines) -> str:
    return ", ".join(str(line) for line in lines)


def _window(lines) -> str:
    """A `--lines START:END` window around the first occurrence named."""
    first = lines[0]
    return f"{max(1, first - CONTEXT_LINES)}:{first + CONTEXT_LINES}"


def _first_line(text: str) -> str:
    """The first line of a replacement, as a report row shows it."""
    lines = text.splitlines()
    return lines[0] if lines else ""
