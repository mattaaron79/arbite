"""The project config file and its `review:` flag.

One suite per module is this repo's convention (`test_schema.py`, `test_query.py`,
...), and no suite covered `config.py` yet; the unit under test here is
`config.review_enabled` itself. No command reads the flag (it gates a future
`arbite submit`), so its contract is the accessor's own -- except where a broken
value has to reach a real command, which is why the error lives in `load_config`.

The file-level effects are pinned too: turning review off must not shrink the
status vocabulary or the folder layout, or tickets already in review are stranded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arbite.config import (
    ARBITE_DIRNAME,
    CONFIG_FILENAME,
    REVIEW_KEY,
    load_config,
    open_sink,
    review_enabled,
)
from arbite.errors import TicketError
from arbite.schema import STATUSES
from arbite.sinks.file import FLAT_STATUS_DIRS


def write_config(project_root: Path, text: str) -> Path:
    """Write `<project_root>/.arbite/project.yaml` and return its path."""
    directory = project_root / ARBITE_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONFIG_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


# --- the default: absent reads as true --------------------------------------


def test_an_absent_key_reads_as_true(tmp_project):
    write_config(tmp_project, "sink: file\n")
    assert review_enabled(tmp_project) is True


def test_the_repos_own_config_carries_the_owner_chosen_review_false():
    """`.arbite/project.yaml` sets `review: false` as the repo owner's deliberate choice,
    so this repo's flag is the explicit boolean written, not the absent-key default."""
    repo_root = Path(__file__).resolve().parents[1]
    config = load_config(repo_root)
    assert config == {"sink": "file", REVIEW_KEY: False}
    assert config[REVIEW_KEY] is False  # read as written: a real boolean, not a falsey stand-in
    assert review_enabled(repo_root) is False


def test_a_project_with_no_config_file_reads_as_true(tmp_project):
    """No `.arbite/` at all is "no config", not an error: the default holds."""
    assert not (tmp_project / ARBITE_DIRNAME).exists()
    assert review_enabled(tmp_project) is True


# --- explicit booleans ------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "True", "false", "False"])
def test_an_explicit_boolean_is_read_as_written(tmp_project, value):
    write_config(tmp_project, f"review: {value}\n")
    assert review_enabled(tmp_project) is (value.lower() == "true")


@pytest.mark.parametrize(
    "word,expected",
    [("yes", True), ("no", False), ("on", True), ("off", False)],
)
def test_yaml_1_1_boolean_words_are_accepted(tmp_project, word, expected):
    """PyYAML follows YAML 1.1, where yes/no/on/off are booleans. Recorded here on
    purpose: it is a decision (they work) rather than an accident of the parser."""
    write_config(tmp_project, f"review: {word}\n")
    assert review_enabled(tmp_project) is expected


# --- anything that is not a real boolean is an error ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "review:\n",  # blank -> YAML null
        "review: null\n",  # spelled-out null
        "review: 'false'\n",  # quoted: a string, not a boolean
        'review: "no"\n',
        "review: 0\n",  # int
        "review: 1\n",
        "review: []\n",  # list
        "review: [true]\n",
        "review: {}\n",  # mapping
        "review: {enabled: true}\n",
    ],
)
def test_a_non_boolean_review_value_errors_naming_file_and_key(tmp_project, text):
    """A blank or quoted value is a mistake, not an intentional "off", so it is
    reported instead of collapsing to a falsey default."""
    path = write_config(tmp_project, "sink: file\n" + text)
    with pytest.raises(TicketError) as excinfo:
        review_enabled(tmp_project)

    message = str(excinfo.value)
    assert str(path) in message, message
    assert "review" in message, message


def test_the_error_reaches_commands_that_read_config(tmp_project):
    """The check lives in `load_config`, so a plain command -- which resolves its
    sink from config before doing anything else -- reports it too, rather than
    quietly using the default."""
    write_config(tmp_project, "sink: file\nreview: maybe\n")
    with pytest.raises(TicketError):
        load_config(tmp_project)


# --- the flag changes neither the vocabulary nor the layout -----------------


def test_review_false_keeps_the_status_and_the_review_folder(tmp_project):
    """Turning review off must not strand tickets already awaiting review: the
    `review` status stays in the vocabulary and the file layout keeps its folder."""
    write_config(tmp_project, "sink: file\nreview: false\n")
    assert review_enabled(tmp_project) is False

    assert "review" in STATUSES
    assert "review" in FLAT_STATUS_DIRS  # the folders `init` creates from STATUSES

    sink = open_sink(project_root=tmp_project, require_initialised=False)
    sink.init()
    assert (tmp_project / ARBITE_DIRNAME / "review").is_dir()
