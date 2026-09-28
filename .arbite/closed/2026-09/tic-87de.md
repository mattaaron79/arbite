---
id: tic-87de
title: 'Render deps/--tree as a DAG: memoised expansion, not unrolled DFS'
status: closed
type: refactor
tier: high
domain: cli
epic: dependency-graph
priority: 2
tags:
- cli
- tree
- deps
- graph
assignee: deepseek.flash.001
depends_on: []
blocked_by: null
created: '2026-09-28T15:09:58'
updated: '2026-09-28T15:34:34'
closed: '2026-09-28T15:34:34'
---

## Description
Both `arbite deps <id>` (cmd_deps) and `arbite list --tree` render a DAG by DFS-unrolling it: the `seen` set is threaded down the current path only (`seen | {tid}`), so it guards cycles but not duplication. Any node reachable by more than one path is re-expanded once per path with its whole subtree, so output is O(paths) not O(V+E) and is exponential in depth for a lattice. On dark-pact, `arbite deps tic-0365` (the release gate every ticket converges on) prints the same tickets dozens of times.

Secondary problems: the traversal is implemented twice (cmd_deps and _print_tree/_tree_payload) and has drifted -- deps keeps depends_on declaration order while --tree sorts by priority; deps prints `(missing)` inline while --tree silently drops dangling ids; closed prerequisites are visually identical to blocking ones; rendering is bare 2-space indent with no branch connectors; deps only walks requirements, never dependents.

Fix: one pure walker in graph.py used by both commands. Separate the path set (cycle guard) from the expanded set (duplication guard). First occurrence expands, later occurrences print a back-reference and stop. Sort siblings by priority_sort_key in both. Classify edges as unmet/closed/cycle. Add --full (restore today's unrolled output), --all (include closed prerequisite subtrees) and --dependents (invert the walk). --json gains repeat/ref/edge fields.

## Notes
- 2026-09-28T15:34:31 deepseek.flash.001: Both dependency views now run on one walk (graph.dependency_rows), replacing the two duplicated recursions in cli.py.

The guards are two sets, not one: `path` marks a cycle edge, `expanded` marks a ticket already printed, so a shared prerequisite is expanded at the first path that reaches it and back-referenced after that. Rows carry depth/edge/last/mark, and both renderers fold the same rows -- _print_tree for the text view, _tree_payload for --json.

Rendering: box-drawing connectors with an ASCII fallback for streams that cannot encode them, a tick on a satisfied (closed) edge, trailing (already shown above)/(cycle)/(missing) marks, siblings sorted by priority in both views (deps previously kept depends_on declaration order, list --tree sorted), and a dangling id is now reported by list --tree as well instead of being dropped silently.

Flags: deps and list --tree gain --dependents (walk the forest from its other end) and --full (the older per-path unrolled walk); both are refused by a plain list rather than silently ignored. --json gains edge and repeat/cycle keys while keeping the nested depends shape.

Observable by anyone running it: `arbite deps tic-0365` against dark-pact went from 10679 lines to 95 -- the same 40 tickets, with 55 back-references -- and `arbite list --tree` renders the same forest with the same priority order. `arbite deps <id> --json` is the same 95 nodes with "repeat": true on the 55 references, and `--full` reproduces the old per-path output for anyone who preferred it. 1093 tests pass, 12 of them new: 7 pinned row expectations in test_graph.py and 5 end-to-end renderings in test_cli.py.

- 2026-09-28T15:34:34 system: Submitted; closed (review disabled).
