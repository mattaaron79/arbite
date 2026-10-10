---
id: tic-0dca
title: 'Show bucket tickets in list and search: --buckets flag and a bucket key in
  every ticket JSON'
status: closed
type: feature
tier: medium
domain: cli
epic: milieu
priority: null
tags: []
assignee: claude.opus-5-5.001
depends_on: []
blocked_by: null
created: '2026-10-10T01:36:05'
updated: '2026-10-10T01:59:18'
closed: '2026-10-10T01:59:18'
---

## Description
Milieu round 3, item 1. list and search hide a ticket filed in a bucket (arbite move <id> /plans); show <id> does show it, so a caller building a ticket list cannot find it. Add --buckets to list and search to include bucketed tickets (default output unchanged), and a 'bucket' key to every ticket in all --json output: the root-relative bucket path such as '/plans', null for a ticket not in a bucket. Check: after 'arbite move <id> /plans', 'list --buckets --json' has the ticket with "bucket": "/plans" and 'list --json' does not have it.

## Notes
- 2026-10-10T01:48:46 claude.opus-5-5.001: Added --buckets to list (flat, --tree, --topo, raw) and search; refused with 'list next' because a filed ticket is never workable. Ticket.to_dict now always emits 'bucket' ('/plans', null when unfiled), fed by a new sink bucket_map() (base/file/sqlite). Docs (conventions, fields, sinks, --json help) and the CR1/CR2 interaction examples updated.

Observable: after 'arbite move <id> /plans', 'arbite list --json' and 'arbite search <text> --json' omit the ticket as before, while the same commands with --buckets include it with "bucket": "/plans". Every ticket document under --json (list, list raw, list next, search, show, deps, list --tree, progress, fetch, create, raw, delete, claim) now has a 'bucket' key, null for an unfiled ticket. 'list raw --buckets' also shows promoted wishes sitting in /wishlist. 'list next --buckets' exits 1 with an explanation. Output without --buckets is unchanged apart from the new key.

- 2026-10-10T01:59:18 system: Submitted; closed (review disabled).
