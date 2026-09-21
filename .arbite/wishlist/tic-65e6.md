---
id: tic-65e6
title: Decide whether agents must route edits through arbite
status: raw
type: feature
tier: high
domain: io
epic: shared-directory-coordination
priority: null
tags:
- policy
- passthrough
- enforcement
assignee: null
depends_on: []
blocked_by: null
created: '2026-09-21T01:16:58'
updated: '2026-09-21T01:16:58'
closed: null
---

## Description
Policy question, not scheduled work. Evidence to collect first: the passthrough.exec event stream from C13 (tic-faae) plus C14 (tic-42d2) guarded-mode outcomes. Once available, decide between mandating arbite cmd in the generated guide, restricting writes at the harness level, or leaving passthrough advisory. Depends on C13 and C14 being implemented and observed in real use; do not schedule before that.

Original request: Require agents to route edits through arbite instead of raw shell writes. Decision deferred until C13 (tic-faae) has produced passthrough.exec evidence: which tools agents actually reach for, how often a change escapes a claimed set, whether guarded mode blocks real work, and whether any agent bypasses the proxy entirely. Options to weigh then: mandate arbite cmd in the generated guide, restrict writes at the harness level, or leave it advisory.

## Notes
