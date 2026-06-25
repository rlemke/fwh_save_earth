"""Handler-side re-export of the save-earth tool library.

The real implementation lives in ``save_earth/tools/_save_earth_tools/`` (inside this
package). It is shared verbatim by:

- the save-earth CLI tools (``save_earth/tools/``), and
- the FFL handlers in this package.

Both entry points read and write the same on-disk cache
(``$AFL_DATA_ROOT/cache/save-earth/...``) with per-entry
``.meta.json`` sidecars — the tool and the FFL are two surfaces onto
one cache.

One-time sys.path shim so handlers can import the _save_earth_tools modules via a
natural ``from ..shared.save_earth_utils import ...`` without every
caller repeating the path gymnastics. ``parents[2]`` is the package
root (``save_earth/``); ``/ "tools"`` reaches the bundled CLI tools.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _save_earth_tools import (  # noqa: E402,F401
    epa_cleanups,
    lgbtq,
    map_render,
    nuclear,
    openlittermap,
    seismic,
    sidecar,
    tesla,
    tri,
    volcanoes,
)
from _save_earth_tools.storage import LocalStorage, get_storage  # noqa: E402,F401


def parse_bbox(s: str) -> tuple[float, float, float, float] | None:
    """Parse ``min_lon,min_lat,max_lon,max_lat`` → tuple. Empty string → None.

    FFL workflows pass bbox as a String for schema simplicity; the
    handlers translate to the tuple the _save_earth_tools API wants.
    """
    if not s:
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must have 4 comma-separated numbers (got {len(parts)}): {s!r}")
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError as exc:
        raise ValueError(f"bbox parse failed: {exc}") from exc


__all__ = [
    "LocalStorage",
    "get_storage",
    "epa_cleanups",
    "map_render",
    "nuclear",
    "openlittermap",
    "parse_bbox",
    "seismic",
    "sidecar",
    "tesla",
    "tri",
    "volcanoes",
    "lgbtq",
]
