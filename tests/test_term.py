"""The colour policy, tested as the pure decision it is.

`arbite.term` is the one place that decides whether escape codes may be written, so
these are the rules every command inherits: `auto` needs a positive signal, `always`
and `never` are the caller's word, `NO_COLOR` outranks `ARBITE_COLOR`, and anything
the module cannot inspect degrades to plain text rather than raising.
"""

from __future__ import annotations

import pytest

from arbite import schema, term


class FakeStream:
    """A stand-in for stdout: a terminal (or not), with an encoding to test against."""

    def __init__(self, tty, encoding="utf-8"):
        self._tty = tty
        self.encoding = encoding

    def isatty(self):
        return self._tty


class RaisingStream(FakeStream):
    """A stream that cannot answer, like a closed or already-wrapped handle."""

    def isatty(self):
        raise ValueError("closed")


#: `WT_SESSION` makes the Windows branch of the decision agree with the Linux one, so
#: the same assertion means the same thing on either platform.
TERMINAL = {"TERM": "xterm-256color", "WT_SESSION": "1"}


@pytest.fixture(autouse=True)
def plain_text():
    """Leave the module's process-wide switch off, so no test leaks into the next."""
    term.configure("never")
    yield
    term.configure("never")


def test_auto_needs_a_terminal_on_the_other_end():
    assert term.color_enabled("auto", stream=FakeStream(True), env=TERMINAL)
    assert not term.color_enabled("auto", stream=FakeStream(False), env=TERMINAL)


def test_never_and_always_are_the_callers_word():
    assert term.color_enabled("always", stream=FakeStream(False), env={})
    assert not term.color_enabled("never", stream=FakeStream(True), env=TERMINAL)


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"TERM": "dumb"}, id="dumb-terminal"),
        pytest.param({}, id="no-TERM"),
        pytest.param({**TERMINAL, "NO_COLOR": "1"}, id="NO_COLOR"),
    ],
)
def test_auto_stays_off_without_a_positive_signal(env):
    assert not term.color_enabled("auto", stream=FakeStream(True), env=env)


def test_an_empty_no_color_means_unset():
    """The no-color.org rule: it is the *value* being present that counts, so an empty
    `NO_COLOR` -- `NO_COLOR=` in a shell profile, say -- does not silence anything."""
    assert term.color_enabled("auto", stream=FakeStream(True), env={**TERMINAL, "NO_COLOR": ""})


def test_a_windows_console_is_only_trusted_when_it_says_so():
    """A console that does not render escapes prints them literally, so `auto` needs a
    signal: Windows Terminal, ConEmu, ANSICON, or an MSYS-style pty that sets TERM."""
    assert term._windows_console_renders_ansi({"WT_SESSION": "1"})
    assert term._windows_console_renders_ansi({"ANSICON": "1"})
    assert term._windows_console_renders_ansi({"ConEmuANSI": "ON"})
    assert term._windows_console_renders_ansi({"TERM": "xterm"})
    assert not term._windows_console_renders_ansi({})
    assert not term._windows_console_renders_ansi({"ConEmuANSI": "OFF"})


def test_a_stream_that_cannot_be_inspected_is_not_a_terminal():
    assert not term.color_enabled("auto", stream=RaisingStream(True), env=TERMINAL)
    assert not term.color_enabled("auto", stream=object(), env=TERMINAL)


def test_resolve_mode_prefers_the_flag_then_no_color_then_the_environment():
    assert term.resolve_mode("never", {term.ENV_COLOR: "always"}) == "never"
    assert term.resolve_mode(None, {term.ENV_COLOR: "always"}) == "always"
    assert term.resolve_mode(None, {term.ENV_COLOR: "ALWAYS"}) == "always"
    assert term.resolve_mode(None, {term.ENV_COLOR: "always", term.ENV_NO_COLOR: "1"}) == "never"
    assert term.resolve_mode(None, {}) == "auto"
    # A value that names no mode is ignored rather than fatal.
    assert term.resolve_mode(None, {term.ENV_COLOR: "mauve"}) == "auto"


def test_painting_is_identity_until_it_is_switched_on():
    assert term.paint("tic-a1b2", term.BOLD) == "tic-a1b2"
    assert term.paint_status("closed") == "closed"

    term.configure("always")
    try:
        assert term.paint("tic-a1b2", term.BOLD) == "\x1b[1mtic-a1b2\x1b[0m"
        assert term.paint_status("closed") == "\x1b[2mclosed\x1b[0m"
    finally:
        term.configure("never")


def test_a_padded_table_cell_finds_its_colour_and_keeps_its_columns():
    term.configure("always")
    try:
        padded = term.paint_status("closed        ")
        assert padded == "\x1b[2mclosed        \x1b[0m"
        # An escape takes no columns, so a painted cell is as wide as the plain one.
        assert term.visible_width(padded) == len("closed        ")
    finally:
        term.configure("never")


def test_an_unknown_status_prints_plain():
    """A status added to the vocabulary later degrades instead of failing."""
    term.configure("always")
    try:
        assert term.paint_status("moonwalking") == "moonwalking"
    finally:
        term.configure("never")


def test_every_status_in_the_vocabulary_has_a_colour():
    assert set(term.STATUS_CODES) == set(schema.STATUSES)


def test_the_live_statuses_do_not_share_an_accent():
    """The four statuses a reader scans mid-flight, the epic violet and the assignee
    orange are six different colours, so no row reads as two things at once."""
    accents = [
        term.status_codes(status)[0]
        for status in ("open", "in_progress", "review", "blocked")
    ]
    assert len(set(accents)) == 4
    for accent in (term.EPIC_256, term.EPIC_16, term.ASSIGNEE_256, term.ASSIGNEE_16):
        assert accent not in accents


def test_the_extended_palette_is_only_used_when_the_terminal_advertises_it():
    assert term.supports_256_colors({"TERM": "xterm-256color"})
    assert term.supports_256_colors({"TERM": "screen-256color"})
    assert term.supports_256_colors({"TERM": "xterm", "COLORTERM": "truecolor"})
    assert term.supports_256_colors({"TERM": "xterm", "COLORTERM": "24bit"})
    assert not term.supports_256_colors({"TERM": "xterm"})
    assert not term.supports_256_colors({})


def test_the_accents_take_the_extended_palette_or_the_nearest_base_colour():
    term.configure("always", env={"TERM": "xterm-256color"})
    assert term.rich_palette()
    assert term.epic_code() == term.EPIC_256
    assert term.paint_epic("mesh-pipeline") == "\x1b[38;5;135mmesh-pipeline\x1b[0m"
    assert term.paint_assignee("claude.haiku.001") == "\x1b[38;5;208mclaude.haiku.001\x1b[0m"

    term.configure("always", env={"TERM": "xterm"})
    assert not term.rich_palette()
    assert term.epic_code() == term.EPIC_16
    assert term.paint_heading("mesh-pipeline") == "\x1b[1m\x1b[35mmesh-pipeline\x1b[0m"
    assert term.paint_assignee("claude.haiku.001") == "\x1b[91mclaude.haiku.001\x1b[0m"


def test_colour_off_means_no_accent_either():
    term.configure("never", env={"TERM": "xterm-256color"})
    assert not term.rich_palette()
    assert term.paint_epic("mesh-pipeline") == "mesh-pipeline"
    assert term.paint_assignee("claude.haiku.001") == "claude.haiku.001"


def test_visible_width_ignores_escapes():
    assert term.visible_width("\x1b[1mab\x1b[0m") == 2
    assert term.visible_width("ab") == 2


def test_the_rule_degrades_to_ascii_when_it_cannot_be_encoded():
    """A rule is decoration: a stream that cannot encode the box-drawing character gets
    one it can, rather than a UnicodeEncodeError at print time."""
    assert term.rule_char(FakeStream(True, encoding="utf-8")) == "─"
    assert term.rule_char(FakeStream(True, encoding="ascii")) == "-"
    assert term.rule(3, stream=FakeStream(True, encoding="ascii")) == "---"
    assert term.rule(0) == ""
