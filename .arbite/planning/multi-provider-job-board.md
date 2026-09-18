# Multi-provider job board

Status: ready for implementation ticketing. Epic: `multi-provider-job-board`.
Depends on the shared-directory coordination core. No provider API integration,
agent spawning, daemon, background subscriptions, or automatic recovery.

## Outcome and metaphor

Arbite is a dispatch board. A worker can pick up an unreserved work order. A
foreman can reserve a collection, assign some orders directly, and post others
for pickup. The foreman remains responsible for the collection while workers
execute individual orders. Every participant invokes the same short-lived CLI.

The owner initially starts separate Claude/OpenAI/API/local agents manually and
observes how pickup works. Different provider names are metadata, not integration
code. No runtime must keep a model alive while waiting for work.

## Records

- Worker profile: worker ID; optional provider/model/runtime labels; configured
  tier; capability/tool labels; execution locality; cost class (`local`, `paid`,
  `unknown`) and optional estimated cost with explicit units/provenance; declared
  capacity; enabled/disabled; created/updated and optional last_checkin timestamps.
  Availability is declared, never inferred as live. No secrets or API keys.
- Reservation: owner/coordinator ID, explicit member ticket IDs, revision,
  created/updated/released state. Selecting an epic resolves membership once;
  newly added epic tickets are not silently captured. Membership changes are
  explicit and atomic. V1 has no nested reservations.
- Offer: ticket or ordered package, optional reservation ID, required minimum
  tier/capabilities, allowed worker(s) where directly assigned, cost/locality
  constraints and separately labeled preferences, published/withdrawn/accepted/
  completed/cancelled state, revision and provenance.
- Work package: ordered ticket IDs, same-worker policy, current bound worker,
  current member, outcome/handoff. One package membership per ticket initially.
- Attempt: reused from coordination. Reservations never masquerade as an attempt
  or set tickets in_progress by themselves.

Optional registration must not exclude ad-hoc workers. Existing agent IDs and
tier declarations remain usable for unrestricted work. For restricted offers,
missing capability/cost data fails eligibility with a reason. Profile values are
operator assertions, not verified identity; restrictive policy is not a security
boundary in a cooperating local store.

## Core semantics

Ad-hoc claim requires a ready, classified, open ticket whose dependencies are
closed, with no conflicting reservation/package and no active attempt. Tier and
capability constraints are checked at acquisition, not only in list filters.
Configured profile values are authoritative for that profile; per-call values
cannot silently elevate it. Administrative changes are explicit and recorded.

A reservation restricts who may execute its members. Its owner may delegate
directly or publish an offer. Publishing does not release the reservation. Workers
claim an offer directly through the store; the owner need not be online. A
successful offer acceptance creates the execution attempt and consumes/advances
the offer atomically. Two workers cannot both accept the same offer.

Reserve an explicit ticket set all-or-nothing; refuse overlaps and active work
owned elsewhere. A coordinator who already owns active work needs an explicit
transition rather than implicitly transferring it. Assignment identifies who may
claim, not proof that a process started. Manual launch remains external.

Withdrawing an unaccepted offer removes future eligibility. Withdrawing or
releasing a reservation must not silently cancel a worker's active attempt:
refuse while active or require an explicit administrative interruption with a
reason and coordination lifecycle cleanup. Releasing a quiescent reservation
withdraws its offers and returns still-open members to ad-hoc availability.
Force/set/direct claim must respect the same rules.

## Continuity packages

A package is the contract "A then B by the same worker." Validate ordering,
duplicate membership, and dependency cycles; package order is a scheduling edge
even if ordinary ticket dependencies do not duplicate it. Reject impossible
combined package/dependency graphs and expose external prerequisites.

Accepting a package binds its members to one worker but starts only the first
ready member. Later members do not become in_progress early. Completion makes
the next eligible member discoverable exclusively to that worker. Each member
gets its own attempt; file claims are released between tickets and files must be
read again. Same worker means continuity identity, not a promise of the same
model context window. Durable handoff notes/evidence support a resumed session.

If the worker cannot continue, an explicit handoff releases remaining members
or rebinds the package with a reason. Completed members remain completed. No
timeout-based rebinding. An external unmet dependency pauses eligibility without
holding file claims. Blocked/failed members require an explicit resolution; do
not advance merely because execution stopped. Packages prevent workers from
cherry-picking later members through direct claim.

## Selection, cost, and readiness

Keep requirements separate from preferences. Minimum tier/tools, explicit allowed
workers, a local-only requirement, or a defined cost ceiling can reject a claim.
Prefer-local/prefer-low-cost are hints in passive mode, not guarantees: the first
eligible claimant wins. A human/coordinator can inspect candidates and explicitly
assign an offer to ensure its preference is applied. Do not implement an auction,
currency conversion, model pricing catalog, or scheduler.

One readiness evaluator should return ready/not-ready with structured reasons:
dependencies, reservation, package order/continuity, worker constraints, capacity,
classification/status. Board and list-next selection use it; claim rechecks under
the transaction. Claimed active attempts count toward declared capacity, not
reservations or future package members. Profile changes affect future acquisition;
they do not silently revoke running work. No trustworthy global pool occupancy is
claimed across independent project stores.

Suggested one-shot surfaces (spelling may be refined):

```
arbite worker register|show|list|update|disable ... --json
arbite reserve create|show|release ... --json
arbite offer publish|assign|list|withdraw|claim ... --json
arbite package create|show|handoff ... --json
arbite board --worker WORKER --json
arbite events --after CURSOR --limit N --json
```

Existing `claim` and `list next --claim` remain supported and use the same guarded
application operations. Board queries explain exclusions and suggest compatible
ready work; they cannot promise that a discovered ticket is still free later.
No unbounded polling command is required.

## Events and orchestration observations

Use the shared durable event mechanism for reservation/offer/attempt/package
changes. One-shot queries return ordered results, next cursor, and a clear stale
or invalid-cursor error. Filters must advance cursors consistently; retries can
deliver events again and consumers deduplicate by event ID. Record state and
events together so committed work cannot disappear from the event feed. In file
sinks recovery must resolve incomplete commits before reporting their finality.

Readiness is derived from current state. A dependency completion event is a hint
to query the board, not a durable promise that a specific ticket is now ready.
Publish/complete/release events include enough IDs to discover affected work.
There are no webhooks, subscription server, agent wakeups, or delivery guarantees
beyond querying durable local events in this release.

An orchestration status query should show a reservation's completed, active,
ready, dependency-waiting, and explicitly blocked members, current workers, and
latest recorded activity. A pulse is an observation timestamp, not a liveness
guarantee. This supports a later external loop without implementing one.

## Acceptance and implementation slices

1. Worker profiles, optional declarations, and eligibility vocabulary.
2. Atomic reservations and explicit membership management.
3. Offers, direct assignments, guarded acquisition, and withdrawal semantics.
4. Same-worker ordered packages and explicit handoff.
5. Unified board/readiness/capacity and routing explanations.
6. Durable event queries and reservation progress views.
7. Migration/export/doctor and documentation with a manual two-provider recipe.
8. Multi-process conformance and end-to-end acceptance.

Acceptance demonstration: manually identify workers A and B with different
provider labels. An unreserved ticket is claimable by either; a coordinator
reserves three other tickets, assigns one to A and offers a two-ticket sequence
to eligible workers. B accepts the sequence, completes its members in order,
and produces proxy change receipts. A cannot steal B's second member; B cannot
claim A's direct assignment. Both can read structured progress without a resident
orchestrator. Simultaneous offer acceptance has one winner. Direct claim cannot
bypass reservation or dependencies. Withdraw-vs-claim and reserve-vs-claim races
have defined serial outcomes. A capacity-limited worker cannot overclaim via a
batch. Events can be queried incrementally after the querying process restarts.

Also test partial package completion/handoff; profile disable does not delete
history; capability/tier/cost unknowns; reservation release while active; event
replay after a crash; quiescent transfer between both sinks preserving board state.

## Deferred

Live fleet registration, cross-project discovery, network transport, provider
adapters, spawning, bidding, dynamic price lookup, agent authentication,
notification delivery, heartbeat expiration, automatic stale recovery, and
cross-machine/global capacity arbitration. Document fields/IDs needed for future
integration, without building dormant scheduling machinery.
