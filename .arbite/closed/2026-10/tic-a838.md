---
id: tic-a838
title: 'description: one read rule, and the write follows it, for a body with no Description
  header'
status: closed
type: bug
tier: high
domain: cli
epic: milieu-integration
priority: 1
tags:
- description
- body
- json
- milieu
assignee: omp.qwen3.8-next-flash.001
depends_on: []
references:
- plans/milieu-cli-gaps.md
blocked_by: null
created: '2026-10-09T22:37:24'
updated: '2026-10-09T22:44:48'
closed: '2026-10-09T22:44:48'
---

## Description
Round-1 `description` handling produced a surprise for a heading-less body: the read rule returned `null`, but the first `set <id> description <text>` inserted `## Description` at the top and left the old prose below it, so the prose read back as *part of* the new description (`"new desc\n\nplain old text"`). A caller never saw that text as the description, yet the next write replaces it.

Change to one rule, with the write following the read exactly -- what a caller reads must always be what the next write replaces:

- Read, header present: the text under `## Description`, up to the next `## ` line or the end of the body. Unchanged from today.
- Read, header absent: the text BEFORE the first `## ` line -- the whole body when the body has no headings; `null` when there is no such text (e.g. a body that starts with `## Notes`, or an empty body, or only blank lines).
- Write: `set <id> description <text>` replaces exactly the text the read rule identifies, emitting `## Description\n<text>\n` at the top of the body. Text under other headings (e.g. `## Notes`) must not change, and no other heading may be invented or dropped.

Anchors (all in `src/arbite/schema.py` unless stated): `DESCRIPTION_HEADING` and `DEFAULT_BODY` must stay the single source of the heading text; `description_body` currently returns `None` whenever `_description_heading_index` is `None` and must instead fall back to the pre-first-heading text (returning `None` only when that text is empty); `replace_description` currently appends the old body *below* the inserted section (`"\n".join(section) + ("\n\n" + body if body else "\n")`) and must instead place the section at the top and keep a following heading block only when one exists. `Ticket.to_dict` (the single `--json` projection) keeps deriving `description` from `description_body`, so show/list/search/progress/delete/deps/claim payloads follow automatically. `cmd_set`'s `description` branch in `src/arbite/cli.py` calls `schema.replace_description(t.body, value)` and needs no shape change -- but the "empty value keeps the heading with an empty section" rule from round 1 must survive (`set <id> description ''` on a heading-less body is a legitimate write of the pre-heading text to empty, i.e. the body becomes `## Description\n`). `create --description` / `promote --description` build bodies through `DEFAULT_BODY` and are unaffected.

Do NOT change storage: the description stays body text, no frontmatter key, no sink change, no schema-version bump.

Existing tests that pin the OLD rule must be updated rather than kept: round 1 asserted `description is None` for a heading-less body and asserted the surviving prose sits *inside* the new section (see the parametrized description test in `tests/test_cli.py` and the extract/replace cases in `tests/test_schema.py`, plus `tests/test_sink_conformance.py::test_description_survives_a_round_trip`).

Acceptance (each proved with the real CLI, file sink AND sqlite sink):
- body `plain old text` (no headings): `show --json` gives `description == "plain old text"`; after `set description "new desc"` the body is exactly `## Description\nnew desc\n` and nothing else.
- body `## Notes\n- n1\n` only: `description` reads `null`; after `set description "new desc"` the body has `## Description\nnew desc\n` above the unchanged notes block.
- body with a description plus notes (the round-1 shape): read unchanged, write replaces only the description, notes byte-for-byte identical.
- a description containing a literal `## Description` line still resolves by the FIRST heading.
- `set description ''` keeps the heading and produces an empty description (`""`), never `null`.
- idempotence: read-then-write with the value that was just read leaves the body byte-identical.

## Notes
- 2026-10-09T22:40:59 omp.qwen3.8-next-flash.007: Read and write now share one rule. schema.description_body falls back to the text before the first '## ' line when there is no '## Description' heading -- the whole body when it is heading-less, None only when that text is empty -- and schema.replace_description replaces exactly that text, emitting '## Description\n<text>\n' at the top of the body and keeping any following heading block byte-for-byte; a new _first_heading_index backs both. cmd_set needed no code change; its docstring and parser help, the 'set' field note and JSON-contract prose in docs.py, and to_dict's docstring now state the rule. Tests updated/added: tests/test_schema.py (heading-less read; whole-body and leading-heading-block writes; read-then-write-back idempotence), tests/test_cli.py (prose body, notes-only body, exact body, idempotence -- both sinks), tests/test_sink_conformance.py (round trip over heading / no-heading / notes-only). A verifier sees 'set <id> description' replace exactly what 'show --json .description' reported, 'set description ""' keep the heading and read '', and create/promote --description unchanged. Deliberately left alone: storage (description stays body text, no frontmatter key, no sink schema change, no store-version bump) and the heading-present read/write path.

- 2026-10-09T22:44:44 omp.qwen3.8-next-flash.001: verified by the parent, both sinks, 30 checks: heading-less prose reads as the description and the first write leaves exactly '## Description\nnew desc\n'; a notes-only body reads null and the write lands above notes that stay byte-for-byte; the description-plus-notes read is unchanged from HEAD and the write moves only the description; a literal '## Description' inside the section still resolves by the first heading; 'set description ' keeps the heading and reads '' (never null); read-then-write-back is byte-identical; create --description is unaffected. Observable: what show --json reports as description is exactly what the next set description replaces.

- 2026-10-09T22:44:48 system: Submitted; closed (review disabled).
