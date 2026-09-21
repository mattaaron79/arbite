"""The outcome vocabulary: exit codes, labels, `next:` lines and the JSON result.

The point of these tests is that a caller branching on an exit code, a caller
reading stderr and a caller reading `next_actions` in JSON all learn the same thing,
and that busy (4) and stale (5) are distinguishable from a genuine error without
parsing prose.
"""

from __future__ import annotations

import pytest

from arbite.coordination import results
from arbite.errors import Busy, CoordinationError, RecordError, Stale, TicketError


def test_the_exit_code_table_matches_the_handoff():
    """0-5, and 4/5 mean "nothing changed" rather than "something failed"."""
    assert results.EXIT_CODES == {
        "ok": 0,
        "error": 1,
        "empty": 2,
        "problems": 3,
        "busy": 4,
        "stale": 5,
    }
    assert results.OUTCOME_KINDS == ("ok", "error", "empty", "problems", "busy", "stale")


def test_passthrough_has_its_own_documented_codes():
    """A wrapped tool's own exit code must survive, so arbite's refusals there use
    125/126/127 -- and none of them can be confused with 0-5."""
    assert (results.EXIT_PASSTHROUGH_REFUSED, results.EXIT_PASSTHROUGH_UNSUPPORTED,
            results.EXIT_PASSTHROUGH_NOT_FOUND) == (125, 126, 127)


def test_ok_prints_no_next_line_and_carries_no_hints():
    result = results.succeeded(lines=["workspace: ws-7c41"])

    assert result.exit_code == 0
    assert result.to_text() == "workspace: ws-7c41"
    assert result.to_json() == {"next_actions": []}


def test_a_single_hint_renders_on_one_line():
    """The shape every frozen transcript uses for one follow-up command."""
    result = results.OperationResult(
        results.Outcome(results.ERROR, "not_ready"),
        ["tic-9b57 is not ready"],
        next_actions=["'arbite deps tic-9b57' to see the chain"],
    )

    assert result.to_text() == (
        "tic-9b57 is not ready\nnext: 'arbite deps tic-9b57' to see the chain"
    )


def test_two_hints_continue_under_the_first():
    """Two follow-ups share one `next:` line, continued under it with `or`."""
    line = results.render_next_line(
        ["'arbite list next --tier high' for workable tickets", "'arbite deps tic-9b57'"]
    )

    assert line == (
        "next: 'arbite list next --tier high' for workable tickets,\n"
        "      or 'arbite deps tic-9b57'"
    )


def test_hints_are_rendered_from_the_reason_not_the_call_site():
    """A reason's hint wins over its kind's, so a specific answer is never buried
    under a generic one."""
    results.register_next_actions("test_reason", ["'arbite show tic-a1b2' to re-read it"])

    assert results.next_actions_for("stale", "test_reason") == [
        "'arbite show tic-a1b2' to re-read it"
    ]
    assert results.next_actions_for("stale") == [
        "'arbite show <id>' to re-read the current state, then retry the change"
    ]


def test_an_unknown_outcome_kind_is_a_bug_not_a_silent_default():
    with pytest.raises(RecordError):
        results.Outcome("almost")

    with pytest.raises(RecordError):
        results.next_actions_for("almost")


def test_a_hint_must_be_a_real_command_line():
    with pytest.raises(RecordError):
        results.register_next_actions("test_blank", [""])


def test_busy_and_stale_results_carry_their_own_labels_and_codes():
    busy = results.refused_busy("held by tic-cf9f / att-91bd since 06:12:41")
    stale = results.refused_stale("attempt att-91bd generation 2 is no longer current")

    assert (busy.exit_code, busy.outcome.label) == (4, "busy")
    assert (stale.exit_code, stale.outcome.label) == (5, "stale_read")
    assert busy.to_stderr_text() == "busy: held by tic-cf9f / att-91bd since 06:12:41"
    assert stale.to_stderr_text().startswith("stale_read: attempt att-91bd")
    # ...and each one tells the caller what to do next, from the vocabulary's table.
    assert busy.next_actions and stale.next_actions


def test_an_error_keeps_the_text_the_cli_has_always_printed():
    result = results.failed("tic-9b57 is not ready: depends_on tic-cf9f is in_progress")

    assert result.exit_code == 1
    assert result.outcome.label == "error"
    assert result.to_stderr_text().startswith("error: tic-9b57 is not ready")


def test_exceptions_map_onto_the_vocabulary():
    """One place turns a failure into a branch key, so the CLI cannot map the codes
    differently from the rest of the coordination layer."""
    assert results.outcome_of(TicketError("bad input")).kind == "error"
    assert results.outcome_of(CoordinationError("no store")).kind == "error"
    assert results.outcome_of(Busy("held")).kind == "busy"
    assert results.outcome_of(Stale("stale token")).kind == "stale"


def test_an_exception_can_carry_its_own_hints():
    """A command that knows the exact follow-up passes it with the failure; the
    vocabulary's fallback is only for the cases that do not."""
    error = Busy("held by att-91bd")
    error.next_actions = ["'arbite file claim other.py --ticket tic-9b57 --attempt att-4c81'"]

    assert results.next_actions_of(error) == [
        "'arbite file claim other.py --ticket tic-9b57 --attempt att-4c81'"
    ]


def _run_a_command_that_raises(monkeypatch, argv, error):
    """Drive `main()` with a throwaway parser whose only command raises `error`.

    In-process because the thing under test is the CLI's single failure path: the
    exit code, the label on stderr and the rendered hints."""
    import argparse
    import sys

    from arbite import cli as arbite_cli

    def raise_it(_args):
        raise error

    def fake_build_parser():
        parser = argparse.ArgumentParser(prog="arbite")
        sub = parser.add_subparsers()
        command = sub.add_parser("boom")
        command.set_defaults(func=raise_it)
        return parser, {"boom": command}

    monkeypatch.setattr(arbite_cli, "build_parser", fake_build_parser)
    monkeypatch.setattr(sys, "argv", ["arbite", *argv])
    return arbite_cli


def test_the_cli_exits_four_for_busy_and_names_it_on_stderr(monkeypatch, capsys):
    """The vocabulary is only real if the process actually exits 4 and prints the
    same word the code carries, so a caller branching on the code and one reading
    stderr agree."""
    arbite_cli = _run_a_command_that_raises(
        monkeypatch, ["boom"], Busy("held by tic-cf9f / att-91bd since 06:12:41, gen 1")
    )

    with pytest.raises(SystemExit) as exit_info:
        arbite_cli.main()

    assert exit_info.value.code == 4
    captured = capsys.readouterr()
    assert captured.err.startswith("busy: held by tic-cf9f / att-91bd")
    assert "next: " in captured.err


def test_the_cli_exits_five_for_stale_and_names_it_on_stderr(monkeypatch, capsys):
    """Stale (5) is distinct from error (1) because the caller response is
    different: re-read and retry, not fix the command."""
    arbite_cli = _run_a_command_that_raises(
        monkeypatch,
        ["boom"],
        Stale("attempt att-91bd generation 2 is no longer current"),
    )

    with pytest.raises(SystemExit) as exit_info:
        arbite_cli.main()

    assert exit_info.value.code == 5
    assert capsys.readouterr().err.startswith("stale_read: attempt att-91bd")


def test_a_plain_error_keeps_exit_one_and_its_old_message(monkeypatch, capsys):
    """The 4/5 outcomes are additions: an ordinary failure still prints exactly what
    it always has, and gains no hint it never had."""
    arbite_cli = _run_a_command_that_raises(
        monkeypatch, ["boom"], TicketError("tic-9b57 is not ready")
    )

    with pytest.raises(SystemExit) as exit_info:
        arbite_cli.main()

    assert exit_info.value.code == 1
    assert capsys.readouterr().err == "error: tic-9b57 is not ready\n"


def test_the_json_payload_mirrors_the_next_actions():
    """A fact that exists only in the text is a fact a machine consumer cannot
    branch on, which is why the same outcome produces both."""
    result = results.OperationResult(
        results.Outcome(results.BUSY, "file_busy"),
        ["held by att-91bd"],
        data={"path": "src/arbite/schema.py", "holder": "att-91bd"},
        next_actions=["'arbite list next --claim claude.sonnet.002' instead"],
    )

    assert result.to_json() == {
        "path": "src/arbite/schema.py",
        "holder": "att-91bd",
        "next_actions": ["'arbite list next --claim claude.sonnet.002' instead"],
    }
