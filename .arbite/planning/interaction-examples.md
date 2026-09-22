# Interaction examples: the acceptance-target catalogue

Status: normative. Every example below is a target, not an illustration.
Epic: `shared-directory-coordination`. Handoff: [shared-directory-coordination.md](shared-directory-coordination.md).
Read [README.md](README.md) for the owner's mandate and shared rules.

## How to use this document

This is the contract for the agent-facing surface of the file proxy. A block is
frozen the moment it lands here: the command, its stdout, its stderr and its exit
code are all asserted. Scenario IDs (`FC3`, `WR2`, …) are stable and are cited
from tickets, from `docs.py`, and from test names, so a ticket cannot claim to be
done while the transcript it owns still differs.

Two rules make the examples checkable rather than decorative:

- **Verifiable against a revision of this repo.** Every path, line number and
  literal token in a block is taken from the source tree as it stood when the
  block was written, so a reader who checks out that revision can run the command
  and get the block back. The tree has moved since -- this checkout's
  `src/arbite/sinks/file.py` is 579 lines, not the 412 the blocks print, and its
  `src/arbite/cli.py` is 4604 lines, not the 3015 RD3 ranges over -- and a digest
  or a size is a fact about bytes that no fixture can reproduce while also keeping
  those line counts. So the harness normalises digests, and the fixtures build the
  line counts, paths and dates each block asserts. `O_CREAT` in `LS3` is the flag
  spelled at `src/arbite/sinks/file.py:141` when the block was written, letter O,
  not a zero. When a block and the code disagree, the block is wrong until proven
  otherwise -- and where a block was provably wrong, C15 corrected the block
  rather than the code, with the contradiction recorded on the ticket.
- **Text is primary; JSON is the same facts.** Language agents read the text, so
  the text carries the whole story in one screen: what happened, what it cost,
  and what to do next. `--json` is the branchable form (field names match
  frontmatter, plus `next_actions`), and a test asserts every fact present in
  text is present in JSON. Machine consumers branch on exit codes, never on
  prose.

**Ticket ids in the transcripts are the live tickets of this cut**, used as
illustrative holders: C03 `tic-cf9f`, C04 `tic-9b57`, C02 `tic-1a75`, C10
`tic-e9ed`. They indicate which slice a scenario belongs to and are not a claim
about the current state of those tickets — `tic-cf9f` reads as "worker A's ticket"
in a claim scenario even though the ticket itself is simply `open` today. The
harness substitutes every id before comparing output.

## Global conventions

### Exit codes

| Code | Meaning | Correct caller response |
| --- | --- | --- |
| 0 | success; state changed, or bytes were served | continue |
| 1 | error: bad input, not found, policy refusal | fix the command |
| 2 | query ran, matched nothing | nothing to do |
| 3 | `doctor` found problems | repair |
| 4 | `busy` — a live claim or attempt holds it; **nothing changed** | pick other work; do not retry blindly |
| 5 | `stale` — token, digest or generation no longer current; **nothing changed** | re-read, then retry |

`arbite cmd` (passthrough) is the one deliberate exception, because a wrapped
tool's own exit code must survive untouched and 0–5 are all reachable that way:
it returns the wrapped command's code for a command that ran, and `125` refused
before running (busy, policy), `126` unsupported invocation, `127` tool not
found. A refusal never runs the command and says which invocation was refused:
`125` and `127` also print `command did not run`, while the `126` refusals state
that the invocation is unsupported. The `127` refusal is the one non-zero
outcome in this catalogue that prints no `next:` line -- no arbite command can
put a missing tool on `PATH`.

### The `next:` line

Any outcome that is not plain success ends with a `next:` line naming the exact
command(s) that follow. It is arbite's routing advice, rendered from a table keyed
by outcome kind — the same key that chooses the exit code — and mirrored into JSON
as `next_actions`. Guardrails: a hint is computed from the state at the moment of
failure, may only reference tokens that appear in this command's own output, and
never suggests waiting, retrying a busy path, or working around arbite with the
shell.

### Display rules

- Digests print as a 12-hex-character prefix (`sha256:4b8a1f0c9d2e`); JSON carries
  the full 64. `doctor` is the exception: it prints full digests, because the
  whole point there is comparing them.
- Ordering is deterministic: paths sort lexically by canonical relative path,
  events by cursor, tickets by the existing canonical order. Two runs of one
  command on unchanged state produce identical text.
- Timestamps print as `HH:MM:SS` in local time for reading, while JSON and stored
  records are UTC RFC 3339 to the second.
- IDs: `tic-XXXX` tickets, `ws-XXXX` workspaces, `att-XXXX` attempts, `op-XXXX`
  operations and receipts — same 4-hex style as `ID_PATTERN`.

### Where state lives, and what is tracked in git

```
.arbite/open|in_progress|review|blocked|shelved|closed/   tickets  -> TRACKED
.arbite/agents/                                          scratchpads -> TRACKED
.arbite/planning/                                        planning docs -> TRACKED
.arbite/coordination/claims/                             claim records -> IGNORED
.arbite/coordination/events/                             event stream -> IGNORED
.arbite/coordination/receipts/                           receipts -> IGNORED
.arbite/coordination/artifacts/                          before/after bytes -> IGNORED
.arbite/scratch/                                         payload transport -> IGNORED
.arbite/coordination/lock                                runtime mutex -> IGNORED
```

Tickets answer "what did we do"; coordination state answers "what is happening
now, and what exactly changed". Only the first is a development record, so only
the first is committed. Consequences to accept deliberately: evidence is local
and is lost with the machine unless the owner keeps it, and a devlog is generated
from tickets (plus a receipt summary taken *before* pruning), never from the
coordination store.

`.gitignore` gains exactly:

```
/.arbite/coordination/
/.arbite/scratch/
/.arbite/arbite.db*
```

### Normalisation for byte-comparison tests

`tests/test_examples.py` asserts exit code, stdout and stderr per scenario, with a
helper substituting `tic-XXXX`/`att-XXXX`/`op-XXXX` ids, `HH:MM:SS` times and
absolute paths, exactly as the existing suite normalises ticket ids. Scenario IDs
name the tests: `test_FC3_claim_multi_busy_all_or_nothing`.

---

# WS — workspace

## WS1 · report the derived workspace

```sh
$ arbite workspace show
workspace: ws-7c41
root:      /media/matt/m2tb/projects/arbite
store:     file (sink: file in .arbite/project.yaml)
coordination: .arbite/coordination/  (2 active claims, 31 events, 14 receipts)
scratch:   .arbite/scratch/  (1 file, 4.1 KiB)
# exit 0
```

*target:* the workspace is **derived** from the located `.arbite/` directory and
the resolved sink, never bound by a command. This is the only workspace command;
there is no `bind`, no `--force` override and no conflict path, because a second
root only becomes ambiguous once a store is shared across roots, which
centralized storage — a later, document-only concern — has to introduce.

## WS2 · workspace with nothing active

```sh
$ arbite workspace show
workspace: ws-7c41
root:      /media/matt/m2tb/projects/arbite
store:     file (sink: file in .arbite/project.yaml)
coordination: .arbite/coordination/  (no active claims, 31 events, 14 receipts)
scratch:   .arbite/scratch/  (empty)
# exit 0
```

*target:* "nothing is happening" is a first-class answer, not a missing section.

---

# CL — ticket claim and attempts

## CL1 · claim creates the attempt

```sh
$ arbite claim tic-cf9f --agent claude.opus.001
claimed tic-cf9f for claude.opus.001 -> .arbite/in_progress/tic-cf9f.md
attempt: att-91bd (generation 1, ticket tic-cf9f, workspace ws-7c41)
next: claim the files you will change -- 'arbite file claim <path>... --ticket tic-cf9f --attempt att-91bd'
# exit 0
```

JSON (abridged, showing the added fields):

```json
{
  "id": "tic-cf9f", "status": "in_progress", "assignee": "claude.opus.001",
  "path": ".arbite/in_progress/tic-cf9f.md",
  "attempt": {"id": "att-91bd", "generation": 1, "state": "active",
              "workspace": "ws-7c41", "started": "2026-09-21T13:12:04Z"},
  "next_actions": ["arbite file claim <path>... --ticket tic-cf9f --attempt att-91bd"]
}
```

*target:* acquisition and attempt creation are one operation, and the attempt id
is handed to the caller because every later file command needs it.

## CL2 · claim with an unmet dependency

```sh
$ arbite claim tic-9b57 --agent claude.opus.001
error: tic-9b57 is not ready: depends_on tic-cf9f is in_progress (not closed)
next: 'arbite list next --tier high --epic shared-directory-coordination' for workable tickets,
      or 'arbite deps tic-9b57' to see the chain
# exit 1
```

*target:* readiness is checked inside the same transaction that assigns the
ticket, so this cannot pass and then race a reopen of the prerequisite.

## CL3 · claim loses a race

```sh
$ arbite claim tic-cf9f --agent claude.haiku.003
error: tic-cf9f is not in the expected state (status is 'in_progress', expected 'open';
       assignee is claude.opus.001, expected unassigned); re-read it with 'arbite show tic-cf9f'
attempt held by: att-91bd (claude.opus.001), started 06:12:04, generation 1
next: 'arbite list next --claim claude.haiku.003' to take the next workable ticket instead
# exit 1
```

*target:* the existing `Conflict` text gains the holder's attempt, which is the
fact the current message lacks.

## CL4 · `list next` when nothing is ready

```sh
$ arbite list next --tier high --epic shared-directory-coordination
no workable open tickets matching tier=high epic=shared-directory-coordination
blocked by dependencies: 9 (run 'arbite list --topo --status open --epic shared-directory-coordination')
# exit 2
```

## CL5 · batch claim, partially satisfied

```sh
$ arbite list next --claim claude.sonnet.002 --count 3 --tier high
tic-cf9f  in_progress  1  high  io  shared-directory-coordination  claude.sonnet.002  Create work attempts and guard every ticket acquisition path
tic-9b57  in_progress  1  high  io  shared-directory-coordination  claude.sonnet.002  Bind canonical paths and implement exclusive file claims
note: asked for 3 ticket(s), claimed 2 -- no more workable tickets match
# exit 0, note on stderr
```

*target:* the existing partial-batch note still holds once attempts exist, and
each claimed row has its own attempt.

## CL6 · adopt legacy in-progress work

```sh
$ arbite attempt adopt tic-e9ed --agent claude.opus.001
adopted tic-e9ed for claude.opus.001: attempt att-a71f created (generation 1)
no prior activity is implied by this record; the ticket was already in_progress when arbite began tracking attempts
next: claim its files before changing them -- 'arbite file list' then 'arbite file claim <path> --ticket tic-e9ed --attempt att-a71f'
# exit 0
```

*target:* legacy `in_progress` tickets need an explicit adoption, and the output
refuses to invent history for them.

## CL7 · forced takeover

```sh
$ arbite claim tic-cf9f --agent claude.haiku.003 --force --reason "original worker stopped; user reassigned"
claimed tic-cf9f for claude.haiku.003 -> .arbite/in_progress/tic-cf9f.md (taken over from claude.opus.001)
revoked: attempt att-91bd generation 1 (reason recorded); its 2 file claims were released
released: src/arbite/schema.py, src/arbite/sinks/base.py (partial work left on disk: 1 file modified)
new attempt: att-c50e (generation 1)
# exit 0
```

*target:* takeover reports generation revocation *and* the fact that partial bytes
remain — the two facts the next worker must have.

---

# LS — discovery

## LS1 · bounded listing

```sh
$ arbite file list src/arbite/sinks
src/arbite/sinks/__init__.py    78 lines   2.1 KiB  unclaimed
src/arbite/sinks/base.py       570 lines  26.4 KiB  CLAIMED tic-cf9f/att-91bd
src/arbite/sinks/file.py       412 lines  18.4 KiB  unclaimed
src/arbite/sinks/sqlite.py     520 lines  24.9 KiB  unclaimed
4 files (no truncation)
# exit 0
```

*target:* discovery labels claim state so "can I plan against this?" needs no
second command, and discovery never authorizes a write.

## LS2 · deterministic truncation with a continuation token

```sh
$ arbite file list src/arbite --count 100
src/arbite/__init__.py          12 lines   0.4 KiB  unclaimed
… 99 more files
truncated: 137 more files match; continue with 'arbite file list src/arbite --after src/arbite/cli.py --count 100'
# exit 0
```

*target:* the hint names a token printed by this command (`--after
src/arbite/cli.py`), so it is executable rather than descriptive.

## LS3 · search

```sh
$ arbite file search "O_CREAT" src/arbite
src/arbite/sinks/file.py:141   fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
1 match in 1 file (no truncation)
# exit 0
```

## LS4 · search truncation

```sh
$ arbite file search "def " src/arbite
src/arbite/cli.py:56    def _split_csv(value):
… 499 more matches
truncated: 499 more matches in 11 files; narrow with 'arbite file search "def " src/arbite/sinks',
           or continue with '--after src/arbite/schema.py:470'
# exit 0
```

## LS5 · scratch is invisible to discovery

```sh
$ arbite file list .arbite
.arbite/AGENTS.md                  (generated, not a ticket)
.arbite/agents/claude.opus.001.md     1 lines   0.0 KiB  unclaimed
.arbite/project.yaml
.arbite/scratch/                   (transport, 1 file -- not listed as a file, never claimable)
4 entries (no truncation)

$ arbite file search "base.py" .arbite/scratch
no matches (scratch is excluded from discovery: it is transport, not project content)
# exit 2
```

*target:* the scratch area and the coordination tree are invisible to `file
list`, `file search`, ticket scanning and claims. Non-`.md` extensions plus an
entry in the file sink's `RESERVED_DIRS` are the mechanism, so a stray payload
can never be read as a ticket.

## LS6 · refuse a protected or escaping path

```sh
$ arbite file read ../../etc/passwd
error: '../../etc/passwd' resolves outside the workspace root (/media/matt/m2tb/projects/arbite);
       paths are validated against the project root
next: 'arbite file list .' to list the workspace
# exit 1
```

```sh
$ arbite file list .git
error: '.git' is protected: arbite does not manage .git metadata
# exit 1
```

---

# RD — reads

## RD1 · read an unclaimed file

```sh
$ arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd
src/arbite/sinks/base.py  sha256:77c0ab19d3f1  570 lines  26.4 KiB
claim: none (readable by anyone)   workspace: ws-7c41
read token: op-4f19 (spent after one mutation of this path)
---
 144 | class TicketSink(ABC):
 145 |     """A ticket store. Implementations: `FileSink`, `SqliteSink`.
```

## RD2 · read a file another attempt holds

```sh
$ arbite file read src/arbite/sinks/file.py --ticket tic-9b57 --attempt att-4c81
src/arbite/sinks/file.py  sha256:4b8a1f0c9d2e  412 lines  18.4 KiB
claim: HELD by tic-cf9f / att-91bd (claude.opus.001) since 06:12:04, generation 3
       bytes are served, but this read token cannot authorize a write
read token: op-7f3a (read-only)
---
```

```sh
$ arbite file read src/arbite/sinks/file.py --ticket tic-9b57 --attempt att-4c81 --fail-if-busy
busy: src/arbite/sinks/file.py is held by tic-cf9f / att-91bd (claude.opus.001) since 06:12:04
no bytes were served
next: plan against an unclaimed path, or 'arbite file claims' to see what is free
# exit 4
```

*target:* two behaviours, one flag: bytes plus a busy banner by default, and a
token-saving refusal when the caller knows it cannot use the bytes.

## RD3 · ranged read keeps the whole-file digest

```sh
$ arbite file read src/arbite/cli.py --ticket tic-cf9f --attempt att-91bd --lines 1254:1260
src/arbite/cli.py  sha256:9a1c40de77b2  3015 lines  142.0 KiB
claim: none   lines 1254-1260 of 3015   read token: op-88ba
---
1254 | def cmd_claim(args):
1255 |     """Claim a ticket for an agent: ...
```

*target:* a range never narrows the digest, because a range-scoped hash would
authorize a range-scoped lie.

## RD4 · read after an unattributed external edit

```sh
$ arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd
src/arbite/sinks/base.py  sha256:c19d7be0a4f5  588 lines  27.1 KiB
claim: none (readable by anyone)   workspace: ws-7c41
note: on-disk bytes differ from the last version arbite observed (sha256:77c0ab19d3f1);
      an external edit is attributable to no ticket
read token: op-c201 (spent after one mutation of this path)
---
 144 | class TicketSink(ABC):
 145 |     """A ticket store. Implementations: `FileSink`, `SqliteSink`.
```

## RD5 · read a path that does not exist

```sh
$ arbite file read src/arbite/sinks/old.py --ticket tic-cf9f --attempt att-91bd
error: no such path 'src/arbite/sinks/old.py'
next: 'arbite file list src/arbite/sinks' to see what exists, or
      'arbite file claim src/arbite/sinks/old.py --ticket tic-cf9f --attempt att-91bd' to create it
# exit 1
```

*target:* creating a file is an explicit, claimable act, and the refusal says so
instead of leaving the agent to reach for `touch`.

---

# FC — file claims

## FC1 · claim one path

```sh
$ arbite file claim src/arbite/sinks/file.py --ticket tic-cf9f --attempt att-91bd
claimed 1 path for tic-cf9f / att-91bd (generation 1):
  src/arbite/sinks/file.py   sha256:4b8a1f0c9d2e  412 lines
next: 'arbite file read src/arbite/sinks/file.py --ticket tic-cf9f --attempt att-91bd'
      -- a pre-claim read does not authorize a write
# exit 0
```

## FC2 · claim several, all-or-nothing

```sh
$ arbite file claim src/arbite/sinks/file.py src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd
claimed 2 paths for tic-cf9f / att-91bd (generation 2):
  src/arbite/sinks/base.py   sha256:77c0ab19d3f1  570 lines
  src/arbite/sinks/file.py   sha256:4b8a1f0c9d2e  412 lines
acquired in canonical path order; all-or-nothing, so a conflict leaves no partial claims
# exit 0
```

## FC3 · one busy path in a multi-path claim

```sh
$ arbite file claim src/arbite/sinks/file.py src/arbite/schema.py --ticket tic-9b57 --attempt att-4c81
busy: 1 of 2 paths is held; nothing was claimed (all-or-nothing)
  src/arbite/schema.py      held by tic-cf9f / att-91bd (claude.opus.001) since 06:12:04, gen 3
  src/arbite/sinks/file.py  free
your attempt att-4c81 holds no claims and is still active
next: 'arbite file list src/arbite/sinks' to plan against what is free, or
      'arbite changes tic-cf9f' to see what the holder has done, or
      'arbite release tic-9b57 --agent claude.sonnet.002 --reason "needs src/arbite/schema.py"'
# exit 4
```

JSON (abridged):

```json
{
  "error": "file_busy",
  "held": [{"path": "src/arbite/schema.py", "ticket": "tic-cf9f", "attempt": "att-91bd",
            "actor": "claude.opus.001", "generation": 3, "since": "2026-09-21T13:12:41Z"}],
  "free": ["src/arbite/sinks/file.py"],
  "claimed": [],
  "next_actions": [
    "arbite file list src/arbite/sinks",
    "arbite changes tic-cf9f",
    "arbite release tic-9b57 --agent claude.sonnet.002 --reason \"needs src/arbite/schema.py\""
  ]
}
```

*target:* no waiting, no stealing, holder named, alternatives offered, nothing
claimed.

## FC4 · claim a path that does not exist yet

```sh
$ arbite file claim src/arbite/coordination/records.py --ticket tic-cf9f --attempt att-91bd
claimed 1 path for tic-cf9f / att-91bd (generation 3):
  src/arbite/coordination/records.py   ABSENT (creation is authorized by a probe receipt)
# exit 0
```

## FC5 · claim for an attempt that does not own the ticket

```sh
$ arbite file claim src/arbite/sinks/base.py --ticket tic-1a75 --attempt att-c50e
error: tic-1a75 is in_progress for att-91bd; attempt att-c50e does not own this ticket
next: 'arbite file claim src/arbite/sinks/base.py --ticket tic-1a75 --attempt att-91bd',
      or 'arbite claim tic-1a75 --agent <your-id> --force --reason "<why>"' to take it over
# exit 1
```

## FC6 · what is held right now

```sh
$ arbite file claims
2 active claims in ws-7c41:
  src/arbite/schema.py      tic-cf9f / att-91bd  claude.opus.001    gen 3  since 06:12:41  sha256:1f3a9c04b2d8
  src/arbite/sinks/base.py  tic-cf9f / att-91bd  claude.opus.001    gen 2  since 06:13:41  sha256:77c0ab19d3f1
# exit 0
```

*target:* "who holds what" is one command, so an agent never infers it from `ls`
or from failures.

## FC7 · release one of several

```sh
$ arbite file release src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --reason "edits complete"
released src/arbite/sinks/base.py (claim generation 2 revoked; the attempt stays active)
bytes on disk are unchanged and stay visible to the next worker (sha256:77c0ab19d3f1)
next: another mutation of this path needs a fresh claim and a fresh read
# exit 0
```

## FC8 · re-acquire after release

```sh
$ arbite file claim src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd
claimed 1 path for tic-cf9f / att-91bd (generation 3):
  src/arbite/sinks/base.py   sha256:77c0ab19d3f1
note: this is a new claim generation (3); any token from generation 2 is dead
# exit 0
```

---

# SC — scratch transport

## SC1 · a payload is consumed on success

```sh
$ arbite scratch list
1 file in .arbite/scratch/:
  base.py   4.1 KiB  written 06:13:02 (agent claude.opus.001)
# exit 0
```

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input base.py
wrote src/arbite/sinks/base.py  sha256:aa10f7b2c4e9  570 -> 588 lines  +18 -0
receipt: op-2b8d17 · tic-cf9f / att-91bd · claim gen 2 · before sha256:77c0ab19d3f1
payload: .arbite/scratch/base.py consumed and cleared (bytes retained as receipt artifact)
next: the read token op-4f19 is spent -- 'arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd' before another change
# exit 0
```

*target:* the agent never has to remember what it wrote; the payload is cleared
because the bytes now live in the receipt.

## SC2 · a payload survives a failure

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-9b57 --attempt att-4c81 --read-token op-1c77 --input base.py
stale_read: you read sha256:1f3a9c04b2d8 but the file is now sha256:77c0ab19d3f1 (changed 06:14:02)
no bytes were changed
payload: .arbite/scratch/base.py kept, so you can re-apply without re-sending the file
next: 'arbite file read src/arbite/sinks/base.py --ticket tic-9b57 --attempt att-4c81',
      re-apply your change, then write with the new token
# exit 5
```

*target:* keep-on-failure is the asymmetry that makes a recoverable error cheap
instead of forcing a model to re-emit an entire file.

## SC3 · stdin payload, no file at all

```sh
$ arbite file write src/arbite/coordination/records.py --ticket tic-cf9f --attempt att-91bd --read-token op-5d11 --input -
(payload read from stdin: 84 lines, 2.3 KiB)
wrote src/arbite/coordination/records.py  sha256:c02b77a1e8d4  created, 84 lines
receipt: op-3c90 · tic-cf9f / att-91bd · claim gen 3
# exit 0
```

## SC4 · clear scratch

```sh
$ arbite scratch clear --all
cleared 1 file from .arbite/scratch/ (base.py, 4.1 KiB)
# exit 0
```

```sh
$ arbite scratch clear base.py
cleared .arbite/scratch/base.py (4.1 KiB)
next: 'arbite scratch list' to see what remains
# exit 0
```

## SC5 · refuse a payload from outside the project

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input /tmp/base.py
error: '--input' takes a name inside .arbite/scratch/ or '-' for stdin; '/tmp/base.py' is outside the project
next: 'arbite scratch list' to see staged payloads, or pipe the content with '--input -'
# exit 1
```

*target:* the only documented payload paths are the project's own scratch area and
stdin. Nothing in the documented workflow needs a write outside the project, which
is what keeps the harness from prompting on every edit.

---

# WR — whole-file writes

## WR1 · write with a valid token

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input base.py
wrote src/arbite/sinks/base.py  sha256:aa10f7b2c4e9  570 -> 588 lines  +18 -0
receipt: op-2b8d17 · tic-cf9f / att-91bd · claim gen 2 · before sha256:77c0ab19d3f1
payload: .arbite/scratch/base.py consumed and cleared (bytes retained as receipt artifact)
next: the read token op-4f19 is spent -- 'arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd' before another change
# exit 0
```

## WR2 · stale token after a concurrent change

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-9b57 --attempt att-4c81 --read-token op-1c77 --input base.py
stale_read: you read sha256:1f3a9c04b2d8 but the file is now sha256:77c0ab19d3f1 (changed 06:14:02)
no bytes were changed
payload: .arbite/scratch/base.py kept, so you can re-apply without re-sending the file
next: 'arbite file read src/arbite/sinks/base.py --ticket tic-9b57 --attempt att-4c81',
      re-apply your change, then write with the new token
# exit 5
```

*target:* the plan-a-version-that-changed case, answered with both digests and a
repair path.

## WR3 · one token, two writes

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input base.py
stale_read: read token op-4f19 was already spent by op-2b8d17
no bytes were changed
next: 'arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd' for a fresh token
# exit 5
```

## WR4 · write without a claim

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-88ba --input base.py
error: tic-cf9f / att-91bd does not hold a claim on src/arbite/sinks/base.py (no_claim);
       a read does not authorize a write
next: 'arbite file claim src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd'
# exit 1
```

## WR5 · write after the ticket closed

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input base.py
stale_read: attempt att-91bd generation 2 is no longer current (tic-cf9f closed 06:20:03)
no bytes were changed
next: reopen the ticket ('arbite reopen tic-cf9f --reason "<why>"') and claim it again, or stop work on it
# exit 5
```

## WR6 · write a binary file

```sh
$ arbite file write assets/icon.png --ticket tic-9b57 --attempt att-4c81 --read-token op-5d11 --input icon.png
wrote assets/icon.png  sha256:e4d9a1c07b3f  1024 -> 1187 bytes (binary)
receipt: op-3c90 · tic-9b57 / att-4c81 · claim gen 1 (binary receipt holds a byte payload, not a text diff)
# exit 0
```

## WR7 · refuse a write with no declared paths and no claim context

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --input base.py
error: --attempt is required: every mutation is attributed to a work attempt
next: 'arbite claim tic-cf9f --agent <your-id>' to start one
# exit 1
```

---

# ED — targeted edits

## ED1 · edit batch

```sh
$ arbite file edit src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --edits edits.json
edited src/arbite/sinks/base.py  sha256:aa10f7b2c4e9  570 -> 573 lines  +5 -2
  1/2 replace at line 222: "The one write path" -> "The single write path"
  2/2 replace at line 229: "Raises Conflict" -> "Raises Conflict or StaleRead"
receipt: op-2b8d17 · claim gen 2 · payload .arbite/scratch/edits.json consumed and cleared
# exit 0
```

## ED2 · an ambiguous edit changes nothing

```sh
$ arbite file edit src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --edits edits.json
error: edit 2/3 does not apply: "Raises Conflict" occurs 3 times (lines 85, 229, 366);
       an edit must name one occurrence; no bytes were changed
next: read those lines ('arbite file read src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --lines 80:90'),
      then re-send the edit with an explicit occurrence
# exit 1
```

## ED3 · edit from stdin

```sh
$ arbite file edit src/arbite/schema.py --ticket tic-cf9f --attempt att-91bd --read-token op-88ba --edits -
(payload read from stdin: 2 edits)
edited src/arbite/schema.py  sha256:b7710e4cc9aa  657 -> 660 lines  +3 -0
  1/2 replace at line 2: "import os" -> "import os"
  2/2 replace at line 1: "from __future__ import annotations" -> "from __future__ import annotations"
receipt: op-4d17 · claim gen 3
# exit 0
```

---

# RN — remove and rename

## RN1 · rename claims both paths

```sh
$ arbite file rename src/arbite/old.py src/arbite/new.py --ticket tic-9b57 --attempt att-4c81 --read-token op-5d11
renamed src/arbite/old.py -> src/arbite/new.py  sha256:c02b77a1e8d4
claim: moved to src/arbite/new.py (generation 2); src/arbite/old.py released
receipt: op-9a14 · tic-9b57 / att-4c81
# exit 0
```

## RN2 · rename onto an existing path

```sh
$ arbite file rename src/arbite/a.py src/arbite/b.py --ticket tic-9b57 --attempt att-4c81 --read-token op-5d11
error: destination src/arbite/b.py exists (sha256:aa10f7b2c4e9) and no destination version was given
next: re-run with '--expect-dest sha256:aa10f7b2c4e9' to replace it deliberately, or pick another name
# exit 1
```

## RN3 · remove

```sh
$ arbite file remove src/arbite/dead.py --ticket tic-9b57 --attempt att-4c81 --read-token op-5d11
removed src/arbite/dead.py (was sha256:d81f002ac39b, 84 lines; bytes kept in receipt op-77ff)
# exit 0
```

## RN4 · no recursive directory deletion

```sh
$ arbite file remove src/arbite/legacy
error: 'src/arbite/legacy' is a directory; recursive deletion is not supported
next: remove the files individually ('arbite file list src/arbite/legacy'), then 'rmdir src/arbite/legacy'
# exit 1
```

---

# EV — evidence and events

## EV1 · net changes for a ticket

```sh
$ arbite changes tic-cf9f
tic-cf9f · attempt att-91bd (claude.opus.001) · 5 operations
M src/arbite/sinks/base.py  sha256:77c0ab19d3f1 -> sha256:aa10f7b2c4e9  +18 -0         (op-2b8d17)
A src/arbite/coordination/records.py                                            created        (op-3f02)
M src/arbite/schema.py      sha256:1f3a9c04b2d8 -> sha256:1f3a9c04b2d8  no net change  (op-51bb, op-6cd2)
      edit-then-revert: both operations remain in the log ('--all')
# exit 0
```

## EV2 · tail the stream

```sh
$ arbite events --tail 4
31  write.file    src/arbite/sinks/base.py    op-9a14  tic-cf9f/att-91bd  claude.opus.001   06:14:02  +18 -0
32  claim.file    src/arbite/schema.py        op-3f02  tic-cf9f/att-91bd  claude.opus.001   06:15:11  gen 3
33  read.file     src/arbite/graph.py         op-4a90  tic-9b57/att-4c81  claude.sonnet.002 06:15:40  read-only
34  close.ticket  tic-cf9f                    op-7c11  tic-cf9f/att-91bd  claude.opus.001   06:20:03  2 claims released
cursor: 34 (resume with 'arbite events --after 34')
# exit 0
```

*target:* one line per event carrying kind, subject, ticket, attempt, actor, time
and the outcome — readable at a glance, no second command needed to identify who
did what.

## EV3 · resume from a cursor

```sh
$ arbite events --after 32
33  read.file     src/arbite/graph.py         op-4a90  tic-9b57/att-4c81  claude.sonnet.002 06:15:40  read-only
34  close.ticket  tic-cf9f                    op-7c11  tic-cf9f/att-91bd  claude.opus.001   06:20:03  2 claims released
cursor: 34 (resume with 'arbite events --after 34')
# exit 0
```

JSON (abridged) — the orchestrator's poll shape:

```json
{
  "events": [{"cursor": 33, "kind": "read.file", "subject": "src/arbite/graph.py",
              "ticket": "tic-9b57", "attempt": "att-4c81", "actor": "claude.sonnet.002",
              "operation": "op-4a90", "at": "2026-09-21T13:15:40Z", "result": "read-only"}],
  "cursor": 34,
  "next_actions": ["arbite events --after 34"]
}
```

## EV4 · nothing new since a cursor

```sh
$ arbite events --after 34
no events since cursor 34
cursor: 34
# exit 2
```

*target:* "nothing happened" is an answer with an exit code, so a polling loop
does not have to parse text to know it.

## EV5 · reads are a separate category

```sh
$ arbite events --tail 2 --include-reads
33  read.file     src/arbite/graph.py    op-4a90  tic-9b57/att-4c81  claude.sonnet.002 06:15:40  read-only
34  close.ticket  tic-cf9f               op-7c11  tic-cf9f/att-91bd  claude.opus.001   06:20:03  2 claims released
cursor: 34
# exit 0
```

*target:* mutation and lifecycle events are the default so a progress poll is
signal; read observations need `--include-reads`, because a research-heavy agent
emits dozens of reads per write.

## EV6 · one receipt

```sh
$ arbite receipt op-2b8d17
operation: op-2b8d17  kind: write  result: ok  2026-09-21T13:14:02Z
ticket: tic-cf9f  attempt: att-91bd  actor: claude.opus.001  claim generation: 2
path: src/arbite/sinks/base.py
before: sha256:77c0ab19d3f1 (570 lines)   after: sha256:aa10f7b2c4e9 (588 lines)
artifact: before image stored and retained
# exit 0
```

## EV7 · no `--follow`

```sh
$ arbite events --follow
error: unknown argument '--follow'; arbite commands are one-shot and never block
next: 'arbite events --tail 20' to read the end of the stream, or poll with
      'arbite events --after <cursor>' and keep the cursor in your own loop
# exit 1
```

*target:* the plan's "no background watcher, no sleeping loop, no worker process"
is enforced by refusing the flag, and the refusal teaches the correct pattern.

---

# PC — passthrough (`arbite cmd`)

## PC1 · wrap a familiar tool in observed mode

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd -- sed -i 's/O_EXCL/O_EXCL|O_NOFOLLOW/' src/arbite/sinks/file.py
arbite cmd: sed -i s/O_EXCL/O_EXCL|O_NOFOLLOW/ src/arbite/sinks/file.py
exit: 0 (14 ms)  mode: observed (no exclusivity claimed)
changed 1 path:
  M src/arbite/sinks/file.py  sha256:4b8a1f0c9d2e -> sha256:9c2e40a71b88  +1 -1  (op-8a31)
event: passthrough.exec   tool: sed   ticket: tic-cf9f / att-91bd  actor: claude.opus.001
next: 'arbite changes tic-cf9f' to review, or claim paths next time ('--claim src/arbite/sinks/file.py') for exclusivity
# exit 0
```

*target:* an agent keeps `sed` muscle memory and arbite still gets a receipt and
an event. Observed mode does not claim exclusivity, and says so.

## PC2 · guarded mode

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd --claim src/arbite/sinks/file.py -- sed -i 's/O_EXCL/O_EXCL|O_NOFOLLOW/' src/arbite/sinks/file.py
claimed 1 path for tic-cf9f / att-91bd (generation 4):
  src/arbite/sinks/file.py   sha256:4b8a1f0c9d2e  412 lines
arbite cmd: sed -i s/O_EXCL/O_EXCL|O_NOFOLLOW/ src/arbite/sinks/file.py
exit: 0 (14 ms)  mode: guarded (exclusive on 1 path)
changed 1 path, all inside the claimed set:
  M src/arbite/sinks/file.py  sha256:4b8a1f0c9d2e -> sha256:9c2e40a71b88  +1 -1  (op-8a31)
event: passthrough.exec   tool: sed   ticket: tic-cf9f / att-91bd  actor: claude.opus.001
claims released (work complete for this command)
next: 'arbite changes tic-cf9f' to review
# exit 0
```

## PC3 · guarded mode, a busy declared path

```sh
$ arbite cmd --ticket tic-9b57 --attempt att-4c81 --claim src/arbite/schema.py -- sed -i 's/a/b/' src/arbite/schema.py
busy: 1 of 1 declared path is held; nothing was claimed and the command did not run
  src/arbite/schema.py  held by tic-cf9f / att-91bd (claude.opus.001) since 06:12:04, gen 3
command did not run
next: work a different ticket ('arbite list next --claim claude.sonnet.002'), or
      'arbite changes tic-cf9f' to see whether the holder has finished
# exit 125
```

*target:* passthrough cannot become the bypass that quietly evades claims — a
guarded command refuses before running, and its exit code cannot be confused with
the wrapped tool's own result.

## PC4 · guarded mode, a change outside the claimed set

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd --claim src/arbite/schema.py -- sed -i 's/x/y/' src/arbite/schema.py src/arbite/query.py
claimed 1 path for tic-cf9f / att-91bd (generation 1):
  src/arbite/schema.py   sha256:1f3a9c04b2d8  40 lines
arbite cmd: sed -i s/x/y/ src/arbite/schema.py src/arbite/query.py
exit: 0 (11 ms)  mode: guarded (exclusive on 1 path)
changed 2 paths, 1 OUTSIDE the claimed set:
  M src/arbite/query.py   sha256:aa9c31f0be77 -> sha256:bb02d1c93e10  +1 -1  NOT claimed  (op-8a32)
  M src/arbite/schema.py  sha256:1f3a9c04b2d8 -> sha256:9d0b7c11a4e2  +1 -1  claimed  (op-8a31)
event: passthrough.exec   tool: sed   ticket: tic-cf9f / att-91bd  actor: claude.opus.001
unclaimed_write: src/arbite/query.py was modified without being claimed; the bytes are recorded
                 and left as they are (arbite does not undo a command it did not perform)
claims released (the run is over; the unclaimed write is left in place)
next: 'arbite file claim src/arbite/query.py --ticket tic-cf9f --attempt att-91bd' and re-read it,
      or 'arbite changes tic-cf9f' and correct by hand
# exit 1
```

*target:* drift is detected and attributed honestly instead of being hidden or
silently rolled back.

## PC5 · shell mode, stated limits

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd --shell -- 'grep -c def src/arbite/cli.py > .arbite/scratch/count.txt'
arbite cmd: sh -c 'grep -c def src/arbite/cli.py > .arbite/scratch/count.txt'
exit: 0 (9 ms)  mode: observed  note: redirections happen in the shell and are visible only after the fact
event: passthrough.exec   tool: sh   ticket: tic-cf9f / att-91bd  actor: claude.opus.001
# exit 0
```

## PC6 · refusals

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd -- sed -i 's/a/b/' src/arbite/cli.py > /tmp/sed.out
error: '>' style shell syntax needs '--shell'; without it arbite executes argv directly
next: re-run with '--shell -- "<command>"', or pass arguments without shell syntax
# exit 126
```

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd -- not-a-real-tool --version
error: 'not-a-real-tool' was not found on PATH
command did not run
# exit 127
```

```sh
$ arbite cmd --ticket tic-cf9f --attempt att-91bd -- vim src/arbite/cli.py
error: interactive commands are not supported (no terminal is provided)
next: edit through 'arbite file edit', or run the editor outside arbite and accept that the change is unattributed
# exit 126
```

---

# LC — lifecycle cascade

## LC1 · close releases claims

```sh
$ arbite close tic-cf9f
closed tic-cf9f (claude.opus.001) -> .arbite/closed/2026-09/tic-cf9f.md
ended attempt att-91bd (2 file claims released):
  src/arbite/schema.py      free (last written by tic-cf9f, sha256:1f3a9c04b2d8)
  src/arbite/sinks/base.py  free (unchanged since claim, sha256:aa10f7b2c4e9)
receipts: 5 operations retained ('arbite changes tic-cf9f')
# exit 0
```

## LC2 · block ends the attempt and keeps partial work

```sh
$ arbite block tic-9b57 --reason "waiting on tic-cf9f to close"
blocked tic-9b57 (blocked_by: waiting on tic-cf9f to close) -> .arbite/blocked/tic-9b57.md
ended attempt att-4c81 (1 file claim released): src/arbite/sinks/file.py is free
partial work is left on disk and visible; the next worker must re-read it
next: 'arbite unblock tic-9b57 --agent claude.sonnet.002' when the blocker clears (a fresh attempt is created)
# exit 0
```

## LC3 · delete refused while claims are live

```sh
$ arbite delete tic-cf9f --force
error: tic-cf9f has an active attempt (att-91bd) with 2 file claims; delete would discard change history
next: 'arbite release tic-cf9f --agent claude.opus.001' then delete, or 'arbite close tic-cf9f' to keep the record
# exit 1
```

## LC4 · reopen does not resurrect claims

```sh
$ arbite reopen tic-cf9f --reason "review found the lock window unguarded" --agent claude.haiku.003
reopened tic-cf9f -> .arbite/open/tic-cf9f.md (reason recorded)
note: previous attempts and file claims are historical; nothing is re-acquired
next: 'arbite claim tic-cf9f --agent claude.haiku.003' to start a new attempt
# exit 0
```

## LC5 · setters cannot bypass the lifecycle

```sh
$ arbite set tic-cf9f status closed
error: 'set status' cannot close a ticket that has an active attempt and file claims;
       use 'arbite close tic-cf9f' so the attempt ends and its claims are released
next: 'arbite close tic-cf9f'
# exit 1
```

*target:* the plan's "no backdoor through force": the generic setter routes to the
lifecycle command or refuses.

---

# DR — doctor and recovery

## DR1 · problems found

```sh
$ arbite doctor
problem [tic-cf9f] orphaned_claim: claim on src/arbite/sinks/base.py names attempt att-91bd,
        which is not active (ticket closed 2026-09-21T13:20:03Z)
problem [tic-1a75] pending_operation: op-4f19 staged a write of src/arbite/sinks/base.py and was
        not finalized; on-disk bytes (sha256:d33e77c0a1b2) match neither before
        (sha256:77c0ab19d3f1) nor after (sha256:aa10f7b2c4e9)
problem [tic-1a75] claim_without_attempt: claim on src/arbite/schema.py names att-91bd, which
        does not exist in this store
note: .arbite/scratch/ holds 3 files (12.4 KiB) -- transport left behind, expected after an
      interrupted run; clear with 'arbite scratch clear --all'
checked 22 tickets: 3 problem(s), 0 fixed
re-run with --fix to repair what arbite can correct automatically
# exit 3
```

*target:* scratch is *reported* — the owner asked to know whether it ever gets
cleaned up — but it is a note, not a problem, so it never changes the exit code.

## DR2 · `--fix` repairs only the unambiguous

```sh
$ arbite doctor --fix
fixed [tic-cf9f] orphaned_claim: released the claim on src/arbite/sinks/base.py (bytes and receipts unchanged)
problem [tic-1a75] pending_operation: op-4f19 ... (not fixed: arbite will not guess which version is
        correct; inspect 'arbite receipt op-4f19' and 'arbite changes tic-1a75', then restore or re-apply by hand)
fixed [tic-1a75] claim_without_attempt: released the claim on src/arbite/schema.py
note: .arbite/scratch/ holds 3 files (12.4 KiB)
checked 22 tickets: 1 problem(s), 2 fixed
# exit 3
```

## DR3 · clean store with empty scratch

```sh
$ arbite doctor
checked 22 tickets: no problems found
note: .arbite/scratch/ is empty
# exit 0
```

## DR4 · doctor names the store's coordination backend

```sh
$ arbite doctor --json
{
  "sink": {"kind": "file", "root": ".arbite"},
  "coordination": {"kind": "file", "root": ".arbite/coordination",
                   "claims_active": 2, "events": 31, "pending_operations": 1},
  "tickets_checked": 22, "problems": [], "fixed": 0, "remaining": 0,
  "scratch": {"files": 3, "bytes": 12700}
}
# exit 0
```

---

# RC — race transcripts

## RC1 · two workers, one ticket, one file

```sh
# worker A                                            # worker B
$ arbite list next --claim claude.opus.001
claimed tic-cf9f -> .arbite/in_progress/tic-cf9f.md
attempt: att-91bd
                                                      $ arbite list next --claim claude.sonnet.002
                                                      tic-9b57  open  ...  (tic-cf9f not offered: attempted)
$ arbite file claim src/arbite/schema.py --ticket tic-cf9f --attempt att-91bd
claimed 1 path ... (generation 1)
                                                      $ arbite file claim src/arbite/schema.py --ticket tic-9b57 --attempt att-4c81
                                                      busy: ... held by tic-cf9f / att-91bd since 06:12:41, gen 1
                                                      # exit 4, nothing claimed, no waiting
```

*target:* exactly one winner, the loser told why in one line, no partial state.

## RC2 · close races a write

```sh
$ arbite file write src/arbite/sinks/base.py --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input base.py
stale_read: attempt att-91bd generation 2 is no longer current (tic-cf9f closed 06:20:03)
no bytes were changed
# exit 5
```

*target:* either the write completes first and the close records it, or the close
wins and no observer can mutate under the old token — never both.

---

# BY — honest limits

## BY1 · a shell write is drift, not an error

```sh
$ sed -i 's/foo/bar/' src/arbite/schema.py     # bypasses arbite entirely
$ arbite file read src/arbite/schema.py --ticket tic-cf9f --attempt att-91bd
src/arbite/schema.py  sha256:b7710e4cc9aa  ...
claim: none
note: on-disk bytes differ from the last version arbite observed (sha256:1f3a9c04b2d8);
      the change is attributable to no ticket -- arbite enforces its own operations and
      cannot prove who wrote a file
```

## BY2 · generated output is refused by default

```sh
$ arbite file write .pytest_cache/v/cache/lastfailed --ticket tic-cf9f --attempt att-91bd --read-token op-4f19 --input lastfailed
error: '.pytest_cache/v/cache/lastfailed' is excluded by policy (generated or build output);
       arbite will not record a proxy write it cannot attribute
next: keep generated output outside managed source paths
# exit 1
```

## BY3 · the guide says what arbite does not enforce

```sh
$ arbite file list docs
docs/arbite-guide.md   84 lines   3.2 KiB  unclaimed
1 file (no truncation)
# exit 0
```

*target:* the generated guide states plainly that agents are instructed to use
these tools, that a shell or editor can bypass them, that arbite detects observed
drift, and that it cannot prove who made a direct filesystem change. The claim is
about what arbite enforces, not about what it wishes were true.

---

# Scenario to ticket map

| Ticket | Key | Scenarios that must pass |
| --- | --- | --- |
| Define versioned coordination records and application operations | C01 | WS1, WS2, DR4 |
| Implement transactional coordination storage and durable events | C02 | EV2, EV3, EV4, EV5, EV7 |
| Create work attempts and guard every ticket acquisition path | C03 | CL1, CL2, CL3, CL4, CL5, CL6, LC5, RC1 |
| Implement canonical paths and exclusive file claims | C04 | FC1, FC2, FC3, FC4, FC5, FC6, FC7, FC8, LS6, RD5 |
| Build the file-operation intent journal and recovery engine | C05 | DR1, DR2, RC2 |
| Expose bounded discovery and versioned reads | C06 | LS1, LS2, LS3, LS4, LS5, RD1, RD2, RD3, RD4 |
| Expose version-checked writes and exact edits | C07 | WR1, WR4, WR5, WR6, WR7, ED1, ED2, ED3, BY2 |
| Expose tracked creation, removal and rename | C08 | RN1, RN2, RN3, RN4, FC4 |
| Add scratch payload transport | C09 | SC1, SC2, SC3, SC4, SC5, DR3 |
| Cascade ticket lifecycle through file ownership and receipts | C10 | CL7, LC1, LC2, LC3, LC4 |
| Expose change receipts and net ticket change views | C11 | EV1, EV6 |
| Add coordination migrations, export and integrity recovery | C12 | no single-command scenarios; multi-record round trip |
| Add passthrough command observation | C13 | PC1, PC5, PC6 |
| Add passthrough guarded mode | C14 | PC2, PC3, PC4 |
| Validate and document the shared-directory proxy workflow | C15 | BY1, BY3, all exit codes, full-suite conformance |

The wishlist ticket for mandating passthrough carries no scenarios yet by design;
it is a policy question, not a behaviour.
