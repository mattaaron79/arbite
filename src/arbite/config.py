"""Project configuration: where the arbite directory is, and which sink to use.

Two jobs that both come down to "find the thing, or say clearly what is missing":

- locate the `.arbite/` directory by walking up from the working directory, so
  every command works from a subdirectory of the repo;
- resolve the *sink* -- the storage implementation tickets live in -- from, in
  order of precedence: the `--sink` flag, the `ARBITE_SINK` environment variable,
  the `sink:` key in `arbite.yaml`, then the default (`file`).

Agent identity assignment and collision detection remain out of scope: this only
reads a static list of known ids so `arbite init` can create scratchpads for them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from .errors import SinkNotInitialised, TicketError
from .sinks import DEFAULT_SINK_KIND, SINK_KINDS, SinkSpec, build_sink, default_location

CONFIG_FILENAMES = ["arbite.yaml", ".arbite.yaml"]

ARBITE_DIRNAME = ".arbite"

#: Environment override, for a shell that wants one command to talk to a
#: different store without editing the config.
ENV_SINK = "ARBITE_SINK"

#: Config keys inside the `sinks:` mapping that give a sink's location.
SINK_LOCATION_KEYS = {"file": "root", "sqlite": "path"}


def find_arbite_dir(start: Optional[Path] = None) -> Optional[Path]:
    """Walks up from `start` (default: cwd) looking for a .arbite/ directory."""
    start = (start or Path.cwd()).resolve()
    for candidate_root in [start, *start.parents]:
        candidate = candidate_root / ARBITE_DIRNAME
        if candidate.is_dir():
            return candidate
    return None


def find_project_root(start: Optional[Path] = None) -> Path:
    """The directory that contains (or should contain) .arbite/."""
    arbite_dir = find_arbite_dir(start)
    if arbite_dir is not None:
        return arbite_dir.parent
    return (start or Path.cwd()).resolve()


def config_path(project_root: Path) -> Optional[Path]:
    for name in CONFIG_FILENAMES:
        candidate = project_root / name
        if candidate.is_file():
            return candidate
    return None


def load_config(project_root: Path) -> dict:
    """The project's arbite.yaml/.arbite.yaml as a dict ({} when absent).

    A malformed config is reported rather than ignored: silently falling back to
    the default sink would send tickets to the wrong store."""
    path = config_path(project_root)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise TicketError(f"{path}: config is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise TicketError(f"{path}: config must be a YAML mapping of key: value")
    return data


def load_known_agent_ids(project_root: Path) -> list:
    agents = load_config(project_root).get("agents", [])
    return [str(a) for a in agents]


def sink_spec(
    cli_sink: Optional[str] = None,
    project_root: Optional[Path] = None,
    use_env: bool = True,
) -> SinkSpec:
    """Resolve which sink to use: flag, then environment, then config, then the
    default. An unknown name is an error listing the valid kinds.

    `use_env=False` answers a different question -- "what will a plain command use
    in *this project*" -- which is what `.arbite/AGENTS.md` has to state, since that
    file is committed and read by processes whose environment arbite cannot know."""
    project_root = project_root or find_project_root()
    config = load_config(project_root)

    kind = (
        cli_sink
        or (os.environ.get(ENV_SINK) if use_env else None)
        or config.get("sink")
        or DEFAULT_SINK_KIND
    )
    kind = str(kind).strip().lower()
    if kind not in SINK_KINDS:
        raise TicketError(
            f"unknown sink '{kind}' (valid: {', '.join(SINK_KINDS)}); choose one with "
            f"--sink, the {ENV_SINK} environment variable, or a 'sink:' key in "
            "arbite.yaml"
        )

    options = {}
    configured = config.get("sinks") or {}
    if isinstance(configured.get(kind), dict):
        options = dict(configured[kind])
    location_key = SINK_LOCATION_KEYS.get(kind, "root")
    root = options.pop(location_key, None)
    return SinkSpec(kind=kind, root=root, options=options)


def configured_sink_spec(project_root: Optional[Path] = None) -> SinkSpec:
    """The sink a plain command uses in this project, from committed config alone.

    Deliberately ignores both `--sink` and `ARBITE_SINK`: those are per-invocation
    decisions made by whoever runs a command, and the one thing an agent reading a
    committed guide needs is the answer that holds when nobody passes anything."""
    return sink_spec(None, project_root, use_env=False)


def open_sink(
    cli_sink: Optional[str] = None,
    project_root: Optional[Path] = None,
    require_initialised: bool = True,
) -> object:
    """The configured sink, ready to use.

    Raises SinkNotInitialised when the arbite directory (or the configured store)
    does not exist, so the CLI can say "run `arbite init` first" rather than
    producing an empty listing that looks like an empty backlog."""
    project_root = project_root or find_project_root()
    spec = sink_spec(cli_sink, project_root)
    arbite_dir = project_root / ARBITE_DIRNAME
    if require_initialised and not arbite_dir.is_dir():
        raise SinkNotInitialised(
            f"no {ARBITE_DIRNAME}/ directory found (run 'arbite init' first)"
        )
    return build_sink(spec, arbite_dir)


def open_sink_kind(kind: str, project_root: Optional[Path] = None) -> object:
    """A sink of an explicit kind, using that kind's configured location.

    Used by `arbite migrate --to <sink>`, which must be able to build a sink the
    project is not currently using (and, since migration may be the first thing a
    user does with a new sink, one that does not exist yet)."""
    project_root = project_root or find_project_root()
    spec = sink_spec(kind, project_root)
    # `sink_spec` takes the kind as its selection override, so this resolves that
    # kind's location from config without otherwise changing precedence rules.
    spec = SinkSpec(kind=kind, root=spec.root, options=spec.options)
    return build_sink(spec, project_root / ARBITE_DIRNAME)


def describe_default_location(kind: str, project_root: Optional[Path] = None) -> str:
    project_root = project_root or find_project_root()
    return str(default_location(kind, project_root / ARBITE_DIRNAME))
