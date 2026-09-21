# Implementation ticket index

Revised 2026-09-21. Cut: `C01`–`C15` for the epic `shared-directory-coordination`,
and `B01`–`B08` for `multi-provider-job-board`, plus one wishlist item. The
coordination cut is active; the job-board cut is **shelved** and is not part of the
current commitment — see "Deferred epic" below.

Epics are existing arbite grouping labels, not parent execution tickets. Sibling
dependencies specify safe *order*, not safe concurrent source edits — see the note
at the end.

## Store status, read this first

The cut below was created on 2026-09-21 in this checkout's live store: the `file` sink
selected by `.arbite/project.yaml`, with `.arbite/` tracked in git. The 18 closed
tickets filed before it are untouched, nothing is assigned, and `arbite doctor` reports
no problems.

## Rejected approach, do not rebuild it

An earlier attempt at this epic ran on 2026-09-18 and was abandoned. It is worth knowing
about, because the debris is easy to mistake for prior art and resume.

- It created 20 tickets — 12 for `shared-directory-coordination`, 8 for
  `multi-provider-job-board` — in a **SQLite store**, and closed all 12 coordination
  tickets the same day. All 8 job-board tickets were left `open` and unassigned.
- It implemented coordination as roughly 30 new modules and ~17,400 lines on a branch,
  against an older main lineage: an application layer, a coordination storage layer,
  two coordination sink backends, plus separate modules for paths, locking, claims,
  reads, mutations, lifecycle, mutation journaling, artifacts, changes, workspace,
  doctor and export, with about sixteen test modules and a proxy recipe script.
- It also carried a `workspace.py` and a binding concept, which this cut deliberately
  drops.
- Verdict from the owner: overengineered and unintuitive. Treat it as the failure mode
  to avoid, not as a foundation.

None of it is on `main`. The store travelled on branch `temp-checkin` (commit `5f70f03`,
"Apply stashed changes to temp-checkin", parent `c80cc63`), the only ref that reached it,
with no remote copy. That branch was deleted on 2026-09-21, so its objects are now
unreferenced and will go at git's next gc. There is nothing left to recover, nothing to
check out, and no reason to re-derive the cut from those closed tickets: the live tickets
of this epic are the ones listed below.

[`ticket-manifest.json`](ticket-manifest.json) is the full-text record of that abandoned
cut, kept only so this note has provenance. Its ids are dead.

## The cut

Every ticket cites the scenarios from
[interaction-examples.md](interaction-examples.md) that it must make pass. A ticket
is not done while a scenario it owns still differs from the frozen transcript.

| Key | Epic | Title | Depends on | Tier | Domain | Scenarios |
| --- | --- | --- | --- | --- | --- | --- |
| C01 | shared-directory-coordination | Define versioned coordination records and application operations | none | high | io | WS1, WS2, DR4 |
| C02 | shared-directory-coordination | Implement transactional coordination storage and durable events | C01 | high | io | EV2, EV3, EV4, EV5, EV7 |
| C03 | shared-directory-coordination | Create work attempts and guard every ticket acquisition path | C02 | high | io | CL1-CL6, LC5, RC1 |
| C04 | shared-directory-coordination | Implement canonical paths and exclusive file claims | C03 | high | io | FC1-FC8, LS6, RD5 |
| C05 | shared-directory-coordination | Build the file-operation intent journal and recovery engine | C04 | high | io | DR1, DR2, RC2 |
| C06 | shared-directory-coordination | Expose bounded discovery and versioned reads | C04 | medium | ui | LS1-LS5, RD1-RD4 |
| C07 | shared-directory-coordination | Expose version-checked writes and exact edits | C05, C06 | medium | ui | WR1, WR4-WR7, ED1-ED3, BY2 |
| C08 | shared-directory-coordination | Expose tracked creation, removal and rename | C07 | medium | ui | RN1-RN4, FC4 |
| C09 | shared-directory-coordination | Add scratch payload transport | C07 | low | ui | SC1-SC5, DR3 |
| C10 | shared-directory-coordination | Cascade ticket lifecycle through file ownership and receipts | C08 | high | io | CL7, LC1-LC4 |
| C11 | shared-directory-coordination | Expose change receipts and net ticket change views | C10 | medium | ui | EV1, EV6 |
| C12 | shared-directory-coordination | Add coordination migrations, export and integrity recovery | C11 | high | io | multi-record round trip |
| C13 | shared-directory-coordination | Add passthrough command observation | C11 | medium | ui | PC1, PC5, PC6 |
| C14 | shared-directory-coordination | Add passthrough guarded mode | C13, C08 | high | ui | PC2, PC3, PC4 |
| C15 | shared-directory-coordination | Validate and document the shared-directory proxy workflow | C12, C14 | high | ui | BY1, BY3, all scenarios, both-sink conformance |
| B01 | multi-provider-job-board | Add passive worker profiles and eligibility declarations | C03 | high | io | — |
| B02 | multi-provider-job-board | Add atomic coordinator reservations over explicit ticket sets | C03 | high | io | — |
| B03 | multi-provider-job-board | Publish offers and direct assignments with atomic worker pickup | B01, B02, C10 | medium | io | — |
| B04 | multi-provider-job-board | Implement ordered same-worker packages and explicit continuity handoff | B03, C10 | high | io | — |
| B05 | multi-provider-job-board | Unify job-board readiness, worker capacity and routing explanations | B04 | medium | ui | — |
| B06 | multi-provider-job-board | Expose resumable event queries and coordinator progress views | B04, C11 | medium | ui | — |
| B07 | multi-provider-job-board | Preserve job-board records across migrations and integrity checks | B05, B06, C12 | high | io | — |
| B08 | multi-provider-job-board | Validate and document manual multi-provider job-board coordination | B07, C15 | medium | ui | — |

Two changes from the 2026-09-18 cut, both from the owner's decisions on
2026-09-21: the workspace-binding ticket is gone (the workspace is derived from the
located `.arbite/` directory and the resolved sink), and the passthrough work is
split into observation (C13) and guarded mode (C14) so the owner can evaluate
whether to keep going after the first half.

Priorities are deliberately unset. Topological order is the sequencing mechanism
for this epic, so a priority would only be a way to pull one ticket forward, and
nothing needs that yet.

## Minted ids

Created 2026-09-21 in the file sink (`sink: file`). The fifteen coordination tickets
are `open` and unclaimed; the eight job-board tickets were shelved on 2026-09-21.

| Key | Ticket | Key | Ticket |
| --- | --- | --- | --- |
| C01 | tic-7918 | C09 | tic-95c0 |
| C02 | tic-1a75 | C10 | tic-e9ed |
| C03 | tic-cf9f | C11 | tic-7c42 |
| C04 | tic-9b57 | C12 | tic-008f |
| C05 | tic-b03b | C13 | tic-faae |
| C06 | tic-1c4f | C14 | tic-42d2 |
| C07 | tic-60c7 | C15 | tic-6015 |
| C08 | tic-74e2 | | |
| B01 | tic-ada8 | B05 | tic-b82e |
| B02 | tic-0cdb | B06 | tic-8c9a |
| B03 | tic-f576 | B07 | tic-4178 |
| B04 | tic-e59a | B08 | tic-920d |

Wishlist item: `tic-65e6` (status `raw`, type `feature`, filed in `wishlist/`,
epic `shared-directory-coordination`) — deliberately out of both the work and the
triage queues until its evidence exists.

Verified after creation: `arbite doctor` reports no problems, `arbite list next
--epic shared-directory-coordination` offers `tic-7918` (C01) and nothing else, and
the raw queue is empty. After shelving: 15 open, 8 shelved, 18 closed, 41 total, and
`arbite list next` offers `tic-7918` alone — shelved work is excluded from `next` and
from `progress`, while `arbite list --epic multi-provider-job-board` still shows the
eight, labelled `shelved`.

## Deferred epic: multi-provider-job-board

`B01`–`B08` are shelved (`tic-ada8`, `tic-0cdb`, `tic-f576`, `tic-e59a`, `tic-b82e`,
`tic-8c9a`, `tic-4178`, `tic-920d`). Each carries the reason in its notes: the design
was inherited from the abandoned 2026-09-18 run and has to be re-cut against the
coordination primitives that actually land before any work starts. Nothing failed —
the epic was simply never started, and its eight tickets were left open by that run.

Two things make deferring it the right call rather than merely cautious. The design
was written against the rejected coordination API, so its vocabulary (reservations,
offers, packages, continuity) describes records this cut has not defined. And the
simple half — provider-neutral worker profiles with declared tier and capacity — is
defensible, while the packaged-workflow half is exactly the kind of ambition that
made the earlier attempt overengineered for one checkout and two hand-started agents.

Re-cut it after the coordination core has been used in anger. The plan document
[multi-provider-job-board.md](multi-provider-job-board.md) stays as the reference, and
`shelve`/`unshelve` are reversible, so nothing here is a dead end.

## How this cut was materialized

The epic has no parent ticket, so each row was created with its own ticket, then
the edges were wired, then the plan attached:

```sh
arbite create --title "Define versioned coordination records and application operations" \
  --type feature --tier high --domain io --epic shared-directory-coordination \
  --tags coordination,records,schema \
  --references planning/shared-directory-coordination.md,planning/interaction-examples.md \
  --description "$(cat <<'EOF'
Planning key: C01
Handoff: .arbite/planning/shared-directory-coordination.md
Examples: .arbite/planning/interaction-examples.md (WS1, WS2, DR4)

## Outcome and scope
...
EOF
)"

# Dependencies are wired in a second pass: create mints the ids, and a
# dependency has to name one that already exists.
arbite depend <id> <dependency-id>
arbite list --epic shared-directory-coordination --topo    # verify the order
```

References are root-relative to the arbite directory, so
`planning/shared-directory-coordination.md` means
`.arbite/planning/shared-directory-coordination.md`. A `..` segment is refused,
which is why the planning path is written as `planning/...` and not
`plans/../planning/...`.

The JSON form of the cut is [`ticket-manifest.json`](ticket-manifest.json), whose
loader is the intended path if the cut is larger than a handful of edits: keep the
`key`, `title`, `tier`, `epic`, `dependencies` and `description` fields, add
`domain` and `scenarios`, and regenerate rather than hand-editing tickets.

A ticket's description must carry, at minimum: the slice text from the handoff
doc, its scenario IDs, and the sentence that sibling tickets may touch the same
files until the proxy exists. Notes added while working should record the
validation results and any observable behaviour a reviewer can check, since the
ticket is the development record.

## Wishlist item to file now

One question needs a decision with evidence rather than a plan, so it belongs in
the wishlist bucket, not in the epic:

**Require agents to route edits through arbite.** Once C13 lands, the
`passthrough.exec` stream answers it: which tools agents actually reach for, how
often a change escapes a claimed set, whether guarded mode blocks real work, and
whether any agent bypasses arbite entirely. The policy choices that follow —
mandate `arbite cmd` in the generated guide, restrict writes at the harness level,
or leave it advisory — depend on those numbers. File it as a wish (reclassified to
`feature`, epic `shared-directory-coordination`, no priority) with the C13
dependency noted in the body, and deliberately do not schedule it.

## Inspect work

```sh
arbite list --epic shared-directory-coordination --topo
arbite list next --epic shared-directory-coordination
arbite progress --epic shared-directory-coordination
arbite list --status shelved                      # the deferred job-board cut
arbite list --epic multi-provider-job-board --topo # same eight, labelled shelved
```

Do not interpret sibling dependencies as safe concurrent source edits. Until the
proxy exists, implementers coordinate shared source changes manually or work
serially — which is the bootstrapping problem the epic itself is meant to end.
