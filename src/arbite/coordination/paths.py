"""Canonical paths: what arbite will manage, and the refusals for what it will not.

Every path the proxy touches is typed by a model, so this module is the single place
that decides what a path *is* before any operation looks at a ticket, an attempt or a
claim. Two questions, deliberately separate:

- **Canonicalisation** (`canonical_relative`) is textual. It resolves `.`/`..`, empty
  and duplicate separators, and an absolute path *inside* the root, then refuses
  anything that lands outside the root or on arbite's own state. Because it is
  textual it is cheap and deterministic, and because every spelling of one file
  collapses to one relative path, two aliases cannot reach two different claim
  records.
- **Observation** (`probe`) is about the filesystem, and it is re-run at every use
  rather than trusted from an earlier validation: whether a component is a symlink,
  whether the target is a regular file and whether it is hard-linked can all change
  between the two moments. This is the "validate again at use time" rule of the
  handoff; the boundary it cannot cross is stated honestly in the durability section
  -- no check eliminates a race with an unrestricted external writer.

The refusals are `PathRefused` (exit 1, "fix the command"), because that is the
caller's response to all of them. Two of them are frozen transcripts owned by this
slice -- the escape refusal (LS6) and the protected-path refusal (LS6) -- and one is
the refusal the *read* surface raises for a path that does not exist (RD5), kept here
with the other path rules so the slice that owns reads (tic-1c4f) raises exactly the
frozen block rather than re-inventing its wording.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import PathRefused
from .records import ABSENT, digest_bytes
from .results import REFUSAL_INDENT
from .scratch import SCRATCH_DIRNAME, human_size

#: The names arbite protects. `.git` is VCS metadata rather than content; the
#: coordination and scratch trees are runtime state (a claim record is not a file a
#: writer may claim, and a payload is transport that authorises nothing); the store
#: files and `project.yaml` are arbite's own state and configuration, which describe
#: the workspace rather than belonging to the work in it. Tickets, plan documents and
#: agent scratchpads under `.arbite/` are *not* protected here: they are documents,
#: and whether the proxy should list or read them is the discovery slice's question
#: (tic-1c4f), not a path rule.
GIT_METADATA_DIRNAME = ".git"
ARBITE_DIRNAME = ".arbite"
PROTECTED_ARBITE_DIRS = ("coordination", "scratch")
PROTECTED_ARBITE_FILES = ("project.yaml",)
PROTECTED_ARBITE_PREFIXES = ("arbite.db",)

#: The parts of arbite's own state a path can name, as `arbite_state` reports them.
#: Separate names because discovery renders them differently -- the coordination
#: tree is invisible to it while `project.yaml` is a document it lists -- even
#: though every one of them is refused as a *mutation* target with one message.
STATE_GIT = "git"
STATE_SCRATCH = "scratch"
STATE_COORDINATION = "coordination"
STATE_STORE = "store"
STATE_CONFIG = "config"

#: Why each protected path is refused, in the words the frozen LS6 block prints
#: (`.git`), C04's protected-path cases pin (runtime state, configuration).
PROTECTED_STATE_REFUSALS = {
    STATE_GIT: "arbite does not manage .git metadata",
    STATE_SCRATCH: "arbite does not manage its own runtime state",
    STATE_COORDINATION: "arbite does not manage its own runtime state",
    STATE_STORE: "arbite does not manage its own runtime state",
    STATE_CONFIG: "arbite does not manage its own configuration",
}

DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class Version:
    """What arbite observed at a path when it looked at it.

    A whole-file digest, or the explicit `ABSENT` marker -- never an empty string and
    never a guess, because this value is what a later mutation is checked against.
    `lines` is the line count a report prints for text (`None` for bytes that are not
    UTF-8 text, where the size is printed instead); `size` is always exact, and JSON
    carries it rather than the rendered text."""

    digest: str
    size: int = 0
    lines: Optional[int] = None

    @property
    def is_absent(self) -> bool:
        return self.digest == ABSENT

    def describe(self) -> str:
        """The version as a report prints it beside a digest."""
        if self.is_absent:
            return ABSENT
        if self.lines is None:
            return f"{human_size(self.size)} (binary)"
        noun = "line" if self.lines == 1 else "lines"
        return f"{self.lines} {noun}"

    def to_dict(self) -> dict:
        return {"digest": self.digest, "size": self.size, "lines": self.lines}


def canonical_relative(raw, root, allow_arbite_state: bool = False) -> str:
    """`raw` as the one project-relative path that names it, or a refusal.

    The single definition of "which file is this" for the whole proxy: absolute
    spellings inside the root, `.`/`..` segments, doubled separators and the same path
    written twice all collapse to one string, so a claim, a read and a write can never
    disagree about which file they mean.

    `allow_arbite_state` is for *discovery*, which has to canonicalise paths it will
    not manage -- `project.yaml` is a document a listing shows, and the scratch area
    is reported rather than silently dropped -- before applying its own, more
    specific rules. It never relaxes the `.git` refusal, and every mutation target
    keeps the default: a claim, a write or a read must still be refused."""
    text = raw if isinstance(raw, str) else str(raw)
    if not text:
        raise PathRefused("a path is required, and an empty one names nothing")
    if "\\" in text:
        raise PathRefused(
            f"'{text}' is not a project-relative path: arbite paths use '/', and a "
            "backslash is refused rather than guessed at"
        )

    root = Path(root)
    if _is_absolute(text):
        # An absolute spelling *inside* the root is an alias, not an escape: it
        # collapses to the same relative path, and therefore to the same claim.
        relative = _collapse(os.path.relpath(_fold(text), _fold(str(root))))
    else:
        relative = _collapse(text)

    if relative == ".." or relative.startswith("../"):
        raise _escape_refusal(text, root)
    if not relative:
        raise PathRefused(
            f"'{text}' is the workspace root itself; arbite manages whole files inside "
            "it ('arbite file list .' to see what is there)"
        )
    if not allow_arbite_state or arbite_state(relative) == STATE_GIT:
        _refuse_protected(text, relative)
    return relative


def arbite_state(relative: str):
    """Which part of arbite's own state `relative` names, or None if it names none.

    One classifier for both questions the proxy asks about arbite's own tree: "may
    this path be claimed, read or written" (any answer but None is a refusal, see
    `PROTECTED_STATE_REFUSALS`) and "how does discovery render it" (the coordination
    tree and the store files are invisible, the scratch area is reported as
    transport, `project.yaml` is a document). Compared with `normcase`, so a
    case-insensitive filesystem cannot spell its way past the rule."""
    segments = relative.split("/")
    if _fold(segments[0]) == _fold(GIT_METADATA_DIRNAME):
        return STATE_GIT
    if len(segments) < 2 or _fold(segments[0]) != _fold(ARBITE_DIRNAME):
        return None
    inner = _fold(segments[1])
    if inner == _fold(SCRATCH_DIRNAME):
        return STATE_SCRATCH
    if inner in tuple(_fold(name) for name in PROTECTED_ARBITE_DIRS):
        return STATE_COORDINATION
    if inner.startswith(PROTECTED_ARBITE_PREFIXES):
        return STATE_STORE
    if inner in tuple(_fold(name) for name in PROTECTED_ARBITE_FILES):
        return STATE_CONFIG
    return None


def probe(root, relative) -> Version:
    """Observe `relative` inside `root`: its version, or `ABSENT`, or a refusal.

    The use-time half of path validation. Every component must be a real directory
    entry rather than a symlink, the target must be a regular file (or absent, which
    is a legitimate answer: creating a file is a claimable act), and a hard-linked
    target is refused because a second name for the same bytes is exactly how an alias
    would evade a claim."""
    current = Path(root)
    for segment in relative.split("/"):
        current = current / segment
        if current.is_symlink():
            raise PathRefused(
                f"'{relative}' is reached through the symbolic link '{segment}'; arbite "
                "refuses to follow links, because the file behind one is not the path a "
                "claim would name"
            )
    if not current.exists():
        parent = posixpath.dirname(relative) or "."
        if not (Path(root) / parent).is_dir():
            raise PathRefused(
                f"'{relative}' cannot be created: the directory '{parent}' does not exist "
                "(arbite does not create directories for a mutation)"
            )
        return Version(ABSENT)
    if current.is_dir():
        raise PathRefused(
            f"'{relative}' is a directory; arbite manages whole files, so name one inside it"
        )
    if not current.is_file():
        raise PathRefused(
            f"'{relative}' is not a regular file; arbite manages regular files"
        )
    stat = current.stat()
    if stat.st_nlink > 1:
        raise PathRefused(
            f"'{relative}' has {stat.st_nlink} hard links, so a second name for the same "
            "bytes could bypass a claim; arbite refuses to manage it"
        )
    data = current.read_bytes()
    return Version(digest_bytes(data), len(data), _line_count(data))


def missing_path_refusal(path: str, ticket_id: str, attempt_id: str) -> PathRefused:
    """The refusal a read raises for a path that does not exist (the frozen RD5 block).

    Creating a file is an explicit, claimable act, so the refusal says so instead of
    leaving the caller to reach for `touch`. Raised by the read surface (tic-1c4f);
    kept here so its wording lives with the other path rules rather than being copied
    into a command."""
    directory = posixpath.dirname(path) or "."
    list_hint = f"arbite file list {directory}"
    claim_hint = f"arbite file claim {path} --ticket {ticket_id} --attempt {attempt_id}"
    return PathRefused(
        f"no such path '{path}'",
        [list_hint, claim_hint],
        # The frozen block joins its two hints at the end of the first line rather than
        # in front of the second (see `results.text_hint_of`).
        text_hint=f"next: '{list_hint}' to see what exists, or\n      '{claim_hint}' to create it",
    )


def escape_refusal(raw, root) -> PathRefused:
    """The refusal for a path that lands outside the project root (the frozen LS6 block).

    Public because a later slice may have to explain the same rule while asking a
    different question (a scratch payload name, a rename destination)."""
    return _escape_refusal(raw, root)


def _escape_refusal(raw, root) -> PathRefused:
    return PathRefused(
        f"'{raw}' resolves outside the workspace root ({root});\n"
        f"{REFUSAL_INDENT}paths are validated against the project root",
        ["'arbite file list .' to list the workspace"],
    )


def _refuse_protected(raw, relative: str) -> None:
    """Refuse arbite's own state, `.git`, and the store files, by name (see
    `arbite_state` for which paths those are)."""
    state = arbite_state(relative)
    if state is not None:
        raise PathRefused(f"'{raw}' is protected: {PROTECTED_STATE_REFUSALS[state]}")


def _is_absolute(text: str) -> bool:
    return text.startswith("/") or bool(DRIVE_LETTER.match(text))


def _collapse(text: str) -> str:
    """Resolve `text` textually: drop empty and `.` segments, pop on `..`.

    Textual rather than filesystem-resolving on purpose: `Path.resolve()` follows
    symlinks, which would accept an alias this module is here to refuse. A leading
    `..` that has nothing to pop is kept, so the caller sees the escape instead of a
    silently clamped path."""
    parts: list = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append("..")
            continue
        parts.append(segment)
    return "/".join(parts)


def _fold(text: str) -> str:
    """`text` with filesystem case rules applied, for comparing names.

    Identical to `text` on a case-sensitive filesystem; on a case-insensitive one it
    is what stops `.GIT` or `.Arbite/Project.yaml` from being a second spelling of a
    protected path."""
    return os.path.normcase(text)


def _line_count(data: bytes) -> Optional[int]:
    """How many lines `data` has, or `None` when it is not UTF-8 text.

    A report prints a line count for text and a size for bytes it cannot read as
    text, rather than calling a binary blob "N lines"."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return len(data.splitlines())
