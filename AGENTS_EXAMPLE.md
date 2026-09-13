# Ticketing
Use arbite ticketing system for all tasks. See /.arbite/AGENTS.md. Create a ticket if required and claim the ticket before starting work.

# Sole command: "Work Next <epic>"

If your sole command is "Work Next", you can use the following commands to find the next arbite ticket:
arbite list next   # Show next workable ticket
arbite list --topo --status open [--epic <epic>]

Note: If there are no tickets, see next command "Classify"

# Sole command "Classify"

If your sole command is "Classify" use the following command to list all tickets that require classification:
arbite list raw

Use your session to classify all tickets, looking deeper into the requirements, adding notes, etc until all raw tickets are classified.

# Git
By default and unless otherwise specified, check into main/master after closing a ticket.

Do not modify this file without explicit permission.

# Comment Protocol

Do not fill the codebase with comments containing history, musings, or overly wordy explanations. Comments should be maximally useful and concise. Put long explanations and related context in Arbite tickets, and refer to the relevant ticket from a code comment when needed.
