"""Terminal styling: the one place that decides whether ANSI escapes are safe.

Reports are read by people *and* by programs, so colour is never automatic: `auto`
(the default) colours only when stdout is a terminal that will render escapes,
`never` is plain text, and `always` is the caller's explicit override. `NO_COLOR`
is honoured. `FORCE_COLOR` deliberately is not -- a harness that sets it is usually
capturing output to a file or a log, which is exactly where escapes are junk; the
override for those callers is `--color always` or `ARBITE_COLOR=always`.

Every command that prints a table, a heading or a rule goes through here rather
than writing escape codes itself, so the decision is made once and the fallbacks
(no terminal, `TERM=dumb`, a stream that cannot encode the rule character) live in
one module. Colour never carries information on its own: a status word, a count and
a heading all say the same thing in plain text.
"""

from __future__ import annotations

import os
import re
import sys

# SGR codes, in the 8/16-colour range only: any terminal that renders ANSI at all
# renders these, where 256-colour and truecolour codes degrade to something
# arbitrary on a console that does not expect them.
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"

#: The values `--color` accepts, and the only ones `ARBITE_COLOR` may name.
COLOR_MODES = ("auto", "always", "never")

#: `ARBITE_COLOR` sets the default mode for a whole environment. `NO_COLOR` is the
#: cross-tool switch, honoured when it is set to anything but the empty string (an
#: empty `NO_COLOR` means 'unset', which is how no-color.org defines it).
ENV_COLOR = "ARBITE_COLOR"
ENV_NO_COLOR = "NO_COLOR"
ENV_TERM = "TERM"

#: How each ticket status prints. Keyed by the values in `schema.STATUSES`; a status
#: this map has not heard of prints plain, so a status added to the vocabulary later
#: degrades instead of failing. `tests/test_term.py` pins the keys against
#: `schema.STATUSES` so the two cannot drift.
STATUS_CODES = {
    "raw": (DIM,),
    "open": (GREEN,),
    "in_progress": (YELLOW,),
    "review": (MAGENTA,),
    "blocked": (RED,),
    "shelved": (DIM,),
    "closed": (DIM,),
}

#: The horizontal rule character, falling back to '-' on a stream that cannot
#: encode it (see `rule_char`).
RULE_CHARACTER = "─"

# ANSI/CSI escape sequences, for measuring a painted line in visible columns.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Whether this process may paint. Set once by `configure`; a caller that never
# configures (a library user, a unit test) gets plain text.
_enabled = False


def _stream_is_tty(stream) -> bool:
    """Whether `stream` is a terminal, treating anything odd as 'not one'."""
    try:
        return bool(stream.isatty())
    except Exception:
        # No `isatty` at all, or one that raises: a closed handle, a logger dressed
        # up as a file, a proxy object. None of those renders escapes.
        return False


def _windows_console_renders_ansi(env) -> bool:
    """Whether this Windows console is one that renders ANSI escapes.

    Windows Terminal and ConEmu say so directly; an MSYS/Cygwin/Git-Bash session
    runs in a pty that understands escapes and sets `TERM`. Anything else is a
    console that would print the codes literally, so `auto` stays off there and
    `--color always` remains the way to insist.
    """
    if env.get("WT_SESSION") or env.get("ANSICON"):
        return True
    if (env.get("ConEmuANSI") or "").upper() == "ON":
        return True
    return bool(env.get(ENV_TERM))


def color_enabled(mode="auto", stream=None, env=None) -> bool:
    """Whether escapes may be written, decided from a mode, an environment, a stream.

    Pure, so the policy is testable without a terminal. `never` and `always` are the
    caller's word and are taken as given; `auto` needs a positive signal, which means
    `NO_COLOR` unset, stdout an actual terminal, `TERM` set and not `dumb`, and -- on
    Windows -- a console known to render escapes.

    `NO_COLOR` is checked here as well as in `resolve_mode`, because a caller can
    reach this function directly rather than through the CLI's one resolution step.
    """
    if mode == "never":
        return False
    if mode == "always":
        return True
    env = os.environ if env is None else env
    stream = sys.stdout if stream is None else stream
    if env.get(ENV_NO_COLOR):
        return False
    if not _stream_is_tty(stream):
        return False
    term = env.get(ENV_TERM, "")
    if not term or term == "dumb":
        return False
    if os.name == "nt":
        return _windows_console_renders_ansi(env)
    return True


def resolve_mode(flag=None, env=None) -> str:
    """The mode to use: `--color`, else `NO_COLOR`, else `ARBITE_COLOR`, else `auto`.

    The flag is this invocation's decision and outranks everything. Without one, the
    cross-tool `NO_COLOR` switch beats `ARBITE_COLOR`, because a user who exported it
    in their shell profile means every tool in that shell. An `ARBITE_COLOR` naming
    something other than `auto`/`always`/`never` is ignored rather than fatal: it is
    not this command's business to break every other tool.
    """
    env = os.environ if env is None else env
    if flag:
        return flag
    if env.get(ENV_NO_COLOR):
        return "never"
    configured = (env.get(ENV_COLOR) or "").strip().lower()
    return configured if configured in COLOR_MODES else "auto"


def configure(mode="auto", stream=None, env=None) -> bool:
    """Decide once, for this process, whether commands may paint their output."""
    global _enabled
    _enabled = color_enabled(mode, stream=stream, env=env)
    return _enabled


def enabled() -> bool:
    """Whether painting currently does anything."""
    return _enabled


def paint(text, *codes) -> str:
    """Wrap `text` in the SGR codes, or return it untouched when colour is off."""
    if not _enabled or not codes:
        return text
    return "".join(codes) + text + RESET


def paint_id(text) -> str:
    """A ticket id, which a reader scans a table by."""
    return paint(text, BOLD)


def status_codes(status: str):
    """The SGR codes for a status, or `()` for one this module does not know."""
    return STATUS_CODES.get(status.strip(), ())


def paint_status(text) -> str:
    """A status word, coloured by its meaning (plain for an unknown status).

    Accepts a padded table cell: the surrounding whitespace is ignored for the
    lookup, so a column laid out by width still finds its colour."""
    return paint(text, *status_codes(text))


def paint_heading(text) -> str:
    """An epic or section heading."""
    return paint(text, BOLD, CYAN)


def paint_muted(text) -> str:
    """Context rather than subject: a count line, a hint, a closed row."""
    return paint(text, DIM)


def rule_char(stream=None) -> str:
    """The rule character, or `-` when the stream cannot encode it.

    `main()` asks for UTF-8, but a caller can force an encoding (or hand arbite an
    already-wrapped stream), and a box-drawing character that cannot be encoded
    would raise UnicodeEncodeError at print time. A rule is decoration, so it
    degrades instead.
    """
    stream = sys.stdout if stream is None else stream
    try:
        RULE_CHARACTER.encode(getattr(stream, "encoding", None) or "utf-8")
    except (LookupError, UnicodeEncodeError, TypeError):
        return "-"
    return RULE_CHARACTER


def rule(width: int, stream=None) -> str:
    """A horizontal rule `width` visible columns wide, dimmed when colour is on."""
    return paint(rule_char(stream) * max(0, width), DIM)


def visible_width(text: str) -> int:
    """The printed width of `text`: escape codes take no columns.

    Used to size a rule to the heading above it, which is painted and so longer
    than it looks.
    """
    return len(_ANSI_RE.sub("", text))
