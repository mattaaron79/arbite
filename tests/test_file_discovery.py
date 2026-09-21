"""Discovery's guarantees, asserted where a transcript cannot show them.

The LS blocks pin output: ordering, counts, the tokens a continuation prints. What they
cannot show is the property the slice exists for -- that finding a path is never the same
as being allowed to change it -- plus the exclusion rules, the two sinks agreeing, and
what a malformed continuation does. Those are asserted here, against real processes and
against the store.
"""

from __future__ import annotations

import json
import os

import pytest

import discovery_state as state
import examples
from arbite.coordination import discovery as coordination_discovery
from arbite.sinks import file as file_sink

SINKS = ("file", "sqlite")
BASE_PY = state.BASE_PY
FILE_PY = state.FILE_PY
SCHEMA_PY = state.SCHEMA_PY


def rows_of(text: str) -> list:
    """A listing's or search's rows: every line but its closing report lines.

    A complete listing ends with `N files (no truncation)`; a truncated one ends with
    the `truncated:` block, whose second line is indented under it. None of those is a
    row, so none of them is returned."""
    return [
        line
        for line in text.splitlines()
        if not line.startswith("truncated:")
        and " (no truncation)" not in line
        and not line.startswith(" " * 11 + "or ")
    ]


def paths_of(text: str) -> list:
    return [row.split()[0] for row in rows_of(text)]


# --- the exclusions ---------------------------------------------------------


def test_the_generated_guide_list_is_the_file_sinks_own(tmp_path):
    """Two modules name the generated guide; this is what stops them drifting."""
    assert coordination_discovery.GENERATED_FILES == file_sink.GENERATED_FILES


def test_the_coordination_tree_and_the_store_are_invisible(tmp_path):
    """arbite's runtime state is not discovery's surface, however it is reached."""
    project = state.listing_project(tmp_path)

    walked = examples.run_cli(project, "file", "list", ".")
    named = examples.run_cli(project, "file", "list", ".arbite/coordination")
    searched = examples.run_cli(project, "file", "search", "claim", ".arbite/coordination/claims")

    assert walked.returncode == 0, walked.stderr
    assert "coordination" not in walked.stdout
    for refused in (named, searched):
        assert refused.returncode == 1, refused.stdout
        assert "arbite does not manage its own runtime state" in refused.stderr


def test_scratch_is_one_line_of_transport_and_never_a_file(tmp_path):
    """The payload area is reported, not walked, and what is in it cannot be claimed."""
    project = state.arbite_dir_project(tmp_path)

    listed = examples.run_cli(project, "file", "list", ".arbite")
    payload = examples.run_cli(project, "file", "list", ".arbite/scratch")
    claimed = examples.run_cli(
        project,
        "file",
        "claim",
        ".arbite/scratch/payload.py",
        "--ticket",
        state.HOLDER_TICKET,
        "--attempt",
        state.HOLDER,
    )

    assert ".arbite/scratch/" in listed.stdout
    assert "(transport, 1 file -- not listed as a file, never claimable)" in listed.stdout
    assert "payload.py" not in listed.stdout, "the payload itself is not a row"
    assert "payload.py" not in payload.stdout
    assert claimed.returncode == 1
    assert "runtime state" in claimed.stderr


def test_only_manageable_files_are_offered_as_rows(tmp_path):
    """A link is not a row, and a hard-linked target is named without being offered.

    Both are paths `probe` refuses to manage, so a listing that offered them would be
    inviting a claim the very next command rejects."""
    project = state.listing_project(tmp_path)
    (project / "src/link").symlink_to(project / "src/arbite", target_is_directory=True)
    os.mkfifo(project / "pipe.py")
    os.link(project / state.BASE_PY, project / "hard.py")

    listed = examples.run_cli(project, "file", "list", ".")

    assert listed.returncode == 0, listed.stderr
    assert "link/" not in listed.stdout and "link" not in paths_of(listed.stdout)
    assert "pipe.py" not in listed.stdout
    assert "hard.py" in paths_of(listed.stdout), "it is there, and it is not offered"
    assert not any(
        row.startswith("hard.py") and "unclaimed" in row for row in rows_of(listed.stdout)
    )


# --- nothing is authorised --------------------------------------------------


def test_a_listing_and_a_search_record_nothing_at_all(tmp_path):
    """The acceptance criterion's first half: discovery is not a read.

    It mints no observation (so there is no token to present), writes no receipt, takes
    no claim and appends no event -- the only claim and event in the store are the one a
    real acquisition made before the listing."""
    project = state.listing_project(tmp_path)
    store = state.store_for(project)
    events = len(store.events())

    listed = examples.run_cli(project, "file", "list", "src/arbite")
    searched = examples.run_cli(project, "file", "search", "# ", "src/arbite")

    assert listed.returncode == 0 and searched.returncode == 0, (listed.stderr, searched.stderr)
    store = state.store_for(project)
    assert store.records("observation") == [], "no read token exists"
    assert store.records("receipt") == [], "no operation happened"
    assert [claim.path for claim in store.active_claims()] == [BASE_PY]
    assert len(store.events()) == events, "and nothing was appended to the stream"


def test_a_read_taken_before_a_claim_authorises_nothing(tmp_path):
    """The acceptance criterion's second half, one step further.

    A listing cannot be turned into a write; neither can the read a caller takes next,
    because a pre-claim observation records no claim generation. The claim the caller
    takes afterwards is what a write is checked against, and it does not revive the
    token."""
    project = state.project_fixture(tmp_path)
    state.sized_file(project, SCHEMA_PY, 40, 1200)

    read = examples.run_cli(
        project,
        "file",
        "read",
        SCHEMA_PY,
        "--ticket",
        state.HOLDER_TICKET,
        "--attempt",
        state.HOLDER,
    )
    claimed = examples.run_cli(
        project,
        "file",
        "claim",
        SCHEMA_PY,
        "--ticket",
        state.HOLDER_TICKET,
        "--attempt",
        state.HOLDER,
    )

    assert read.returncode == 0 and claimed.returncode == 0, (read.stderr, claimed.stderr)
    observation = state.store_for(project).records("observation")[0]
    claim = state.store_for(project).claims_for_path(SCHEMA_PY)[0]
    assert observation.claim_generation == 0
    assert observation.authorizes_write(claim) is False


# --- ordering, counts and continuations -------------------------------------


@pytest.mark.parametrize("kind", SINKS)
def test_paging_visits_every_row_exactly_once(tmp_path, kind):
    """The continuation contract, on both sinks and at a page size the default is not."""
    project = state.project_fixture(tmp_path, kind)
    for index in range(20):
        state.sized_file(project, f"src/arbite/paged_{index:02d}.py", 5, 200)

    visited, pages, argv = [], 0, ("file", "list", "src/arbite", "--count", "7")
    while True:
        proc = examples.run_cli(project, *argv, sink=kind)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        visited.extend(line.split()[0] for line in lines[:-1])
        pages += 1
        if not lines[-1].startswith("truncated:"):
            break
        assert lines[-1].split("--after ")[1].split(" --count")[0] == lines[-2].split()[0]
        argv = ("file", "list", "src/arbite", "--after", lines[-2].split()[0], "--count", "7")

    assert pages == 3, "7 + 7 + 6"
    assert len(visited) == 20
    assert visited == sorted(visited) and len(set(visited)) == 20


def test_a_truncated_listing_states_the_real_remainder(tmp_path):
    """The count in the hint is the number of rows the caller has not seen."""
    project = state.truncated_project(tmp_path)

    truncated = examples.run_cli(project, "file", "list", "src/arbite", "--count", "30")
    everything = examples.run_cli(project, "file", "list", "src/arbite", "--count", "1000")

    remainder = int(truncated.stdout.split("truncated: ")[1].split(" more")[0])
    assert remainder == len(rows_of(everything.stdout)) - 30
    assert remainder == state.LS2_TOTAL - 30
    assert rows_of(truncated.stdout)[-1].split()[0] in truncated.stdout.split("--after ")[1]


def test_search_pages_by_path_and_line(tmp_path):
    """`--after PATH:LINE` resumes after exactly one match, not after a whole file."""
    project = state.search_truncated_project(tmp_path)

    first = examples.run_cli(project, "file", "search", "def ", "src/arbite", "--count", "50")
    everything = examples.run_cli(
        project, "file", "search", "def ", "src/arbite", "--count", "1", "--json"
    )
    page = rows_of(first.stdout)
    token = _token_of(page[-1])

    second = examples.run_cli(
        project, "file", "search", "def ", "src/arbite", "--after", token, "--count", "50"
    )

    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    assert len(page) == 50
    assert json.loads(everything.stdout)["total"] == 999
    everything = examples.run_cli(
        project, "file", "search", "def ", "src/arbite", "--count", "1000"
    )
    assert token in first.stdout, "the token is one this command printed"
    assert second.stdout.splitlines()[0] != page[-1], "the token's own match is not re-served"
    assert rows_of(second.stdout) == rows_of(everything.stdout)[50:100], "the next fifty matches"


def _token_of(row: str) -> str:
    """The `PATH:LINE` token a search row carries, for a `--after` continuation."""
    import re

    match = re.match(r"^(?P<path>\S+?):(?P<line>\d+)\s", row)
    assert match is not None, row
    return f"{match.group('path')}:{match.group('line')}"


def test_search_reports_files_and_matches_in_canonical_order(tmp_path):
    """Literal text, path order then line order: the order the continuation relies on.

    `arbite file search "O_CREAT"` is a literal search, so the pattern in a line of
    ordinary prose matches too -- and the row's shape is the frozen one, gutter and all.
    """
    project = state.search_project(tmp_path)
    state.written(project, "src/arbite/early.py", "# nothing here\nO_CREAT is mentioned\n")

    proc = examples.run_cli(project, "file", "search", "O_CREAT", "src/arbite")

    assert proc.returncode == 0, proc.stderr
    assert rows_of(proc.stdout) == [
        "src/arbite/early.py:2     O_CREAT is mentioned",
        "src/arbite/sinks/file.py:141   fd = os.open(path, os.O_CREAT | os.O_EXCL | "
        "os.O_WRONLY, 0o644)",
    ]
    assert proc.stdout.splitlines()[-1] == "2 matches in 2 files (no truncation)"


def test_search_and_list_disagree_about_nothing(tmp_path):
    """A search can only find what a listing shows: both walk the same set, so a binary
    file is listed with its shape and contributes no matches."""
    project = state.project_fixture(tmp_path)
    blob = project / "src/arbite/blob.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"\x00\x01\xdef \x02")

    listed = examples.run_cli(project, "file", "list", "src/arbite")
    searched = examples.run_cli(project, "file", "search", "def ", "src/arbite")

    assert "src/arbite/blob.bin" in listed.stdout
    assert "bytes" in [row for row in rows_of(listed.stdout) if row.startswith("src/arbite/blob")][0]
    assert "blob.bin" not in searched.stdout
    assert searched.returncode == 2, "a binary file cannot match literal text"


# --- the surface's own refusals ---------------------------------------------


def test_a_bad_continuation_or_count_is_refused(tmp_path):
    """Both flags take tokens this command printed, and a bad one is exit 1."""
    project = state.listing_project(tmp_path)

    malformed = examples.run_cli(project, "file", "search", "x", "src", "--after", "src/arbite")
    empty = examples.run_cli(project, "file", "list", "src/arbite", "--count", "0")
    escaping = examples.run_cli(project, "file", "list", "..", "--after", "")

    assert malformed.returncode == 1 and "PATH:LINE" in malformed.stderr
    assert empty.returncode == 1 and "--count" in empty.stderr
    assert escaping.returncode == 1, escaping.stderr


def test_a_listing_of_nothing_is_an_answer(tmp_path):
    """Exit 2 with no rows, and the scratch search's frozen wording is what an empty
    search says when the area it was asked about is the transport one."""
    project = state.arbite_dir_project(tmp_path)
    (project / "src/arbite").mkdir(parents=True)

    empty = examples.run_cli(project, "file", "list", "src/arbite")
    missing = examples.run_cli(project, "file", "list", "src/nowhere")
    scratch = examples.run_cli(project, "file", "search", "anything", ".arbite/scratch")

    assert empty.returncode == 2 and empty.stdout.startswith("no files under 'src/arbite'")
    assert missing.returncode == 1 and "no such path" in missing.stderr
    assert scratch.returncode == 2 and scratch.stdout.startswith("no matches (scratch is excluded")


# --- both sinks, and JSON ---------------------------------------------------


@pytest.mark.parametrize("kind", SINKS)
def test_both_sinks_list_and_search_the_same_rows(tmp_path, kind):
    """The claim index decides a row's state on either sink, so the rows agree."""
    project = state.listing_project(tmp_path, kind)

    listed = examples.run_cli(project, "file", "list", "src/arbite/sinks", sink=kind)
    searched = examples.run_cli(project, "file", "search", "# 141", "src/arbite/sinks", sink=kind)

    assert listed.returncode == 0 and searched.returncode == 0, (listed.stderr, searched.stderr)
    assert len(paths_of(listed.stdout)) == 4
    assert any("CLAIMED" in row for row in rows_of(listed.stdout))
    assert searched.stdout.splitlines()[-1] == "3 matches in 3 files (no truncation)"


@pytest.mark.parametrize("kind", SINKS)
def test_the_json_payload_carries_the_same_facts_as_the_text(tmp_path, kind):
    """Text is primary and JSON is the same facts: a caller must not have to parse the
    rows to learn a count, a path's state or where the next page starts."""
    project = state.listing_project(tmp_path, kind)

    listed = examples.run_cli(project, "file", "list", "src/arbite/sinks", "--json", sink=kind)
    searched = examples.run_cli(
        project, "file", "search", "# 141", "src/arbite/sinks", "--json", sink=kind
    )

    listing = json.loads(listed.stdout)
    search = json.loads(searched.stdout)
    assert listing["shown"] == listing["total"] == 4 and listing["truncated"] is False
    assert [entry["path"] for entry in listing["entries"]] == [
        FILE_PY.replace("file", "__init__"),
        BASE_PY,
        FILE_PY,
        FILE_PY.replace("file.py", "sqlite.py"),
    ]
    held = [entry for entry in listing["entries"] if entry["state"] == "claimed"]
    assert len(held) == 1 and held[0]["path"] == BASE_PY
    assert held[0]["claim"]["attempt"] == state.HOLDER
    assert held[0]["digest"].startswith("sha256:") and held[0]["lines"] == 570
    assert search["total"] == len(search["matches"]) == 3
    assert search["files"] == 3 and search["truncated"] is False
    assert all("141" in match["text"] for match in search["matches"])


def test_a_truncated_json_listing_publishes_the_continuation(tmp_path):
    """The command in `next_actions` is the one the text's truncation line names."""
    project = state.truncated_project(tmp_path)

    proc = examples.run_cli(project, "file", "list", "src/arbite", "--count", "10", "--json")

    payload = json.loads(proc.stdout)
    assert payload["truncated"] is True
    assert payload["shown"] == 10 and payload["total"] == state.LS2_TOTAL
    assert payload["next_actions"] == [
        "arbite file list src/arbite --after src/arbite/a008.py --count 10"
    ]
    assert not any(entry["path"] == "src/arbite/coordination" for entry in payload["entries"])
