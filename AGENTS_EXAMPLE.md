<!-- BEGIN ARBITE INSTRUCTIONS -->
# Arbite Ticketing System

## Ticketing
Use arbite ticketing system for all tasks. See /.arbite/AGENTS.md. Create a ticket if required and claim the ticket before starting work.

# Agent Identiy

When claiming a ticket, please use an identity format like: "claude.opus-5.001" where the company.model.instance is your best educated guess unless otherwise specified.
If orchestrating, let subagents know their identity and instance number.

## Shared directory: use arbite for file work
Read, claim and mutate source files through arbite -- not a shell or editor -- so a competing agent cannot silently overwrite your work:
```bash
arbite file list [PATH] --json                        # discover
arbite file search PATTERN [PATH] --json             # discover
arbite file claim PATH --ticket T --attempt A        # own the whole file
arbite file read  PATH --ticket T --attempt A --json # RE-READ; a pre-claim read does not authorize a write
arbite file write|edit|remove|rename ... --read-token R
```
Re-read after every claim, takeover and ticket boundary, and take a fresh read before each mutation: a stale or consumed token is refused with `stale_read` and no bytes change.
Every `arbite file` command needs your active `--attempt` id; get it from `arbite export --scope coordination --no-artifacts` (the `work_attempts` entry with your ticket_id and `state: active`).
There is no runner, daemon, watcher or scheduler, and stale work is never taken over automatically -- agents are started manually and may use different providers; only `arbite claim --force --reason <why>` moves live work. Keep build/test output outside managed source paths: arbite records proxy mutations only, so a generated file written into the source tree is unattributed drift. Mutation evidence and artifacts accumulate and are never garbage-collected. Arbite enforces its own operations and reports observed drift, but it cannot prove who made a direct filesystem change -- an external editor or shell can still bypass the proxy.

## Sole command: "Work Next|All <epic>"

If your sole command is "Work Next" or "Work All", you can use the following commands to find the next arbite ticket(s):
```bash
arbite list next   # Show next workable ticket
arbite list --topo --status open [--epic <epic>]
```

Note: If there are no tickets, see next command "Classify". If "Work All", try to orchestrate tickets if that is in your skill set, otherwise
work in sequence until finished.

## Sole command "Classify"

If your sole command is "Classify" use the following command to list all tickets that require classification:
```bash
arbite list raw
```

Use your session to classify all tickets, looking deeper into the requirements, adding notes, etc until all raw tickets are classified.

# Ticketing etiquette addendum

In addition to etiquette specified in .arbite/AGENTS.md, please add to notes of ticket when closing a paragraph explaining what the user, QA, or other
agents will be able to observe via integration testing, if any new effects will be observable.

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->