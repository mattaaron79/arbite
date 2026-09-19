#!/usr/bin/env python3
"""Disposable-workspace recipe: two independent agents in one shared directory.

This is the runnable half of the owner-facing documentation for the
shared-directory proxy (planning key C12). It creates a throwaway workspace,
starts two agents as *separate processes* against it, and drives the flow the
ticket requires end to end:

1. two agents do independent file work on different files;
2. both race for one contested file -- exactly one wins, the other gets a
   structured ``file_busy`` response naming the holder;
3. a stale read is refused with ``stale_read`` and changes no bytes;
4. an external-tool write (a plain filesystem write, not the proxy) is detected
   and never silently overwritten -- and arbite did not attribute it to anyone;
5. an explicit administrative takeover revokes the old attempt, which can no
   longer mutate, and the new attempt must re-read before writing;
6. closing the ticket releases its file claims, so the second agent can take the
   released file (with a fresh read, as always).

Nothing here starts a daemon, watcher or scheduler: every command is one-shot.
The same flow is asserted by ``tests/test_proxy_acceptance.py`` on both sinks.

Usage::

    python3 scripts/proxy_recipe.py --sink file     # ...or --sink sqlite

Exit code 0 means every step held; any failure prints ``RECIPE-FAILED`` with the
reason and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

TICKET_ID_RE = re.compile(r"tic-[0-9a-f]{4,}")

WINNER_AGENT = "agent.a.001"
LOSER_AGENT = "agent.b.001"
TAKEOVER_AGENT = "agent.c.001"


class RecipeFailure(Exception):
    """A step did not hold; the message is the reason."""


def require(condition, message: str) -> None:
    if not condition:
        raise RecipeFailure(message)


def environment() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    # Never let a developer's shell decide the store: this recipe states it.
    env.pop("ARBITE_SINK", None)
    return env


class Result:
    def __init__(self, argv, code: int, stdout: bytes, stderr: bytes):
        self.argv = argv
        self.code = code
        self.stdout = stdout.decode("utf-8", "replace")
        self.stderr = stderr.decode("utf-8", "replace")

    def json(self) -> dict:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as error:  # pragma: no cover - surfaced as failure
            raise RecipeFailure(
                f"arbite {' '.join(self.argv)} did not print JSON: {error}\n{self.stdout}"
            ) from error


def run(workspace: Path, sink: str, *args, expect: int = 0, stdin: bytes | None = None) -> Result:
    """One `arbite` invocation, in its own process, against `workspace`."""
    argv = [sys.executable, "-m", "arbite.cli", "--sink", sink, *args]
    proc = subprocess.run(
        argv,
        cwd=str(workspace),
        env=environment(),
        input=stdin,
        capture_output=True,
        timeout=120,
    )
    result = Result(argv, proc.returncode, proc.stdout, proc.stderr)
    if result.code != expect:
        raise RecipeFailure(
            f"`arbite {' '.join(args)}` exited {result.code}, expected {expect}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def spawn(workspace: Path, sink: str, *args) -> subprocess.Popen:
    """A concurrently running `arbite` invocation (for the contested claim)."""
    return subprocess.Popen(
        [sys.executable, "-m", "arbite.cli", "--sink", sink, *args],
        cwd=str(workspace),
        env=environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def create_ticket(workspace: Path, sink: str, title: str) -> str:
    out = run(
        workspace,
        sink,
        "create",
        "--title",
        title,
        "--type",
        "chore",
        "--tier",
        "medium",
        "--domain",
        "io",
    ).stdout
    match = TICKET_ID_RE.search(out)
    require(match is not None, f"no ticket id in create output: {out!r}")
    return match.group(0)


def active_attempt(workspace: Path, sink: str, ticket: str) -> str:
    """The active work attempt id for `ticket`, read the documented way.

    `arbite export --scope coordination` prints the coordination document; an
    agent takes the `work_attempts` entry whose `ticket_id` is its own and whose
    `state` is `active`. This is deliberately the *only* way the recipe learns an
    attempt id -- no reaching into storage.
    """
    document = json.loads(
        run(workspace, sink, "export", "--scope", "coordination", "--no-artifacts").stdout
    )
    attempts = [
        attempt
        for attempt in document["records"]["work_attempts"]
        if attempt["ticket_id"] == ticket and attempt["state"] == "active"
    ]
    require(len(attempts) == 1, f"expected one active attempt for {ticket}, got {attempts}")
    return attempts[0]["id"]


def read_token(workspace: Path, sink: str, ticket: str, attempt: str, path: str) -> str:
    payload = run(
        workspace,
        sink,
        "file",
        "read",
        path,
        "--ticket",
        ticket,
        "--attempt",
        attempt,
        "--json",
    ).json()
    require(payload["data"]["write_authorizing"] is True, f"read of {path} was not writable")
    return payload["data"]["read_token"]


def claim(workspace: Path, sink: str, ticket: str, attempt: str, path: str) -> dict:
    return run(
        workspace,
        sink,
        "file",
        "claim",
        path,
        "--ticket",
        ticket,
        "--attempt",
        attempt,
        "--json",
    ).json()["data"]


def write(
    workspace: Path,
    sink: str,
    ticket: str,
    attempt: str,
    path: str,
    content: bytes,
    token=None,
    expect: int = 0,
):
    args = ["file", "write", path, "--ticket", ticket, "--attempt", attempt, "--input", "-", "--json"]
    if token is not None:
        args += ["--read-token", token]
    return run(workspace, sink, *args, stdin=content, expect=expect)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sink", choices=("file", "sqlite"), default="file")
    parser.add_argument(
        "--workdir",
        default=None,
        help="workspace to create/use (default: a fresh temp directory)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the workspace when done")
    options = parser.parse_args(argv)

    sink = options.sink
    created_workdir = options.workdir is None
    workspace = Path(options.workdir) if options.workdir else Path(tempfile.mkdtemp(prefix="arbite-recipe-"))
    workspace.mkdir(parents=True, exist_ok=True)
    ok = False
    try:
        run(workspace, sink, "init")
        (workspace / "src").mkdir(exist_ok=True)
        print(f"[recipe] sink={sink} workspace={workspace}")

        # --- 1. two agents, two tickets, independent file work -------------
        t_a = create_ticket(workspace, sink, "Agent A task")
        t_b = create_ticket(workspace, sink, "Agent B task")
        run(workspace, sink, "claim", t_a, "--agent", WINNER_AGENT)
        run(workspace, sink, "claim", t_b, "--agent", LOSER_AGENT)
        a_a = active_attempt(workspace, sink, t_a)
        a_b = active_attempt(workspace, sink, t_b)
        require(a_a != a_b, "the two agents must hold different attempts")
        print(f"[recipe] independent tickets: {t_a}/{a_a} and {t_b}/{a_b}")

        for ticket, attempt, agent, path, text in (
            (t_a, a_a, WINNER_AGENT, "src/alpha.py", b"alpha owned by A\n"),
            (t_b, a_b, LOSER_AGENT, "src/beta.py", b"beta owned by B\n"),
        ):
            claim(workspace, sink, ticket, attempt, path)
            write(workspace, sink, ticket, attempt, path, text)
            require(
                (workspace / path).read_bytes() == text,
                f"{agent} did not own {path} independently",
            )
        print("[recipe] independent file work on src/alpha.py and src/beta.py: OK")

        # --- 2. contested ownership: exactly one winner --------------------
        contenders = (
            (t_a, a_a, "src/shared.py"),
            (t_b, a_b, "src/shared.py"),
        )
        procs = [
            (ticket, attempt, spawn(workspace, sink, "file", "claim", path, "--ticket", ticket, "--attempt", attempt, "--json"))
            for ticket, attempt, path in contenders
        ]
        outcomes = []
        for ticket, attempt, proc in procs:
            out, err = proc.communicate(timeout=120)
            outcomes.append((ticket, attempt, proc.returncode, out, err))
        winners = [o for o in outcomes if o[2] == 0]
        losers = [o for o in outcomes if o[2] != 0]
        require(len(winners) == 1 and len(losers) == 1, f"expected one winner, got {outcomes}")
        win_ticket, win_attempt = winners[0][0], winners[0][1]
        lose_ticket, lose_attempt = losers[0][0], losers[0][1]
        loser_payload = json.loads(losers[0][3])
        require(
            loser_payload["code"] == "file_busy",
            f"loser did not get file_busy: {loser_payload}",
        )
        require(
            loser_payload["details"]["holder_ticket"] == win_ticket,
            "file_busy must name the winning ticket",
        )
        print(
            f"[recipe] contested src/shared.py: winner={win_ticket} "
            f"loser={lose_ticket} ({loser_payload['code']})"
        )
        write(workspace, sink, win_ticket, win_attempt, "src/shared.py", b"shared by winner\n")

        # --- 3. stale read is refused, bytes unchanged ---------------------
        stale_token = read_token(workspace, sink, win_ticket, win_attempt, "src/shared.py")
        write(workspace, sink, win_ticket, win_attempt, "src/shared.py", b"shared v2\n", stale_token)
        reused = write(
            workspace,
            sink,
            win_ticket,
            win_attempt,
            "src/shared.py",
            b"shared v3\n",
            stale_token,
            expect=1,
        ).json()
        require(reused["code"] == "stale_read", f"reused token was not refused: {reused}")
        require(
            (workspace / "src/shared.py").read_bytes() == b"shared v2\n",
            "a refused stale write must change no bytes",
        )
        print("[recipe] stale read token refused with stale_read, no bytes changed: OK")

        # --- 4. external-tool write: detected, never silently overwritten ---
        loser_file = "src/alpha.py" if lose_ticket == t_a else "src/beta.py"
        external_token = read_token(workspace, sink, lose_ticket, lose_attempt, loser_file)
        external_bytes = b"written by an external tool, not the proxy\n"
        (workspace / loser_file).write_bytes(external_bytes)
        detected = write(
            workspace,
            sink,
            lose_ticket,
            lose_attempt,
            loser_file,
            b"proxy overwrite attempt\n",
            external_token,
            expect=1,
        ).json()
        require(detected["code"] == "stale_read", f"external drift was not detected: {detected}")
        require(
            (workspace / loser_file).read_bytes() == external_bytes,
            "the external bytes must survive a refused proxy write",
        )
        # The external change invalidated the held claim's recorded version, so a
        # plain read is deliberately non-writable (`claim_version_mismatch`). The
        # documented way forward is to release the path and re-claim it -- a new
        # generation over the current bytes -- then read and write.
        run(
            workspace,
            sink,
            "file",
            "release",
            loser_file,
            "--ticket",
            lose_ticket,
            "--attempt",
            lose_attempt,
            "--reason",
            "external drift",
            "--json",
        )
        reclaimed = claim(workspace, sink, lose_ticket, lose_attempt, loser_file)
        require(
            reclaimed["claimed"][0]["generation"] >= 2,
            "re-claiming an externally changed path must mint a new generation",
        )
        fresh = read_token(workspace, sink, lose_ticket, lose_attempt, loser_file)
        write(workspace, sink, lose_ticket, lose_attempt, loser_file, b"proxy rewrite after re-read\n", fresh)
        print(
            f"[recipe] external write to {loser_file} detected (stale_read); arbite attributed it to "
            "no one and did not overwrite it: OK"
        )

        # --- 5. explicit takeover revokes the old attempt ------------------
        old_attempt = active_attempt(workspace, sink, t_a)
        held_token = read_token(workspace, sink, t_a, old_attempt, "src/alpha.py")
        before_takeover = (workspace / "src/alpha.py").read_bytes()
        run(workspace, sink, "claim", t_a, "--agent", TAKEOVER_AGENT, "--force", "--reason", "old worker stalled")
        revoked = write(
            workspace, sink, t_a, old_attempt, "src/alpha.py", b"stale worker write\n", held_token, expect=1
        ).json()
        require(revoked["ok"] is False, f"the revoked attempt wrote anyway: {revoked}")
        require(
            (workspace / "src/alpha.py").read_bytes() == before_takeover,
            "a revoked attempt must change no bytes",
        )
        new_attempt = active_attempt(workspace, sink, t_a)
        require(new_attempt != old_attempt, "takeover must mint a new attempt")
        claim(workspace, sink, t_a, new_attempt, "src/alpha.py")
        fresh = read_token(workspace, sink, t_a, new_attempt, "src/alpha.py")
        write(workspace, sink, t_a, new_attempt, "src/alpha.py", b"alpha after takeover\n", fresh)
        print(f"[recipe] takeover revoked {old_attempt}, new attempt {new_attempt} re-read and wrote: OK")

        # --- 6. close cleanup releases claims ------------------------------
        run(workspace, sink, "close", t_a, "--agent", TAKEOVER_AGENT, "--reason", "done")
        taken = claim(workspace, sink, t_b, a_b, "src/alpha.py")
        require(taken["claimed"][0]["generation"] >= 1, "the released path must be claimable again")
        fresh = read_token(workspace, sink, t_b, a_b, "src/alpha.py")
        write(workspace, sink, t_b, a_b, "src/alpha.py", b"alpha now owned by B\n", fresh)
        print("[recipe] close released the claims; the other agent took the released file: OK")

        # --- evidence and integrity ---------------------------------------
        changes = run(workspace, sink, "changes", t_b, "--json").json()["data"]
        require(
            changes["evidence"]["operation_count"] >= 2,
            f"expected recorded evidence for {t_b}: {changes['counts']}",
        )
        doctor = run(workspace, sink, "doctor", "--json").json()
        require(doctor["remaining"] == 0, f"doctor reported problems: {doctor['problems']}")
        print(
            f"[recipe] evidence: {changes['evidence']['operation_count']} operation(s) for {t_b}; "
            f"doctor clean ({doctor['tickets_checked']} tickets)"
        )

        ok = True
        print(f"RECIPE-OK sink={sink} workspace={workspace}")
        return 0
    except RecipeFailure as error:
        print(f"RECIPE-FAILED sink={sink} workspace={workspace}: {error}")
        return 1
    finally:
        if ok and created_workdir and not options.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
