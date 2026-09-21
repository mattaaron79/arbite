"""Bounded discovery: `arbite file list` and `arbite file search`.

Discovery answers "what is there, and may I plan against it" without ever
authorising a change. That distinction is the whole point of the slice:

- **A listing is not a read.** `file list` and `file search` mint no observation,
  record no token and change no ownership. Knowing a path is free is not permission
  to write it: a writer still has to claim it and read it again under the claim,
  which is what the frozen FC1 hint says in words.
- **A listing labels ownership** so "can I plan against this?" needs no second
  command: every claimable row ends in `unclaimed` or `CLAIMED tic-XXXX/att-XXXX`.
- **Output is bounded and its continuation is executable.** A row set is cut at
  `--count`, the count is stated, and the truncation line names the *exact* command
  that continues from the last row printed -- a token from this command's own
  output, never a description of where to look next.
- **Scratch and coordination state are not content.** The coordination tree and the
  store files are skipped by path, the scratch area is reported as transport in one
  line instead of being walked, and the generated guide is labelled rather than
  listed as if it were work.

Two rules come from the plan and are enforced here rather than left to a caller:
discovery never sleeps or retries (a one-shot bounded question), and it never
returns a row for a path it would refuse to manage -- symlinks, special files and
directories are not rows, so no row invites a claim that `probe` would reject.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Optional

from ..errors import PathRefused
from .paths import (
    ARBITE_DIRNAME,
    PROTECTED_STATE_REFUSALS,
    STATE_CONFIG,
    STATE_COORDINATION,
    STATE_GIT,
    STATE_SCRATCH,
    STATE_STORE,
    Version,
    arbite_state,
    canonical_relative,
    probe,
)
from .results import EMPTY, OperationResult, Outcome, succeeded
from .scratch import printable_size, scratch_summary

#: The listing's columns, pinned byte for byte by the frozen LS1/LS2 rows: the path,
#: the line count right-aligned in its field, the word that names the count, the size
#: right-aligned in its field, and the claim state after a two-space gap. The widths
#: are minima -- a longer path simply pushes the rest of the row right, which is what
#: a reader expects of a table.
LIST_PATH_COLUMN = 28
LIST_COUNT_COLUMN = 6
LIST_SIZE_COLUMN = 10
LIST_STATE_GAP = 2

#: How many rows `file list` and `file search` print unless `--count` says otherwise.
#: The count that was used is always printed back in the truncation hint, so a
#: continuation never depends on the caller remembering what the default was.
DEFAULT_LIST_COUNT = 100
DEFAULT_SEARCH_COUNT = 500

#: A search row's line number is left-aligned in this field, then followed by
#: `SEARCH_TEXT_GAP` spaces. The frozen LS3 and LS4 rows are one space apart in raw
#: counting and agree exactly under this rule: `141` fills the field, `56` is padded by
#: two, and both put the text where the document puts it.
LINE_NUMBER_COLUMN = 4
SEARCH_TEXT_GAP = 2

#: The second line of a truncation block lines up under the first.
TRUNCATION_INDENT = " " * len("truncated: ")

#: What a listing prints instead of version metadata. `GENERATED_FILES` is the file
#: sink's list of documents arbite writes rather than finds
#: (`sinks.file.GENERATED_FILES`); a test pins the two together so neither can drift.
GENERATED_FILES = ("AGENTS.md",)
GENERATED_NOTE = "(generated, not a ticket)"
SCRATCH_NOTE = "(transport, {files} -- not listed as a file, never claimable)"
ENTRY_NOTE_GAP = 2

#: The one answer a search inside the scratch area gives. Scratch is transport, not
#: project content, so the honest reply is "nothing here is searchable" rather than a
#: refusal: the caller asked about an area the proxy documents as excluded.
SCRATCH_EXCLUDED = (
    "no matches (scratch is excluded from discovery: it is transport, not project "
    "content)"
)

#: The entry kinds a listing renders.
KIND_FILE = "file"
KIND_DOCUMENT = "document"
KIND_GENERATED = "generated"
KIND_SCRATCH = "scratch"


@dataclass(frozen=True)
class DiscoveryEntry:
    """One row of a listing.

    `KIND_FILE` is a regular file the proxy may manage, with the version and the
    active claim a plan needs. `KIND_DOCUMENT` is a file it may not (`project.yaml`,
    or a hard-linked target it refuses to own), `KIND_GENERATED` a guide arbite
    writes itself, and `KIND_SCRATCH` the transport area reported as one line with
    the count of payloads inside it."""

    path: str
    kind: str
    version: Optional[Version] = None
    claim: Optional[dict] = None
    files: int = 0

    @property
    def is_claimable(self) -> bool:
        return self.kind == KIND_FILE

    @property
    def printed(self) -> str:
        """The path as a row prints it: the scratch area keeps its trailing slash, so
        the row reads as "this is a directory, and it is not walked"."""
        return f"{self.path}/" if self.kind == KIND_SCRATCH else self.path


class FileDiscovery:
    """Bounded listing and text search over the paths the proxy may manage."""

    def __init__(self, sink, lifecycle):
        self.sink = sink
        self.lifecycle = lifecycle
        self.app = lifecycle.app
        self.store = lifecycle.store
        self.project_root = self.app.project_root

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list(self, raw_path=None, count: Optional[int] = None, after=None) -> OperationResult:
        """List the files at or under a path, in canonical order, bounded by `count`.

        The listing is the whole answer to "what is here and who holds it": each
        claimable row carries its shape and its claim state, and a truncated listing
        ends with the command that continues from the last row printed.
        """
        start = self._canonical_start(raw_path)
        self._refuse_invisible(start)
        entries = self._entries_under(start)
        limit = self._limit(count, DEFAULT_LIST_COUNT)
        if after is not None:
            cursor = canonical_relative(after, self.project_root, allow_arbite_state=True)
            entries = [entry for entry in entries if entry.path > cursor]
        shown, remaining = entries[:limit], entries[limit:]

        if not shown:
            return OperationResult(
                Outcome(EMPTY),
                [self._empty_message(start)],
                self._list_payload(start, raw_path, limit, [], len(entries), False),
                [],
            )

        noun = "entries" if any(not entry.is_claimable for entry in shown) else "files"
        lines = self._list_rows(shown)
        actions = []
        if remaining:
            action = self._continuation(start, raw_path, shown[-1], limit)
            lines.append(
                f"truncated: {len(remaining)} more {noun} match; continue with '{action}'"
            )
            actions = [action]
        else:
            lines.append(f"{len(shown)} {noun} (no truncation)")
        return succeeded(
            lines=lines,
            data=self._list_payload(
                start, raw_path, limit, shown, len(entries), bool(remaining)
            ),
            # The truncation line *is* the continuation, so the text gains no `next:`
            # line of its own -- but JSON still publishes the command to run next.
            next_actions=actions,
            text_hint="",
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        pattern: str,
        raw_path=None,
        count: Optional[int] = None,
        after=None,
    ) -> OperationResult:
        """Find `pattern` in the text of every managed file at or under a path.

        Literal text (not a regular expression), in canonical path order and then
        line order, because that is the order a caller can continue from: the
        truncation hint names a `path:line` token that appears in this command's own
        output.
        """
        start = self._canonical_start(raw_path)
        self._refuse_invisible(start)
        limit = self._limit(count, DEFAULT_SEARCH_COUNT)
        if in_scratch(start):
            return OperationResult(
                Outcome(EMPTY),
                [SCRATCH_EXCLUDED],
                self._search_payload(pattern, start, [], [], None),
                [],
            )
        cursor = self._search_cursor(after)

        matches = []
        for entry in self._entries_under(start):
            if not entry.is_claimable:
                continue
            for number, text in self._numbered_lines(entry.path):
                if pattern in text:
                    matches.append((entry.path, number, text))
        if cursor is not None:
            matches = [match for match in matches if (match[0], match[1]) > cursor]
        shown, remaining = matches[:limit], matches[limit:]

        if not shown:
            return OperationResult(
                Outcome(EMPTY),
                [self._no_match_message(start)],
                self._search_payload(pattern, start, [], matches, cursor),
                [],
            )

        lines = [self._search_row(match) for match in shown]
        actions = []
        if remaining:
            action = self._search_continuation(pattern, start, shown[-1])
            lines.append(
                f"truncated: {len(remaining)} more matches in "
                f"{len({match[0] for match in remaining})} files; narrow with "
                f"'{self._narrow_command(pattern, remaining[0][0])}',"
            )
            lines.append(
                f"{TRUNCATION_INDENT}or continue with '--after {called(shown[-1])}'"
            )
            actions = [action]
        else:
            lines.append(self._match_footer(shown))
        return succeeded(
            lines=lines,
            data=self._search_payload(pattern, start, shown, matches, cursor),
            next_actions=actions,
            # As in `list`: the truncation block carries the continuation itself.
            text_hint="",
        )

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    def _entries_under(self, start: str) -> list:
        """Every discoverable entry at or under `start`, in canonical path order."""
        if in_scratch(start):
            return [self._scratch_entry(self._scratch_root(start))]
        target = self._absolute(start)
        if not target.exists():
            raise PathRefused(
                f"no such path '{start or '.'}'",
                ["'arbite file list .' to list the workspace"],
            )
        if target.is_symlink():
            raise PathRefused(
                f"'{start}' is a symbolic link; arbite refuses to follow links, because "
                "the file behind one is not the path a claim would name"
            )
        if target.is_file():
            return [self._entry(start)]
        if not target.is_dir():
            raise PathRefused(
                f"'{start}' is not a regular file or a directory; arbite manages whole files"
            )
        found = []
        self._walk(start, found)
        found.sort(key=lambda entry: entry.path)
        return found

    def _walk(self, relative_dir: str, found: list) -> None:
        """Collect the entries under one directory, skipping what is not content."""
        directory = self._absolute(relative_dir)
        try:
            names = sorted(os.listdir(directory))
        except OSError as e:
            raise PathRefused(f"'{relative_dir}' cannot be listed: {e}")
        for name in names:
            child = f"{relative_dir}/{name}" if relative_dir else name
            full = directory / name
            if full.is_symlink():
                # A path reached through a link is one the proxy refuses to manage,
                # so it is not a row: no listing may invite a claim `probe` rejects.
                continue
            state = arbite_state(child)
            if state in (STATE_GIT, STATE_COORDINATION, STATE_STORE):
                continue  # invisible: arbite's own state is not a discovery surface
            if state == STATE_SCRATCH:
                found.append(self._scratch_entry(child))
                continue
            if full.is_dir():
                self._walk(child, found)
                continue
            if not full.is_file():
                continue
            found.append(self._entry(child))

    def _entry(self, relative: str) -> DiscoveryEntry:
        """One file's row: its version and claim, or the note that explains it."""
        if arbite_state(relative) == STATE_CONFIG:
            return DiscoveryEntry(relative, KIND_DOCUMENT)
        if self._is_generated(relative):
            return DiscoveryEntry(relative, KIND_GENERATED)
        claim = self._active_claims().get(_key(relative))
        try:
            version = probe(self.project_root, relative)
        except PathRefused:
            # A hard-linked target is not a file arbite will manage (a second name
            # for the same bytes could bypass a claim), so it is not offered as one.
            return DiscoveryEntry(relative, KIND_DOCUMENT)
        return DiscoveryEntry(
            relative,
            KIND_FILE,
            version,
            None if claim is None else self._claim_entry(claim),
        )

    def _scratch_entry(self, relative: str) -> DiscoveryEntry:
        """The scratch area as one row: how much transport is sitting in it."""
        summary = scratch_summary(self.app.arbite_dir)
        return DiscoveryEntry(relative, KIND_SCRATCH, files=summary.files)

    def _is_generated(self, relative: str) -> bool:
        """Whether a file is a guide arbite writes rather than content it found."""
        return (
            relative.split("/")[0] == ARBITE_DIRNAME
            and posixpath.basename(relative) in GENERATED_FILES
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _list_rows(self, entries) -> list:
        """The rows of a listing, with notes aligned under the widest path."""
        width = max(len(entry.printed) for entry in entries) + ENTRY_NOTE_GAP
        rows = []
        for entry in entries:
            note = self._note(entry)
            rows.append(f"{entry.printed.ljust(width)}{note}" if note else self._row(entry))
        return rows

    def _note(self, entry: DiscoveryEntry) -> str:
        if entry.kind == KIND_GENERATED:
            return GENERATED_NOTE
        if entry.kind == KIND_SCRATCH:
            noun = "file" if entry.files == 1 else "files"
            return SCRATCH_NOTE.format(files=f"{entry.files} {noun}")
        return ""

    @staticmethod
    def _file_row(entry: DiscoveryEntry) -> str:
        """A claimable file's row: shape, size and state, in the frozen columns."""
        version = entry.version
        shape = (
            f"{version.lines:>{LIST_COUNT_COLUMN}} lines"
            if version.lines is not None
            else f"{'bytes':>{LIST_COUNT_COLUMN}}"
        )
        size = f"{printable_size(version.size):>{LIST_SIZE_COLUMN}}"
        state = (
            "unclaimed"
            if entry.claim is None
            else f"CLAIMED {entry.claim['ticket']}/{entry.claim['attempt']}"
        )
        return (
            f"{entry.path:<{LIST_PATH_COLUMN}}{shape}{size}"
            f"{' ' * LIST_STATE_GAP}{state}"
        )

    def _row(self, entry: DiscoveryEntry) -> str:
        return self._file_row(entry) if entry.is_claimable else entry.printed

    @staticmethod
    def _search_row(match) -> str:
        path, number, text = match
        return f"{path}:{number:<{LINE_NUMBER_COLUMN}}{' ' * SEARCH_TEXT_GAP}{text}"

    @staticmethod
    def _match_footer(shown) -> str:
        matches = len(shown)
        files = len({match[0] for match in shown})
        match_noun = "match" if matches == 1 else "matches"
        file_noun = "file" if files == 1 else "files"
        return f"{matches} {match_noun} in {files} {file_noun} (no truncation)"

    # ------------------------------------------------------------------
    # Continuations
    # ------------------------------------------------------------------

    def _continuation(self, start: str, raw_path, last: DiscoveryEntry, limit: int) -> str:
        """The exact command that continues a listing from its last row."""
        return (
            f"arbite file list {self._called_path(start, raw_path)} "
            f"--after {last.printed} --count {limit}"
        )

    def _search_continuation(self, pattern: str, start: str, last) -> str:
        """The exact command that continues a search from its last match."""
        return (
            f'arbite file search "{pattern}" {start or "."} '
            f"--after {called(last)}"
        )

    def _narrow_command(self, pattern: str, path: str) -> str:
        directory = posixpath.dirname(path) or "."
        return f'arbite file search "{pattern}" {directory}'

    @staticmethod
    def _called_path(start: str, raw_path) -> str:
        """The path as the caller spelled it, so a continuation is that command again."""
        if raw_path not in (None, ""):
            return raw_path
        return start or "."

    @staticmethod
    def _empty_message(start: str) -> str:
        where = start or "."
        return f"no files under '{where}' (discovery skips scratch and coordination state)"

    @staticmethod
    def _no_match_message(start: str) -> str:
        return f"no matches in '{start or '.'}'"

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------

    def _list_payload(self, start, raw_path, limit, shown, total, truncated) -> dict:
        return {
            "path": self._called_path(start, raw_path),
            "count": limit,
            "entries": [self._entry_dict(entry) for entry in shown],
            "shown": len(shown),
            "total": total,
            "truncated": truncated,
        }

    @staticmethod
    def _search_payload(pattern, start, shown, matches, cursor) -> dict:
        return {
            "pattern": pattern,
            "path": start or ".",
            "after": None if cursor is None else called(cursor),
            "matches": [
                {"path": path, "line": number, "text": text} for path, number, text in shown
            ],
            "shown": len(shown),
            "total": len(matches),
            "files": len({match[0] for match in matches}),
            "truncated": len(shown) < len(matches),
        }

    @staticmethod
    def _entry_dict(entry: DiscoveryEntry) -> dict:
        data = {"path": entry.path, "kind": entry.kind, "state": None}
        if entry.version is not None:
            data.update(
                {
                    "lines": entry.version.lines,
                    "bytes": entry.version.size,
                    "digest": entry.version.digest,
                    "state": "claimed" if entry.claim else "unclaimed",
                    "claim": entry.claim,
                }
            )
        if entry.kind == KIND_SCRATCH:
            data["files"] = entry.files
        return data

    @staticmethod
    def _claim_entry(claim) -> dict:
        return {
            "ticket": claim.ticket_id,
            "attempt": claim.attempt_id,
            "generation": claim.generation,
        }

    # ------------------------------------------------------------------
    # Small shared pieces
    # ------------------------------------------------------------------

    def _active_claims(self) -> dict:
        """The current claim index, keyed the way a path is compared on this machine."""
        return {_key(claim.path): claim for claim in self.store.active_claims()}

    def _canonical_start(self, raw_path) -> str:
        """The start path as a canonical relative path (`''` for the project root).

        `.` and an absolute spelling of the root are both "the whole workspace",
        which the frozen LS6 hint offers (`arbite file list .`) -- and which
        `canonical_relative` deliberately refuses for a *mutation* target. Discovery
        also asks about paths a mutation refuses (`project.yaml` is a document it
        lists), so the arbite-state refusal is deferred to `_refuse_invisible`.
        """
        text = raw_path if raw_path not in (None, "") else "."
        if text in (".", "./") or self._is_root(text):
            return ""
        return canonical_relative(text, self.project_root, allow_arbite_state=True)

    def _refuse_invisible(self, start: str) -> None:
        """Refuse a path discovery does not have: arbite's coordination state.

        Naming the coordination tree or the store is not a listing of anything -- they
        are invisible to discovery whichever way a caller reaches for them -- so the
        refusal is the protected-path one C04 pinned, in the same words a claim uses.
        """
        state = arbite_state(start) if start else None
        if state in (STATE_COORDINATION, STATE_STORE):
            raise PathRefused(f"'{start}' is protected: {PROTECTED_STATE_REFUSALS[state]}")

    def _is_root(self, text: str) -> bool:
        if not os.path.isabs(text):
            return False
        return os.path.normcase(os.path.normpath(text)) == os.path.normcase(
            os.path.normpath(str(self.project_root))
        )

    def _absolute(self, relative: str):
        return self.project_root / relative if relative else self.project_root

    def _scratch_root(self, start: str) -> str:
        """The scratch area's own relative path, whatever depth `start` named."""
        return "/".join(start.split("/")[:2])

    def _numbered_lines(self, relative: str):
        """`(line number, text)` for a file's text lines; bytes that are not UTF-8
        text have no lines to search, so they contribute none."""
        try:
            text = (self.project_root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        return list(enumerate(text.splitlines(), start=1))

    @staticmethod
    def _limit(count, default: int) -> int:
        if count is None:
            return default
        if count < 1:
            raise PathRefused(
                f"--count must be at least 1, got {count}; there is no 'print nothing' "
                "mode (a listing with nothing to show exits 2)"
            )
        return count

    def _search_cursor(self, after):
        """`--after PATH:LINE` as a `(path, line)` cursor, or None."""
        if after is None:
            return None
        text = str(after)
        path, _, number = text.rpartition(":")
        if not path or not number.isdigit():
            raise PathRefused(
                f"--after expects PATH:LINE, got '{text}' (the token a previous search's "
                "truncation line printed, e.g. '--after src/arbite/cli.py:56')"
            )
        return (
            canonical_relative(path, self.project_root, allow_arbite_state=True),
            int(number),
        )


def in_scratch(relative: str) -> bool:
    """Whether a relative path names the scratch area or something inside it."""
    return bool(relative) and arbite_state(relative) == STATE_SCRATCH


def called(match) -> str:
    """A `(path, line)` search match as the token a `--after` flag takes."""
    return f"{match[0]}:{match[1]}"


def _key(path: str) -> str:
    """A path's comparison key: the case-insensitive spelling on a case-insensitive
    filesystem, the path itself on a case-sensitive one."""
    return os.path.normcase(path)
