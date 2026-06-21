"""Test bootstrap for the save-earth package.

Ensures the in-repo ``src/`` is importable so ``import save_earth`` resolves to
the checked-out source even when the package isn't pip-installed into the active
environment. Fully offline — no MongoDB, no network, no external binaries.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
