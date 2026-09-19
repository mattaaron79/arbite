# Centralized storage and project/workspace identity

Status: future planner handoff; documentation only. PostgreSQL is a candidate,
not a committed dependency or immediate implementation epic.

## Outcome

One deployment can hold several projects while local file/SQLite stores remain
first-class. A project can have multiple workspace locations. Humans and agents
can identify where work lives without mistaking a host-local folder for shared
storage. No central service or database is required by the first two epics.

## Model boundaries

- Deployment/store: stable identity and supported schema/capabilities.
- Project: stable ID, human name and lifecycle; not identified by folder or name.
- Workspace: stable ID, project ID when available, host identifier, canonical
  root on that host, authoritative store binding, optional repository/ref metadata.
- Agent/attempt: stable references to the actual workspace used for execution.
- Artifact: digest, media type, size, storage reference and access locality.

The initial coordination epic creates workspace/store identity only; project
identity can be associated later without reminting old attempts. Preserve current
ticket IDs; a centralized store scopes them by project (or adds an opaque global
record ID) instead of assuming four hexadecimal digits are globally unique.
Display names and codenames are mutable. Host IDs are stable configuration, not
proof of authenticated machines.

## Architectural requirements

Storage operations must expose atomic claim/readiness, reservation, and lifecycle
semantics instead of leaking SQL transactions into CLI commands. PostgreSQL must
implement that contract and the same conformance suite. Capability discovery
should state which operations/transports are available; never silently weaken
coordination because a backend is remote.

A database sink stores coordination metadata. It does not make remote workspace
bytes available. A CLI running on a workspace host can use a central sink while
performing local file operations, subject to the same intent/recovery protocol.
A browser or remote agent needs an explicit authorized host adapter to read or
write those bytes. A host/path tuple is a locator, not an execution channel.

Network loss during file writes produces the same split-durability challenge as
local crashes, with more ambiguous outcomes. Operation IDs, durable local intents,
generations and reconciliation are foundational. Do not allow disconnected
writers to continue under cached claims or treat a lost connection as release.
Leases/fencing across hosts need a fresh design review; local claims are not
automatically distributed leases.

## Migration and operation

Define a versioned export containing tickets, attempts, claims/history, events,
offers/packages, project/epic data when implemented, and referenced artifacts.
Prefer quiescent migration; refuse active attempts, in-flight writes and live
claims unless a dedicated handoff protocol exists. Validate destination before
switching the authoritative binding. Handle ID collisions explicitly, preserve
references, and avoid accidental dual active stores. A new event cursor namespace
must be signaled rather than silently treating old cursors as meaningful.

Authentication, project authorization, credentials outside tickets, encrypted
transport, backups/restore, retention and schema upgrades become real deployment
requirements for shared remote service access. Keep these out of the local v1
implementation, but budget them in any central deployment epic. Decide whether
clients connect directly to a DB or through an API; a browser must not receive
database credentials. Do not simultaneously build both transport models without
a concrete need.

## Future implementation slices and acceptance

1. Select deployment/transport/trust model and supported topology.
2. Project/workspace scoping and unambiguous identifiers.
3. PostgreSQL or chosen sink with transactional conformance.
4. Configuration, credential handling, identity and access checks.
5. Quiescent migration/export/artifact transfer and backup/restore.
6. Network-failure recovery and deployment documentation.

Acceptance: two projects can share ticket display IDs without accidental cross-
project access; two clients race for one claim with one winner; disconnection
after a local file change preserves an inspectable pending intent; restore and
migration retain references and evidence; a remote client cannot infer that it
can edit a host path merely because metadata lists it; local sinks remain usable
without any centralized infrastructure.

Future decisions: target DB, API versus direct DB clients, remote workspace
transport, tenant boundary, supported network filesystem topology, artifact
hosting and retention, distributed stale recovery and authentication authority.
