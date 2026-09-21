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
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
EXAMPLES_DOC = REPO_ROOT / ".arbite" / "planning" / "interaction-examples.md"

#: The path the document was written against (it is verifiable against this repo,
#: so its blocks name this checkout's root literally).
DOC_ROOT = "/media/matt/m2tb/projects/arbite"

SCENARIO_HEADING = re.compile(r"^## (?P<id>[A-Z]{2}[0-9]+) · (?P<title>.+?)\s*$")
EXIT_LINE = re.compile(r"^#\s*exit\s+(?P<code>\d+)\s*$")
COMMAND_PREFIX = "$ "

ID_RE = re.compile(r"\b(tic|ws|att|clm|op|art|evt)-[0-9a-f]{4}\b")
UTC_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
LOCAL_TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
ARBITE_PATH_RE = re.compile(r"(?:[^\s\"'()]*[/\\])?\.arbite((?:[/\\][^\s\"'(),]*)?)")


@dataclass(frozen=True)
class Scenario:
    """One frozen block: the command, what it must print, and its exit code."""

    id: str
    title: str
    command: tuple
    exit_code: int
    stdout: str
    json_payload: Optional[dict] = None

    @property
    def is_json(self) -> bool:
        return self.json_payload is not None


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
    comment is the exit code, exactly as the text blocks carry theirs."""
    for found_id, title, lines in _blocks(path.read_text(encoding="utf-8")):
        if found_id != scenario_id:
            continue
        commands, body, exit_code = [], [], 0
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
                continue
            body.append(line)
        if not commands:
            raise AssertionError(f"scenario {scenario_id} has no command line in its block")
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
            command=tuple(commands[0].split()[1:]),  # drop the leading `arbite`
            exit_code=exit_code,
            stdout="" if payload is not None else text,
            json_payload=payload,
        )
    raise AssertionError(f"no scenario {scenario_id} in {path}")


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


def assert_scenario(scenario: Scenario, cwd, sink: Optional[str] = None) -> str:
    """Run `scenario` and assert it matches its frozen transcript exactly."""
    proc = run_scenario(scenario, cwd, sink=sink)
    where = f"scenario {scenario.id} ('arbite {' '.join(scenario.command)}')"
    assert proc.returncode == scenario.exit_code, (
        f"{where} exited {proc.returncode}, expected {scenario.exit_code}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert proc.stderr == "", f"{where} wrote to stderr:\n{proc.stderr}"
    if scenario.is_json:
        actual = normalise_payload(json.loads(proc.stdout), cwd)
        expected = normalise_payload(scenario.json_payload, cwd)
        assert actual == expected, (
            f"{where} JSON differs\nactual:   {json.dumps(actual, indent=2, sort_keys=True)}\n"
            f"expected: {json.dumps(expected, indent=2, sort_keys=True)}"
        )
    else:
        actual = normalise(proc.stdout, cwd).strip("\n")
        expected = normalise(scenario.stdout, cwd).strip("\n")
        assert actual == expected, (
            f"{where} text differs\n--- actual ---\n{actual}\n--- expected ---\n{expected}"
        )
    return proc.stdout
