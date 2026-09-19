"""Filesystem-aware canonical workspace path identity (planning key C04).

`coordination.canonical_relative_path()` is *string* policy: it rejects absolute
paths and `..` traversal before anything touches a disk. This module is the next
layer down -- it turns an accepted workspace-relative path into the single
canonical identity arbite will key a file claim on, and it refuses the aliases
that would let a caller claim one file while mutating another.

What "canonical" means here, honestly stated for the supported-platform boundary:

- **The path is validated component by component against the bound root.** No
  symlink component is followed (v1 rejects them rather than following arbitrary
  links), the final target must be a regular file, and a missing parent is a
  refusal -- not a silent deep `mkdir`.
- **Special files are rejected.** A directory, FIFO, socket or device node is not
  a whole-file mutation target.
- **Existing hard-linked files are rejected as mutation targets.** A second name
  for the same inode could evade a per-path claim, so v1 refuses it instead of
  pretending aliases are equivalent.
- **Case is folded where the filesystem folds it.** On a case-insensitive volume
  (`A.py` and `a.py` are the same file) aliasing would otherwise bypass a claim,
  so the canonical identity is lower-cased and any case variant resolves to the
  same key. On a case-sensitive volume the case is preserved and `A.py`/`a.py`
  are genuinely distinct paths. Detection is a side-effect-free inode comparison
  (see `detect_case_insensitive`), not a write probe.
- **Absent paths are representable.** A creation or rename *destination* does not
  exist yet, so a claim for it carries `observed_version = ABSENT`. The parents
  must already exist and must not be symlinks.
- **Root escape is re-checked.** After resolution the absolute path is confirmed
  to sit inside the real workspace root, so a platform quirk cannot smuggle a
  path outside even though traversal and symlinks are already refused.

This module deliberately reads no coordination state and writes nothing: it is a
pure function of the workspace tree plus the path string. It does read a file's
bytes to compute the version digest a claim records; that read is not a claim and
confers no write authority (see `arbite.application`'s guards).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Optional

from .coordination import (
    ABSENT,
    canonical_relative_path,
    digest_of_bytes,
    is_protected_path,
)
from .errors import CoordinationNotFound, UnsupportedCoordination

#: A mutation target: an existing regular file, or an absent creation/rename
#: destination whose parents exist. Hard-linked existing files are refused.
MODE_CLAIM = "claim"
#: A read target: must exist as a regular file (hard links are fine to read).
MODE_READ = "read"
#: Releasing a claim: only the canonical *string* identity is needed, so the
#: target need not exist and no symlink/special-file question is raised -- a
#: claim is keyed by the path string that was recorded when it was acquired.
MODE_RELEASE = "release"
#: A discovery prefix (planning key C06): a directory to enumerate, or a single
#: regular file to report. This is the canonical pipeline C06's list/search reuse
#: so a discovery prefix cannot traverse, address protected metadata or follow a
#: symlink any more than a claim/read can. It accepts a *directory* as its final
#: component (unlike `read`/`claim`) and, like `read`, requires the target to
#: exist; a not-yet-existing prefix is `CoordinationNotFound`, not a creation.
MODE_LIST = "list"

_MODES = (MODE_CLAIM, MODE_READ, MODE_RELEASE, MODE_LIST)
#: Modes whose final component may be a directory rather than a regular file.
_DIRECTORY_MODES = (MODE_LIST,)

#: Per-process cache of case-sensitivity detection keyed by real workspace root.
#: Filesystem case behaviour does not change under a running process's feet in
#: any way worth re-probing for; a test injects `case_insensitive` explicitly.
_CASE_CACHE: dict = {}


@dataclass(frozen=True)
class ResolvedTarget:
    """The canonical identity of one workspace path.

    `relative` is the canonical key a `FileClaim` stores; `absolute` is the
    on-disk path (its casing is what actually exists, even when `relative` was
    folded lower-case); `digest` is the whole-file version or `ABSENT`.
    """

    relative: str
    absolute: str
    exists: bool
    digest: str
    case_folded: bool = False
    mode: str = MODE_CLAIM

    @property
    def is_creation(self) -> bool:
        """True for an absent creation/rename destination (a probe receipt)."""
        return not self.exists


def detect_case_insensitive(root) -> bool:
    """Whether the filesystem holding `root` treats case variants as one file.

    Side-effect free and inode-based: an existing entry whose case-swapped name
    resolves to the *same* device/inode is proof of a case-insensitive volume.
    On Windows `os.path.normcase` already tells us; everywhere else the probe is
    an observation, not a created temp file, so validation never mutates the
    workspace. The answer is cached per real root for the life of the process.
    """
    real = os.path.realpath(str(root))
    cached = _CASE_CACHE.get(real)
    if cached is not None:
        return cached
    answer = _probe_case_insensitive(real)
    _CASE_CACHE[real] = answer
    return answer


def _probe_case_insensitive(real_root: str) -> bool:
    if os.path.normcase("A") != "A":  # Windows: normcase lower-cases
        return True
    candidates = []
    base = os.path.basename(real_root)
    if base:
        candidates.append((real_root, base))
    try:
        with os.scandir(real_root) as it:
            for index, entry in enumerate(it):
                if index >= 64:
                    break
                candidates.append((entry.path, entry.name))
    except OSError:
        pass
    for path, name in candidates:
        if not any(ch.isalpha() for ch in name):
            continue
        swapped = name.swapcase()
        if swapped == name:
            continue
        alternate = os.path.join(os.path.dirname(path), swapped)
        try:
            if not os.path.exists(alternate):
                continue
            first = os.stat(path)
            second = os.stat(alternate)
        except OSError:
            continue
        if (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino):
            return True
    return False


def resolve_target(
    root,
    path,
    *,
    mode: str = MODE_CLAIM,
    case_insensitive: Optional[bool] = None,
) -> ResolvedTarget:
    """Canonicalise `path` against `root`, refusing the documented aliases.

    Raises `UnsupportedCoordination` for a protected/traversal path, a symlink
    component, a special file, a hard-linked mutation target or a missing parent;
    `CoordinationNotFound` when a `read` target does not exist. `case_insensitive`
    overrides detection (used by tests to exercise both platforms on one host).
    """
    if mode not in _MODES:
        raise UnsupportedCoordination(
            f"unknown path mode {mode!r} (valid: {', '.join(_MODES)})"
        )
    canonical = canonical_relative_path(path)
    if is_protected_path(canonical):
        raise UnsupportedCoordination(
            f"{canonical!r} addresses protected arbite/.git metadata and is never "
            "a workspace file target",
            details={"path": canonical},
        )

    real_root = os.path.realpath(str(root))
    insensitive = (
        detect_case_insensitive(real_root) if case_insensitive is None else bool(case_insensitive)
    )
    parts = canonical.split("/")

    if mode == MODE_RELEASE:
        folded = "/".join(part.lower() for part in parts) if insensitive else canonical
        return ResolvedTarget(
            relative=folded,
            absolute=os.path.join(real_root, *parts),
            exists=False,
            digest=ABSENT,
            case_folded=folded != canonical,
            mode=mode,
        )

    current = real_root
    canonical_parts: list = []
    case_folded = False
    for index, component in enumerate(parts):
        last = index == len(parts) - 1
        matched, folded = _match_component(current, component, insensitive)
        if matched is None:
            if last and mode == MODE_CLAIM:
                name = component.lower() if insensitive else component
                canonical_parts.append(name)
                absolute = os.path.join(current, component)
                _assert_within_root(real_root, absolute)
                return ResolvedTarget(
                    relative="/".join(canonical_parts),
                    absolute=absolute,
                    exists=False,
                    digest=ABSENT,
                    case_folded=case_folded or name != component,
                    mode=mode,
                )
            if last:
                raise CoordinationNotFound(
                    f"no file at {canonical!r} in the workspace root",
                    details={"path": canonical},
                )
            raise UnsupportedCoordination(
                f"parent directory {component!r} of {canonical!r} does not exist; "
                "arbite does not create intermediate directories implicitly",
                details={"path": canonical, "missing_parent": component},
            )

        canonical_parts.append(matched.lower() if insensitive else matched)
        case_folded = case_folded or folded
        candidate = os.path.join(current, matched)

        if os.path.islink(candidate):
            raise UnsupportedCoordination(
                f"{canonical!r} has a symlink component ({component!r}); arbite v1 "
                "rejects symlinks rather than following arbitrary links",
                details={"path": canonical, "symlink_component": component},
            )

        if last:
            info = os.lstat(candidate)
            if stat.S_ISDIR(info.st_mode):
                if mode not in _DIRECTORY_MODES:
                    raise UnsupportedCoordination(
                        f"{canonical!r} is a directory, not a regular file",
                        details={"path": canonical},
                    )
                _assert_within_root(real_root, candidate)
                # A directory has no whole-file version: `digest` is ABSENT because
                # there are no bytes to digest, and discovery reports `kind` instead.
                return ResolvedTarget(
                    relative="/".join(canonical_parts),
                    absolute=candidate,
                    exists=True,
                    digest=ABSENT,
                    case_folded=case_folded,
                    mode=mode,
                )
            if not stat.S_ISREG(info.st_mode):
                raise UnsupportedCoordination(
                    f"{canonical!r} is not a regular file (special files are not "
                    "whole-file mutation targets)",
                    details={"path": canonical},
                )
            if mode == MODE_CLAIM and info.st_nlink > 1:
                raise UnsupportedCoordination(
                    f"{canonical!r} has {info.st_nlink} hard links; a second name for "
                    "the same inode could evade a per-path claim, so arbite v1 refuses "
                    "hard-linked mutation targets",
                    details={"path": canonical, "link_count": info.st_nlink},
                )
            _assert_within_root(real_root, candidate)
            with open(candidate, "rb") as handle:
                content = handle.read()
            return ResolvedTarget(
                relative="/".join(canonical_parts),
                absolute=candidate,
                exists=True,
                digest=digest_of_bytes(content),
                case_folded=case_folded,
                mode=mode,
            )

        if not os.path.isdir(candidate):
            raise UnsupportedCoordination(
                f"path component {component!r} of {canonical!r} is not a directory",
                details={"path": canonical, "component": component},
            )
        current = candidate

    raise AssertionError("resolve_target fell through without resolving")  # pragma: no cover


def plan_missing_parents(
    root,
    path,
    *,
    case_insensitive: Optional[bool] = None,
) -> tuple:
    """Validate a creation target and report the in-root directories it needs.

    This is the *policy* half of "safe parent creation" (planning key C08): it
    never creates anything. It applies the same canonical pipeline as
    `resolve_target` -- path traversal, protected arbite/.git metadata, symlink
    components, and a component that exists but is not a directory are all
    refused -- so a parent chain can never be created outside the root or through
    a link, and returns the directories a caller may then `mkdir`.

    Returns `(canonical_relative, [relative_directory, ...])`, parents before
    children. Only *parent* components are reported: the final component is the
    file (or rename destination) itself, which the mutation creates, never this
    helper.
    """
    canonical = canonical_relative_path(path)
    if is_protected_path(canonical):
        raise UnsupportedCoordination(
            f"{canonical!r} addresses protected arbite/.git metadata and is never "
            "a workspace file target",
            details={"path": canonical},
        )

    real_root = os.path.realpath(str(root))
    insensitive = (
        detect_case_insensitive(real_root) if case_insensitive is None else bool(case_insensitive)
    )
    parts = canonical.split("/")
    current = real_root
    canonical_parts: list = []
    missing: list = []
    for index, component in enumerate(parts):
        last = index == len(parts) - 1
        matched, _folded = _match_component(current, component, insensitive)
        if matched is None:
            # `component` is absent, so everything below it is absent too: plan each
            # parent directory (all but the final component) in creation order.
            for offset, name in enumerate(parts[index:], start=index):
                canonical_parts.append(name.lower() if insensitive else name)
                if offset < len(parts) - 1:
                    absolute = os.path.join(real_root, *canonical_parts)
                    _assert_within_root(real_root, absolute)
                    missing.append("/".join(canonical_parts))
            break

        candidate = os.path.join(current, matched)
        if os.path.islink(candidate):
            raise UnsupportedCoordination(
                f"{canonical!r} has a symlink component ({component!r}); arbite v1 "
                "rejects symlinks rather than following arbitrary links",
                details={"path": canonical, "symlink_component": component},
            )
        if not last and not os.path.isdir(candidate):
            raise UnsupportedCoordination(
                f"path component {component!r} of {canonical!r} is not a directory",
                details={"path": canonical, "component": component},
            )
        _assert_within_root(real_root, candidate)
        canonical_parts.append(matched.lower() if insensitive else matched)
        current = candidate
    return "/".join(canonical_parts), missing


def _match_component(parent: str, name: str, insensitive: bool):
    """`(on_disk_name, folded)` for `name` under `parent`, or `(None, False)`.

    An exact-case entry always wins. On a case-insensitive volume a differing-case
    sibling is the same file, so it is returned as the on-disk spelling (folded).
    On a case-sensitive volume a differing-case sibling is a *different* path, so
    it is reported absent -- which is what keeps `A.py` and `a.py` distinct there.
    """
    exact = os.path.join(parent, name)
    if os.path.lexists(exact):
        return name, False
    if not insensitive:
        return None, False
    try:
        with os.scandir(parent) as it:
            for entry in it:
                if entry.name.lower() == name.lower():
                    return entry.name, True
    except OSError:
        return None, False
    return None, False


def _assert_within_root(real_root: str, target: str) -> None:
    resolved = os.path.realpath(target)
    if resolved != real_root and not resolved.startswith(real_root + os.sep):
        raise UnsupportedCoordination(
            f"path {target!r} escapes the workspace root {real_root!r}",
            details={"root": real_root, "target": target},
        )


__all__ = [
    "MODE_CLAIM",
    "MODE_LIST",
    "MODE_READ",
    "MODE_RELEASE",
    "ResolvedTarget",
    "detect_case_insensitive",
    "plan_missing_parents",
    "resolve_target",
]
