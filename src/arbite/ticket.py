"""Ticket read/write: YAML frontmatter + markdown body, folder <-> status sync."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

STATUSES = ["open", "in_progress", "blocked", "closed"]

# Statuses that live directly under tickets/<status>/ (closed is special-cased
# into tickets/closed/YYYY-MM/).
FLAT_STATUS_DIRS = {"open", "in_progress", "blocked"}

ID_PATTERN = re.compile(r"^tic-[0-9a-f]{4}$")

# Frontmatter field order, matching the schema in CLAUDE.md exactly.
FIELD_ORDER = [
    "id",
    "title",
    "status",
    "type",
    "tier",
    "domain",
    "tags",
    "assignee",
    "depends_on",
    "blocked_by",
    "created",
    "updated",
    "closed",
]

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
    tags: list = field(default_factory=list)
    assignee: Optional[str] = None
    depends_on: list = field(default_factory=list)
    blocked_by: Optional[str] = None
    created: str = ""
    updated: str = ""
    closed: Optional[str] = None
    body: str = ""

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


def find_ticket(tickets_root: Path, ticket_id: str):
    """Returns (path, Ticket) for the given id, or raises TicketError."""
    for path, ticket in load_all_tickets(tickets_root):
        if ticket.id == ticket_id:
            return path, ticket
    raise TicketError(f"no ticket found with id {ticket_id}")


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
