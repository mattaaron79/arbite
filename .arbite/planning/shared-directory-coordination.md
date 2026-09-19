# Shared-directory coordination and file proxy

Status: ready for implementation ticketing. Epic: `shared-directory-coordination`.
Read README.md in this directory for the owner's mandate and shared rules.

## Outcome

Two independently started agents can work in one checkout through arbite,
without a successful proxy write silently overwriting a competing proxy write.
Every successful write has durable evidence linked to its ticket and work
attempt. Ticket closure releases all its active file claims. The workspace
itself is the visible working state; there is no merge-back step.

Agents are instructed to use arbite for source discovery, reads, and mutations.
Shell commands for tests remain available. A shell/editor/build can bypass the
proxy unless the user's runtime prevents it. Document this honestly: arbite
enforces its own operations, detects observed drift, and supplies hooks for
future restriction. It cannot prove who made a direct filesystem change.

## Records and ownership

- Workspace: opaque ID, canonical local root, authoritative store identity.
  Root relocation is explicit. One workspace cannot safely coordinate against
  two independently selected sinks; the proxy must reject a conflicting binding.
- Work attempt: opaque ID, ticket ID, worker ID, workspace ID, generation,
  active/finished/released/interrupted state, UTC started/last_activity/ended,
  outcome and handoff. One active attempt per ticket initially. Reopen creates a
  new attempt on the next claim. Existing in-progress tickets require an explicit
  adoption operation; do not invent past activity.
- File claim: workspace ID + canonical relative path, ticket and attempt IDs,
  generation, acquired time, current state, observed version. Exclusive writer
  ownership is per whole file in v1. Keep release events after removing active
  ownership. Reacquiring a path must not reactivate an old token.
- Read observation: attempt/optional actor, operation ID, path, content digest,
  observed claim generation, range, timestamp. The content digest covers the
  entire file even when returning a line range. A read receipt says bytes were
  served, not that the model understood or retained them.
- Operation/receipt: operation ID, attempt, ticket, actor, operation kind,
  path(s), before/after digest or explicit absent marker, artifact references,
  claim generation, time, result, schema version. Keep actual bytes or an exact
  reversible representation for successful changes. Agent prose is separate.
- Events: monotonic per-store cursor, unique event ID, kind, subject IDs,
  operation ID, timestamp, versioned payload. Read observations may have a
  separate stream/category to avoid flooding ordinary job queries.

Keep a current-state claim index plus append-only operation/events history;
do not replay the whole log for every claim check. Append-only is an application
contract, not tamper-proof storage. Do not delete evidence on ticket closure.

## File tool contract (proposed spelling)

Commands are one-shot, JSON-capable, and usable without an LLM SDK:

```
arbite file list [PATH] --json
arbite file search PATTERN [PATH] --json
arbite file read PATH --ticket T --attempt A [--lines START:END] --json
arbite file claim PATH... --ticket T --attempt A --json
arbite file write PATH --ticket T --attempt A --read-token R --input CONTENT_FILE
arbite file edit PATH --ticket T --attempt A --read-token R --edits EDITS_FILE
arbite file remove PATH --ticket T --attempt A --read-token R
arbite file rename SOURCE DEST --ticket T --attempt A --read-token R
arbite file release PATH... --ticket T --attempt A --reason TEXT
arbite changes T --json
```

Exact parser names may be refined before implementation; the semantics below
are acceptance requirements. Stdin and external payload files avoid command-line
length/quoting limits. Payload temp files are transport, not project mutations.

Reads do not take an exclusive lock. A read of a file claimed by another attempt
returns bytes with an explicit busy owner, observed version, and non-writable
receipt. An optional fail-if-busy read can avoid wasted tokens. A writer must
claim first and read again after claim acquisition: pre-claim observations do
not authorize a write. A successful write returns an updated version but requires
a fresh proxy read before another mutation, keeping the first contract simple.

Every mutation checks ticket state, current attempt, claim generation, and the
expected whole-file digest while holding the relevant operation lock. Two writes
using one read token cannot both succeed. A mismatch returns `stale_read` and
changes no bytes. Direct external edits also invalidate observed versions when
detected, but no check eliminates a race with an unrestricted external writer.

Whole-file write supports creation via a read/probe receipt for an absent path.
Existing-file writes preserve supported permissions. Targeted edit accepts an
ordered batch of exact old/new text substitutions, with explicit occurrence
rules; ambiguous, absent, or overlapping selections fail with no partial write.
Apply the batch to the validated in-memory version, then replace once. No fuzzy
patching, AST edit, or concurrent region ownership in v1. UTF-8 text editing must
preserve untouched bytes/newline conventions; binary whole-file writes use byte
payloads and digests, and binary receipts need not contain a textual diff.

Remove and rename are required to keep agents from falling back to shell writes.
Rename claims both source and destination, requires absence or an explicit
destination version, and records both paths. No recursive directory deletion
in v1. Parent creation is constrained to safe in-root directories. Unsupported
file types and operations fail clearly rather than following arbitrary links.

List/search provide bounded output, deterministic pagination/truncation markers,
and path/version metadata. Discovery does not count as a write-authorizing read.
Initial search is text/path discovery; import tracing, dependency analysis, and
semantic reads are extension points, not required implementation.

## Paths, conflicts, and waiting

Canonicalize against a bound workspace root. Reject traversal outside root,
protected arbite state and .git metadata, special files, and symlink components
in v1; reject hard-linked mutation targets rather than allow aliases to evade
claims. Account for case-insensitive filesystems when supported. Validate again
at use time and use safe directory/file handles where available; document the
supported-platform boundary rather than claim protection from hostile local
filesystem races. Rename/create must validate missing destination components.

Acquire requested claim sets all-or-nothing in deterministic order. If an agent
already owns claims and requests another busy path, return `file_busy` promptly
with holder ticket/attempt and available next actions. No automatic waiting or
claim stealing. If progress requires yielding, preserve completed edits, release
the ticket with a handoff, and select another ticket. A new worker reads current
bytes; release never silently reverts partial work. No automatic deadlock solver
or promise of starvation freedom in v1.

## Lifecycle and administrative operations

- Claim ticket: check readiness in the same coordinated transaction as assignment
  and attempt creation. List-next is a convenience over the same operation.
- Close: validate actor/attempt, reconcile pending operations, finalize the
  attempt's receipt manifest, mark closed, and release its file claims. No
  observer may successfully mutate under an old token after close succeeds.
- Release and shelve: end the attempt, release file claims, retain partial work
  and evidence. Handoff/reason is recorded. Block also ends the active attempt
  and releases claims in v1; unblock creates a fresh attempt if resuming work.
- Explicit file release: retain the ticket attempt but revoke that file token;
  later reacquisition requires a new read.
- Forced takeover: explicit administrative reason, revoke old attempt generation,
  finalize/interruption evidence, release claims, start a new attempt. A stopped
  or absent worker is never inferred merely from timestamps.
- Reopen: retains old attempts; no old file claims are resurrected.
- Delete: reject tickets with active claims/attempts. For historical evidence,
  retain a tombstone/reference or refuse destructive deletion with an explicit
  explanation; never cascade away change history silently.
- Generic set/status/assignee commands must route through these transitions or
  refuse them with the correct lifecycle command. No backdoor through force.

A direct dependency change or reopen racing a new claim must have a defined
serial order. Claims check readiness at acquisition; later reopening a completed
prerequisite emits an invalidation event but does not secretly undo running work.

## Durability and recovery

The workspace and sink are separate durability domains. A SQL transaction alone
cannot atomically commit both. Use a recoverable write protocol: persist intent
and before/after artifacts, stage bytes, verify claims and versions, apply the
filesystem operation, then finalize the receipt. Use operation IDs for retry
deduplication. Serialize lifecycle operations with in-flight file operations.

Inject failures before/after each boundary, including file replacement followed
by sink failure and rename interrupted between paths. On the next relevant
operation, detect incomplete intent and reconcile using recorded versions. If
observed bytes match neither before nor after, report drift and preserve evidence;
do not guess, overwrite, or release ownership as if the operation completed.
`doctor` exposes pending operations and repairs only unambiguous cases. There is
no recovery daemon. Errors identify whether bytes may already have changed and
which operation must be inspected/retried.

The file sink needs process-safe serialization and recoverable multi-record
updates; existing single-ticket exclusive-create logic is insufficient for all
new invariants. SQLite uses real transactions for store-local state. A small,
coarse local coordination lock is acceptable initially. Lock implementation must
handle process death without inventing an agent-staleness policy. Do not hold an
operation lock for an agent's whole ticket duration; durable claims do that job.

## Evidence and storage policy

Capture mutation evidence automatically, including create/delete/rename/binary
changes, not only the final ticket diff. Provide both ordered receipts and a net
change view per attempt/ticket, including edit-then-revert. Store content once by
digest where practical, verify artifacts, and make size limits explicit. If required
evidence cannot be stored, fail before modifying bytes. Default exclusions for
private/internal paths must fail clearly, not produce unrecorded proxy writes.
No automatic LLM summary or remote upload. No artifact garbage collection until
retention and reference rules are designed; document disk growth.

Non-proxy tool writes are unattributed drift. Build/test output is not magically
ticket-attributed. Document how to keep generated output outside managed source
paths, or explicitly ingest it as an observed artifact with limited attribution.

## Acceptance and implementation slices

1. Versioned domain/application contract and migration of old stores.
2. Sink transaction/revision/event primitives and crash-safe local coordination.
3. Attempts, guarded ticket transitions, dependency-safe acquisition.
4. Workspace binding, canonical paths, exclusive claims and generations.
5. Intent/artifact journal and filesystem recovery engine.
6. Discovery and versioned reads; whole writes and exact edits; remove/rename.
7. Lifecycle cascade and administrative recovery integration.
8. Change queries, export/migration/doctor, and agent-facing documentation.
9. Multiprocess and crash-injection acceptance across both sinks.

Required scenarios: two workers race for one ticket/file (one wins); different
files can be owned independently; stale read is rejected; close races a write;
old worker tries writing after release/takeover; multi-file claim conflict leaves
no partial claims; crash before/after replacement has honest receipt recovery;
foreign read is flagged and cannot authorize mutation; aliases cannot bypass
claims; rename/delete/create and binary evidence round-trip; block/release leaves
current partial bytes visible and next worker must reread; existing tickets
migrate without fabricated history; attempts/events/artifacts survive file-to-
SQLite-to-file transfer when quiescent. Existing regression tests still pass or
are deliberately updated for documented safety restrictions.

## Deferred decisions

No shared-region editing, import analysis, agent authentication, sandbox setup,
stale timers, heartbeat worker, or automatic takeover. Preserve attempt activity,
generation, actor, and operation fields so those features need not reconstruct
missing history. Runtime enforcement and smarter reads are future additions to
this same proxy, not reasons to bypass it now.
