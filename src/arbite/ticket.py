"""Ticket read/write: YAML frontmatter + markdown body, folder <-> status sync."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

STATUSES = ["raw", "open", "in_progress", "blocked", "shelved", "closed"]

# Controlled vocabularies. `type` includes "memo" and "wish" because `arbite
# raw memo` and `arbite raw wish` mint them; `create` deliberately does not
# offer them (they're captured requests, not something authored directly).
# Kept here rather than inline in argparse so create, set and doctor all
# validate against the same list.
TYPES = ["bug", "feature", "refactor", "chore", "memo", "wish"]
CREATE_TYPES = ["bug", "feature", "refactor", "chore"]

# Agent capability tiers, ascending. `tier` answers "how capable does the agent
# working this need to be", which is why it is a ladder rather than a set of
# labels: an agent may work a ticket at or below its own tier, so ordering is
# meaningful in a way domain/tags ordering never is.
TIERS = ["low", "medium", "high", "frontier"]

TIER_VALUES = " | ".join(TIERS)

# The shared explanation of what a tier *is*, with no leading clause, so each
# call site can put its own sentence in front without the two colliding.
TIER_HELP = (
    "Tier is how capable the agent must be, ascending -- not how urgent the work "
    "is (that's priority, lower = more urgent) and not what specialization it "
    "needs (that's domain). Those axes are independent: a trivial chore can be "
    "urgent, and a low-tier ticket can still be audio_gen-only. An agent is "
    "either told its own tier by the harness or self-assesses from its model "
    "class (the company.model prefix of its agent id, e.g. claude.haiku sits "
    "below claude.opus), and should only claim tickets at or below that tier."
)

# created/updated/closed timestamps: a plain date (YYYY-MM-DD, kept so tickets
# written before timestamps went granular still validate) or a full timestamp
# down to the second (YYYY-MM-DDTHH:MM:SS). The 'T' separator with no timezone
# keeps string sort == chronological sort, and PyYAML quotes it back to a plain
# string on load (see parse_ticket), so it round-trips as text.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?$")
DATE_FIELDS = ("created", "updated", "closed")

# Statuses that live directly under <arbite_root>/<status>/ (closed is special-cased
# into <arbite_root>/closed/YYYY-MM/).
FLAT_STATUS_DIRS = {"raw", "open", "in_progress", "blocked", "shelved"}

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
BLANK_TIER = "TODO: " + "|".join(TIERS)
BLANK_DOMAIN = "TODO: e.g. mesh, image_gen, audio_gen, ui, io"
BLANK_DESCRIPTION = "TODO: describe the task."
BLANK_WARNING = (
    "> **TEMPLATE -- not ready.** This ticket was scaffolded blank by "
    "`arbite create --blank` and has not been filled in yet. Do not claim or "
    "work it until a human has replaced the TODO placeholders above, written "
    "a real description below, and saved the file."
)

# Raw tickets (`arbite raw <memo|feature|bug> <message>`): deliberately
# unclassified quick captures. They get status "raw" and live in raw/, not
# open/, precisely so they never show up in `arbite list next` -- only the
# type and a placeholder title are set; everything needed to actually work
# them (a real title, tier, domain, epic, priority, and an expanded
# description) is left to be filled in by triage/classification (see
# `arbite fetch`), which also moves them to status "open" or claims them.
RAW_TYPE_CHOICES = ["memo", "feature", "bug", "wish"]

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
    "What still needs to be done (human or agent triage, typically via `arbite fetch`):\n"
    "- title -- replace \"Requires Classification\" with a short human-readable summary\n"
    "- tier -- " + " | ".join(TIERS) + " (agent capability tier required to work it; "
    "how capable the agent must be, not how urgent the work is)\n"
    "- domain -- e.g. mesh, image_gen, audio_gen, ui, io (drives routing)\n"
    "- epic -- this raw ticket is auto-grouped under the 'classification' epic "
    "(so triage can find it with `arbite list next --epic classification`); replace "
    "it with the real epic this work belongs to, e.g. mesh-pipeline\n"
    "- priority -- numeric urgency index, lower = more urgent\n"
    "- description -- expand this body into a proper task description based on the "
    "original request, including any acceptance criteria\n"
    "- status -- set to `open` once classified so it becomes workable via `arbite list "
    "next` (skip this if you're claiming it yourself instead -- `arbite claim` sets "
    "status to `in_progress` directly)"
)

MEMO_RAW_NOTE = (
    "> **Note:** a memo is primarily a request to update any project notes / "
    "documentation that is being maintained, rather than a code change."
)

# Appended to `arbite raw wish` tickets. Wishlist items are deliberately NOT
# opened as work: triage/classification reclassifies them as `feature`, fills
# in tags/description/analysis/possible epic, then files the ticket in the
# .arbite/wishlist/ folder (creating it if needed) with `arbite move <id>
# /wishlist`.
WISH_RAW_NOTE = (
    "> **Note:** this is a **wishlist** item, not ordinary feature work. When it is "
    "classified, reclassify it as `feature` (not `wish`), with the correct `tags`, an "
    "expanded `description`, an analysis of the request, and a possible `epic`, then file "
    "the ticket in the `.arbite/wishlist/` folder -- creating it if it doesn't exist -- "
    "with `arbite move {id} /wishlist`. A wish is captured so it isn't forgotten, not so "
    "it is worked: leave it in the wishlist until it is deliberately promoted to real work."
)

# Instructions injected at the top of `arbite fetch` output (via --json's
# 'derived_note' field, or as a leading block in text mode). Not stored in the
# ticket file itself -- it's guidance for whichever agent fetches the ticket,
# not part of its permanent record.
DERIVED_NOTE_FORMAT = (
    "Raw ticket {id} is unclassified (status: raw). Fill in title, tier, domain, epic, "
    "priority, and an expanded description -- see the Description section below for what "
    "the original request needs. If you are only triaging/classifying it: use `arbite "
    "set {id} <property> <value> ...` to write those fields, then `arbite set {id} status "
    "open` so it becomes available via `arbite list next`. If you are going to work it "
    "yourself: classify it the same way, then run `arbite claim {id} --agent <your-id>` "
    "immediately instead of setting status to open."
)


# Wishlist raw tickets are classified differently from ordinary raw tickets:
# they are reclassified as `feature` and filed in .arbite/wishlist/ rather
# than opened or claimed as work.
DERIVED_NOTE_WISH = (
    "Wishlist item {id} is unclassified (status: raw, type: wish). Wishlist items are NOT "
    "opened as work: reclassify them as `feature` -- set the correct tags, an expanded "
    "description, an analysis of the request, and a possible epic -- then file the ticket "
    "in the .arbite/wishlist/ folder (creating it if it doesn't exist) with `arbite move "
    "{id} /wishlist`. Do not set status to 'open' and do not claim it."
)


def derived_note(ticket_id: str, ticket_type: Optional[str] = None) -> str:
    """Brief instructions for the agent that just ran `arbite fetch`: classify the raw
    ticket, then either open it for someone else (triage-only) or claim it immediately
    (going to work it now). Wishlist items are the exception: they are reclassified as
    `feature` and filed in .arbite/wishlist/ instead of being opened or claimed."""
    if ticket_type == "wish":
        return DERIVED_NOTE_WISH.format(id=ticket_id)
    return DERIVED_NOTE_FORMAT.format(id=ticket_id)


def raw_captured_request(ticket: Ticket) -> str:
    """The brief request a raw ticket was captured from: the text after the
    'Original request:' line that `arbite raw` writes into the Description.

    `arbite list raw` shows this text on every line of the raw backlog so a
    human or triage run can see at a glance what each still-unclassified
    ticket is about, without opening it. Returns '' if the body no longer
    carries that line (e.g. the description was rewritten by hand, which a
    partially-completed classification can legitimately do) -- callers fall
    back to a placeholder in that case."""
    marker = "Original request:"
    for line in (ticket.body or "").splitlines():
        if line.lstrip().startswith(marker):
            return line.split(marker, 1)[1].strip()
    return ""


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

    def to_dict(self, path: Optional[Path] = None) -> dict:
        """Plain JSON-serialisable form: every frontmatter field, plus the
        markdown body and (when known) the ticket's path relative to the
        tickets root. Used for `--json` output so agents get the same field
        names as the frontmatter instead of parsing a formatted table."""
        data = {name: getattr(self, name) for name in FIELD_ORDER}
        data["body"] = self.body
        if path is not None:
            data["path"] = str(path)
        return data

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
    try:
        data = yaml.safe_load(front_yaml) or {}
    except yaml.YAMLError as e:
        raise TicketError(f"ticket frontmatter is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise TicketError("ticket frontmatter must be a YAML mapping of field: value")
    known = {f.name for f in fields(Ticket)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for k in ("tags", "depends_on"):
        if kwargs.get(k) is None:
            kwargs[k] = []
    try:
        return Ticket(body=body, **kwargs)
    except TypeError as e:
        # Missing a required field (id/title/status/type/tier/domain). Surface
        # it as a TicketError so the CLI reports which file is broken rather
        # than dying with a raw traceback -- `arbite doctor` relies on this.
        raise TicketError(f"ticket is missing required frontmatter fields: {e}")


def load_ticket(path: Path) -> Ticket:
    try:
        return parse_ticket(path.read_text(encoding="utf-8"))
    except TicketError as e:
        raise TicketError(f"{path}: {e}")


# Temp files staged during an atomic save/move. Dot-prefixed and not suffixed
# .md, so iter_ticket_paths' glob("*.md") never mistakes one for a ticket; a
# leftover is a crash artifact, and `arbite doctor` reports it.
TMP_PREFIX = ".arbite-tmp-"


def _write_atomic(text: str, path: Path) -> None:
    """Write `text` to `path` atomically: a complete temp file in the same
    directory, then os.replace (atomic on POSIX and Windows alike). A crash can
    leave a temp file behind but can never leave a half-written ticket."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def save_ticket(ticket: Ticket, path: Path) -> None:
    _write_atomic(ticket.to_markdown(), path)


def gen_id(existing_ids: set) -> str:
    while True:
        candidate = f"tic-{uuid.uuid4().hex[:4]}"
        if candidate not in existing_ids:
            return candidate


def now() -> str:
    """Current local timestamp to the second (YYYY-MM-DDTHH:MM:SS), used for
    created/updated/closed and for notes. ISO-8601 with a 'T' separator so a
    string sort is a chronological sort, and so PyYAML round-trips it as text
    (see DATE_PATTERN)."""
    return datetime.now().isoformat(timespec="seconds")


def status_dir(status: str, tickets_root: Path, closed_date: Optional[str] = None) -> Path:
    if status == "closed":
        month = (closed_date or now())[:7]
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


def find_ticket(tickets_root: Path, ticket_id: str, unique: bool = False):
    """Returns (path, Ticket) for the ticket matching `ticket_id` by wildcard
    (substring) search. Raises TicketError if nothing matches.

    An exact id match always wins outright, so a full id is never ambiguous
    even when it happens to be a substring of another id. Otherwise, with
    unique=True the caller gets a TicketError listing the candidates rather
    than a silently chosen one: commands that mutate a ticket pass unique=True,
    because guessing there means writing to the wrong ticket, while read-only
    commands keep the convenience of the first alphabetical match."""
    matches = find_tickets(tickets_root, ticket_id)
    if not matches:
        raise TicketError(f"no ticket found matching '{ticket_id}'")
    exact = [m for m in matches if m[1].id.lower() == ticket_id.lower()]
    if exact:
        return exact[0]
    if unique and len(matches) > 1:
        candidates = ", ".join(t.id for _, t in matches)
        raise TicketError(
            f"'{ticket_id}' is ambiguous -- it matches {len(matches)} tickets: "
            f"{candidates}. Pass a full ticket id."
        )
    return matches[0]


def append_note(ticket: Ticket, agent_id: str, message: str, note_date: Optional[str] = None) -> None:
    """Appends a timestamped, agent-identified entry to the ticket's '## Notes'
    section, with a blank line between entries. The timestamp is granular to the
    second (YYYY-MM-DDTHH:MM:SS) unless an explicit note_date is passed.
    Mutates ticket.body in place; caller is responsible for saving."""
    note_date = note_date or now()
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
    disagree. Returns the new path.

    The content is staged into a temp file in the destination directory, the
    source is unlinked, and only then is the temp file renamed into place. That
    ordering is deliberate: the worst case of a crash mid-move is a ticket that
    is briefly invisible but fully recoverable from its temp file (`arbite
    doctor` finds it), never two files sharing one id -- which would silently
    corrupt every listing, dependency walk and topological order."""
    dest_dir = status_dir(new_status, tickets_root, closed_date=ticket.closed)
    dest_path = dest_dir / path.name
    ticket.status = new_status
    if dest_path == path:
        save_ticket(ticket, dest_path)
        return dest_path

    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=TMP_PREFIX, dir=str(dest_dir))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ticket.to_markdown())
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            path.unlink()
        os.replace(tmp, dest_path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise
    return dest_path


def claim_ticket(path: Path, ticket: Ticket, tickets_root: Path, agent: str) -> Path:
    """Atomically claim a ticket for `agent`: move it into in_progress/ and set
    assignee/status/updated in one operation.

    The O_CREAT|O_EXCL create at the destination is the mutex. Two agents that
    both read the same open ticket and race to claim it will both try to create
    in_progress/<id>.md; exactly one wins, and the loser gets a
    TicketError instead of silently overwriting the winner's assignee. This is
    not liveness or staleness detection -- that stays the harness's job -- it
    just makes a claim a compare-and-swap rather than a lost update."""
    dest_dir = tickets_root / "in_progress"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / path.name
    ticket.status = "in_progress"
    ticket.assignee = agent
    ticket.updated = now()

    if dest_path == path:
        # Already in in_progress (a re-claim, or a takeover the caller has
        # already vetted against the existing assignee): rewrite in place.
        save_ticket(ticket, dest_path)
        return dest_path

    try:
        fd = os.open(dest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise TicketError(
            f"ticket {ticket.id} is already in progress (another agent claimed "
            f"it first); re-read it with 'arbite show {ticket.id}'"
        )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ticket.to_markdown())
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if dest_path.exists():
            dest_path.unlink()
        raise
    if path.exists():
        path.unlink()
    return dest_path


def find_cycles(by_id: dict) -> list:
    """Every depends_on cycle among the given tickets, each as a list of ids in
    cycle order. Iterative DFS with an explicit stack, so a pathological
    dependency graph can't exhaust the recursion limit."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in by_id}
    cycles = []
    seen_keys = set()
    for start in sorted(by_id):
        if color[start] != WHITE:
            continue
        color[start] = GREY
        stack = [(start, iter(by_id[start].depends_on))]
        chain = [start]
        while stack:
            node, deps = stack[-1]
            descended = False
            for dep in deps:
                if dep not in by_id:
                    continue
                if color[dep] == GREY:
                    cycle = chain[chain.index(dep):]
                    key = tuple(sorted(cycle))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        cycles.append(cycle)
                    continue
                if color[dep] == WHITE:
                    color[dep] = GREY
                    stack.append((dep, iter(by_id[dep].depends_on)))
                    chain.append(dep)
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()
                chain.pop()
    return cycles
