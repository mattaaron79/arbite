"""The status vocabulary: the one list every other status order derives from.

These assertions are about `schema.STATUSES` itself, not about any sink: the file
sink's folder set, the rendered guide and the `--status` filter all derive from
this list, so pinning it here is what "the status vocabulary changed" means.
"""

from __future__ import annotations

import pytest
import yaml

from arbite import schema

from arbite.errors import TicketError
from arbite.schema import (
    BLANK_TITLE,
    DEFAULT_BODY,
    FIELD_ORDER,
    RAW_TITLE_FORMAT,
    RAW_TYPE_CHOICES,
    SETTABLE_PROPERTIES,
    STATUSES,
    coerce_field_value,
    description_body,
    is_placeholder,
    is_raw_title_placeholder,
    parse_ticket,
    raw_captured_request,
    replace_description,
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


# --- the description section ------------------------------------------------


def test_description_is_a_settable_property_but_not_a_frontmatter_field():
    """`set` writes it like the fields, yet adding it to FIELD_ORDER would put
    a `description:` line into the frontmatter -- it is body text, like `body`."""
    assert "description" in SETTABLE_PROPERTIES
    assert "description" not in FIELD_ORDER


def test_default_body_and_the_helpers_agree_on_the_section_shape():
    """`create`/`promote` write through DEFAULT_BODY; the extract/replace
    helpers must read and rewrite exactly what that template produces."""
    assert description_body(DEFAULT_BODY.format(description="do the thing")) == "do the thing"


def test_description_body_extracts_the_first_section_only():
    body = "## Description\nfirst para\n\nsecond para\n\n## Notes\n- 2026-01-01 a.1: hi\n"
    assert description_body(body) == "first para\n\nsecond para"


def test_description_body_with_no_heading_is_the_prose_before_the_first_heading():
    """With no `## Description` heading the description is the text before the
    first '## ' line -- the whole body when there are no headings, and None
    when there is no such text (a body that starts with a heading, or is empty
    or blank). It is the same text `replace_description` replaces, so what a
    caller reads is always what the next write overwrites."""
    assert description_body("just prose\n") == "just prose"
    assert description_body("para one\n\npara two") == "para one\n\npara two"
    assert description_body("") is None
    assert description_body("\n\n") is None
    assert description_body("## Notes\n- n1\n") is None


def test_description_body_tolerates_trailing_spaces_and_a_final_bare_heading():
    assert description_body("## Description  \nthe text\n") == "the text"
    assert description_body("prose\n## Description") == ""


def test_description_body_takes_the_first_heading_not_the_last():
    """notes_body deliberately takes the LAST '## Notes' heading because a
    description may quote it; the hazard is mirrored here -- a description
    that quotes '## Description', or a note line spelled exactly like the
    heading, must not move the section."""
    assert description_body(
        "## Description\nthe real text\n## Description\nquoted inside the section\n\n## Notes\n"
    ) == "the real text"
    assert description_body(
        "## Description\nreal\n\n## Notes\n## Description\n- a note quoting it\n"
    ) == "real"


def test_replace_description_swaps_only_the_section():
    body = "## Description\nold\n\n## Notes\n- 2026-01-01 a.1: hi\n"
    assert replace_description(body, "new") == (
        "## Description\nnew\n\n## Notes\n- 2026-01-01 a.1: hi\n"
    )


def test_replace_description_when_the_section_runs_to_the_end_of_the_body():
    assert replace_description("## Description\nold\n", "new") == "## Description\nnew\n"


def test_replace_description_replaces_the_whole_heading_less_body():
    """A body that is only prose has no other heading to keep: the section
    takes the body's place and nothing of the old text survives below it."""
    assert replace_description("plain old text", "new desc") == "## Description\nnew desc\n"


def test_replace_description_replaces_the_prose_above_the_first_heading():
    """The prose a heading-less read calls the description is exactly what the
    write replaces; the heading block below it is left byte-for-byte."""
    body = "prose that predates the heading\n\n## Notes\n- n1\n"
    assert description_body(body) == "prose that predates the heading"
    assert replace_description(body, "new") == "## Description\nnew\n\n## Notes\n- n1\n"


def test_replace_description_keeps_a_leading_heading_block_that_follows():
    """A body that starts with another heading reads no description text; the
    write inserts the section at the top and keeps the heading block below."""
    assert replace_description("## Notes\n- n1\n", "new desc") == (
        "## Description\nnew desc\n\n## Notes\n- n1\n"
    )
    assert replace_description("## Notes\n- n1\n", "") == "## Description\n\n## Notes\n- n1\n"


def test_replace_description_read_back_is_idempotent():
    """What the read returns is what a write-back replaces, so reading the
    description and setting it again cannot change a byte of the body."""
    body = "## Description\nsteady\n\n## Notes\n- 2026-01-01 a.1: hi\n"
    assert replace_description(body, description_body(body)) == body


def test_replace_description_keeps_the_heading_when_the_text_is_empty():
    """An empty description is an empty section, never a removed heading."""
    out = replace_description("## Description\nold\n\n## Notes\n", "")
    assert out == "## Description\n\n## Notes\n"
    assert description_body(out) == ""


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


# --- the derived `request` key ---------------------------------------------


def test_to_dict_derives_request_for_raw_tickets_only():
    """`request` (tic-e5b9) is the one projection's derived key: a raw ticket
    reports the text it was captured from, every other status reports null, and a
    raw ticket whose body lost the 'Original request:' line reports null rather
    than ''. The key is always present, so a caller reads its value instead of
    probing for it."""
    body = "## Description\nprose\n\nOriginal request: fix the door\n\n## Notes\n"
    raw = make_ticket("tic-a1b2", status="raw", body=body)
    assert raw.to_dict()["request"] == raw_captured_request(raw) == "fix the door"

    opened = make_ticket("tic-a1b2", status="open", body=body)
    assert opened.to_dict()["request"] is None

    rewritten = make_ticket("tic-a1b2", status="raw", body="## Description\nby hand\n")
    assert raw_captured_request(rewritten) == ""
    assert rewritten.to_dict()["request"] is None
    # Derived, so the stored form is untouched: no `request:` frontmatter line.
    assert "\nrequest:" not in rewritten.to_markdown()


@pytest.mark.parametrize("loader", [yaml.SafeLoader, getattr(yaml, "CSafeLoader", yaml.SafeLoader)])
def test_parse_ticket_is_the_same_under_either_yaml_loader(monkeypatch, loader):
    """The libyaml loader is an optimisation with a pure-Python fallback (tic-a583):
    both parse a ticket to the same value and report bad YAML the same way."""
    monkeypatch.setattr(schema, "SAFE_LOADER", loader)
    ticket = make_ticket(
        title="Größe: a 'quoted' title", tags=["a", "b"], depends_on=["tic-c3d4"], priority=2
    )
    assert schema.parse_ticket(ticket.to_markdown()) == ticket
    with pytest.raises(TicketError, match="not valid YAML"):
        schema.parse_ticket("---\ntitle: [unclosed\n---\n")
