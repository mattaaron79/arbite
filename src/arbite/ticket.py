"""Ticket read/write: YAML frontmatter + markdown body, folder <-> status sync."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

STATUSES = ["open", "in_progress", "blocked", "shelved", "closed"]

# Statuses that live directly under tickets/<status>/ (closed is special-cased
# into tickets/closed/YYYY-MM/).
FLAT_STATUS_DIRS = {"open", "in_progress", "blocked", "shelved"}

ID_PATTERN = re.compile(r"^tic-[0-9a-f]{4}$")

# Frontmatter field order, matching the schema in CLAUDE.md exactly.
FIELD_ORDER = [
    "id",
    "title",
    "status",
    "type",
    "tier",
    "domain",
    "epic",
    "priority",
    "tags",
    "assignee",
    "depends_on",
    "blocked_by",
    "created",
    "updated",
    "closed",
]

# Sentinels so unset priorities sort after every explicit numeric priority.
PRIORITY_MAX = float("inf")

DEFAULT_BODY = "## Description\n{description}\n\n## Notes\n"

BLANK_TITLE = "TODO: replace with a short title"
BLANK_TYPE = "TODO: bug|feature|refactor|chore"
BLANK_TIER = "TODO: low|medium|high"
BLANK_DOMAIN = "TODO: e.g. mesh, image_gen, audio_gen, ui, io"
BLANK_DESCRIPTION = "TODO: describe the task."
BLANK_WARNING = (
    "> **TEMPLATE -- not ready.** This ticket was scaffolded blank by "
    "`arbite create --blank` and has not been filled in yet. Do not claim or "
    "work it until a human has replaced the TODO placeholders above, written "
    "a real description below, and saved the file."
)

# Raw tickets (`arbite raw <memo|feature|bug> <message>`): deliberately
# unclassified quick captures. Only the type and a placeholder title are set;
# everything needed to actually work them (a real title, tier, domain, epic,
# priority, and an expanded description) is left to be filled in by triage, so
# raw tickets must be classified before they can be claimed or worked.
RAW_TYPE_CHOICES = ["memo", "feature", "bug"]

RAW_TITLE_FORMAT = "{type} (raw): Requires Classification"

# Every raw ticket is auto-grouped under this epic so triage/classification
# jobs can discover them with `arbite list next --epic classification` (or
# `arbite list --epic classification`) and pick them up. When triage replaces
# the placeholder fields, it should also move the ticket to a real epic.
CLASSIFICATION_EPIC = "classification"

RAW_DESCRIPTION = (
    "This is a **raw** ticket: it was captured from a brief request without proper "
    "classification. It must be filled out before it can be worked.\n\n"
    "Original request: {message}\n\n"
    "What still needs to be done (human or agent triage):\n"
    "- title -- replace \"Requires Classification\" with a short human-readable summary\n"
    "- tier -- low | medium | high (capability level required)\n"
    "- domain -- e.g. mesh, image_gen, audio_gen, ui, io (drives routing)\n"
    "- epic -- this raw ticket is auto-grouped under the 'classification' epic "
    "(so triage can find it with `arbite list next --epic classification`); replace "
    "it with the real epic this work belongs to, e.g. mesh-pipeline\n"
    "- priority -- numeric urgency index, lower = more urgent\n"
    "- description -- expand this body into a proper task description based on the "
    "original request, including any acceptance criteria"
)

MEMO_RAW_NOTE = (
    "> **Note:** a memo is primarily a request to update any project notes / "
    "documentation that is being maintained, rather than a code change."
)


class TicketError(Exception):
    pass


@dataclass
class Ticket:
    id: str
    title: str
    status: str
    type: str
    tier: str
    domain: str
    epic: Optional[str] = None
    priority: Optional[int] = None
    tags: list = field(default_factory=list)
    assignee: Optional[str] = None
    depends_on: list = field(default_factory=list)
    blocked_by: Optional[str] = None
    created: str = ""
    updated: str = ""
    closed: Optional[str] = None
    body: str = ""

    def priority_sort_key(self) -> float:
        """Sort key for urgency: lower number = more urgent. Unset (None)
        tickets sort after every explicit priority so they are picked up last."""
        return self.priority if self.priority is not None else PRIORITY_MAX

    def to_markdown(self) -> str:
        data = {}
        for name in FIELD_ORDER:
            data[name] = getattr(self, name)
        front = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
        return f"---\n{front}---\n\n{self.body.strip()}\n"


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        raise TicketError("ticket file is missing YAML frontmatter (must start with '---')")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise TicketError("ticket file has malformed frontmatter (missing closing '---')")
    return parts[1], parts[2].lstrip("\n")


def parse_ticket(text: str) -> Ticket:
    front_yaml, body = _split_frontmatter(text)
    data = yaml.safe_load(front_yaml) or {}
    known = {f.name for f in fields(Ticket)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for k in ("tags", "depends_on"):
        if kwargs.get(k) is None:
            kwargs[k] = []
    return Ticket(body=body, **kwargs)


def load_ticket(path: Path) -> Ticket:
    return parse_ticket(path.read_text(encoding="utf-8"))


def save_ticket(ticket: Ticket, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ticket.to_markdown(), encoding="utf-8")


def gen_id(existing_ids: set) -> str:
    while True:
        candidate = f"tic-{uuid.uuid4().hex[:4]}"
        if candidate not in existing_ids:
            return candidate


def today() -> str:
    return date.today().isoformat()


def status_dir(status: str, tickets_root: Path, closed_date: Optional[str] = None) -> Path:
    if status == "closed":
        month = (closed_date or today())[:7]
        return tickets_root / "closed" / month
    if status not in FLAT_STATUS_DIRS:
        raise TicketError(f"unknown status: {status}")
    return tickets_root / status


def iter_ticket_paths(tickets_root: Path):
    for status in FLAT_STATUS_DIRS:
        d = tickets_root / status
        if d.is_dir():
            yield from sorted(d.glob("*.md"))
    closed_dir = tickets_root / "closed"
    if closed_dir.is_dir():
        for month_dir in sorted(closed_dir.iterdir()):
            if month_dir.is_dir():
                yield from sorted(month_dir.glob("*.md"))


def load_all_tickets(tickets_root: Path):
    """Yields (path, Ticket) for every ticket under tickets_root."""
    for path in iter_ticket_paths(tickets_root):
        yield path, load_ticket(path)


def find_tickets(tickets_root: Path, term: str):
    """Returns a sorted list of (path, Ticket) whose ids contain `term` as a
    case-insensitive substring (wildcard) search -- e.g. 'f6' matches
    tic-f607, and 'tic-' matches every ticket. Sorted by ticket id."""
    term_lower = term.lower()
    matches = [
        (path, ticket)
        for path, ticket in load_all_tickets(tickets_root)
        if term_lower in ticket.id.lower()
    ]
    matches.sort(key=lambda pair: pair[1].id)
    return matches


def find_ticket(tickets_root: Path, ticket_id: str):
    """Returns (path, Ticket) for the ticket matching `ticket_id` by wildcard
    (substring) search; if several tickets match, the first alphabetically is
    returned. Raises TicketError if nothing matches."""
    matches = find_tickets(tickets_root, ticket_id)
    if not matches:
        raise TicketError(f"no ticket found matching '{ticket_id}'")
    return matches[0]


def append_note(ticket: Ticket, agent_id: str, message: str, note_date: Optional[str] = None) -> None:
    """Appends a timestamped, agent-identified entry to the ticket's '## Notes'
    section, with a blank line between entries. Mutates ticket.body in place;
    caller is responsible for saving."""
    note_date = note_date or today()
    entry = f"- {note_date} {agent_id}: {message}"
    marker = "## Notes"
    idx = ticket.body.rfind(marker)
    if idx == -1:
        head = ticket.body.rstrip()
        sep = "\n\n" if head else ""
        ticket.body = f"{head}{sep}{marker}\n{entry}\n"
        return
    head = ticket.body[: idx + len(marker)]
    existing = ticket.body[idx + len(marker) :].strip("\n")
    if existing.strip():
        ticket.body = f"{head}\n{existing}\n\n{entry}\n"
    else:
        ticket.body = f"{head}\n{entry}\n"


def move_ticket(path: Path, ticket: Ticket, tickets_root: Path, new_status: str) -> Path:
    """Moves a ticket's file to the folder matching new_status and rewrites its
    frontmatter status in the same operation, so folder and frontmatter never
    disagree. Returns the new path."""
    dest_dir = status_dir(new_status, tickets_root, closed_date=ticket.closed)
    dest_path = dest_dir / path.name
    ticket.status = new_status
    save_ticket(ticket, dest_path)
    if dest_path != path and path.exists():
        path.unlink()
    return dest_path
