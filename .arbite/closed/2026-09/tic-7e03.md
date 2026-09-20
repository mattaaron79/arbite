---
id: tic-7e03
title: Add 'arbite promote' -- turn a raw request into an open ticket
status: closed
type: feature
tier: high
domain: cli
epic: workflow
priority: 3
tags:
- cli
- raw
- triage
assignee: null
depends_on:
- tic-3b55
blocked_by: null
created: '2026-09-20T12:26:17'
updated: '2026-09-20T13:48:20'
closed: '2026-09-20T13:48:20'
---

## Description
Add 'arbite promote <id>': the write half of triage. 'arbite fetch' stays a read-only queue that prints a raw ticket with its derived_note; promote is what an agent runs once it has classified the thing.

What it does:
1. Writes a verbatim snapshot of the raw ticket, exactly as captured, to raw/processed/<id>.raw.md (convention fixed by the raw/processed ticket). The snapshot is frozen -- never rewritten, never scanned.
2. Classifies the ticket IN PLACE, carrying the id forward: --title, --tier, --domain, --epic, --priority, --description, --tags. The id surviving promotion is the point -- anything that already referenced the raw request stays valid.
3. Moves it to status 'open' (or claims it directly with --agent, matching what fetch's derived_note already offers).

The wish case, which fetch already describes: a raw ticket of type 'wish' is retyped to 'feature' and filed in the wishlist bucket rather than opened. Keep that behaviour and route it through this command.

Required classification arguments mirror 'arbite create': refuse to promote while any of title/tier/domain is still a TODO placeholder, naming which. A promoted ticket must never reach 'list next' with placeholder fields.

Snapshot first, then mutate, so a crash leaves the raw ticket intact rather than a half-classified one with no record of the original.

Acceptance: promote produces a frozen snapshot invisible to fetch/list/doctor plus a fully classified open ticket at the same id; a second promote of the same id errors rather than overwriting the snapshot.

## Notes
- 2026-09-20T13:48:20 zoo.orch.001: Added arbite promote <id>: writes a frozen verbatim snapshot to raw/processed/<id>.raw.md (exclusive create, never overwritten) then classifies in place through the sink CAS, carrying the id forward; moves to open or claims with --agent; a wish is retyped to feature and filed in the wishlist bucket instead of opened; refuses placeholder title/tier/domain naming the field; cleared the classification epic; fetch derived_note and docs now name promote
