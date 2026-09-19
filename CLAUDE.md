<!-- BEGIN ARBITE INSTRUCTIONS -->
# Arbite Ticketing System

## Ticketing
Use arbite ticketing system for all tasks. Read `.arbite/AGENTS.md` (the short quickstart) before your first arbite command; `.arbite/REFERENCE.md` is the full reference -- read only the section you need. Create a ticket if required and claim the ticket before starting work.

# Agent Identity

When claiming a ticket, please use an identity format like: "claude.opus-5.001" where the company.model.instance is your best educated guess unless otherwise specified.
If orchestrating, let subagents know their identity and instance number.

## Shared directory: use arbite for file work
Claim and mutate source files through arbite -- not a shell or editor -- so a competing agent cannot silently overwrite your work:
```bash
arbite claim T --agent ME                                           # prints your attempt id A
arbite file claim PATH [PATH ...] --ticket T --attempt A            # own every file of the task at once
arbite file read PATH --ticket T --attempt A --version-only --json  # read token R, no content served
arbite file edit PATH --ticket T --attempt A --read-token R --edits -    # stdin: [{"old": "...", "new": "..."}]
arbite file write PATH --ticket T --attempt A [--read-token R] --input - # new (claimed-absent) file: no token
```
Read files with your own tools -- reading changes nothing; only mutations must go through arbite. A read token proves the file is unchanged since you took it, not that you read it. Take a fresh one after every claim and before each mutation: tokens are single-use, and a stale or consumed token is refused with `stale_read` and no bytes change. Lost the attempt id? `arbite show T --json` reports `active_attempt.id`.
There is no runner, daemon, watcher or scheduler, and stale work is never taken over automatically -- agents are started manually and may use different providers; only `arbite claim --force --reason <why>` moves live work. Keep build/test output outside managed source paths: a generated file written into the source tree is unattributed drift. Mutation evidence is never garbage-collected. Arbite cannot prove who made a direct filesystem change -- an external editor or shell can still bypass the proxy.

## Sole command: "Work Next|All <epic>"

If your sole command is "Work Next" or "Work All", you can use the following commands to find the next arbite ticket(s):
```bash
arbite list next [--epic <epic>]   # Show next workable ticket
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

## Arbite feedback
Before closing a ticket, add one note that starts with `arbite feedback:` saying what helped and what got in the way when using arbite for that work (commands, refusals, extra calls, missing features, confusing docs). Keep it to a few lines, and say "nothing notable" for a side with nothing to report. The owner compiles these with `arbite search "arbite feedback:"`.

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->
