"""Explicit artifact size and verification policy (planning key C05).

Mutation evidence is only useful if it can be trusted, and trust needs two
statements made out loud: *how big* stored content may be, and *how* it is
verified. This module is where both live, so "make size limits explicit" is a
constant rather than a hope.

- **Content addressing.** An artifact's identity is its `sha256:` digest. Its
  record id is derived from that digest (`coordination.artifact_id_for_digest`),
  so identical bytes yield one artifact record and one stored blob -- content is
  stored once per digest where practical.
- **Verification on write.** `verify_artifact` hashes the bytes it is about to
  store and refuses a mismatch, so an artifact record can never claim a digest
  its content does not have.
- **Verification on read.** A sink hashes stored content again when serving it
  and raises `ArtifactCorrupt` on a mismatch rather than handing back
  unverifiable evidence.
- **Explicit limits.** `DEFAULT_MAX_ARTIFACT_BYTES` bounds one stored artifact.
  A mutation whose before- or after-bytes exceed it fails with
  `ArtifactCapacityError` *before* any filesystem change -- "fail before
  modifying bytes". The limit is a constructor argument on the mutation engine so
  a deployment (or a test) can state its own.

No garbage collection: stored artifacts are retained indefinitely. This is
deliberate -- retention and reference-counting rules are a separate design task --
and it means a busy workspace's coordination store grows with every distinct
version it has ever recorded. See the C05 ticket note.
"""

from __future__ import annotations

from .coordination import (
    DIGEST_PREFIX,
    artifact_id_for_digest,
    digest_of_bytes,
    is_digest,
)
from .errors import ArtifactCapacityError, ArtifactCorrupt, InvalidRecord

#: Maximum size of one stored artifact, in bytes (16 MiB). Chosen as a documented
#: default rather than a discovered one: a mutation whose before- or after-bytes
#: exceed it is refused before any filesystem change. Override per engine/service.
DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024

#: Content-addressed artifacts default to opaque bytes; a caller may state a more
#: specific media type (for example `text/plain; charset=utf-8`).
DEFAULT_MEDIA_TYPE = "application/octet-stream"


def check_capacity(data: bytes, limit: int, *, path: str = "", role: str = "content") -> int:
    """Validate `data` against `limit`, returning its size.

    Raises `ArtifactCapacityError` (never `bytes_may_have_changed`) so a caller
    can fail before touching the filesystem."""
    size = len(data)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise InvalidRecord(f"artifact size limit must be a non-negative integer, got {limit!r}")
    if size > limit:
        where = f" for {path!r}" if path else ""
        raise ArtifactCapacityError(
            f"required {role} evidence{where} is {size} bytes, over the configured "
            f"artifact limit of {limit} bytes; refusing before modifying any bytes",
            details={"path": path, "role": role, "size": size, "limit": limit},
        )
    return size


def verify_artifact(data: bytes, *, expected_digest: str, expected_size: int) -> str:
    """Hash `data`, requiring `expected_digest`/`expected_size`, and return the digest.

    Used on write (before storing) and on read (before serving); a mismatch is
    `ArtifactCorrupt`, never a silent pass."""
    actual = digest_of_bytes(data)
    if actual != expected_digest or len(data) != expected_size:
        raise ArtifactCorrupt(
            f"stored artifact does not verify: expected {expected_digest} "
            f"({expected_size} bytes), observed {actual} ({len(data)} bytes)",
            details={
                "expected_digest": expected_digest,
                "observed_digest": actual,
                "expected_size": expected_size,
                "observed_size": len(data),
            },
        )
    return actual


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MEDIA_TYPE",
    "DIGEST_PREFIX",
    "artifact_id_for_digest",
    "check_capacity",
    "digest_of_bytes",
    "is_digest",
    "verify_artifact",
]
