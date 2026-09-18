# Implementation ticket index

Created in the configured SQLite sink on 2026-09-18. All tickets are open and unclaimed.

Epics are existing arbite grouping labels, not parent execution tickets. Full portable ticket descriptions are in ticket-manifest.json.

| Key | Ticket | Epic | Title | Dependencies |
| --- | --- | --- | --- | --- |
| C01 | tic-d471 | shared-directory-coordination | Define versioned coordination records and application operations | None |
| C02 | tic-12a4 | shared-directory-coordination | Implement transactional coordination storage and durable events in both sinks | tic-d471 |
| C03 | tic-c4d2 | shared-directory-coordination | Create work attempts and guard every ticket acquisition path | tic-12a4 |
| C04 | tic-0742 | shared-directory-coordination | Bind workspaces and implement exclusive file claims with canonical paths | tic-c4d2 |
| C05 | tic-5196 | shared-directory-coordination | Build recoverable file-operation intent and artifact journal | tic-0742 |
| C06 | tic-9710 | shared-directory-coordination | Expose bounded file discovery and versioned read tools | tic-0742 |
| C07 | tic-baa7 | shared-directory-coordination | Expose version-checked whole-file writes and exact targeted edits | tic-5196, tic-9710 |
| C08 | tic-2cef | shared-directory-coordination | Expose tracked file creation, removal and rename operations | tic-baa7 |
| C09 | tic-b675 | shared-directory-coordination | Cascade ticket lifecycle changes through file ownership and receipts | tic-2cef |
| C10 | tic-ccd3 | shared-directory-coordination | Expose automatic change receipts and net ticket change views | tic-b675 |
| C11 | tic-d047 | shared-directory-coordination | Add coordination migrations, exports and integrity recovery | tic-ccd3 |
| C12 | tic-d60f | shared-directory-coordination | Validate and document the shared-directory proxy workflow | tic-d047 |
| B01 | tic-799e | multi-provider-job-board | Add passive worker profiles and eligibility declarations | tic-c4d2 |
| B02 | tic-ecd0 | multi-provider-job-board | Add atomic coordinator reservations over explicit ticket sets | tic-c4d2 |
| B03 | tic-ea07, tic-b675 | multi-provider-job-board | Publish offers and direct assignments with atomic worker pickup | tic-799e, tic-ecd0 |
| B04 | tic-38a0 | multi-provider-job-board | Implement ordered same-worker packages and explicit continuity handoff | tic-ea07, tic-b675 |
| B05 | tic-3c9c | multi-provider-job-board | Unify job-board readiness, worker capacity and routing explanations | tic-38a0 |
| B06 | tic-7431 | multi-provider-job-board | Expose resumable event queries and coordinator progress views | tic-38a0, tic-ccd3 |
| B07 | tic-1dce | multi-provider-job-board | Preserve job-board records across migrations and integrity checks | tic-3c9c, tic-7431, tic-d047 |
| B08 | tic-da32 | multi-provider-job-board | Validate and document manual multi-provider job-board coordination | tic-1dce, tic-d60f |

Inspect work:

```sh
arbite list --epic shared-directory-coordination --topo
arbite list --epic multi-provider-job-board --topo
arbite list next --epic shared-directory-coordination
```

Do not interpret sibling dependencies as safe concurrent source edits. The implementation itself starts before the new proxy exists.
