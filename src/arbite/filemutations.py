"""Whole-file writes, exact targeted edits, removal and rename (C07 and C08).

This is the mutation half of the shared-directory file proxy. It sits above three
already-merged pieces and adds no new durability machinery of its own:

- `arbite.filereads` -- the versioned read that mints the `read_token` a mutation
  must present (`read_observation` + `ReadReceipt`).
- `arbite.application.require_write_authorization` -- the guard that decides
  whether an observation authorizes a write under the *current* claim.
- `arbite.mutation.MutationEngine` -- the recoverable intent/artifact journal that
  performs the actual filesystem replacement. Its existing digest check against
  the claim's `observed_version` is the "current whole-file version" check; C07
  wires the read token through an `authorize` hook the engine calls *inside* the
  operation lock, after the holder guard and before any bytes move.

The contract, made executable here:

- **A write needs a fresh read token.** Replacing an existing file requires a
  `read_token` recorded by this attempt after it claimed the path. A pre-claim
  read, a foreign read, an old generation, or already-consumed observation is
  refused with `stale_read` and no bytes change. The engine separately refuses if
  the file's current digest no longer matches the claim's recorded version, so an
  outside write is caught even without a token -- but a token is still required so
  that claiming a file does not, by itself, authorize overwriting it.
- **A create needs an absent-path claim.** No read can be served for an absent
  path (v1 reads existing regular files only), so the absent-path token is the
  claim whose `observed_version` is `ABSENT`: the engine enforces
  `observed == held.observed_version == ABSENT`, and a caller that presents a read
  token for a create has that token refused (its digest could never equal `ABSENT`).
- **Targeted edits are an ordered batch of exact substitutions.** Each edit names
  an exact `old` string, its replacement `new`, and an explicit `occurrence` rule
  (`unique` by default, `all`, `first`, `last`, `nth`). Ambiguous (more matches
  than allowed), absent (no match) or overlapping selections reject the WHOLE
  batch before any byte changes -- there is no fuzzy matching, no AST editing and
  no partial application. The batch is applied to the validated in-memory version
  and the engine replaces the file once.
- **Text edits preserve untouched bytes and newline conventions.** The decode /
  encode policy is `filereads`' (UTF-8 and UTF-8-with-BOM only; binary/UTF-16 are
  refused). The `old`/`new` patterns -- not the file -- are normalised to the
  file's newline shape, so a CRLF file keeps its CRLF bytes everywhere the edit
  did not touch and inserted text follows the same convention.
- **Binary whole-file writes are byte payloads.** The shell reads bytes and the
  engine stores digests; no textual diff is produced or required. Replacing an
  *existing* binary file needs a read token like any other replacement: the text
  surface cannot serve its bytes, so the token is a version-only read
  (`FileReadService.read(..., version_only=True)`, `arbite file read PATH
  --version-only`), which records the whole-file version under the claim.
  Permissions are the engine's existing policy: a replaced file keeps its
  supported mode.
- **Removal and rename are the same journal, not shell operations (C08).** A
  removal needs the same fresh read token a replacement write needs and stores the
  deleted bytes before unlinking; a rename needs the source token, claims BOTH the
  source and the destination (all-or-nothing), and requires the destination to be
  ABSENT or to be named by its explicit version. A binary source (which the text
  surface cannot serve) takes a version-only read token, exactly as a binary
  replacement does: holding the claim never authorizes a change by itself. An
  existing destination's bytes
  are stored as evidence before it is replaced, both paths land in the receipt, and
  an interruption between the two paths is completed by C05's recovery.
- **A directory is never a mutation target.** `remove` reports
  `recursive_delete_unsupported` and rename refuses a directory destination: v1 has
  no recursive deletion and no metadata (chmod/chown) proxy operations. Missing
  in-root parent directories of a create/rename destination are created safely
  (`paths.plan_missing_parents` refuses traversal, protected metadata, symlinks and
  non-directory components first); a create whose parents had to be created cannot
  have been pre-claimed, so the operation acquires the absent-path claim itself.

Nothing here is a daemon: every call is one-shot, and recovery stays where C05 put
it (the next relevant operation reconciles an interrupted intent).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

from . import artifacts, filereads
from .application import require_write_authorization
from .coordination import ABSENT, digest_of_bytes
from .errors import (
    CoordinationError,
    CoordinationNotFound,
    EditSelectionError,
    FileBusy,
    SinkError,
    StaleRead,
    UnsupportedCoordination,
)
from .fileclaims import FileClaimService
from .mutation import MutationEngine, MutationResult
from .paths import MODE_CLAIM, MODE_LIST, plan_missing_parents, resolve_target

# ---------------------------------------------------------------------------
# Occurrence rules and selection failure reasons
# ---------------------------------------------------------------------------

#: Exactly one match is required; zero is `absent`, more than one `ambiguous`.
OCCURRENCE_UNIQUE = "unique"
#: Every non-overlapping match is replaced; zero is `absent`.
OCCURRENCE_ALL = "all"
#: The first match is replaced; zero is `absent`.
OCCURRENCE_FIRST = "first"
#: The last match is replaced; zero is `absent`.
OCCURRENCE_LAST = "last"
#: The 1-based `index`-th match is replaced; an out-of-range index is `absent`.
OCCURRENCE_NTH = "nth"
OCCURRENCES = (
    OCCURRENCE_UNIQUE,
    OCCURRENCE_ALL,
    OCCURRENCE_FIRST,
    OCCURRENCE_LAST,
    OCCURRENCE_NTH,
)

#: Why an edit selection was refused. Stable vocabulary; a caller branches on it.
REASON_EDIT_ABSENT = "absent"
REASON_EDIT_AMBIGUOUS = "ambiguous"
REASON_EDIT_OVERLAPPING = "overlapping"
EDIT_REASONS = (REASON_EDIT_ABSENT, REASON_EDIT_AMBIGUOUS, REASON_EDIT_OVERLAPPING)

#: Default media type for the stored before/after evidence. Binary payloads may
#: override it, but the bytes and digests are the contract, not the label.
DEFAULT_MEDIA_TYPE = artifacts.DEFAULT_MEDIA_TYPE

#: Stable `details["reason"]` for a write/edit/remove/rename that omitted its token.
REASON_MISSING_READ_TOKEN = "missing_read_token"
#: Stable `details["reason"]` for a token presented against an absent path.
REASON_ABSENT_PATH_TOKEN = "absent_path_token_expected"
#: Stable `details["reason"]` for a create/rename destination that already exists
#: and was not authorized with an explicit destination version.
REASON_DESTINATION_EXISTS = "destination_exists"
#: Stable `details["reason"]` for a removal that targeted a directory: v1 has no
#: recursive deletion, so the whole operation is refused.
REASON_RECURSIVE_DELETE = "recursive_delete_unsupported"
#: Stable `details["reason"]` for a rename whose destination is a directory.
REASON_DIRECTORY_DESTINATION = "directory_destination_unsupported"
#: Stable `details["reason"]` for an invalid/missing parent chain on a creation.
REASON_MISSING_PARENT = "missing_parent"


# ---------------------------------------------------------------------------
# Edit batch model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """One exact substitution in a targeted-edit batch."""

    old: str
    new: str
    occurrence: str = OCCURRENCE_UNIQUE
    index: Optional[int] = None

    def to_dict(self) -> dict:
        payload = {"old": self.old, "new": self.new, "occurrence": self.occurrence}
        if self.index is not None:
            payload["index"] = self.index
        return payload

    @classmethod
    def from_mapping(cls, item, *, position: Optional[int] = None) -> "Edit":
        """Validate one payload object into an `Edit`.

        Shape/usage problems raise `UnsupportedCoordination`; they are not
        selection failures. `position` makes the message name the offending edit.
        """
        label = f"edit {position}" if position is not None else "edit"
        if not isinstance(item, dict):
            raise UnsupportedCoordination(
                f"{label} must be an object with 'old' and 'new' string fields",
                details={"edit_index": position},
            )
        if "old" not in item or "new" not in item:
            raise UnsupportedCoordination(
                f"{label} must have both 'old' and 'new' fields",
                details={"edit_index": position},
            )
        old, new = item["old"], item["new"]
        if not isinstance(old, str) or not isinstance(new, str):
            raise UnsupportedCoordination(
                f"{label} 'old' and 'new' must be strings",
                details={"edit_index": position},
            )
        occurrence = item.get("occurrence", OCCURRENCE_UNIQUE)
        if not isinstance(occurrence, str) or occurrence not in OCCURRENCES:
            raise UnsupportedCoordination(
                f"{label} occurrence {occurrence!r} is not one of "
                f"{', '.join(OCCURRENCES)}",
                details={"edit_index": position, "occurrence": occurrence},
            )
        index = item.get("index")
        if index is not None and (
            isinstance(index, bool) or not isinstance(index, int) or index < 1
        ):
            raise UnsupportedCoordination(
                f"{label} 'index' must be a 1-based integer",
                details={"edit_index": position, "index": index},
            )
        if occurrence == OCCURRENCE_NTH and index is None:
            raise UnsupportedCoordination(
                f"{label} occurrence 'nth' requires an 'index'",
                details={"edit_index": position},
            )
        return cls(old=old, new=new, occurrence=occurrence, index=index)


def parse_edits(payload) -> List[Edit]:
    """Parse a JSON-decoded edit batch: a list, or an object with `edits`."""
    if isinstance(payload, dict):
        raw = payload.get("edits")
        if raw is None:
            raise UnsupportedCoordination(
                "the edits payload object must have an 'edits' list",
                details={"payload_keys": sorted(payload.keys())},
            )
    elif isinstance(payload, list):
        raw = payload
    else:
        raise UnsupportedCoordination(
            "the edits payload must be a JSON list or an object with an 'edits' list",
            details={"payload_type": type(payload).__name__},
        )
    if not isinstance(raw, list) or not raw:
        raise UnsupportedCoordination("at least one edit is required")
    return [Edit.from_mapping(item, position=index) for index, item in enumerate(raw)]


def parse_edits_json(text: str) -> List[Edit]:
    """Parse the text of an edits payload file (or stdin) into `Edit`s."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise UnsupportedCoordination(
            f"the edits payload is not valid JSON: {error}"
        ) from error
    return parse_edits(payload)


def _find_all(text: str, needle: str) -> List[Tuple[int, int]]:
    """Every non-overlapping `(start, end)` of the exact `needle` in `text`."""
    if needle == "":
        raise EditSelectionError(
            "an empty 'old' string cannot select an exact region",
            details={"reason": REASON_EDIT_ABSENT, "match_count": 0},
        )
    spans: List[Tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return spans
        spans.append((found, found + len(needle)))
        start = found + len(needle)


def _absent(position: int, edit: Edit) -> EditSelectionError:
    return EditSelectionError(
        f"edit {position} found no exact match for {edit.old!r}",
        details={
            "reason": REASON_EDIT_ABSENT,
            "edit_index": position,
            "occurrence": edit.occurrence,
            "match_count": 0,
        },
    )


def _select(
    text: str, old: str, occurrence: str, index: Optional[int], position: int
) -> List[Tuple[int, int]]:
    """The `(start, end)` spans one edit selects, or an `EditSelectionError`."""
    edit = Edit(old=old, new="", occurrence=occurrence, index=index)
    matches = _find_all(text, old)
    if occurrence == OCCURRENCE_UNIQUE:
        if not matches:
            raise _absent(position, edit)
        if len(matches) > 1:
            raise EditSelectionError(
                f"edit {position} matched {len(matches)} times, but occurrence "
                "'unique' requires exactly one; make the selection exact or choose "
                "an explicit occurrence rule",
                details={
                    "reason": REASON_EDIT_AMBIGUOUS,
                    "edit_index": position,
                    "occurrence": occurrence,
                    "match_count": len(matches),
                },
            )
        return matches
    if occurrence == OCCURRENCE_ALL:
        if not matches:
            raise _absent(position, edit)
        return matches
    if occurrence == OCCURRENCE_FIRST:
        if not matches:
            raise _absent(position, edit)
        return matches[:1]
    if occurrence == OCCURRENCE_LAST:
        if not matches:
            raise _absent(position, edit)
        return matches[-1:]
    if occurrence == OCCURRENCE_NTH:
        if index is None:  # pragma: no cover - `Edit.from_mapping` enforces this
            raise UnsupportedCoordination(
                f"edit {position} occurrence 'nth' requires an 'index'",
                details={"edit_index": position},
            )
        if index > len(matches):
            raise _absent(position, edit)
        return [matches[index - 1]]
    raise UnsupportedCoordination(  # pragma: no cover - from_mapping guards this
        f"edit {position} has unknown occurrence rule {occurrence!r}",
        details={"edit_index": position, "occurrence": occurrence},
    )


def _newline_adapter(newline: str):
    """Return a function adapting `old`/`new` patterns to the file's newlines.

    Only the *patterns* are normalised -- never the file text -- so untouched
    bytes, including every existing CRLF, are carried through verbatim.
    """
    if newline == filereads.NEWLINE_CRLF:

        def adapt(text: str) -> str:
            return text.replace("\r\n", "\n").replace("\n", "\r\n")

        return adapt
    if newline == filereads.NEWLINE_LF:

        def adapt(text: str) -> str:
            return text.replace("\r\n", "\n")

        return adapt
    return lambda text: text


def apply_edits(data: bytes, edits: Sequence[Edit]) -> bytes:
    """Apply an exact edit batch to `data`, returning the new whole-file bytes.

    Any selection failure (`absent`, `ambiguous`, `overlapping`) or an unsupported
    encoding raises before a result is produced, so a caller can never get a
    partially-applied buffer back.
    """
    batch = list(edits)
    if not batch:
        raise UnsupportedCoordination("at least one edit is required")
    text, encoding, newline = filereads._decode(data)
    adapt = _newline_adapter(newline)

    replacements: List[Tuple[int, int, str]] = []
    for position, edit in enumerate(batch):
        old, new = adapt(edit.old), adapt(edit.new)
        spans = _select(text, old, edit.occurrence, edit.index, position)
        for start, end in spans:
            replacements.append((start, end, new))

    replacements.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(replacements, replacements[1:]):
        if current[0] < previous[1]:
            raise EditSelectionError(
                "two edits selected overlapping regions; the whole batch was "
                "refused and no bytes were changed",
                details={
                    "reason": REASON_EDIT_OVERLAPPING,
                    "region_start": current[0],
                    "region_end": current[1],
                    "overlaps_start": previous[0],
                    "overlaps_end": previous[1],
                },
            )

    pieces: List[str] = []
    cursor = 0
    for start, end, new in replacements:
        pieces.append(text[cursor:start])
        pieces.append(new)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces).encode(encoding)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FileMutationService:
    """Version-checked whole-file writes and exact targeted edits for one workspace.

    Construct with the workspace's `CoordinationService` (the caller has already
    claimed the path). `reads`, `claims` and `engine` may be injected so tests can
    share instances; otherwise they are built from the same root/case policy.
    """

    def __init__(
        self,
        service,
        *,
        root: Optional[str] = None,
        case_insensitive: Optional[bool] = None,
        reads: Optional[filereads.FileReadService] = None,
        claims: Optional[FileClaimService] = None,
        engine: Optional[MutationEngine] = None,
        fault_injector=None,
        max_artifact_bytes: int = artifacts.DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self.service = service
        self.workspace = service.workspace
        self.root = os.path.realpath(str(root) if root is not None else service.workspace.root)
        self._case = case_insensitive
        self.claims = claims or FileClaimService(
            service, root=self.root, case_insensitive=case_insensitive
        )
        self.reads = reads or filereads.FileReadService(
            service,
            root=self.root,
            case_insensitive=case_insensitive,
            claims=self.claims,
        )
        self.engine = engine or MutationEngine(
            service,
            self.claims,
            root=self.root,
            case_insensitive=case_insensitive,
            max_artifact_bytes=max_artifact_bytes,
            fault_injector=fault_injector,
        )

    # -- helpers -----------------------------------------------------------

    def _resolve(self, path: str):
        return resolve_target(
            self.root, path, mode=MODE_CLAIM, case_insensitive=self._case
        )

    @staticmethod
    def _read_bytes(absolute: str) -> bytes:
        with open(absolute, "rb") as handle:
            return handle.read()

    @staticmethod
    def _content_bytes(content) -> bytes:
        if isinstance(content, (bytes, bytearray, memoryview)):
            return bytes(content)
        raise UnsupportedCoordination(
            f"whole-file write content must be bytes, got {type(content).__name__}"
        )

    def _authorize_observation(self, attempt, observation):
        """The engine's inside-the-lock hook for a resolved read observation."""

        def authorize(held):
            require_write_authorization(observation, claim=held, attempt=attempt)

        return authorize

    @staticmethod
    def _normalize_edits(edits) -> List[Edit]:
        if isinstance(edits, (str, bytes, bytearray, dict)):
            raise UnsupportedCoordination(
                "edits must be a sequence of edit objects, not a single value"
            )
        try:
            items = list(edits)
        except TypeError as error:  # pragma: no cover - defensive
            raise UnsupportedCoordination(
                f"edits must be a sequence of edit objects: {error}"
            ) from error
        if not items:
            raise UnsupportedCoordination("at least one edit is required")
        return [
            item if isinstance(item, Edit) else Edit.from_mapping(item, position=position)
            for position, item in enumerate(items)
        ]

    # -- safe parent creation (C08) ---------------------------------------

    def _ensure_parents(self, path: str) -> List[str]:
        """Create the in-root parent directories `path` needs, and return them.

        Only the directories `paths.plan_missing_parents` reports are created, and
        only after that pure check has refused traversal, protected metadata,
        symlink components and non-directory components. Creation is `mkdir` (never
        a recursive shell), so no other path is ever touched, and an already-created
        directory (a concurrent creator) is accepted only if it really is a
        directory. Nothing here deletes anything: v1 has no recursive deletion."""
        _, missing = plan_missing_parents(self.root, path, case_insensitive=self._case)
        created: List[str] = []
        for relative in missing:
            absolute = os.path.join(self.root, *relative.split("/"))
            try:
                os.mkdir(absolute, 0o777)
            except FileExistsError:  # pragma: no cover - a concurrent creator
                if os.path.islink(absolute) or not os.path.isdir(absolute):
                    raise UnsupportedCoordination(
                        f"could not create parent directory {relative!r} for {path!r}: "
                        "a non-directory is in the way",
                        details={"path": relative, "reason": REASON_MISSING_PARENT},
                    )
                continue
            except OSError as error:
                raise SinkError(
                    f"could not create parent directory {relative!r} for {path!r}: {error}"
                ) from error
            created.append(relative)
        return created

    def _resolve_for_creation(self, path: str):
        """`(ResolvedTarget, created_directories)` for a possibly-new path.

        A missing parent is the one resolution failure a create may repair; every
        other refusal (traversal, protected path, symlink, special file) propagates
        unchanged. `resolve_target` is re-run after creation so the caller gets the
        same canonical identity the rest of the pipeline uses."""
        try:
            return self._resolve(path), []
        except UnsupportedCoordination as error:
            if "missing_parent" not in (error.details or {}):
                raise
        created = self._ensure_parents(path)
        return self._resolve(path), created

    def _directory_target(self, path: str) -> Optional[str]:
        """The canonical relative path of `path` when it is a directory, else None.

        Uses the discovery mode (which is *allowed* to name a directory) purely to
        classify a refusal, so `remove`/`rename` can report the documented
        "directories are not supported" reason instead of a generic one."""
        try:
            listed = resolve_target(
                self.root, path, mode=MODE_LIST, case_insensitive=self._case
            )
        except CoordinationError:
            return None
        return listed.relative if os.path.isdir(listed.absolute) else None

    def _resolve_for_removal(self, path: str):
        """Resolve a removal target, refusing a directory with its own reason."""
        try:
            return self._resolve(path)
        except UnsupportedCoordination as error:
            directory = self._directory_target(path)
            if directory is not None:
                raise UnsupportedCoordination(
                    f"cannot remove {directory!r}: arbite v1 never deletes a "
                    "directory (there is no recursive deletion); remove each file, "
                    "then the emptied directory with a shell command",
                    details={"path": directory, "reason": REASON_RECURSIVE_DELETE},
                ) from error
            raise

    @staticmethod
    def _claim_for(result, path: str):
        """The claim for `path` in a `ClaimSetResult` (acquired or reentrant)."""
        for claim in list(result.acquired) + list(result.reentrant):
            if claim.path == path:
                return claim
        return None

    # -- mutations ---------------------------------------------------------

    def write(
        self,
        attempt,
        path: str,
        content,
        *,
        read_token: Optional[str] = None,
        operation_id: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> MutationResult:
        """Create or replace `path` with the whole `content`.

        An existing file needs `read_token` (a write-authorizing observation for
        this attempt, recorded after it claimed the path); an absent path is a
        create authorized by the absent-path claim, and a read token presented for
        it is refused. The token is re-checked inside the engine's operation lock,
        so two mutations sharing one token cannot both succeed.

        C08 addition: a create whose parent directories do not exist yet has them
        created first (only in-root directories, never through a link), and because
        such a path could not have been claimed before the directories existed,
        this call also acquires the absent-path claim it just made possible. A
        create in an existing directory still requires the caller to have claimed
        the path, exactly as in C07.
        """
        content = self._content_bytes(content)
        target, created = self._resolve_for_creation(path)
        claim = None
        authorize = None
        if read_token is not None:
            if not target.exists:
                raise StaleRead(
                    f"a read token cannot authorize creating the absent path "
                    f"{target.relative!r}; claim the absent path (observed_version "
                    "ABSENT) and write without a read token",
                    details={
                        "path": target.relative,
                        "reason": REASON_ABSENT_PATH_TOKEN,
                    },
                )
            observation = self.reads.read_observation(read_token)
            authorize = self._authorize_observation(attempt, observation)
        elif target.exists:
            raise StaleRead(
                f"a write to the existing file {target.relative!r} requires a "
                "read token; read the file through arbite after claiming it (a "
                "binary file takes a version-only read), then write with that token",
                details={"path": target.relative, "reason": REASON_MISSING_READ_TOKEN},
            )
        elif created:
            claimed = self.claims.claim(attempt, [target.relative])
            claim = self._claim_for(claimed, target.relative)
            if self._resolve(target.relative).exists:
                # A concurrent creator won the parent-creation window: the path is no
                # longer a create, so a token is required. No bytes have changed.
                raise StaleRead(
                    f"{target.relative!r} appeared while its parent directories were "
                    "being created; no bytes changed -- read it and retry with a "
                    "token",
                    details={
                        "path": target.relative,
                        "reason": REASON_MISSING_READ_TOKEN,
                    },
                )
        result = self.engine.write(
            attempt,
            target.relative,
            content,
            claim=claim,
            operation_id=operation_id,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
            authorize=authorize,
        )
        return replace(result, created_parents=created) if created else result

    def edit(
        self,
        attempt,
        path: str,
        edits,
        *,
        read_token: Optional[str] = None,
        operation_id: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> MutationResult:
        """Replace the *existing* `path` with an exact edit batch applied once.

        The batch is matched against the file's current bytes (which must still
        match `read_token`'s observation), applied in memory, and then handed to
        the engine as a single replacement. Any absent/ambiguous/overlapping
        selection, a missing token, or drift since the read refuses the whole
        batch with no bytes changed.
        """
        batch = self._normalize_edits(edits)
        target = self._resolve(path)
        if not target.exists:
            raise CoordinationNotFound(
                f"cannot edit {target.relative!r}: the file does not exist; use a "
                "whole-file write to create it",
                details={"path": target.relative},
            )
        if read_token is None:
            raise StaleRead(
                f"an edit of {target.relative!r} requires a read token; read the "
                "file through arbite after claiming it, then edit with that token",
                details={"path": target.relative, "reason": REASON_MISSING_READ_TOKEN},
            )
        observation = self.reads.read_observation(read_token)
        current = self._read_bytes(target.absolute)
        current_digest = digest_of_bytes(current)
        if current_digest != observation.digest:
            raise StaleRead(
                f"{target.relative!r} has changed since read token {read_token!r} "
                "was recorded; re-read the file and retry (no bytes were changed)",
                details={
                    "path": target.relative,
                    "observed": current_digest,
                    "expected": observation.digest,
                },
            )
        new_content = apply_edits(current, batch)
        authorize = self._authorize_observation(attempt, observation)
        return self.engine.edit(
            attempt,
            target.relative,
            new_content,
            operation_id=operation_id,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
            authorize=authorize,
        )

    def remove(
        self,
        attempt,
        path: str,
        *,
        read_token: Optional[str] = None,
        operation_id: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> MutationResult:
        """Delete `path`, preserving its bytes as durable evidence.

        Like a replacement write, a removal needs a fresh `read_token` recorded by
        this attempt after it claimed the file, and the file must still match that
        observation (checked inside the engine's operation lock). A file the text
        read surface cannot serve (binary/UTF-16) takes a version-only read token.
        The token is consumed by the removal. A directory is refused
        with the documented `recursive_delete_unsupported` reason: v1 never deletes
        recursively. The deleted bytes are stored content-addressed before the
        unlink, so the receipt can reproduce them."""
        target = self._resolve_for_removal(path)
        if not target.exists:
            raise CoordinationNotFound(
                f"cannot remove {target.relative!r}: the file does not exist",
                details={"path": target.relative},
            )
        if read_token is None:
            raise StaleRead(
                f"a removal of {target.relative!r} requires a read token; read the "
                "file through arbite after claiming it (a binary file takes a "
                "version-only read), then remove with that token",
                details={"path": target.relative, "reason": REASON_MISSING_READ_TOKEN},
            )
        observation = self.reads.read_observation(read_token)
        authorize = self._authorize_observation(attempt, observation)
        return self.engine.remove(
            attempt,
            target.relative,
            operation_id=operation_id,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
            authorize=authorize,
        )

    def rename(
        self,
        attempt,
        source: str,
        destination: str,
        *,
        read_token: Optional[str] = None,
        dest_expected: Optional[str] = None,
        operation_id: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> MutationResult:
        """Move `source` to `destination`, owning and recording BOTH paths.

        The source needs the same fresh `read_token` a write needs (a read this
        attempt recorded after claiming the source). The destination is claimed as
        part of this call -- rename is the one operation that changes two paths, and
        the all-or-nothing claim set is what makes "both or neither" true -- and it
        must be ABSENT, or its current version must be stated explicitly in
        `dest_expected`. A destination held by another attempt is `file_busy` and
        changes nothing. An existing destination's bytes are stored as evidence
        before they are replaced. Both paths, both versions and the moved bytes stay
        in the journal and receipt, and an interruption between the two paths is
        completed by the engine's recovery, not by a shell command.
        """
        source_target = self._resolve(source)
        if not source_target.exists:
            raise CoordinationNotFound(
                f"cannot rename {source_target.relative!r}: the source does not exist",
                details={"path": source_target.relative},
            )

        created: List[str] = []
        try:
            dest_target = self._resolve(destination)
        except UnsupportedCoordination as error:
            directory = self._directory_target(destination)
            if directory is not None:
                raise UnsupportedCoordination(
                    f"cannot rename onto {directory!r}: arbite v1 has no directory "
                    "or recursive operations",
                    details={"path": directory, "reason": REASON_DIRECTORY_DESTINATION},
                ) from error
            if "missing_parent" not in (error.details or {}):
                raise
            created = self._ensure_parents(destination)
            dest_target = self._resolve(destination)

        if dest_target.relative == source_target.relative:
            raise UnsupportedCoordination(
                f"rename source and destination are the same path "
                f"{source_target.relative!r}",
                details={"path": source_target.relative},
            )
        # A destination another attempt owns is a conflict, not an authorization
        # question: refuse it by name before anything is claimed (the all-or-nothing
        # claim set below enforces the same rule a second time, inside the lock).
        holder = self.claims.claim_for(dest_target.relative)
        if holder is not None and holder.attempt_id != attempt.id:
            raise FileBusy(
                f"destination {dest_target.relative!r} is held by ticket "
                f"{holder.ticket_id} (attempt {holder.attempt_id}, generation "
                f"{holder.generation}); a rename never waits for or steals a claim",
                details={
                    "workspace_id": holder.workspace_id,
                    "path": holder.path,
                    "holder_claim": holder.id,
                    "holder_ticket": holder.ticket_id,
                    "holder_attempt": holder.attempt_id,
                    "holder_generation": holder.generation,
                    "holder_observed_version": holder.observed_version,
                    "requested_ticket": attempt.ticket_id,
                    "requested_attempt": attempt.id,
                },
            )
        if dest_target.exists and dest_expected is None:
            raise UnsupportedCoordination(
                f"destination {dest_target.relative!r} already exists "
                f"(version {dest_target.digest}); state that version explicitly as "
                "dest_expected to authorize replacing it, or choose an absent "
                "destination",
                details={
                    "path": dest_target.relative,
                    "observed": dest_target.digest,
                    "reason": REASON_DESTINATION_EXISTS,
                },
            )
        if read_token is None:
            raise StaleRead(
                f"a rename of {source_target.relative!r} requires a read token for "
                "the source; read it through arbite after claiming it (a binary "
                "file takes a version-only read)",
                details={
                    "path": source_target.relative,
                    "reason": REASON_MISSING_READ_TOKEN,
                },
            )
        observation = self.reads.read_observation(read_token)
        authorize = self._authorize_observation(attempt, observation)

        # All-or-nothing: if the destination is held by another attempt this is a
        # `file_busy` refusal and neither path is touched. Re-claiming the source is
        # idempotent and keeps the token's claim generation intact.
        claimed = self.claims.claim(
            attempt, [source_target.relative, dest_target.relative]
        )
        result = self.engine.rename(
            attempt,
            source_target.relative,
            dest_target.relative,
            source_claim=self._claim_for(claimed, source_target.relative),
            dest_claim=self._claim_for(claimed, dest_target.relative),
            operation_id=operation_id,
            source_authorize=authorize,
            # The destination rule is enforced again inside the engine's lock: ABSENT
            # when it did not exist at this point (so a racing creator is caught), or
            # the caller's explicit version.
            dest_expected=dest_expected if dest_target.exists else ABSENT,
            media_type=media_type or DEFAULT_MEDIA_TYPE,
        )
        return replace(result, created_parents=created) if created else result


__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "EDIT_REASONS",
    "Edit",
    "FileMutationService",
    "OCCURRENCE_ALL",
    "OCCURRENCE_FIRST",
    "OCCURRENCE_LAST",
    "OCCURRENCE_NTH",
    "OCCURRENCE_UNIQUE",
    "OCCURRENCES",
    "REASON_ABSENT_PATH_TOKEN",
    "REASON_DESTINATION_EXISTS",
    "REASON_DIRECTORY_DESTINATION",
    "REASON_EDIT_ABSENT",
    "REASON_EDIT_AMBIGUOUS",
    "REASON_EDIT_OVERLAPPING",
    "REASON_MISSING_PARENT",
    "REASON_MISSING_READ_TOKEN",
    "REASON_RECURSIVE_DELETE",
    "apply_edits",
    "parse_edits",
    "parse_edits_json",
]
