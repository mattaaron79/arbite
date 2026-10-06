<!-- BEGIN ARBITE INSTRUCTIONS -->
# Arbite Ticketing System

## Docs

Arbite documents itself from the command line: run `arbite docs` for the overview,
`arbite docs list` for every topic, and `arbite docs commands <name>` for a command's
flags. This block is the short version; `arbite docs` is the source of truth.

## Ticketing
Use arbite for all tasks. Create a ticket if required and claim it before starting work. Tickets live under `.arbite/` in state folders (`open/`, `in_progress/`, `review/`, `blocked/`, `shelved/`, `closed/YYYY-MM/`); `wishlist/` and `plans/` are buckets, not statuses, and `raw/processed/` holds frozen snapshots of promoted captures. The store and the review behaviour come from `.arbite/project.yaml` (`sink:`, `review:`).

# Agent Identity

When claiming a ticket, use an identity like "claude.opus-5.001" -- company.model.instance, your best educated guess unless told otherwise. If orchestrating, tell subagents their identity and instance number.

## Sole command: "Work Next|All <epic>"

```bash
arbite list next                      # next workable ticket
arbite list --topo --status open [--epic <epic>]
```

If there are no tickets, use "Classify". If "Work All", orchestrate if that is in your skill set, otherwise work in sequence until finished.

## Sole command "Classify"

```bash
arbite list raw
```

Classify every raw ticket with `arbite promote <id> --title ... --tier ... --domain ...` (add `--description`/`--epic`/`--priority`/`--tags` as you learn more). Add `--agent <your-id>` to classify and claim one you will work yourself; a wish is reclassified and filed in the wishlist bucket instead of being opened.

## Workflow: claim -> in_progress -> submit -> review -> accept

```bash
arbite claim <id> --agent <your-id>        # take it: status -> in_progress
arbite stream write <id> -                 # narrate as you work (piped from your output)
arbite note <id> <your-id> "what changed"  # log progress as you go
arbite submit <id>                         # finish: status -> review, assignee kept
arbite accept <id> --agent <reviewer-id>   # the reviewer closes it, credited to them
```

Send work back with `arbite reopen <id> --reason "<why>"` (the reason is required -- it is the only record of what the author must fix). With `review: false` in `.arbite/project.yaml`, `arbite submit` closes the ticket directly instead of parking it in review.

# Ticketing etiquette addendum

When closing a ticket, add a note paragraph explaining what the user, QA, or other agents will be able to observe via integration testing (and any new effects that will be observable).

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->
