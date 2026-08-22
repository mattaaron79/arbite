"""Generate realistic seed ticket data for the local tickets/ directory.

Simulates ~2.5 months (2026-06 through 2026-08-18) of a real arbite workflow
for a Blender addon that generates assets across the mesh / image_gen /
audio_gen / ui / io domains. Tickets are created, claimed, noted on, blocked,
unblocked, and closed in *chronological* order, using the same code paths
(`Ticket`, `save_ticket`, `move_ticket`, `status_dir`) that the `arbite` CLI
uses -- so the files land in exactly the folders/format the CLI would produce
and round-trip cleanly through `arbite list` / `arbite deps` etc.

Idempotent: existing *.md tickets under tickets/{open,in_progress,blocked,
closed} are removed first, then regenerated. The agents/ scratchpads are also
rewritten to reflect current state (one active ticket per agent).

Re-run anytime with:
    python scripts/seed_demo.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from arbite.ticket import (  # noqa: E402
    BLANK_DESCRIPTION,
    BLANK_DOMAIN,
    BLANK_TIER,
    BLANK_TITLE,
    BLANK_TYPE,
    BLANK_WARNING,
    DEFAULT_BODY,
    Ticket,
    load_all_tickets,
    move_ticket,
    save_ticket,
    status_dir,
)

TICKETS_ROOT = ROOT / "tickets"


@dataclass
class Event:
    date: str
    action: str  # note | claim | block | unblock | close
    payload: str = ""


@dataclass
class Spec:
    id: str
    title: str
    type: str
    tier: str
    domain: str
    description: str
    priority: int | None = None
    tags: list = field(default_factory=list)
    depends_on: list = field(default_factory=list)
    created: str = ""
    events: list = field(default_factory=list)
    blank: bool = False
    epic: str = ""


SPECS = [
    # ---------------- June 2026 (all closed -> closed/2026-06/) ----------------
    Spec(
        id="tic-a1b2",
        title="Set up image_gen service skeleton",
        type="chore", tier="medium", domain="image_gen",
        epic="image-gen-backend",
        tags=["infra", "scaffolding"],
        description=(
            "Create the package skeleton for the image generation service: module layout, "
            "config loading, structured logging, and a health-check endpoint. This is the "
            "foundation the rest of the image_gen domain builds on, so keep it thin and stable."
        ),
        created="2026-06-02",
        events=[
            Event("2026-06-02", "note", "Scaffolded package layout, entry points, and stub endpoints."),
            Event("2026-06-03", "claim", "claude.haiku.001"),
            Event("2026-06-05", "note", "Wired config loader, structured logging, and /health check. Verified startup end to end."),
            Event("2026-06-05", "close", ""),
        ],
    ),
    Spec(
        id="tic-b2c3",
        title="Add mesh normals computation pipeline",
        type="feature", tier="high", domain="mesh",
        epic="mesh-pipeline",
        tags=["normals", "curves"],
        description=(
            "Implement the core normals pipeline: smooth/flat normals, normal map baking hooks, "
            "and a UI toggle. Must handle non-manifold edges and seams without flipping faces. "
            "Depends on the image_gen skeleton only for the shared config/io glue."
        ),
        depends_on=["tic-a1b2"],
        created="2026-06-03",
        events=[
            Event("2026-06-03", "claim", "claude.sonnet.002"),
            Event("2026-06-06", "note", "Implemented smooth/flat normals with seam-aware adjacency; added UI toggle stub."),
            Event("2026-06-10", "note", "Fixed normal flips on non-manifold edges; added test scene with curved surfaces."),
            Event("2026-06-12", "close", ""),
        ],
    ),
    Spec(
        id="tic-c3d4",
        title="Fix UV seam artifacts on curved meshes",
        type="bug", tier="high", domain="mesh",
        epic="mesh-pipeline",
        tags=["uv", "normals"],
        description=(
            "Curved meshes show a visible seam/band where UV islands are split. Likely the normals "
            "are being averaged across the seam instead of being split per-island. Reported against "
            "the new normals pipeline."
        ),
        depends_on=["tic-b2c3"],
        created="2026-06-06",
        events=[
            Event("2026-06-07", "claim", "claude.sonnet.002"),
            Event("2026-06-08", "note", "Root cause: normals were averaged across UV seam during flatten. Rewrote pass to use seam-aware adjacency."),
            Event("2026-06-09", "close", ""),
        ],
    ),
    Spec(
        id="tic-d4e5",
        title="Route image_gen through shared io layer",
        type="refactor", tier="medium", domain="io",
        epic="io-layer",
        tags=["io", "image_gen"],
        description=(
            "image_gen currently does its own file read/write. Move that into the shared io layer "
            "so retries, path handling, and backpressure are consistent across domains."
        ),
        depends_on=["tic-a1b2"],
        created="2026-06-10",
        events=[
            Event("2026-06-11", "claim", "claude.opus.001"),
            Event("2026-06-15", "note", "Moved file read/write into io; added backpressure on large downloads."),
            Event("2026-06-18", "close", ""),
        ],
    ),

    # ---------------- July 2026 (all closed -> closed/2026-07/) ----------------
    Spec(
        id="tic-e5f6",
        title="Implement audio_gen wave shape preview",
        type="feature", tier="medium", domain="audio_gen",
        epic="audio-gen",
        tags=["waveform", "preview"],
        description=(
            "Add a wave shape preview panel to the audio generator so users see the generated "
            "waveform redraw live as they change parameters."
        ),
        created="2026-07-01",
        events=[
            Event("2026-07-01", "claim", "claude.haiku.001"),
            Event("2026-07-06", "note", "Added wave shape preview panel; live redraw on parameter change works."),
            Event("2026-07-08", "close", ""),
        ],
    ),
    Spec(
        id="tic-f607",
        title="Optimize mesh decimation for large models",
        type="refactor", tier="high", domain="mesh",
        epic="mesh-pipeline",
        tags=["decimation", "performance"],
        description=(
            "Decimation is too slow and memory-hungry on high-poly models. Replace the current "
            "vertex-removal loop with a priority-queue edge-collapse and cap memory on intermediate "
            "buffers."
        ),
        created="2026-07-02",
        events=[
            Event("2026-07-03", "claim", "claude.sonnet.002"),
            Event("2026-07-05", "block", "waiting on tic-b2c3 normals pipeline for edge-collapse cost weighting"),
            Event("2026-07-06", "unblock", ""),
            Event("2026-07-07", "note", "Switched to priority-queue edge collapse; ~40% memory reduction on a 1M-tri test scene."),
            Event("2026-07-11", "close", ""),
        ],
    ),
    Spec(
        id="tic-0718",
        title="Fix crash loading empty OBJ files",
        type="bug", tier="high", domain="io",
        epic="io-layer",
        tags=["crash", "obj"],
        description=(
            "Importing an OBJ with no geometry (header-only file) crashes the addon instead of "
            "showing an empty scene. Guard the empty-geometry path."
        ),
        created="2026-07-05",
        events=[
            Event("2026-07-05", "claim", "claude.opus.001"),
            Event("2026-07-06", "note", "Guarded empty-geometry path; added a regression test for header-only OBJ."),
            Event("2026-07-06", "close", ""),
        ],
    ),
    Spec(
        id="tic-1829",
        title="Document image_gen prompt schema",
        type="chore", tier="low", domain="image_gen",
        epic="image-gen-backend",
        tags=["docs", "schema"],
        description=(
            "Write a reference for the image_gen prompt schema (fields, defaults, examples) and "
            "link it from the addon README so agents and users stop guessing at parameter names."
        ),
        created="2026-07-09",
        events=[
            Event("2026-07-09", "claim", "gpt.4o.001"),
            Event("2026-07-10", "note", "Wrote prompt reference with worked examples; linked from README."),
            Event("2026-07-10", "close", ""),
        ],
    ),
    Spec(
        id="tic-293a",
        title="Normalize UI color palette tokens",
        type="feature", tier="low", domain="ui",
        epic="ui-polish",
        tags=["design", "tokens"],
        description=(
            "Collect the hardcoded hex colors scattered across panels into a single tokens file and "
            "swap the panels over, so a future dark-mode pass has one source of truth."
        ),
        created="2026-07-12",
        events=[
            Event("2026-07-12", "claim", "gpt.4o.001"),
            Event("2026-07-14", "note", "Defined tokens file; swapped hardcoded hex values in all panels."),
            Event("2026-07-14", "close", ""),
        ],
    ),
    Spec(
        id="tic-3a4b",
        title="Backfill unit tests for mesh pipeline",
        type="chore", tier="medium", domain="mesh",
        epic="mesh-pipeline",
        tags=["tests", "normals"],
        description=(
            "The mesh pipeline shipped without tests. Backfill coverage for normal computation, "
            "decimation, and seam handling so future refactors are safe."
        ),
        depends_on=["tic-b2c3"],
        created="2026-07-15",
        events=[
            Event("2026-07-15", "claim", "claude.sonnet.002"),
            Event("2026-07-18", "note", "Added 42 tests covering normal calc, decimation, and seam handling."),
            Event("2026-07-19", "close", ""),
        ],
    ),
    Spec(
        id="tic-c8d9",
        title="Support legacy .3ds import format",
        type="chore", tier="low", domain="io",
        epic="io-layer",
        tags=["import", "legacy"],
        description=(
            "Investigate adding a .3ds importer for old projects. Evaluate scope before committing "
            "to the work."
        ),
        created="2026-07-20",
        events=[
            Event("2026-07-21", "claim", "claude.opus.001"),
            Event("2026-07-22", "note", "Investigated: format is poorly documented and superseded by the glTF importer. Closing as won't fix."),
            Event("2026-07-22", "close", ""),
        ],
    ),

    # ---------------- August 2026 (mixed statuses, current as of 2026-08-18) ----------------
    Spec(
        id="tic-8f90",
        title="Migrate image_gen backend to new diffusion service",
        type="feature", tier="high", domain="image_gen",
        epic="image-gen-backend",
        tags=["backend", "migration"],
        description=(
            "The image_gen service is moving to a new diffusion backend. Port the request/response "
            "adapter, keep the public prompt schema stable, and match current latency targets."
        ),
        created="2026-08-07",
        events=[
            Event("2026-08-07", "claim", "claude.sonnet.002"),
            Event("2026-08-10", "note", "Ported request/response adapter; latency regression of ~12% -- investigating."),
            Event("2026-08-13", "note", "Latency fixed via connection pooling. Full smoke test still pending."),
        ],
    ),
    Spec(
        id="tic-b1c2",
        title="Fix audio latency in preview playback",
        type="bug", tier="high", domain="audio_gen",
        epic="audio-gen",
        tags=["latency", "playback"],
        description=(
            "Preview playback has an audible ~150ms latency spike on first play. Buffers are being "
            "flushed every frame; should be streaming instead."
        ),
        created="2026-08-10",
        events=[
            Event("2026-08-11", "claim", "claude.haiku.001"),
            Event("2026-08-14", "note", "Root cause: buffer flush on every frame. Switched to a ring buffer; verifying under load."),
        ],
    ),
    Spec(
        id="tic-5c6d",
        title="Expose audio sample buffer via io layer",
        type="feature", tier="medium", domain="io",
        epic="io-layer",
        tags=["io", "audio"],
        description=(
            "Add a streaming sample-buffer API to the shared io layer so audio_gen consumers can "
            "read PCM data without copying through the filesystem."
        ),
        created="2026-08-04",
        events=[
            Event("2026-08-06", "claim", "claude.opus.001"),
            Event("2026-08-12", "note", "Added sample buffer API with draft docs. Awaiting the waveform scrubber (tic-4b5c) to validate."),
        ],
    ),
    Spec(
        id="tic-4b5c",
        title="Add real-time waveform scrubbing to audio_gen UI",
        type="feature", tier="high", domain="audio_gen",
        epic="audio-gen",
        tags=["waveform", "ui"],
        description=(
            "Let users scrub the waveform preview in real time. Requires the wave shape preview "
            "(done) plus live PCM from the io sample buffer (tic-5c6d)."
        ),
        depends_on=["tic-e5f6", "tic-5c6d"],
        created="2026-08-03",
        events=[
            Event("2026-08-04", "claim", "gpt.4o.001"),
            Event("2026-08-08", "note", "Scrub handle works on cached waveform; needs live buffer from tic-5c6d to go real-time."),
        ],
    ),
    Spec(
        id="tic-6d7e",
        title="Regenerate PBR textures with new image_gen backend",
        type="refactor", tier="high", domain="image_gen",
        epic="image-gen-backend",
        tags=["pbr", "textures"],
        description=(
            "Point the PBR texture baking at the new image_gen backend and re-baseline the reference "
            "outputs. Can't land until the backend migration API is stable."
        ),
        created="2026-08-05",
        events=[
            Event("2026-08-05", "claim", "claude.sonnet.002"),
            Event("2026-08-09", "note", "Swapped texture baking to the new backend; output differs from reference -- flagged."),
            Event("2026-08-11", "block", "waiting on tic-8f90 backend migration to stabilize the API"),
        ],
    ),
    Spec(
        id="tic-c2d3",
        title="Refactor shared io retry logic",
        type="refactor", tier="medium", domain="io",
        epic="io-layer",
        tags=["retry"],
        description=(
            "The io retry wrapper predates the streaming APIs and assumes whole-buffer reads. Rework "
            "it once the streaming semantics from the sample buffer work are nailed down."
        ),
        created="2026-08-11",
        events=[
            Event("2026-08-12", "claim", "claude.opus.001"),
            Event("2026-08-14", "block", "tic-4b5c exposes the sample buffer first -- retry layer needs its streaming semantics"),
        ],
    ),

    # ---------------- Open / unclaimed ----------------
    Spec(
        id="tic-7e8f",
        title="Investigate intermittent mesh LOD pop-in",
        type="bug", tier="medium", domain="mesh",
        epic="mesh-pipeline",
        priority=3,
        tags=["lod", "pop-in"],
        description=(
            "Large scenes occasionally pop between LOD levels in the viewport. Reproduces ~1 in 5 "
            "loads. Suspect the LOD distance cache is not invalidated on mesh edit."
        ),
        created="2026-08-06",
    ),
    Spec(
        id="tic-90a1",
        title="Add keyboard shortcuts for viewport navigation",
        type="feature", tier="low", domain="ui",
        epic="ui-polish",
        priority=5,
        tags=["shortcuts", "viewport"],
        description=(
            "Add documented keyboard shortcuts for orbit/pan/zoom and a preferences section to "
            "remap them."
        ),
        created="2026-08-08",
    ),
    Spec(
        id="tic-a0b2",
        title="Add glTF 2.0 export support",
        type="feature", tier="high", domain="io",
        epic="io-layer",
        priority=1,
        tags=["export", "gltf"],
        description=(
            "Export meshes (with normals and UVs) as glTF 2.0. Both prerequisite pipelines "
            "(normals, decimation) are done, so this is ready to be claimed."
        ),
        depends_on=["tic-b2c3", "tic-f607"],
        created="2026-08-09",
    ),
    Spec(
        id="tic-d3e4",
        title="Create demo scene pack",
        type="chore", tier="low", domain="mesh",
        epic="mesh-pipeline",
        priority=6,
        tags=["assets", "demo"],
        description=(
            "Assemble a small pack of demo scenes exercising normals, decimation, and PBR baking so "
            "new agents have something to test against."
        ),
        created="2026-08-12",
    ),
    Spec(
        id="tic-e4f5",
        title="Dark mode polish pass on panels",
        type="feature", tier="low", domain="ui",
        epic="ui-polish",
        priority=4,
        tags=["dark-mode", "polish"],
        description=(
            "Now that colors come from tokens, do a dark-mode polish pass: check contrast, focus "
            "states, and empty states across all panels."
        ),
        created="2026-08-13",
    ),
    Spec(
        id="tic-f5a6",
        title="Investigate high memory use on large scenes",
        type="bug", tier="high", domain="mesh",
        epic="mesh-pipeline",
        priority=2,
        tags=["memory", "performance"],
        description=(
            "Loading a 2M-tri scene peaks near the memory ceiling. Profile and find where the "
            "intermediate buffers are duplicated."
        ),
        created="2026-08-14",
    ),
    Spec(
        id="tic-b7c8",
        title="Add image_gen style presets to UI dropdown",
        type="feature", tier="medium", domain="image_gen",
        epic="image-gen-backend",
        priority=2,
        tags=["presets", "ui"],
        description=(
            "Surface common style presets as a dropdown in the image_gen panel. Presets depend on "
            "the migrated backend's parameter names, so wait for tic-8f90."
        ),
        depends_on=["tic-8f90"],
        created="2026-08-15",
    ),
    Spec(
        id="tic-a6b7",
        title="",
        type="", tier="", domain="",
        tags=[],
        description="",
        created="2026-08-15",
        blank=True,
    ),
]

SPECS_BY_ID = {s.id: s for s in SPECS}


def _clean_tickets(root: Path) -> None:
    """Remove previously generated ticket *.md files (keeps agents/ and .gitkeep)."""
    for status in ("open", "in_progress", "blocked"):
        d = root / status
        if d.is_dir():
            for p in d.glob("*.md"):
                p.unlink()
    closed = root / "closed"
    if closed.is_dir():
        for month in sorted(closed.iterdir()):
            if month.is_dir():
                for p in month.glob("*.md"):
                    p.unlink()


def _append_note(t: Ticket, date: str, text: str) -> None:
    t.body = t.body.rstrip("\n") + f"\n- {date}: {text}"


def _create_ticket(spec: Spec, date: str) -> tuple[Ticket, Path]:
    tickets_root = TICKETS_ROOT
    if spec.blank:
        title, typ, tier, domain = BLANK_TITLE, BLANK_TYPE, BLANK_TIER, BLANK_DOMAIN
        desc = BLANK_DESCRIPTION
        body = f"{BLANK_WARNING}\n\n{DEFAULT_BODY.format(description=desc)}"
    else:
        title, typ, tier, domain = spec.title, spec.type, spec.tier, spec.domain
        body = DEFAULT_BODY.format(description=spec.description.strip())
    t = Ticket(
        id=spec.id,
        title=title,
        status="open",
        type=typ,
        tier=tier,
        domain=domain,
        epic=spec.epic or None,
        priority=spec.priority,
        tags=list(spec.tags),
        assignee=None,
        depends_on=list(spec.depends_on),
        blocked_by=None,
        created=date,
        updated=date,
        closed=None,
        body=body,
    )
    path = tickets_root / "open" / f"{spec.id}.md"
    save_ticket(t, path)
    return t, path


def main() -> None:
    _clean_tickets(TICKETS_ROOT)

    # Build a global chronological timeline so the simulation iterates events in
    # an order that makes sense in the real world (a ticket can't be claimed
    # before it's created, and work on one ticket may interleave with another).
    timeline = []  # (date, seq, id, action, payload); seq keeps per-ticket order
    for spec in SPECS:
        timeline.append((spec.created, 0, spec.id, "create", ""))
        for i, ev in enumerate(spec.events, start=1):
            timeline.append((ev.date, i, spec.id, ev.action, ev.payload))
    timeline.sort(key=lambda x: (x[0], x[1], x[2]))

    live: dict[str, tuple[Ticket, Path]] = {}
    for date, _seq, tid, action, payload in timeline:
        spec = SPECS_BY_ID[tid]
        if action == "create":
            t, path = _create_ticket(spec, date)
            live[tid] = (t, path)
            print(f"{date} create  {tid}")
            continue

        t, path = live[tid]
        if action == "note":
            _append_note(t, date, payload)
            t.updated = date
            print(f"{date} note    {tid}: {payload[:60]}{'...' if len(payload) > 60 else ''}")
        elif action == "claim":
            t.assignee = payload
            t.updated = date
            _append_note(t, date, f"Claimed by {payload}.")
            path = move_ticket(path, t, TICKETS_ROOT, "in_progress")
            print(f"{date} claim   {tid} -> {payload}")
        elif action == "block":
            t.blocked_by = payload
            t.updated = date
            _append_note(t, date, f"Blocked: {payload}.")
            path = move_ticket(path, t, TICKETS_ROOT, "blocked")
            print(f"{date} block   {tid}: {payload[:60]}{'...' if len(payload) > 60 else ''}")
        elif action == "unblock":
            t.blocked_by = None
            t.updated = date
            _append_note(t, date, "Unblocked -- resumed work.")
            path = move_ticket(path, t, TICKETS_ROOT, "in_progress")
            print(f"{date} unblock {tid}")
        elif action == "close":
            t.closed = date
            t.updated = date
            _append_note(t, date, "Closed.")
            path = move_ticket(path, t, TICKETS_ROOT, "closed")
            print(f"{date} close   {tid}")
        live[tid] = (t, path)

    _write_agent_scratchpads()
    _summarize()


def _active_ticket(agent: str) -> str:
    """Find the agent's current in_progress ticket id, if any."""
    for _, t in load_all_tickets(TICKETS_ROOT):
        if t.status == "in_progress" and t.assignee == agent:
            return t.id
    return ""


def _write_agent_scratchpads() -> None:
    """Update tickets/agents/ scratchpads to reflect who is working what now."""
    agents = ["claude.haiku.001", "claude.sonnet.002", "claude.opus.001", "gpt.4o.001"]
    for agent in agents:
        active = _active_ticket(agent)
        body = f"# {agent}\n\n" if active else f"# {agent}\n\nNo ticket claimed yet.\n"
        if active:
            body += f"Currently working: {active} (verify it is still in tickets/in_progress/).\n"
        (TICKETS_ROOT / "agents" / f"{agent}.md").write_text(body, encoding="utf-8")
        print(f"{'=' * 8} scratchpad {agent}: {active or 'idle'}")


def _summarize() -> None:
    print("\n" + "=" * 72)
    print("SEED DATA SUMMARY")
    print("=" * 72)
    by_status: dict[str, list] = {"open": [], "in_progress": [], "blocked": [], "closed": []}
    problems: list[str] = []
    for path, t in load_all_tickets(TICKETS_ROOT):
        by_status.setdefault(t.status, []).append((path, t))
        expected = status_dir(t.status, TICKETS_ROOT, closed_date=t.closed)
        if path.parent.resolve() != expected.resolve():
            problems.append(f"{t.id}: frontmatter says {t.status} but file is under {path.parent}")

    total = 0
    for status in ("open", "in_progress", "blocked", "closed"):
        rows = by_status[status]
        total += len(rows)
        print(f"\n[{status}] ({len(rows)})")
        for path, t in sorted(rows, key=lambda r: r[0]):
            who = t.assignee or "-"
            deps = f"  deps={t.depends_on}" if t.depends_on else ""
            blk = f"  blocked_by={t.blocked_by[:48]}" if t.blocked_by else ""
            print(f"  {t.id:<10} {who:<18} {t.title or '(blank template)'}{deps}{blk}")

    print(f"\nTotal tickets: {total}")
    if problems:
        print("\nFOLDER/STATUS MISMATCHES:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("Folder/status sync: OK (every file is in the folder its frontmatter claims)")


if __name__ == "__main__":
    main()
