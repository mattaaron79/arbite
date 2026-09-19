"""Shared test helpers: ticket factories used by the core and sink test suites.

Kept separate from `conftest.py` so test modules can import them directly (a
plain function is easier to use inside a parametrized or table-driven test than a
fixture) while `conftest.py` stays about fixtures and teardown.
"""

from __future__ import annotations

from arbite.schema import Ticket

DEFAULT_BODY = "## Description\ndescribed\n\n## Notes\n"


def make_ticket(ticket_id: str = "tic-a1b2", **overrides) -> Ticket:
    """A valid, minimal ticket. Every field is overridable so a test can state
    only what it is about: `make_ticket(status="closed", closed="2026-02-01")`."""
    data = dict(
        id=ticket_id,
        title="a ticket",
        status="open",
        type="bug",
        tier="medium",
        domain="mesh",
        epic=None,
        priority=None,
        tags=[],
        assignee=None,
        depends_on=[],
        blocked_by=None,
        created="2026-01-01T00:00:00",
        updated="2026-01-01T00:00:00",
        closed=None,
        body=DEFAULT_BODY,
    )
    data.update(overrides)
    return Ticket(**data)


def by_id(*tickets) -> dict:
    """`{id: Ticket}` for the graph helpers."""
    return {t.id: t for t in tickets}
