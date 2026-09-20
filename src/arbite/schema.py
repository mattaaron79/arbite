"""The ticket schema: data model, controlled vocabularies, and text format.

Deliberately sink-independent -- nothing here touches a filesystem or a database.
A sink's whole job is to store and retrieve `Ticket` objects; this module defines
what a `Ticket` *is*, how its fields validate, and how it renders as markdown.

The markdown form is not merely the file sink's storage format: it is the format
`arbite show` prints for *every* sink and the format `Ticket.to_markdown()` must
reproduce byte-for-byte, which is what lets a file sink and a database sink be
interchangeable.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Optional

import yaml

from .errors import TicketError

# The canonical status vocabulary *and order*: this list is the single source
# every other status ordering derives from -- the file sink's folder set, the
# rendered docs, and the `--status` choices all come from here. "review" sits
# between "in_progress" and "blocked" as the finished-awaiting-review state; no
# command sets it by default, so it is reached through `arbite set ... status
# review`.
STATUSES = ["raw", "open", "in_progress", "review", "blocked", "shelved", "closed"]

# Controlled vocabularies. `type` includes "memo" and "wish" because `arbite
# raw memo` and `arbite raw wish` mint them; `create` deliberately does not
# offer them (they're captured requests, not something authored directly).
# Kept here rather than inline in argparse so create, set and doctor all
# validate against the same list.
TYPES = ["bug", "feature", "request", "refactor", "chore", "memo", "wish"]
# The types a plain `arbite create` offers: ordinary work, as opposed to the
# raw-only captures (`memo`, `wish`) whose special handling is described below.
CREATE_TYPES = ["bug", "feature", "request", "refactor", "chore"]

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
    # `references` sits immediately after `depends_on`, the field it most
    # resembles (a comma-separated list), so the rendered frontmatter keeps it
    # there when it is present. Unlike `depends_on` it is omitted entirely when
    # empty (see `to_markdown`), so a ticket with no references renders exactly
    # as it did before this field existed.
    "references",
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

# Raw tickets (`arbite raw <memo|feature|request|bug|wish> <message>`):
# deliberately unclassified quick captures. They get status "raw", precisely so
# they never show up in `arbite list next` -- only the type and a placeholder
# title are set; everything needed to actually work them (a real title, tier,
# domain, epic, priority, and an expanded description) is left to be filled in
# by triage/classification (see `arbite fetch`), which also moves them to
# status "open" or claims them.
RAW_TYPE_CHOICES = ["memo", "feature", "request", "bug", "wish"]

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

# Appended to `arbite raw request` tickets. A request is a request for a change
# that is not necessarily a bug or a new feature: a tweak or lateral change to
# existing behaviour, UI, data or docs. It is ordinary work once classified, so
# unlike a wish it is opened or claimed normally -- the note only records what
# kind of change a request is, so triage doesn't force it into bug/feature.
REQUEST_RAW_NOTE = (
    "> **Note:** this is a **request** for a change -- not necessarily a bug or a new "
    "feature, but a **tweak or lateral change** to something that already exists "
    "(behaviour, UI, data or docs). Classify it like any other raw ticket, but when you "
    "do, keep it as `request`: state the current behaviour, the change being asked for, "
    "and any acceptance criteria, then open or claim it as ordinary work."
)

# Appended to `arbite raw wish` tickets. Wishlist items are deliberately NOT
# opened as work: triage/classification reclassifies them as `feature`, fills
# in tags/description/analysis/possible epic, then files the ticket in the
# wishlist bucket with `arbite move <id> /wishlist`.
WISH_RAW_NOTE = (
    "> **Note:** this is a **wishlist** item, not ordinary feature work. When it is "
    "classified, reclassify it as `feature` (not `wish`), with the correct `tags`, an "
    "expanded `description`, an analysis of the request, and a possible `epic`, then file "
    "the ticket in the wishlist bucket with `arbite move {id} /wishlist` (a folder in the "
    "file sink, a bucket in a database sink). A wish is captured so it isn't forgotten, "
    "not so it is worked: leave it in the wishlist until it is deliberately promoted to "
    "real work."
)

# Instructions injected at the top of `arbite fetch` output (via --json's
# 'derived_note' field, or as a leading block in text mode). Not stored in the
# ticket itself -- it's guidance for whichever agent fetches the ticket,
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
# they are reclassified as `feature` and filed in the wishlist bucket rather
# than opened or claimed as work.
DERIVED_NOTE_WISH = (
    "Wishlist item {id} is unclassified (status: raw, type: wish). Wishlist items are NOT "
    "opened as work: reclassify them as `feature` -- set the correct tags, an expanded "
    "description, an analysis of the request, and a possible epic -- then file the ticket "
    "in the wishlist bucket with `arbite move {id} /wishlist`. Do not set status to 'open' "
    "and do not claim it."
)

# The markdown heading the append-only audit trail lives under.
NOTES_HEADING = "## Notes"

# One note entry as `append_note` writes it: `- <timestamp> <agent>: <message>`.
# The agent and even the timestamp are optional so that hand-written notes and
# the older date-only form the demo seeds use still parse -- the derived note
# index must never fail on a body a human wrote.
NOTE_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?)"
    r"(?:(?:\s+(?P<agent>[^\s:]+))?\s*:\s*|\s+)"
    r"(?P<message>.*)$",
    re.DOTALL,
)

# Values that are placeholders rather than real data: `arbite raw` and
# `create --blank` write them for a human or triage job to replace.
PLACEHOLDER_PREFIX = "TODO:"


def is_placeholder(value) -> bool:
    """True for a scaffolded `TODO: ...` value. Pending work, not corruption --
    `doctor` must not flag an untriaged ticket as broken."""
    return value is not None and str(value).startswith(PLACEHOLDER_PREFIX)


@dataclass
class Note:
    """One entry of a ticket's `## Notes` trail.

    `ordinal` is its position in the body's notes section, which is what lets a
    derived note index (the SQLite sink's `ticket_notes` table) be compared
    against the body and rebuilt when it disagrees."""

    ordinal: int
    date: str
    agent: str
    message: str


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
    # Root-relative plan documents under the arbite dir's `plans/` bucket, e.g.
    # "plans/review-workflow.md" (== .arbite/plans/review-workflow.md). Never
    # repo-root paths, and the referenced file need not exist yet.
    references: list = field(default_factory=list)
    blocked_by: Optional[str] = None
    created: str = ""
    updated: str = ""
    closed: Optional[str] = None
    body: str = ""

    def priority_sort_key(self) -> float:
        """Sort key for urgency: lower number = more urgent. Unset (None)
        tickets sort after every explicit priority so they are picked up last."""
        return self.priority if self.priority is not None else PRIORITY_MAX

    def to_dict(self, path: Optional[str] = None) -> dict:
        """Plain JSON-serialisable form: every frontmatter field, plus the
        markdown body and (when known) the ticket's location from the active
        sink. Used for `--json` output so agents get the same field names as
        the frontmatter instead of parsing a formatted table.

        The key stays `path` because it is part of the agent-facing contract;
        for the file sink the value is a filesystem path, for any other sink it
        is whatever `TicketSink.describe_location()` reports."""
        data = {name: getattr(self, name) for name in FIELD_ORDER}
        data["body"] = self.body
        if path is not None:
            data["path"] = str(path)
        return data

    def to_markdown(self) -> str:
        """The ticket's canonical text form. Identical across sinks: the file
        sink writes exactly this to disk, and `arbite show` prints exactly this
        regardless of where the ticket is stored.

        `references` is omitted entirely when empty, so absent and empty are
        indistinguishable in the stored form: a ticket with no references stays
        byte-for-byte what it was before the field existed, and an explicit
        `references: []` in a hand-written file parses back to an empty list and
        is never re-emitted. This deliberately differs from `depends_on`, which
        always renders `[]`; that output is pinned by tests and not changed."""
        data = {}
        for name in FIELD_ORDER:
            value = getattr(self, name)
            if name == "references" and not value:
                continue
            data[name] = value
        front = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
        return f"---\n{front}---\n\n{self.body.strip()}\n"


def _split_frontmatter(text: str) -> tuple:
    if not text.startswith("---"):
        raise TicketError("ticket file is missing YAML frontmatter (must start with '---')")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise TicketError("ticket file has malformed frontmatter (missing closing '---')")
    return parts[1], parts[2].lstrip("\n")


def parse_ticket(text: str) -> Ticket:
    """Parse the canonical markdown form back into a Ticket. Round-trips
    `Ticket.to_markdown()` exactly, which is what makes the sinks
    interchangeable: both must be able to reproduce the other's text."""
    front_yaml, body = _split_frontmatter(text)
    try:
        data = yaml.safe_load(front_yaml) or {}
    except yaml.YAMLError as e:
        raise TicketError(f"ticket frontmatter is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise TicketError("ticket frontmatter must be a YAML mapping of field: value")
    known = {f.name for f in fields(Ticket)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for k in ("tags", "depends_on", "references"):
        if kwargs.get(k) is None:
            kwargs[k] = []
    try:
        return Ticket(body=body, **kwargs)
    except TypeError as e:
        # Missing a required field (id/title/status/type/tier/domain). Surface
        # it as a TicketError so the CLI reports which ticket is broken rather
        # than dying with a raw traceback -- `arbite doctor` relies on this.
        raise TicketError(f"ticket is missing required frontmatter fields: {e}")


def now() -> str:
    """Current local timestamp to the second (YYYY-MM-DDTHH:MM:SS), used for
    created/updated/closed and for notes. ISO-8601 with a 'T' separator so a
    string sort is a chronological sort, and so PyYAML round-trips it as text
    (see DATE_PATTERN)."""
    return datetime.now().isoformat(timespec="seconds")


def gen_id(existing_ids: set) -> str:
    """A fresh ticket id not present in `existing_ids`. Two hex bytes of a UUID
    keeps ids short and readable (`tic-a1b2`) while collisions stay rare enough
    that the caller's retry loop settles immediately."""
    while True:
        candidate = f"tic-{uuid.uuid4().hex[:4]}"
        if candidate not in existing_ids:
            return candidate


def format_note_entry(note_date: str, agent_id: str, message: str) -> str:
    return f"- {note_date} {agent_id}: {message}"


def append_note(ticket: Ticket, agent_id: str, message: str, note_date: Optional[str] = None) -> None:
    """Appends a timestamped, agent-identified entry to the ticket's '## Notes'
    section, with a blank line between entries. The timestamp is granular to the
    second (YYYY-MM-DDTHH:MM:SS) unless an explicit note_date is passed.
    Mutates ticket.body in place; caller is responsible for saving."""
    note_date = note_date or now()
    entry = format_note_entry(note_date, agent_id, message)
    idx = ticket.body.rfind(NOTES_HEADING)
    if idx == -1:
        head = ticket.body.rstrip()
        sep = "\n\n" if head else ""
        ticket.body = f"{head}{sep}{NOTES_HEADING}\n{entry}\n"
        return
    head = ticket.body[: idx + len(NOTES_HEADING)]
    existing = ticket.body[idx + len(NOTES_HEADING) :].strip("\n")
    if existing.strip():
        ticket.body = f"{head}\n{existing}\n\n{entry}\n"
    else:
        ticket.body = f"{head}\n{entry}\n"


def notes_body(body: str) -> str:
    """The text after the last '## Notes' heading, or '' if the body has none.

    The last heading is used because a ticket's description may legitimately
    quote or discuss the heading; the append-only trail is always the final
    section."""
    idx = (body or "").rfind(NOTES_HEADING)
    if idx == -1:
        return ""
    return body[idx + len(NOTES_HEADING) :]


def parse_notes(body: str) -> list:
    """The `## Notes` entries of a body, as `Note` objects in body order.

    The body is authoritative -- notes are prose an agent or human can edit by
    hand, so this is a *derivation*: the SQLite sink indexes the result so notes
    are queryable in SQL, and compares the index against this to detect drift.
    Tolerates every form seen in practice: the canonical
    `- <timestamp> <agent>: <message>`, the older date-only `- <date>: <message>`,
    and entries with no timestamp at all."""
    section = notes_body(body)
    if not section.strip():
        return []
    groups = [g.strip() for g in re.split(r"\n[ \t]*\n", section.strip()) if g.strip()]
    entries = []
    for group in groups:
        if entries and not group.startswith("-"):
            # A continuation paragraph -- either a hand-written note that wraps
            # across a blank line, or prose that isn't a note at all. It belongs
            # to the entry above it, not to a new one.
            entries[-1] = entries[-1] + "\n\n" + group
            continue
        entries.append(group)

    notes = []
    for ordinal, entry in enumerate(entries):
        text = entry[1:].strip() if entry.startswith("-") else entry
        match = NOTE_LINE_RE.match(text)
        if match:
            notes.append(
                Note(
                    ordinal=ordinal,
                    date=match.group("date"),
                    agent=match.group("agent") or "",
                    message=(match.group("message") or "").strip(),
                )
            )
        else:
            notes.append(Note(ordinal=ordinal, date="", agent="", message=text))
    return notes


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


def derived_note(ticket_id: str, ticket_type: Optional[str] = None) -> str:
    """Brief instructions for the agent that just ran `arbite fetch`: classify the raw
    ticket, then either open it for someone else (triage-only) or claim it immediately
    (going to work it now). Wishlist items are the exception: they are reclassified as
    `feature` and filed in the wishlist bucket instead of being opened or claimed."""
    if ticket_type == "wish":
        return DERIVED_NOTE_WISH.format(id=ticket_id)
    return DERIVED_NOTE_FORMAT.format(id=ticket_id)


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------

# Fields `arbite set` accepts: every frontmatter field except the structural id
# (the id is generated by `arbite create` and never changed), plus 'body' for
# the freeform markdown body.
SETTABLE_PROPERTIES = (set(FIELD_ORDER) | {"body"}) - {"id"}

# Optional text fields: an empty quoted value clears them back to None.
CLEARABLE_TEXT_FIELDS = {"epic", "assignee", "blocked_by", "closed"}

# Fields `arbite search --params` accepts: every frontmatter field plus the body.
SEARCH_FIELDS = set(FIELD_ORDER) | {"body"}


def _reference_entries(value):
    """The entries of a `references` value: a comma-separated string (the CLI
    form, matching `coerce_field_value`) or an already-parsed list. Anything
    else -- an int, a dict, a nested list -- is a value nothing in arbite
    writes, so it is reported rather than coerced."""
    if value is None:
        return []
    if isinstance(value, str):
        return [] if value == "" else [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TicketError(
        f"references must be a list of strings, got {type(value).__name__}: {value!r}"
    )


def validate_references(value) -> None:
    """Validate a `references` value (a list, or its comma-separated CLI form).

    Entries are plan documents under the arbite root's `plans/` bucket, stored
    root-relative -- e.g. 'plans/review-workflow.md', which resolves to
    .arbite/plans/review-workflow.md -- and deliberately *not* repo-root paths.
    Each entry must be a non-empty string that stays under the arbite root, so
    absolute paths and '..' segments are refused. The referenced file is NOT
    required to exist: a ticket may reference a plan that has not been written
    yet."""
    for entry in _reference_entries(value):
        if not isinstance(entry, str):
            raise TicketError(f"references entries must be strings, got {entry!r}")
        if entry == "":
            raise TicketError("references may not contain an empty entry")
        if entry.startswith("/"):
            raise TicketError(
                "references must be root-relative to the arbite dir, e.g. "
                f"'plans/review-workflow.md', not an absolute path: '{entry}'"
            )
        if ".." in entry.split("/"):
            raise TicketError(
                f"references may not contain '..' (they must stay under the arbite "
                f"root): '{entry}'"
            )


def validate_field(prop: str, value) -> None:
    """Reject values `arbite create` would never have produced.

    `set` is the one way to write any field by hand, so without this it is the
    hole every controlled vocabulary leaks through -- a typo'd tier or a
    free-text date silently persists and then quietly fails to match the
    filters that route work to agents. Shared by `set` and by every sink's
    integrity check so the two can never disagree about what is valid."""
    if value == "":
        return
    if prop == "references":
        validate_references(value)
        return
    if prop == "status" and value not in STATUSES:
        raise TicketError(f"invalid status '{value}' (valid: {', '.join(STATUSES)})")
    if prop == "type" and value not in TYPES:
        raise TicketError(f"invalid type '{value}' (valid: {', '.join(TYPES)})")
    if prop == "tier" and value not in TIERS:
        raise TicketError(f"invalid tier '{value}' (valid: {', '.join(TIERS)})")
    if prop == "priority":
        try:
            if int(value) < 1:
                raise TicketError(
                    f"priority must be a positive integer, got '{value}' (lower = more urgent)"
                )
        except ValueError:
            raise TicketError(f"priority must be an integer, got '{value}'")
    if prop in DATE_FIELDS and not DATE_PATTERN.match(value):
        raise TicketError(
            f"{prop} must be a YYYY-MM-DD date or YYYY-MM-DDTHH:MM:SS timestamp, got '{value}'"
        )


def coerce_field_value(prop: str, value: str):
    """Convert a CLI string into the typed value a ticket property expects:
    lists (tags/depends_on/references) are comma-split, priority is parsed as an
    int, and an empty quoted value clears optional/list/int fields."""
    if prop in ("tags", "depends_on", "references"):
        return [] if value == "" else [v.strip() for v in value.split(",") if v.strip()]
    if prop == "priority":
        return None if value == "" else int(value)
    if value == "" and prop in CLEARABLE_TEXT_FIELDS:
        return None
    return value


def validate_ticket(ticket: Ticket) -> list:
    """Human-readable problems with a single ticket's own fields and state.

    Sink-independent by construction: this is the half of `arbite doctor` that
    means the same thing whether the ticket is a file or a row. Cross-ticket
    checks (duplicate ids, dependency cycles, dangling dependencies) live in
    `sinks/base.py`, because those need the whole set. Returns [] when the
    ticket is clean."""
    problems = []

    for prop in ("status", "type", "tier"):
        value = getattr(ticket, prop, None)
        if value is None:
            continue
        # `arbite raw` and `create --blank` deliberately write TODO
        # placeholders for a human or triage job to replace. Those are
        # pending work, not corruption -- flagging them would leave doctor
        # permanently failing in any repo with an untriaged ticket, which
        # is exactly when its exit code needs to mean something.
        if is_placeholder(value):
            continue
        try:
            validate_field(prop, str(value))
        except TicketError as e:
            problems.append(str(e))

    if ticket.priority is not None:
        try:
            validate_field("priority", str(ticket.priority))
        except TicketError as e:
            problems.append(str(e))

    for date_field in DATE_FIELDS:
        value = getattr(ticket, date_field, None)
        if value and not DATE_PATTERN.match(str(value)):
            problems.append(
                f"{date_field} is not a valid date/timestamp "
                f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): '{value}'"
            )

    if ticket.status == "in_progress" and not ticket.assignee:
        problems.append(
            "in_progress but has no assignee -- nobody is accountable for it and "
            "'list next' will never offer it; release it or claim it"
        )
    if ticket.status == "blocked" and not ticket.blocked_by:
        problems.append(
            "blocked but blocked_by is empty -- nothing records what is stalling it"
        )
    if ticket.status == "closed" and not ticket.closed:
        problems.append("closed ticket has no 'closed' date")
    if ticket.status != "closed" and ticket.closed:
        problems.append(f"not closed but has a 'closed' date of {ticket.closed}")

    return problems
