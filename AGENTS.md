<!-- BEGIN ARBITE INSTRUCTIONS -->
# Arbite Ticketing System

## Ticketing
Use arbite ticketing system for all tasks. See /.arbite/AGENTS.md. Create a ticket if required and claim the ticket before starting work.

# Agent Identiy

When claiming a ticket, please use an identity format like: "claude.opus-5.001" where the company.model.instance is your best educated guess unless otherwise specified.
If orchestrating, let subagents know their identity and instance number.

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

Use your session to classify all tickets with `arbite promote <id> --title ... --tier ... --domain ...` (adding `--description`/`--epic`/`--priority`/`--tags` as you learn more), looking deeper into the requirements, adding notes, etc until all raw tickets are classified. Add `--agent <your-id>` to claim one you are going to work yourself; a wish is reclassified and filed in the wishlist bucket by the same command instead of being opened.

## Workflow: claim -> in_progress -> submit -> review -> accept

```bash
arbite claim <id> --agent <your-id>        # take it: status -> in_progress
arbite note <id> <your-id> "what changed"  # log progress as you go
arbite submit <id>                         # finish: status -> review, assignee kept
arbite accept <id> --agent <reviewer-id>   # the reviewer closes it, credited to them
```

A reviewer who sends work back uses `arbite reopen <id> --reason "<why>"` -- the reason is required, because it is the only record of what the author must fix. With the file sink a ticket's status is also the folder it sits in (`open/`, `in_progress/`, `review/`, `blocked/`, `shelved/`, `closed/YYYY-MM/`), while `wishlist/` and `plans/` are buckets rather than statuses and `raw/processed/` holds frozen snapshots of promoted captures. A project can set `review: false` in `.arbite/project.yaml`, in which case `arbite submit` closes the ticket directly instead of parking it in review.

# Ticketing etiquette addendum

In addition to etiquette specified in .arbite/AGENTS.md, please add to notes of ticket when closing a paragraph explaining what the user, QA, or other
agents will be able to observe via integration testing, if any new effects will be observable.

## Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

## Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
<!-- END ARBITE INSTRUCTIONS -->
