---
id: tic-bcaa
title: 'depend <a> <b> --remove: drop one dependency without rewriting the list'
status: closed
type: feature
tier: medium
domain: cli
epic: milieu-integration
priority: 5
tags:
- depend
- depends_on
- cas
- milieu
assignee: omp.qwen3.8-next-flash.001
depends_on: []
references:
- plans/milieu-cli-gaps.md
blocked_by: null
created: '2026-10-09T20:54:27'
updated: '2026-10-09T21:23:05'
closed: '2026-10-09T21:23:05'
---

## Description
`depend <a> <b>` adds one dependency and `depend <a>` clears all of them, so removing a single edge means reading the list and writing it back with `set <a> depends_on <the rest>` -- a read-modify-write in which a dependency another agent added between the read and the write is silently lost. Add --remove so the removal is one atomic write. See plans/milieu-cli-gaps.md.

Change: add --remove (action="store_true") to p_depend (cli.py:4997-5014, description string 5000-5005) and a branch in cmd_depend (2731-2753). With two ticket ids it removes <b> from a's depends_on and keeps the others, written through the same path the add branch uses: mutate t.depends_on, t.updated = schema.now(), sink.update(t, expect=_expect_from(t)) (cli.py:546-554). The file sink writes canonical markdown with write_atomic (sinks/file.py:389-392) and sqlite rewrites the ticket_deps rows (sinks/sqlite.py:326-337), both inside the sink's own compare-and-swap, so no sink code changes and the CAS is not a check in front of the write.

Absent <b> is an error, not a no-op: raise TicketError (errors.py:21-27) naming what is absent and printing the current list. cmd_ref_rm (cli.py:2822-2856) is the exact precedent and already carries this asymmetry -- re-adding is no-op success while removing what is not there fails with "does not reference ...; it references ...". `depend` already treats a duplicate add as no-op success (2753), so keep that and mirror the failure. --remove with no TIC_B is an error too (mirroring `ref rm` refusing an empty list); `depend <a>` without --remove stays clear-all, and the self-depend guard (2745-2746) is unaffected.

Exit codes need no new plumbing: this command exits 0 or 1 only. A lost CAS raises Conflict (sinks/base.py:75-93, errors.py:42-48) and main() maps it through outcome_of (coordination/results.py:297-304) to `error:`/exit 1 -- exits 4 and 5 exist only for the coordination layer. So a concurrent writer is reported as an error with nothing changed, which is the guarantee the caller needs.

Docs: p_depend's description string is what `arbite docs commands depend` prints (docs.py:render_commands, 1475) -- rewrite it to name --remove and the clear-all form. FIELD_NOTES['depends_on'] (docs.py:267) describes the field and stays; README has no depend prose.

Acceptance: a ticket whose depends_on is [b, c] has exactly [c] after `arbite depend a b --remove`, and b's and c's own tickets are untouched; `arbite show a --json` reflects it; `arbite depend a x --remove` where x is absent exits 1, names x, lists a's actual dependencies and leaves the ticket unchanged (an unchanged `updated` stamp, so the retry sees no drift); `arbite depend a --remove` with no second id is refused. Identical on the file and sqlite sinks -- their atomicity mechanisms differ (temp+rename vs transaction) while the observable contract must not (README.md:10-11).

Tests: tests/test_cli.py with descriptive names beside the existing depend call sites (1168, 1254), asserting through `list --topo` / `list --tree --json` / `list next` so the surviving edge is checked rather than the printed line; parametrize sink_kind (785). depends_on persistence is already conformance-covered (test_sink_conformance.py, test_sqlite_sink.py:181). Add two scenario ids to interaction-examples.md -- removal success, and the exit-1 refusal with its stderr annotation -- since no existing scenario runs `depend`.

## Notes
- 2026-10-09T21:19:06 omp.qwen3.8-next-flash.004: depend <a> <b> --remove lands: 'arbite depend a b --remove' now drops b from a's depends_on, keeping the rest in the order they were added, in one compare-and-swap write (t.depends_on mutated, t.updated restamped, sink.update(expect=_expect_from(t))) -- no new sink method, no read-modify-write. Observable: 'a no longer depends on b' on exit 0; show a --json lists only the survivors; list --topo/--tree --json lose the edge (the dropped ticket becomes a tree root and the dependent moves up the topo order); list next offers the dependent once the survivors are closed. Refusals, all exit 1 and all writing nothing (updated unchanged): removing a dependency the ticket lacks says "ticket a does not depend on x; it depends on b, c"; --remove with no TIC_B says it needs TIC_B and lists what the ticket depends on, so bare 'depend a' stays the clear-all form; a self-dependency with --remove is refused as 'cannot depend on itself, so it has no such dependency to remove'. Ambiguous/unknown ids resolve (or refuse) exactly as the add form does, via sink.get(unique=True); duplicate add stays a no-op success. 'arbite depend --help' and 'arbite docs commands depend' now cover all three forms. Tests: five sink-parametrised cases in tests/test_cli.py (topo order, tree JSON, list next, refusal-without-write, prefix resolution, clear-all intact) plus a new DP family in .arbite/planning/interaction-examples.md (DP1 removal, DP2 refusal with its stderr) asserted by tests/test_depend_examples.py; identical on the file and sqlite sinks.

- 2026-10-09T21:23:04 omp.qwen3.8-next-flash.001: verified independently by the parent (both sinks, real CLI): depend a b --remove leaves depends_on [c] with a's updated bumped and b/c payloads and files untouched; the dropped edge disappears from list --topo --json (no ticket now orders after a dependency it no longer has) while list next releases the gate; depend a x --remove exits 1 naming x and listing the real dependencies with the ticket byte-unchanged; depend a --remove is refused; duplicate add stays no-op success and 'depend a' still clears all -- both text forms byte-identical to the HEAD binary from the same starting state; arbite docs commands depend documents all three forms.

- 2026-10-09T21:23:05 system: Submitted; closed (review disabled).
