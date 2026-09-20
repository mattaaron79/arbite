"""The sink registry: how a storage choice becomes an object.

`build_sink()` is the single place a name ('file', 'sqlite') turns into an
implementation, so a one-off `--sink`, the `ARBITE_SINK` environment variable and
the `sink:` key in `.arbite/project.yaml` all funnel through one validated lookup. An
unknown name is reported with the valid options instead of failing inside a
stack trace.

The interface itself lives in `base.py`; this module is only the registry, which
keeps the import graph acyclic (sinks do not import config, config imports sinks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..errors import TicketError
from .base import (  # noqa: F401  (re-exported: the public face of a sink)
    UNSET,
    Expect,
    Problem,
    SinkInfo,
    TicketSink,
    common_problems,
    enforce_expect,
    filter_tickets,
)
from .file import CLOSED_DIR, FLAT_STATUS_DIRS, FileSink
from .sqlite import SCHEMA_VERSION, SqliteSink

#: The sink used when nothing selects one. Files stay the default because the
#: project's stated value is a clone-and-go ticket store that git can version.
DEFAULT_SINK_KIND = "file"

SINK_CLASSES = {"file": FileSink, "sqlite": SqliteSink}

SINK_KINDS = tuple(SINK_CLASSES)

#: Filename for the SQLite sink inside the arbite directory.
SQLITE_FILENAME = "arbite.db"


@dataclass(frozen=True)
class SinkSpec:
    """A resolved storage choice: which implementation, and where.

    `root` is the sink's own location when a config explicitly gives one; when it
    is None the sink falls back to its default inside the arbite directory."""

    kind: str = DEFAULT_SINK_KIND
    root: Optional[str] = None
    options: dict = field(default_factory=dict)


def build_sink(spec: SinkSpec, arbite_dir: Path) -> TicketSink:
    """Instantiate the sink `spec` describes.

    `arbite_dir` is the project's `.arbite/` directory -- the anchor every sink
    defaults its location to, so a project has exactly one place that says "the
    tickets for this repo live around here"."""
    kind = (spec.kind or DEFAULT_SINK_KIND).strip().lower()
    if kind not in SINK_CLASSES:
        raise TicketError(
            f"unknown sink '{spec.kind}' (valid: {', '.join(SINK_KINDS)})"
        )
    if kind == "file":
        root = Path(spec.root) if spec.root else arbite_dir
        return FileSink(root)
    path = Path(spec.root) if spec.root else arbite_dir / SQLITE_FILENAME
    return SqliteSink(path, **spec.options)


def default_location(kind: str, arbite_dir: Path) -> Path:
    """Where a sink of this kind keeps its store by default. Used for messages
    and by `arbite init` before the store exists."""
    if kind == "file":
        return arbite_dir
    return arbite_dir / SQLITE_FILENAME
