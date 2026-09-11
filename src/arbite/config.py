"""Locating the .arbite/ dir and loading known agent identities.

Agent identity assignment/collision-detection is out of scope for arbite
(handled by an external agent harness) -- this just reads a static list of
known agent ids so `arbite init` can create their scratchpad files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

CONFIG_FILENAMES = ["arbite.yaml", ".arbite.yaml"]

ARBITE_DIRNAME = ".arbite"


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


def load_known_agent_ids(project_root: Path) -> list:
    for name in CONFIG_FILENAMES:
        config_path = project_root / name
        if config_path.is_file():
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            agents = data.get("agents", [])
            return [str(a) for a in agents]
    return []
