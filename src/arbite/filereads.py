"""Bounded file discovery and versioned reads (planning key C06).

This is the read half of the shared-directory file proxy. It sits above
`arbite.application` (guards, observed-record bookkeeping) and `arbite.paths`
(the canonical pipeline every read must use), and it owns four operations:

- `list` / `search` -- bounded discovery of *what is there*, with deterministic
  ordering, explicit pagination/truncation markers and per-entry path/version
  metadata. Discovery proves read access only: it never claims a path and never
  mints a write-authorizing observation.
- `read` -- serve a whole file or a requested line range, with the whole-file
  digest recorded even for a ranged read. A read does **not** take an exclusive
  lock and does **not** acquire ownership. `version_only=True` records the
  whole-file version *without* serving content: it works for any regular file,
  including the binary and UTF-16/32 files the text surface refuses, so an
  existing binary file has a read token to be replaced under.
- `probe` -- observe an absent creation destination so a later create is known to
  be safe. A probe is inspection only: it acquires nothing and records no
  observation.

The planning contract, made executable here:

- **Reads never block.** A read of a file claimed by *another* attempt still
  serves the bytes, but the receipt is explicitly non-writable and names the busy
  owner (claim, ticket, attempt, generation, observed version). `fail_if_busy`
  turns that into a prompt `file_busy` refusal instead, so a caller can avoid
  spending tokens on bytes it may not write.
- **Fresh reads authorize writes.** An observation is write-authorizing only when
  the *current, active* attempt already holds the path's active claim and the
  bytes served still match the claim's recorded `observed_version`. A pre-claim
  read, another attempt's read, a read whose claim generation is stale, or a read
  that observed external drift is evidence, not permission. C07's writers pass the
  returned `read_token` (the observation id) to
  `application.require_write_authorization`, which re-checks all of this.
- **Ranged reads identify the whole file.** `digest` covers the entire file even
  when `--lines` returned a window; the receipt states `content_complete: false`
  so a partial view is never presented as the whole file. A version-only receipt
  is likewise `content_complete: false` with `version_only: true` and no text.
- **Absence is explicit.** A probe of an absent path reports `version: ABSENT`
  and `safe_to_create`, and names the claim a create will need. Nothing is
  created, claimed or owned by a probe.
- **Limits are loud.** Output bounds, skipped files (binary, oversized,
  undecodable), capped limits and truncated pages are all carried in the result
  as explicit markers and counts. Unsupported encodings/file types fail with a
  typed `unsupported` error naming the reason, the size and the whole-file digest
  -- never a silent partial read.

Nothing here sleeps, retries, watches or schedules; every call is one-shot.
"""

from __future__ import annotations

import codecs
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import paths as path_policy
from .application import Actor, CoordinationService
from .coordination import (
    ABSENT,
    ReadObservation,
    canonical_relative_path,
    digest_of_bytes,
    is_protected_path,
)
from .errors import CoordinationNotFound, FileBusy, UnsupportedCoordination
from .fileclaims import FileClaimService

# ---------------------------------------------------------------------------
# Bounds and limits. Every one of these is reported in a result when it bites,
# so a caller never has to guess whether output was complete.
# ---------------------------------------------------------------------------

#: Discovery page size default and hard cap.
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 2000
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 500

#: How many filesystem entries a single walk will consider before it stops and
#: says so. Bounds the work a discovery call can do on a huge tree.
MAX_SCAN_ENTRIES = 20000

#: Files larger than this are not text-searched. They are reported as skipped
#: (`file_too_large`) rather than silently omitted.
MAX_SEARCH_FILE_BYTES = 1_000_000

#: A matched line longer than this is returned truncated *with the flag set*.
MAX_MATCH_LINE_CHARS = 500

#: Files larger than this are listed with `version_omitted` instead of being read
#: to compute a digest -- the discovery remains bounded and the omission is said
#: out loud.
MAX_VERSION_BYTES = 8 * 1024 * 1024

#: A whole-file read above this size is refused with an explicit error unless a
#: line range was requested (a range still digests the whole file but returns
#: only the window). Prevents a single command from dumping an enormous file.
MAX_READ_BYTES = 5_000_000

#: How many bytes are inspected to classify a file as text/binary.
SNIFF_BYTES = 8192

#: How many skipped/omitted entries a discovery result reports in detail.
MAX_SKIPPED_REPORTED = 50

#: Marker strings a discovery result uses to describe an incomplete view. Stable
#: vocabulary so a caller branches on a marker, not on prose.
MARKER_LIMIT_CAPPED = "limit_capped"
MARKER_OFFSET_ADVANCED = "offset_advanced"
MARKER_OUTPUT_TRUNCATED = "output_truncated"
MARKER_SCAN_LIMIT_REACHED = "scan_limit_reached"
MARKER_PROTECTED_EXCLUDED = "protected_paths_excluded"
MARKER_SYMLINKS_SKIPPED = "symlinks_skipped"
MARKER_VERSION_OMITTED = "version_omitted"
MARKER_CONTENT_NOT_SCANNED = "content_not_scanned"
MARKER_SKIPPED_TRUNCATED = "skipped_report_truncated"

#: Why a file's content was not searched. Explicit reasons, not silence.
SKIP_BINARY = "binary"
SKIP_NOT_UTF8 = "not_utf8_text"
SKIP_TOO_LARGE = "file_too_large"

#: Newline shapes reported alongside a read.
NEWLINE_LF = "lf"
NEWLINE_CRLF = "crlf"
NEWLINE_NONE = "none"

#: Why a read receipt is non-writable.
REASON_FOREIGN_CLAIM = "foreign_claim"
REASON_NO_CLAIM = "no_own_claim"
REASON_CLAIM_VERSION_MISMATCH = "claim_version_mismatch"
REASON_ATTEMPT_INACTIVE = "attempt_inactive"
REASON_NOT_FRESH = "observation_predates_claim"

_LINE_RANGE_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


def parse_line_range(text: str) -> Tuple[int, int]:
    """Parse a `START:END` line range (1-based, inclusive).

    Raises `UnsupportedCoordination` for anything else, so the CLI can pass this
    straight in as an argparse type and a bad range is a usage error, not a
    silently different read."""
    match = _LINE_RANGE_RE.match(str(text or ""))
    if not match:
        raise UnsupportedCoordination(
            f"line range {text!r} is not START:END (1-based, inclusive)",
            details={"lines": text},
        )
    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < 1:
        raise UnsupportedCoordination(
            f"line range {text!r} must be 1-based (START >= 1)", details={"lines": text}
        )
    if start > end:
        raise UnsupportedCoordination(
            f"line range {text!r} has START after END", details={"lines": text}
        )
    return start, end


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryEntry:
    """One path discovered by `list`."""

    path: str
    kind: str  # "file" | "directory"
    size: Optional[int] = None
    version: Optional[str] = None
    version_omitted: bool = False
    classification: Optional[str] = None  # "text" | "binary" | "unknown"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "version": self.version,
            "version_omitted": self.version_omitted,
            "classification": self.classification,
        }


@dataclass(frozen=True)
class SearchMatch:
    """One search hit: a path match (`line` is None) or a content-line match."""

    path: str
    version: Optional[str] = None
    version_omitted: bool = False
    line: Optional[int] = None
    text: Optional[str] = None
    text_truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "version": self.version,
            "version_omitted": self.version_omitted,
            "line": self.line,
            "text": self.text,
            "text_truncated": self.text_truncated,
        }


@dataclass(frozen=True)
class ListPage:
    """A bounded page of directory entries."""

    root: str
    prefix: str
    entries: List[DiscoveryEntry]
    offset: int
    limit: int
    total_entries: int
    truncated: bool
    next_offset: Optional[int]
    limit_capped: bool
    scan_limit_reached: bool
    excluded_protected: int
    skipped_symlinks: int
    markers: List[str] = field(default_factory=list)

    @property
    def returned(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "prefix": self.prefix,
            "entries": [entry.to_dict() for entry in self.entries],
            "returned": self.returned,
            "offset": self.offset,
            "limit": self.limit,
            "total_entries": self.total_entries,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "limit_capped": self.limit_capped,
            "scan_limit_reached": self.scan_limit_reached,
            "excluded_protected": self.excluded_protected,
            "skipped_symlinks": self.skipped_symlinks,
            "markers": list(self.markers),
        }


@dataclass(frozen=True)
class SearchPage:
    """A bounded page of search matches."""

    root: str
    prefix: str
    pattern: str
    matches: List[SearchMatch]
    offset: int
    limit: int
    scanned_files: int
    matched_files: int
    truncated: bool
    next_offset: Optional[int]
    limit_capped: bool
    scan_limit_reached: bool
    excluded_protected: int
    skipped_symlinks: int
    skipped: List[dict] = field(default_factory=list)
    skipped_truncated: bool = False
    markers: List[str] = field(default_factory=list)

    @property
    def returned(self) -> int:
        return len(self.matches)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "prefix": self.prefix,
            "pattern": self.pattern,
            "matches": [match.to_dict() for match in self.matches],
            "returned": self.returned,
            "offset": self.offset,
            "limit": self.limit,
            "scanned_files": self.scanned_files,
            "matched_files": self.matched_files,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "limit_capped": self.limit_capped,
            "scan_limit_reached": self.scan_limit_reached,
            "excluded_protected": self.excluded_protected,
            "skipped_symlinks": self.skipped_symlinks,
            "skipped": list(self.skipped),
            "skipped_truncated": self.skipped_truncated,
            "markers": list(self.markers),
        }


@dataclass(frozen=True)
class ReadReceipt:
    """What a `read` served, under which claim, and whether it authorizes a write.

    `read_token` is the observation id. C07 presents it to
    `application.require_write_authorization`; a non-writable receipt still has a
    token (the read happened and is durable evidence) but the token will be
    refused there.
    """

    workspace_id: str
    path: str
    attempt_id: str
    actor: str
    operation_id: str
    read_token: str
    whole_file_digest: str
    size: int
    encoding: str
    newline: str
    text: str
    content_complete: bool
    lines_total: int
    line_range_requested: Optional[Tuple[int, int]]
    line_range_returned: Optional[Tuple[int, int]]
    range_clamped: bool
    range_empty: bool
    write_authorizing: bool
    non_writable: bool
    non_writable_reason: Optional[str]
    claim_generation: Optional[int]
    claim_observed_version: Optional[str]
    busy: bool
    busy_owner: Optional[dict]
    observed_at: str
    version_only: bool = False

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "attempt_id": self.attempt_id,
            "actor": self.actor,
            "operation_id": self.operation_id,
            "read_token": self.read_token,
            "version_only": self.version_only,
            "whole_file_digest": self.whole_file_digest,
            "size": self.size,
            "encoding": self.encoding,
            "newline": self.newline,
            "text": self.text,
            "content_complete": self.content_complete,
            "lines_total": self.lines_total,
            "line_range_requested": list(self.line_range_requested)
            if self.line_range_requested
            else None,
            "line_range_returned": list(self.line_range_returned)
            if self.line_range_returned
            else None,
            "range_clamped": self.range_clamped,
            "range_empty": self.range_empty,
            "write_authorizing": self.write_authorizing,
            "non_writable": self.non_writable,
            "non_writable_reason": self.non_writable_reason,
            "claim_generation": self.claim_generation,
            "claim_observed_version": self.claim_observed_version,
            "busy": self.busy,
            "busy_owner": dict(self.busy_owner) if self.busy_owner else None,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ProbeReceipt:
    """A safe-create inspection of one path. Records nothing and owns nothing."""

    workspace_id: str
    path: str
    attempt_id: str
    exists: bool
    version: str
    size: Optional[int]
    safe_to_create: bool
    missing_parent: Optional[str]
    busy: bool
    busy_owner: Optional[dict]
    claim: Optional[dict]
    required_claim_version: Optional[str]
    next_action: str
    observation_recorded: bool = False
    claim_acquired: bool = False

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "path": self.path,
            "attempt_id": self.attempt_id,
            "exists": self.exists,
            "version": self.version,
            "size": self.size,
            "safe_to_create": self.safe_to_create,
            "missing_parent": self.missing_parent,
            "busy": self.busy,
            "busy_owner": dict(self.busy_owner) if self.busy_owner else None,
            "claim": dict(self.claim) if self.claim else None,
            "required_claim_version": self.required_claim_version,
            "next_action": self.next_action,
            "observation_recorded": self.observation_recorded,
            "claim_acquired": self.claim_acquired,
        }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass
class _Walk:
    entries: List[Tuple[str, str]]  # (relative path, "file"|"directory")
    excluded_protected: int
    skipped_symlinks: int
    scan_limit_reached: bool


def _classify(data: bytes) -> str:
    """`utf-8`, `utf-8-sig`, `utf-16`, `utf-32` or `binary` for `data`."""
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return "utf-32"
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if b"\x00" in data[:SNIFF_BYTES]:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "utf-8"


def _decode(data: bytes):
    """`(text, encoding, newline_shape)` for supported text, else raise.

    Supported encodings are UTF-8 and UTF-8-with-BOM. UTF-16/32 and binary bytes
    are refused explicitly (`unsupported`) rather than mojibaked into text."""
    kind = _classify(data)
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig", _newline_shape(data)
    if kind in ("utf-16", "utf-32"):
        raise UnsupportedCoordination(
            "the file is not UTF-8 text and arbite v1 reads only UTF-8 source files",
            details={
                "reason": SKIP_NOT_UTF8,
                "detected_encoding": kind,
                "digest": digest_of_bytes(data),
                "size": len(data),
            },
        )
    if kind == "binary":
        raise UnsupportedCoordination(
            "the file is not UTF-8 text (binary content); arbite v1 serves text reads "
            "only",
            details={
                "reason": SKIP_BINARY,
                "detected_encoding": "binary",
                "digest": digest_of_bytes(data),
                "size": len(data),
            },
        )
    return data.decode("utf-8"), "utf-8", _newline_shape(data)


def _newline_shape(data: bytes) -> str:
    if b"\r\n" in data:
        return NEWLINE_CRLF
    if b"\n" in data:
        return NEWLINE_LF
    return NEWLINE_NONE


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _busy_owner_dict(claim) -> dict:
    """The explicit busy-owner payload, using the same `holder_*` vocabulary as
    `fileclaims`' `file_busy` refusal so a caller parses one shape everywhere."""
    return {
        "workspace_id": claim.workspace_id,
        "path": claim.path,
        "holder_claim": claim.id,
        "holder_ticket": claim.ticket_id,
        "holder_attempt": claim.attempt_id,
        "holder_generation": claim.generation,
        "holder_observed_version": claim.observed_version,
        "holder_acquired": claim.acquired,
        "read_allowed": True,
        "write_authorizing": False,
        "available_actions": [
            "read is allowed but the receipt is non-writable",
            "retry the read with fail_if_busy to avoid spending tokens",
            "work on a different path until the holder releases",
        ],
    }


def _bound_limit(value: Optional[int], default: int, maximum: int):
    """`(limit, capped)`; refuses a nonsensical value, caps an oversized one."""
    if value is None:
        return default, False
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedCoordination(f"limit must be an integer, got {value!r}")
    if value < 1:
        raise UnsupportedCoordination(f"limit must be >= 1, got {value!r}")
    if value > maximum:
        return maximum, True
    return value, False


def _bound_offset(value: Optional[int]) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedCoordination(f"offset must be an integer, got {value!r}")
    if value < 0:
        raise UnsupportedCoordination(f"offset must be >= 0, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FileReadService:
    """Bounded discovery and versioned reads for one bound workspace.

    Construct with the workspace's `CoordinationService`; `root` defaults to the
    workspace root and `case_insensitive` overrides filesystem case detection so
    both platform behaviours can be exercised on one host.
    """

    def __init__(
        self,
        service: CoordinationService,
        *,
        root: Optional[str] = None,
        case_insensitive: Optional[bool] = None,
        claims: Optional[FileClaimService] = None,
    ) -> None:
        self.service = service
        self.workspace = service.workspace
        self.root = os.path.realpath(str(root) if root is not None else self.workspace.root)
        self._case = case_insensitive
        self.claims = claims or FileClaimService(
            service, root=self.root, case_insensitive=case_insensitive
        )

    # -- discovery ---------------------------------------------------------

    def list(
        self,
        prefix: Optional[str] = None,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ListPage:
        """Enumerate workspace entries under `prefix` (or the root), bounded.

        Ordering is by canonical path, so a page is deterministic. Directories,
        regular files, protected paths and symlinks are distinguished; protected
        metadata (`.arbite`/`.git`) and symlinks are excluded and *counted*, and
        the result's markers say so. A file's whole-file version is included
        unless the file is too large to digest, in which case `version_omitted`
        is set for that entry."""
        absolute_prefix, relative_prefix = self._resolve_prefix(prefix, mode=path_policy.MODE_LIST)
        offset = _bound_offset(offset)
        limit, limit_capped = _bound_limit(limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
        walk = self._walk(absolute_prefix, relative_prefix)

        ordered = sorted(walk.entries, key=lambda item: item[0])
        total = len(ordered)
        window = ordered[offset : offset + limit]

        entries = [self._list_entry(rel, kind) for rel, kind in window]
        truncated = (offset + len(window)) < total
        next_offset = None
        if truncated and not walk.scan_limit_reached:
            next_offset = offset + len(window)

        markers = []
        if offset:
            markers.append(MARKER_OFFSET_ADVANCED)
        if limit_capped:
            markers.append(MARKER_LIMIT_CAPPED)
        if truncated:
            markers.append(MARKER_OUTPUT_TRUNCATED)
        if walk.scan_limit_reached:
            markers.append(MARKER_SCAN_LIMIT_REACHED)
        if walk.excluded_protected:
            markers.append(MARKER_PROTECTED_EXCLUDED)
        if walk.skipped_symlinks:
            markers.append(MARKER_SYMLINKS_SKIPPED)
        if any(entry.version_omitted for entry in entries):
            markers.append(MARKER_VERSION_OMITTED)

        return ListPage(
            root=self.root,
            prefix=relative_prefix,
            entries=entries,
            offset=offset,
            limit=limit,
            total_entries=total,
            truncated=truncated,
            next_offset=next_offset,
            limit_capped=limit_capped,
            scan_limit_reached=walk.scan_limit_reached,
            excluded_protected=walk.excluded_protected,
            skipped_symlinks=walk.skipped_symlinks,
            markers=markers,
        )

    def search(
        self,
        pattern: str,
        prefix: Optional[str] = None,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> SearchPage:
        """Text/path discovery for `pattern` (a regular expression).

        A file matches when its canonical path matches, or when any decoded line
        matches. Initial search is deliberately text/path discovery only: no
        import tracing, dependency analysis or semantic reads. Binary,
        non-UTF-8 and oversized files are *reported as skipped* with a reason
        rather than silently skipped, and the page carries deterministic
        `next_offset` pagination plus explicit truncation markers."""
        regex = self._compile(pattern)
        absolute_prefix, relative_prefix = self._resolve_prefix(
            prefix, mode=path_policy.MODE_LIST
        )
        offset = _bound_offset(offset)
        limit, limit_capped = _bound_limit(limit, DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT)
        walk = self._walk(absolute_prefix, relative_prefix, directories=False)

        files = sorted(rel for rel, kind in walk.entries if kind == "file")
        matches: List[SearchMatch] = []
        skipped: List[dict] = []
        skipped_truncated = False
        scanned = 0
        matched_files = 0
        scan_limit_reached = walk.scan_limit_reached
        hit_cap = False

        for relative in files:
            if scanned >= MAX_SCAN_ENTRIES:
                scan_limit_reached = True
                break
            scanned += 1
            absolute = os.path.join(self.root, *relative.split("/"))
            file_matched = False

            if regex.search(relative):
                file_matched = True
                version, version_omitted, data = self._version_and_bytes(absolute)
                matches.append(
                    SearchMatch(
                        path=relative,
                        version=version,
                        version_omitted=version_omitted,
                    )
                )

            if len(matches) >= offset + limit + 1:
                hit_cap = True
                break

            try:
                size = os.path.getsize(absolute)
            except OSError:
                continue
            if size > MAX_SEARCH_FILE_BYTES:
                if not file_matched:
                    skipped_truncated = self._add_skipped(
                        skipped, relative, SKIP_TOO_LARGE, skipped_truncated
                    )
                continue

            data = _read_bytes(absolute)
            kind = _classify(data)
            if kind in ("utf-16", "utf-32", "binary"):
                if not file_matched:
                    skipped_truncated = self._add_skipped(
                        skipped, relative, SKIP_BINARY, skipped_truncated
                    )
                continue

            text = data.decode("utf-8-sig")
            digest = digest_of_bytes(data)
            for number, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                file_matched = True
                shown, text_truncated = _truncate_line(line)
                matches.append(
                    SearchMatch(
                        path=relative,
                        version=digest,
                        line=number,
                        text=shown,
                        text_truncated=text_truncated,
                    )
                )
                if len(matches) >= offset + limit + 1:
                    hit_cap = True
                    break
            if file_matched:
                matched_files += 1
            if hit_cap:
                break

        truncated = len(matches) > offset + limit
        returned = matches[offset : offset + limit]
        next_offset = None
        if truncated and not scan_limit_reached:
            next_offset = offset + len(returned)

        markers = []
        if offset:
            markers.append(MARKER_OFFSET_ADVANCED)
        if limit_capped:
            markers.append(MARKER_LIMIT_CAPPED)
        if truncated:
            markers.append(MARKER_OUTPUT_TRUNCATED)
        if scan_limit_reached:
            markers.append(MARKER_SCAN_LIMIT_REACHED)
        if walk.excluded_protected:
            markers.append(MARKER_PROTECTED_EXCLUDED)
        if walk.skipped_symlinks:
            markers.append(MARKER_SYMLINKS_SKIPPED)
        if skipped:
            markers.append(MARKER_CONTENT_NOT_SCANNED)
        if skipped_truncated:
            markers.append(MARKER_SKIPPED_TRUNCATED)
        if any(match.version_omitted for match in returned):
            markers.append(MARKER_VERSION_OMITTED)

        return SearchPage(
            root=self.root,
            prefix=relative_prefix,
            pattern=str(pattern),
            matches=returned,
            offset=offset,
            limit=limit,
            scanned_files=scanned,
            matched_files=matched_files,
            truncated=truncated,
            next_offset=next_offset,
            limit_capped=limit_capped,
            scan_limit_reached=scan_limit_reached,
            excluded_protected=walk.excluded_protected,
            skipped_symlinks=walk.skipped_symlinks,
            skipped=skipped,
            skipped_truncated=skipped_truncated,
            markers=markers,
        )

    # -- reads -------------------------------------------------------------

    def read(
        self,
        attempt,
        path: str,
        *,
        lines: Optional[Tuple[int, int]] = None,
        actor: Optional[Actor] = None,
        fail_if_busy: bool = False,
        version_only: bool = False,
    ) -> ReadReceipt:
        """Serve `path` (or a line range of it) with an explicit write receipt.

        Does not take an exclusive lock and does not acquire ownership. A file
        held by another attempt is still served, but the receipt is non-writable
        and names the holder; with `fail_if_busy` that becomes a `file_busy`
        refusal before any bytes are returned.

        `version_only=True` records the observation (whole-file digest, claim
        generation and mutation sequence, write authorization) but serves no
        content, so it works for binary and non-UTF-8 files the text surface
        refuses. It is the read token a whole-file replacement of such a file
        presents. It cannot be combined with `lines`.
        """
        if version_only and lines is not None:
            raise UnsupportedCoordination(
                "a version-only read serves no content, so it takes no line range",
                details={"path": str(path), "reason": "version_only_with_lines"},
            )
        target = path_policy.resolve_target(
            self.root, path, mode=path_policy.MODE_READ, case_insensitive=self._case
        )
        holder = self.claims.claim_for(target.relative)
        mine = bool(
            holder is not None
            and holder.attempt_id == attempt.id
            and holder.ticket_id == attempt.ticket_id
        )
        busy = holder is not None and not mine

        if busy and fail_if_busy:
            raise FileBusy(
                f"path {target.relative!r} is held by ticket {holder.ticket_id} "
                f"(attempt {holder.attempt_id}, generation {holder.generation}); "
                "fail-if-busy was requested, so no bytes were served",
                details=_busy_owner_dict(holder),
            )

        data = _read_bytes(target.absolute)
        digest = digest_of_bytes(data)
        size = len(data)
        if version_only:
            # No content is served, so any encoding (and any size) is fine: the
            # receipt is the whole-file version, which is all a replacement needs.
            encoding = _classify(data)
            text = ""
            newline = _newline_shape(data) if encoding in ("utf-8", "utf-8-sig") else NEWLINE_NONE
        else:
            text, encoding, newline = _decode(data)

        if not version_only and lines is None and size > MAX_READ_BYTES:
            raise UnsupportedCoordination(
                f"{target.relative!r} is {size} bytes, above the whole-file read limit "
                f"of {MAX_READ_BYTES}; request a line range with --lines START:END",
                details={
                    "path": target.relative,
                    "size": size,
                    "limit": MAX_READ_BYTES,
                    "digest": digest,
                    "reason": "file_too_large",
                },
            )

        if version_only:
            content, returned_range, clamped, empty, lines_total = "", None, False, False, 0
        else:
            content, returned_range, clamped, empty, lines_total = _extract_range(text, lines)

        now = self.service.now()
        authorizing = bool(
            mine
            and attempt.is_active
            and digest == holder.observed_version
            and now >= holder.acquired
        )
        if authorizing:
            reason = None
        elif mine and not attempt.is_active:
            reason = REASON_ATTEMPT_INACTIVE
        elif mine and digest != holder.observed_version:
            reason = REASON_CLAIM_VERSION_MISMATCH
        elif mine and now < holder.acquired:
            reason = REASON_NOT_FRESH
        elif busy:
            reason = REASON_FOREIGN_CLAIM
        else:
            reason = REASON_NO_CLAIM

        observation = self.service.record_read(
            attempt,
            target.relative,
            data,
            claim=holder if mine and attempt.is_active else None,
            line_range=lines,
            actor=actor,
            authorize=authorizing,
            observed_claim_generation=holder.generation if holder is not None else None,
            version_only=version_only,
        )

        return ReadReceipt(
            workspace_id=self.workspace.id,
            path=target.relative,
            attempt_id=attempt.id,
            actor=(actor or self.service.actor).id,
            operation_id=observation.operation_id,
            read_token=observation.id,
            whole_file_digest=digest,
            size=size,
            encoding=encoding,
            newline=newline,
            text=content,
            content_complete=lines is None and not version_only,
            lines_total=lines_total,
            line_range_requested=tuple(lines) if lines is not None else None,
            line_range_returned=returned_range,
            range_clamped=clamped,
            range_empty=empty,
            write_authorizing=authorizing,
            non_writable=not authorizing,
            non_writable_reason=reason,
            claim_generation=holder.generation if holder is not None else None,
            claim_observed_version=holder.observed_version if holder is not None else None,
            busy=busy,
            busy_owner=_busy_owner_dict(holder) if busy else None,
            observed_at=observation.observed_at,
            version_only=version_only,
        )

    def read_observation(self, token: str) -> ReadObservation:
        """The `ReadObservation` a `read_token` names, or `CoordinationNotFound`.

        This is the lookup C07 (and any caller checking a token) uses before
        calling `application.require_write_authorization`."""
        with self.service.store.transaction(write=False) as tx:
            observation = tx.get("read_observation", token)
        if observation is None:
            raise CoordinationNotFound(
                f"no read observation {token!r} is recorded",
                details={"read_token": token},
            )
        return observation

    # -- probe -------------------------------------------------------------

    def probe(self, attempt, path: str) -> ProbeReceipt:
        """Inspect `path` for a safe create. Records nothing and owns nothing.

        A safe create requires the path to be absent, in-root and canonical, and
        either unclaimed or already claimed by this attempt. The create's authority
        is an `ABSENT`-versioned claim (C04); the probe only *establishes* that the
        destination is safe, so `observation_recorded` and `claim_acquired` are
        always False."""
        try:
            target = path_policy.resolve_target(
                self.root, path, mode=path_policy.MODE_CLAIM, case_insensitive=self._case
            )
        except CoordinationNotFound:
            canonical = canonical_relative_path(path)
            raise UnsupportedCoordination(
                f"cannot probe {canonical!r}: its parent directory does not exist; "
                "arbite does not create intermediate directories",
                details={"path": canonical, "reason": "missing_parent"},
            )
        holder = self.claims.claim_for(target.relative)
        mine = bool(
            holder is not None
            and holder.attempt_id == attempt.id
            and holder.ticket_id == attempt.ticket_id
        )
        busy = holder is not None and not mine
        exists = target.exists
        safe = (not exists) and (not busy)
        if exists:
            action = (
                "the path exists; read it and claim it before editing, or use remove/"
                "rename to replace it"
            )
        elif busy:
            action = (
                f"the absent path is already claimed by ticket {holder.ticket_id} "
                f"(attempt {holder.attempt_id}); wait for release or choose another path"
            )
        elif mine:
            action = (
                "the path is absent and already claimed by this attempt (version ABSENT); "
                "create it with a whole-file write"
            )
        else:
            action = (
                "the path is absent and unclaimed; claim it (observed_version ABSENT), "
                "then create it with a whole-file write"
            )
        return ProbeReceipt(
            workspace_id=self.workspace.id,
            path=target.relative,
            attempt_id=attempt.id,
            exists=exists,
            version=target.digest,
            size=os.path.getsize(target.absolute) if exists else None,
            safe_to_create=safe,
            missing_parent=None,
            busy=busy,
            busy_owner=_busy_owner_dict(holder) if busy else None,
            claim=(
                {
                    "claim_id": holder.id,
                    "generation": holder.generation,
                    "observed_version": holder.observed_version,
                }
                if mine
                else None
            ),
            required_claim_version=ABSENT if not exists else None,
            next_action=action,
        )

    # -- internals ---------------------------------------------------------

    def _resolve_prefix(self, prefix: Optional[str], *, mode: str):
        """`(absolute, canonical relative)` for a discovery prefix.

        No prefix (or `.`) means the workspace root itself. Anything else goes
        through the canonical pipeline, so traversal, protected metadata, symlink
        components and special files are refused exactly as they are for a read."""
        if prefix is None or str(prefix).strip() in ("", "."):
            return self.root, ""
        target = path_policy.resolve_target(
            self.root, prefix, mode=mode, case_insensitive=self._case
        )
        return target.absolute, target.relative

    def _walk(self, absolute_prefix: str, relative_prefix: str, *, directories=True) -> _Walk:
        """Bounded, deterministic enumeration under `absolute_prefix`.

        Deterministic because every directory's children are sorted before use and
        the final list is sorted by canonical path. Bounded because at most
        `MAX_SCAN_ENTRIES` entries are considered; when the bound bites,
        `scan_limit_reached` is set (and the page reports no `next_offset`, since
        a resumed page could not be reproduced)."""
        if os.path.isfile(absolute_prefix):
            return _Walk([(relative_prefix, "file")], 0, 0, False)

        entries: List[Tuple[str, str]] = []
        excluded = 0
        symlinks = 0
        scanned = 0
        reached = False

        for dirpath, dirnames, filenames in os.walk(absolute_prefix, followlinks=False):
            dirnames.sort()
            filenames.sort()
            relative_dir = os.path.relpath(dirpath, self.root)

            kept_dirs = []
            for name in dirnames:
                if scanned >= MAX_SCAN_ENTRIES:
                    reached = True
                    break
                scanned += 1
                relative = _join_rel(relative_dir, name)
                if is_protected_path(relative):
                    excluded += 1
                    continue
                absolute = os.path.join(dirpath, name)
                if os.path.islink(absolute):
                    symlinks += 1
                    continue
                kept_dirs.append(name)
                if directories:
                    entries.append((relative, "directory"))
            dirnames[:] = kept_dirs
            if reached:
                break

            for name in filenames:
                if scanned >= MAX_SCAN_ENTRIES:
                    reached = True
                    break
                scanned += 1
                relative = _join_rel(relative_dir, name)
                if is_protected_path(relative):
                    excluded += 1
                    continue
                absolute = os.path.join(dirpath, name)
                if os.path.islink(absolute):
                    symlinks += 1
                    continue
                entries.append((relative, "file"))
            if reached:
                break

        return _Walk(entries, excluded, symlinks, reached)

    def _list_entry(self, relative: str, kind: str) -> DiscoveryEntry:
        if kind == "directory":
            return DiscoveryEntry(path=relative, kind="directory")
        absolute = os.path.join(self.root, *relative.split("/"))
        try:
            size = os.path.getsize(absolute)
        except OSError:
            return DiscoveryEntry(path=relative, kind="file", classification="unknown")
        if size > MAX_VERSION_BYTES:
            return DiscoveryEntry(
                path=relative,
                kind="file",
                size=size,
                version=None,
                version_omitted=True,
                classification="unknown",
            )
        data = _read_bytes(absolute)
        classification = "text" if _classify(data) in ("utf-8", "utf-8-sig") else "binary"
        return DiscoveryEntry(
            path=relative,
            kind="file",
            size=size,
            version=digest_of_bytes(data),
            classification=classification,
        )

    def _version_and_bytes(self, absolute: str):
        """`(version, version_omitted, data)` for a path-only search match."""
        try:
            size = os.path.getsize(absolute)
        except OSError:
            return None, True, b""
        if size > MAX_VERSION_BYTES:
            return None, True, b""
        data = _read_bytes(absolute)
        return digest_of_bytes(data), False, data

    @staticmethod
    def _add_skipped(skipped: List[dict], path: str, reason: str, truncated: bool) -> bool:
        if len(skipped) < MAX_SKIPPED_REPORTED:
            skipped.append({"path": path, "reason": reason})
            return truncated
        return True

    @staticmethod
    def _compile(pattern: str):
        text = str(pattern or "")
        if not text:
            raise UnsupportedCoordination("a search pattern is required")
        try:
            return re.compile(text)
        except re.error as error:
            raise UnsupportedCoordination(
                f"search pattern {pattern!r} is not a valid regular expression: {error}",
                details={"pattern": pattern},
            )


def _join_rel(parent: str, name: str) -> str:
    if parent in ("", "."):
        return name
    return parent.replace(os.sep, "/").rstrip("/") + "/" + name


def _truncate_line(line: str):
    """`(shown, truncated)` for a matched line, capping length explicitly."""
    if len(line) <= MAX_MATCH_LINE_CHARS:
        return line, False
    return line[:MAX_MATCH_LINE_CHARS], True


def _extract_range(text: str, lines: Optional[Tuple[int, int]]):
    """`(content, returned_range, clamped, empty, lines_total)` for a read.

    Without `lines` the whole text is returned. With `lines`, the requested
    1-based inclusive window is returned joined by `\\n`; an END past the file is
    clamped (and `clamped` set) and a START past the file yields empty content
    with `empty` set -- both explicit, never a silent partial view."""
    all_lines = text.splitlines()
    total = len(all_lines)
    if lines is None:
        return text, None, False, False, total
    start, end = int(lines[0]), int(lines[1])
    if start > total:
        return "", (start, start - 1), True, True, total
    effective_end = min(end, total)
    returned = (start, effective_end)
    content = "\n".join(all_lines[start - 1 : effective_end])
    return content, returned, effective_end != end, False, total


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "DiscoveryEntry",
    "FileReadService",
    "ListPage",
    "MARKER_CONTENT_NOT_SCANNED",
    "MARKER_LIMIT_CAPPED",
    "MARKER_OFFSET_ADVANCED",
    "MARKER_OUTPUT_TRUNCATED",
    "MARKER_PROTECTED_EXCLUDED",
    "MARKER_SCAN_LIMIT_REACHED",
    "MARKER_SKIPPED_TRUNCATED",
    "MARKER_SYMLINKS_SKIPPED",
    "MARKER_VERSION_OMITTED",
    "MAX_LIST_LIMIT",
    "MAX_MATCH_LINE_CHARS",
    "MAX_READ_BYTES",
    "MAX_SCAN_ENTRIES",
    "MAX_SEARCH_FILE_BYTES",
    "MAX_SEARCH_LIMIT",
    "MAX_VERSION_BYTES",
    "NEWLINE_CRLF",
    "NEWLINE_LF",
    "NEWLINE_NONE",
    "ProbeReceipt",
    "REASON_CLAIM_VERSION_MISMATCH",
    "REASON_FOREIGN_CLAIM",
    "REASON_NO_CLAIM",
    "ReadReceipt",
    "SKIP_BINARY",
    "SKIP_NOT_UTF8",
    "SKIP_TOO_LARGE",
    "SearchMatch",
    "SearchPage",
    "parse_line_range",
]
