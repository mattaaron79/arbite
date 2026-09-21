"""The harness that compares real output against the frozen example transcripts.

`.arbite/planning/interaction-examples.md` is normative: a scenario's command, its
stdout, its stderr and its exit code are all asserted, and the scenario id names the
test that asserts them (`test_ws1_report_the_derived_workspace`). Two rules make
that checkable rather than decorative:

- **The transcripts are read from the document**, never copied into a test, so a
  scenario cannot pass while the document says something else.
- **Both sides are normalised** before comparison, because ids, times and paths
  differ on every machine and in every checkout: `normalise()` substitutes
  `tic/ws/att/op/clm/art/evt-XXXX` ids, `HH:MM:SS` times, RFC 3339 UTC timestamps,
  dates, the project root and any path below an `.arbite` directory -- the same idea
  the existing suite uses for ticket ids.

Text scenarios are compared byte for byte after normalisation. A scenario whose
output is a JSON document is compared as *parsed* JSON with its string leaves
normalised, because the document indents its blocks for reading while `--json`
prints one key per line: the facts are the assertion, not the whitespace.

Two shapes the document uses to *abridge* a long transcript are understood, so a
block that shows its facts is still asserted on the facts rather than skipped:

- An **elision line** (`… 99 more files`) asserts that exactly that many rows follow
  where it stands. Their content is the property tests' business; the count is the
  transcript's.
- A **body sample** in a read transcript (gutter lines after the `---` separator) names
  the line it shows by number, so the sample is matched against *that line of the
  body* -- with a trailing `...` meaning "this line continues". A read block's body
  may be sampled rather than complete: a whole-file read prints hundreds of lines and
  the document shows the gutter format, not the file.

A block that needs neither is compared byte for byte, and a block the document
visibly abridges in other ways (a dropped parenthetical, a hand-aligned column) is
asserted with `assert_scenario_abridged`, which says so out loud at the call site.

Which stream a transcript's body belongs to is a fact about the outcome, not a
choice a test makes:

- A body that begins with an outcome label (`error:`, `busy:`, `stale_read:`) is a
  *refusal*, and the CLI prints those on stderr -- that is where the exit-code
  vocabulary has always been reported, whichever command produced it.
- A block annotated `# exit N, note on stderr` says so explicitly (the batch-claim
  transcript does: a table on stdout, one commentary line on stderr), and the trailing
  `note:` lines are compared against stderr while the rest is compared against stdout.
- Everything else is stdout, and the other stream must be empty.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
EXAMPLES_DOC = REPO_ROOT / ".arbite" / "planning" / "interaction-examples.md"

#: The path the document was written against (it is verifiable against this repo,
#: so its blocks name this checkout's root literally).
DOC_ROOT = "/media/matt/m2tb/projects/arbite"

SCENARIO_HEADING = re.compile(r"^## (?P<id>[A-Z]{2}[0-9]+) · (?P<title>.+?)\s*$")
#: `# exit N` ends the transcript; the document may annotate which stream the body
#: went to (`# exit 0, note on stderr`), which is part of the frozen block.
EXIT_LINE = re.compile(r"^#\s*exit\s+(?P<code>\d+)\s*(?P<annotation>.*)$")
COMMAND_PREFIX = "$ "
NOTE_PREFIX = "note:"
#: The labels a refusal prints in front of its message. A transcript that starts with
#: one of these says "this is what the command printed on stderr", so the harness does
#: not have to guess which stream to compare (see the module docstring).
REFUSAL_LABELS = ("error:", "busy:", "stale_read:")
#: The document's elision line: `… N more files`, `… 499 more matches`. The count is
#: asserted (that many rows follow) and the rows themselves are not compared here.
ELISION_RE = re.compile(r"^…\s*(?:(?P<count>\d+)\s+)?more\b")
#: A read transcript's body sample: `1254 | def cmd_claim(args):`.
SAMPLE_RE = re.compile(r"^(?P<number>\d+)\s*\|\s?(?P<text>.*)$")
#: The separator between a read report and the bytes it served.
BODY_SEPARATOR = "---"
#: A sample line that trails off (`...` in the document) matches by prefix.
TRAILING_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|…)\s*$")

ID_RE = re.compile(r"\b(tic|ws|att|clm|op|art|evt)-[0-9a-f]{4}\b")
#: A content digest, shortened for text or whole (`sha256:<hex>`). Normalised because a
#: digest is a fact about *bytes*: the document's blocks were written against an earlier
#: revision of this repo's own files (its `sinks/file.py` is 412 lines, the file here is
#: not), so a literal digest can never be reproduced by a fixture that must also keep the
#: line counts, paths and dates the same block asserts. The relationship the digest
#: stands for is asserted directly instead, against the store (see the claim scenarios).
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{12,64}\b")
UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
LOCAL_TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
ARBITE_PATH_RE = re.compile(r"(?:[^\s\"'()]*[/\\])?\.arbite((?:[/\\][^\s\"'(),]*)?)")


@dataclass(frozen=True)
class Scenario:
    """One frozen block: the command, what it must print, and its exit code.

    `stdout` is the transcript's body, which -- for a refusal, or for a block the
    document annotates as writing a note to stderr -- is what the command printed on
    stderr; `stderr` holds the note lines the annotation moved out of the body."""

    id: str
    title: str
    command: tuple
    exit_code: int
    stdout: str
    json_payload: Optional[dict] = None
    stderr: str = ""

    @property
    def is_json(self) -> bool:
        return self.json_payload is not None

    @property
    def on_stderr(self) -> bool:
        """Whether the body is a refusal, which the CLI reports on stderr."""
        first = self.stdout.lstrip().splitlines()
        return bool(first) and first[0].startswith(REFUSAL_LABELS)

    def with_ticket(self, ticket_id: str, illustrative: str = "tic-cf9f") -> "Scenario":
        """The frozen command with the document's illustrative ticket id replaced.

        The document's ids are illustrative holders in its own words -- "the harness
        substitutes every id before comparing output" -- and a command that *names* a
        ticket has to point at the fixture's real ticket to be runnable at all."""
        return replace(
            self, command=tuple(ticket_id if arg == illustrative else arg for arg in self.command)
        )


def _blocks(text: str) -> list:
    """`(heading_id, title, lines)` for every scenario heading in the document."""
    found = []
    current = None
    for line in text.splitlines():
        match = SCENARIO_HEADING.match(line)
        if match:
            current = [match.group("id"), match.group("title"), []]
            found.append(current)
            continue
        if current is not None:
            current[2].append(line)
    return [(entry[0], entry[1], entry[2]) for entry in found]


def fenced_blocks(scenario_id: str, path: Path = EXAMPLES_DOC) -> list:
    """Every fenced block body under a scenario heading, in document order.

    The *first* is the transcript (`scenario_block` reads it). A heading may carry a
    second one documenting a JSON payload the same command prints -- EV3's "poll
    shape" is one -- and a test that never looked at it could let the JSON drift
    from the text the document promises it mirrors."""
    for found_id, title, lines in _blocks(path.read_text(encoding="utf-8")):
        if found_id != scenario_id:
            continue
        blocks, body, in_fence = [], [], False
        for line in lines:
            if line.startswith("```"):
                if in_fence:
                    blocks.append("\n".join(body))
                    body = []
                in_fence = not in_fence
                continue
            if in_fence:
                body.append(line)
        if in_fence:  # pragma: no cover - a document whose fence is never closed
            blocks.append("\n".join(body))
        return blocks
    raise AssertionError(f"no scenario {scenario_id} in {path}")


def scenario_json_blocks(scenario_id: str, path: Path = EXAMPLES_DOC) -> list:
    """The later fenced blocks under a heading that are JSON documents.

    Parsed rather than compared as text, for the same reason a JSON transcript is:
    the document indents its blocks for reading while `--json` prints one key per
    line, so the facts are the assertion."""
    documents = []
    for text in fenced_blocks(scenario_id, path)[1:]:
        stripped = text.strip()
        if stripped.startswith("{"):
            documents.append(json.loads(stripped))
    return documents


def scenario_block(scenario_id: str, path: Path = EXAMPLES_DOC) -> Scenario:
    """The frozen scenario `scenario_id`, parsed out of the examples document.

    Only the first fenced block under the heading is read: that is the transcript.
    A block whose body is a JSON document becomes a JSON scenario, and its `# exit N`
    comment is the exit code, exactly as the text blocks carry theirs. An annotation
    after the code (`# exit 0, note on stderr`) moves the body's trailing `note:`
    lines into `Scenario.stderr`, which is what makes a transcript that writes to both
    streams assertable instead of half-checked."""
    for found_id, title, lines in _blocks(path.read_text(encoding="utf-8")):
        if found_id != scenario_id:
            continue
        commands, body, exit_code, annotation = [], [], 0, ""
        in_fence = False
        for line in lines:
            if line.startswith("```"):
                if in_fence:
                    break  # the transcript's fence closes: everything after is prose
                in_fence = True
                continue
            if not in_fence:
                continue
            if line.startswith(COMMAND_PREFIX):
                commands.append(line[len(COMMAND_PREFIX):].strip())
                continue
            exit_match = EXIT_LINE.match(line.strip())
            if exit_match:
                exit_code = int(exit_match.group("code"))
                annotation = exit_match.group("annotation")
                continue
            body.append(line)
        if not commands:
            raise AssertionError(f"scenario {scenario_id} has no command line in its block")
        stderr = ""
        if "stderr" in annotation:
            body, notes = _split_trailing_notes(body)
            stderr = "\n".join([*notes, ""]).strip("\n")
        text = "\n".join(body).strip("\n")
        payload = None
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError as e:  # pragma: no cover - a broken document is a bug
                raise AssertionError(f"scenario {scenario_id} has an unparseable JSON block: {e}")
        return Scenario(
            id=scenario_id,
            title=title,
            # Split the way a shell would, so a frozen command that quotes an argument
            # (`--reason "edits complete"`) reaches the CLI as one argument rather than
            # three. Splitting on whitespace alone would only work while no block quoted
            # anything.
            command=tuple(shlex.split(commands[0])[1:]),  # drop the leading `arbite`
            exit_code=exit_code,
            stdout="" if payload is not None else text,
            json_payload=payload,
            stderr=stderr,
        )
    raise AssertionError(f"no scenario {scenario_id} in {path}")


def scenario_from_block(block: str, scenario_id: str = "block", title: str = "") -> Scenario:
    """A `Scenario` built from a fenced block the document nests under a heading.

    A heading may hold more than one transcript -- RD2's `--fail-if-busy` refusal, LS6's
    protected path, LS5's two commands in one fence -- and `scenario_block` reads only
    the first. This turns any other block into the same value, so the second transcript
    is asserted by the same code as the first instead of by a hand-rolled comparison.
    A block without an `# exit N` comment exits 0, which is what the first half of LS5's
    fence is."""
    commands, body, exit_code = [], [], 0
    for line in block.splitlines():
        if line.startswith(COMMAND_PREFIX):
            commands.append(line[len(COMMAND_PREFIX) :].strip())
            continue
        exit_match = EXIT_LINE.match(line.strip())
        if exit_match:
            exit_code = int(exit_match.group("code"))
            continue
        body.append(line)
    if not commands:
        raise AssertionError(f"block {scenario_id} has no command line")
    text = "\n".join(body).strip("\n")
    return Scenario(
        id=scenario_id,
        title=title,
        command=tuple(shlex.split(commands[0])[1:]),
        exit_code=exit_code,
        stdout=text,
    )


def _split_trailing_notes(body: list) -> tuple:
    """`(body_without_notes, notes)`: the run of `note:` lines at the end of a block.

    Only a *trailing* run is taken, so a transcript may still quote the word -- and a
    block that prints notes in the middle keeps them where the document put them."""
    cut = len(body)
    while cut > 0 and body[cut - 1].strip().startswith(NOTE_PREFIX):
        cut -= 1
    return body[:cut], body[cut:]


def normalise(text: str, root=None) -> str:
    """`text` with ids, times and paths replaced by stable placeholders.

    Applied to *both* the transcript and the real output, which is what lets one
    frozen block be asserted on any machine."""
    result = str(text)
    if root is not None:
        result = result.replace(str(root), "<ROOT>")
    result = result.replace(DOC_ROOT, "<ROOT>")
    result = ARBITE_PATH_RE.sub(lambda m: "<ARBITE>" + m.group(1).replace("\\", "/"), result)
    result = ID_RE.sub(lambda m: f"{m.group(1)}-XXXX", result)
    result = DIGEST_RE.sub("sha256:<DIGEST>", result)
    result = UTC_TIMESTAMP_RE.sub("YYYY-MM-DDTHH:MM:SSZ", result)
    result = LOCAL_TIME_RE.sub("HH:MM:SS", result)
    result = DATE_RE.sub("YYYY-MM-DD", result)
    return result


def normalise_payload(payload, root=None):
    """`payload` with every string leaf normalised, for a JSON transcript."""
    if isinstance(payload, dict):
        return {key: normalise_payload(value, root) for key, value in payload.items()}
    if isinstance(payload, list):
        return [normalise_payload(value, root) for value in payload]
    if isinstance(payload, str):
        return normalise(text=payload, root=root)
    return payload


def run_cli(cwd, *args, sink: Optional[str] = None):
    """Run the CLI in `cwd` against this checkout, as a user would.

    ARBITE_SINK is never inherited: each caller says which store it means, and a
    missing environment keeps the project's committed answer."""
    environment = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    environment.pop("ARBITE_SINK", None)
    if sink:
        environment["ARBITE_SINK"] = sink
    return subprocess.run(
        [sys.executable, "-m", "arbite.cli", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
    )


def run_scenario(scenario: Scenario, cwd, sink: Optional[str] = None):
    """Run a scenario's command, exactly as its transcript writes it."""
    return run_cli(cwd, *scenario.command, sink=sink)


def assert_scenario(scenario: Scenario, cwd, sink: Optional[str] = None, stream: Optional[str] = None) -> str:
    """Run `scenario` and assert it matches its frozen transcript exactly.

    Exit code, the body and the *other* stream are all asserted: a transcript that
    belongs on stderr must not also appear on stdout (and the reverse), and a block the
    document annotates as writing a note to stderr is checked on both streams.

    `stream` overrides *which* stream the body belongs to, for the one refusal that is
    deliberately not where refusals normally go: EV7's `--follow` refusal is printed on
    stdout (the application layer says why), and the document records neither stream.
    Everything else is decided by the body's own label (see the module docstring)."""
    proc = run_scenario(scenario, cwd, sink=sink)
    where = f"scenario {scenario.id} ('arbite {' '.join(scenario.command)}')"
    assert proc.returncode == scenario.exit_code, (
        f"{where} exited {proc.returncode}, expected {scenario.exit_code}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    if scenario.stderr:
        _assert_text(scenario, where, proc.stdout, scenario.stdout, cwd, "stdout")
        _assert_text(scenario, where, proc.stderr, scenario.stderr, cwd, "stderr")
        return proc.stdout
    on_stderr = scenario.on_stderr if stream is None else stream == "stderr"
    if on_stderr:
        assert proc.stdout == "", f"{where} also wrote to stdout:\n{proc.stdout}"
        actual_text, carrier = proc.stderr, proc.stderr
    else:
        assert proc.stderr == "", f"{where} wrote to stderr:\n{proc.stderr}"
        actual_text, carrier = proc.stdout, proc.stdout
    expected_text = scenario.stdout
    if scenario.is_json:
        assert carrier is proc.stdout, f"{where} is JSON, so it cannot be a refusal"
        actual = normalise_payload(json.loads(actual_text), cwd)
        expected = normalise_payload(scenario.json_payload, cwd)
        assert actual == expected, (
            f"{where} JSON differs\nactual:   {json.dumps(actual, indent=2, sort_keys=True)}\n"
            f"expected: {json.dumps(expected, indent=2, sort_keys=True)}"
        )
    else:
        _assert_text(scenario, where, actual_text, expected_text, cwd, "text")
    return carrier


def _assert_text(scenario: Scenario, where: str, actual_text: str, expected_text: str, cwd, what: str):
    """One stream compared after normalisation, with both sides printed on failure.

    A transcript that elides rows or samples a body is compared by the rules at the
    top of this module; everything else is compared byte for byte, which is what
    makes "the transcript passes" mean exactly what it says."""
    actual = normalise(actual_text, cwd).strip("\n")
    expected = normalise(expected_text, cwd).strip("\n")
    if _is_abridged(expected):
        _assert_abridged(where, actual.split("\n"), expected.split("\n"), what)
        return
    assert actual == expected, (
        f"{where} {what} differs\n--- actual ---\n{actual}\n--- expected ---\n{expected}"
    )


def _is_abridged(expected_text: str) -> bool:
    """Whether a transcript elides rows or samples a read body (see the docstring)."""
    lines = expected_text.split("\n")
    if any(ELISION_RE.match(line.strip()) for line in lines):
        return True
    return BODY_SEPARATOR in [line.strip() for line in lines]


def _assert_abridged(where: str, actual_lines: list, expected_lines: list, what: str):
    """Compare an abridged transcript line by line, asserting its counts and samples.

    Every expected line is consumed in order: a plain line must equal the next actual
    line, an elision line must be backed by exactly the number of rows it states, and a
    sample names the body line it shows. A read body may be sampled rather than
    complete, so trailing body lines after a sample are allowed to go uncompared."""
    failures = []

    def fail(message):
        failures.append(
            f"{where} {what} differs: {message}\n--- actual ---\n"
            + "\n".join(actual_lines)
            + "\n--- expected ---\n"
            + "\n".join(expected_lines)
        )

    cursor = 0
    sampled = False
    separator = _separator_index(expected_lines)
    for index, line in enumerate(expected_lines):
        elision = ELISION_RE.match(line.strip())
        if elision:
            count = int(elision.group("count") or 1)
            rows = actual_lines[cursor : cursor + count]
            if len(rows) < count or any(not row.strip() for row in rows):
                fail(f"the elision needs {count} rows after line {index + 1}")
                break
            cursor += count
            continue
        if separator is not None and index > separator:
            sample = SAMPLE_RE.match(line.strip())
            if sample:
                sampled = True
                position = _find_sample(actual_lines, cursor, sample)
                if position is None:
                    fail(f"the sample {line.strip()!r} is not the body line it names")
                    break
                cursor = position + 1
                continue
        if cursor >= len(actual_lines) or actual_lines[cursor] != line:
            fail(f"line {index + 1} is not {line!r}")
            break
        cursor += 1
    else:
        if not sampled and separator is None and cursor != len(actual_lines):
            fail(f"{len(actual_lines) - cursor} unexpected line(s) after the transcript")
    assert not failures, "\n".join(failures)


def _separator_index(expected_lines: list):
    """Where a read transcript's `---` sits, or None for a transcript without one."""
    for index, line in enumerate(expected_lines):
        if line.strip() == BODY_SEPARATOR:
            return index
    return None


def _find_sample(actual_lines: list, start: int, sample) -> Optional[int]:
    """The body line a sample names, or None when the body does not hold it."""
    number, text = int(sample.group("number")), sample.group("text")
    wanted = TRAILING_ELLIPSIS_RE.sub("", text)
    for position in range(start, len(actual_lines)):
        actual = SAMPLE_RE.match(actual_lines[position].strip())
        if actual is None or int(actual.group("number")) != number:
            continue
        shown = actual.group("text")
        if shown == text or (wanted != text and shown.startswith(wanted)):
            return position
    return None


def assert_scenario_abridged(
    scenario: Scenario, cwd, sink: Optional[str] = None, stream: Optional[str] = None
) -> str:
    """Assert an *abridged* transcript: every line's facts, not its exact layout.

    The document abridges a handful of blocks in ways no single implementation can
    reproduce: it drops a parenthetical the JSON carries, writes `142 KiB` for
    `142.0 KiB`, and aligns a note column by hand. For those blocks the comparison is
    per line with whitespace collapsed, and each expected line must be a *prefix* of
    an actual line, in order. It is deliberately weaker than `assert_scenario`, so it
    is used only where the block itself is visibly abridged -- and the test that calls
    it says which abridgement it is accepting."""
    proc = run_scenario(scenario, cwd, sink=sink)
    where = f"scenario {scenario.id} ('arbite {' '.join(scenario.command)}')"
    assert proc.returncode == scenario.exit_code, (
        f"{where} exited {proc.returncode}, expected {scenario.exit_code}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    on_stderr = scenario.on_stderr if stream is None else stream == "stderr"
    actual_text, expected_text = proc.stdout, scenario.stdout
    if on_stderr:
        assert proc.stdout == "", f"{where} also wrote to stdout:\n{proc.stdout}"
        actual_text = proc.stderr
    else:
        assert proc.stderr == "", f"{where} wrote to stderr:\n{proc.stderr}"
    assert_facts(where, actual_text, expected_text, cwd)
    return actual_text


def assert_facts(where: str, actual_text: str, expected_text: str, cwd=None, ordered: bool = True) -> None:
    """Each expected line must be a prefix of an actual line, in order (see
    `assert_scenario_abridged` for when that is the right question).

    `ordered=False` asks for the same facts in *any* order. That is for a block whose
    document lists its rows in an order the display rules do not produce (LS5 names
    `project.yaml` before an agent scratchpad, which canonical path order does not), so
    the transcript's *contents* are the assertion and its sequence is not. Every fact
    still has to be present, and each actual line accounts for one expectation at
    most, so a listing cannot pass by repeating one row."""
    actual = [_collapse(_whole_units(line)) for line in normalise(actual_text, cwd).splitlines()]
    expected = [
        _collapse(_whole_units(line))
        for line in normalise(expected_text, cwd).splitlines()
        if line.strip()
    ]
    next_position = 0
    for line in expected:
        search = range(next_position, len(actual)) if ordered else range(len(actual))
        found = next(
            (position for position in search if actual[position].startswith(line)), None
        )
        assert found is not None, (
            f"{where}: no line starts with {line!r}\n--- actual ---\n"
            + "\n".join(actual)
            + "\n--- expected ---\n"
            + "\n".join(expected)
        )
        next_position = found + 1
        if not ordered:
            # Claim the line, so no second fact can be satisfied by the same row.
            actual[found] = "\0"


def _collapse(text: str) -> str:
    """`text` with every whitespace run collapsed to one space.

    Column padding and line wrapping are the document's layout, not its facts."""
    return re.sub(r"\s+", " ", text).strip()


def _whole_units(text: str) -> str:
    """`142.0 KiB` and `142 KiB` are the same number, and the document writes both."""
    return re.sub(r"(\d)\.0 (?=[KMG]iB)", r"\1 ", text)
