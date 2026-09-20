"""The status vocabulary: the one list every other status order derives from.

These assertions are about `schema.STATUSES` itself, not about any sink: the file
sink's folder set, the rendered guide and the `--status` filter all derive from
this list, so pinning it here is what "the status vocabulary changed" means.
"""

from __future__ import annotations

import pytest

from arbite.errors import TicketError
from arbite.schema import (
    BLANK_TITLE,
    FIELD_ORDER,
    RAW_TITLE_FORMAT,
    RAW_TYPE_CHOICES,
    SETTABLE_PROPERTIES,
    STATUSES,
    coerce_field_value,
    is_placeholder,
    is_raw_title_placeholder,
    parse_ticket,
    validate_field,
    validate_ticket,
)
from helpers import make_ticket

CANONICAL_STATUSES = ["raw", "open", "in_progress", "review", "blocked", "shelved", "closed"]


def test_statuses_is_the_canonical_vocabulary_and_order():
    """The exact list, in order: every other status ordering is derived from it,
    so an accidental insertion or reorder here is a vocabulary change."""
    assert STATUSES == CANONICAL_STATUSES


def test_review_sits_between_in_progress_and_blocked():
    assert STATUSES.index("review") == STATUSES.index("in_progress") + 1


def test_review_validates_but_an_unknown_status_does_not():
    validate_field("status", "review")  # a first-class status: does not raise
    with pytest.raises(TicketError):
        validate_field("status", "not_a_status")


def test_a_review_ticket_has_no_intrinsic_problems():
    """`review` carries no per-status rule of its own (unlike in_progress needing
    an assignee), so a plain review ticket must validate cleanly."""
    assert validate_ticket(make_ticket(status="review")) == []


def test_the_per_status_count_table_is_seeded_from_the_vocabulary():
    """`arbite status` renders its table from this list -- its entries, in this
    order, zeros included -- so a status added here appears with no change to any
    command or test. The sparse form (only statuses that hold tickets) is what
    `sink info` reports, from the same counting implementation."""
    from arbite.sinks import count_by_status

    assert list(count_by_status([], vocabulary=True)) == list(STATUSES)
    assert count_by_status([]) == {}
    ticket = make_ticket(status="review")
    assert count_by_status([ticket], vocabulary=True) == {
        **{status: 0 for status in STATUSES},
        "review": 1,
    }


# --- the references field ---------------------------------------------------


def test_references_sits_next_to_depends_on_in_field_order():
    """`references` most resembles `depends_on` (a comma-separated list), so it is
    pinned immediately after it: that fixes where it renders when present."""
    assert FIELD_ORDER.index("references") == FIELD_ORDER.index("depends_on") + 1


def test_references_is_a_settable_property():
    assert "references" in SETTABLE_PROPERTIES


def test_validate_field_accepts_the_comma_separated_and_list_forms():
    validate_field("references", "plans/a.md,plans/b.md")
    validate_field("references", "plans/review-workflow.md")
    validate_field("references", "")
    validate_field("references", [])
    validate_field("references", ["plans/a.md", "plans/nested/b.md"])


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",  # absolute path
        "/plans/a.md",  # absolute, even under the arbite root
        "../outside/a.md",  # parent traversal in the first segment
        "plans/../secret.md",  # parent traversal mid-path
        "plans/..",
        5,  # a non-list value
        ["plans/a.md", 5],  # a non-string entry
        ["plans/a.md", ""],  # an empty-string entry
    ],
)
def test_validate_field_rejects_bad_references(bad):
    with pytest.raises(TicketError):
        validate_field("references", bad)


def test_coerce_field_value_parses_references():
    assert coerce_field_value("references", "plans/a.md, plans/b.md") == [
        "plans/a.md",
        "plans/b.md",
    ]
    assert coerce_field_value("references", "") == []


def test_an_empty_reference_list_renders_as_absent():
    """Absent and empty are indistinguishable by design: neither emits a line, so
    a ticket with no references stays byte-for-byte what it was before the field
    existed (unlike `depends_on`, which always renders `[]`)."""
    assert "references" not in make_ticket("tic-a1b2").to_markdown()
    assert "references" not in make_ticket("tic-a1b2", references=[]).to_markdown()
    # `depends_on` still renders its empty list -- that output is pinned and not
    # changed to match `references`.
    assert "depends_on: []" in make_ticket("tic-a1b2").to_markdown()


def test_references_round_trips_through_markdown_in_order():
    ticket = make_ticket("tic-a1b2", references=["plans/b.md", "plans/a.md"])
    text = ticket.to_markdown()
    reparsed = parse_ticket(text)
    assert reparsed.references == ["plans/b.md", "plans/a.md"]
    assert reparsed.to_markdown() == text


def test_a_hand_written_empty_references_list_parses_then_renders_as_absent():
    text = make_ticket("tic-a1b2").to_markdown().replace(
        "depends_on: []", "depends_on: []\nreferences: []"
    )
    reparsed = parse_ticket(text)
    assert reparsed.references == []
    assert "references" not in reparsed.to_markdown()


def test_the_raw_title_placeholder_predicate_follows_the_format():
    """`arbite promote` refuses a title that is still a raw capture's placeholder, and the
    test for it is derived from `RAW_TITLE_FORMAT` -- every raw type in turn -- rather than
    restating its text, so it cannot drift from what `arbite raw` writes. A `TODO: ...`
    title is a placeholder too, but by the generic rule rather than this one, which is why
    promote checks both."""
    for raw_type in RAW_TYPE_CHOICES:
        assert is_raw_title_placeholder(RAW_TITLE_FORMAT.format(type=raw_type))
    assert not is_raw_title_placeholder("Add per-mesh LOD")
    assert not is_raw_title_placeholder(None)
    assert not is_raw_title_placeholder(BLANK_TITLE)
    assert is_placeholder(BLANK_TITLE)
