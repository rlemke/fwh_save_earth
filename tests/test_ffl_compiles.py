"""Offline compile check for every shipped FFL file in the save_earth package.

Each ``*.ffl`` under ``src/save_earth/`` must parse, validate, and emit cleanly.
Each file is compiled as the *primary* source with **all other shipped FFL files
supplied as libraries** (the dependency closure), mirroring how the runtime
resolves a workflow against the rest of the deployed library — so a clean result
here means the file is genuinely deployable, not merely parseable in isolation.

The test is parametrized over the files so a failure names the offending file.
It is fully offline and in-process: it uses the Facetwork compiler directly
(``FFLParser`` + ``validate`` + ``emit_dict``) — no MongoDB, no network, no
external binaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from facetwork.emitter import emit_dict
from facetwork.parser import FFLParser
from facetwork.source import CompilerInput, FileOrigin, SourceEntry
from facetwork.validator import validate

# ---------------------------------------------------------------------------
# Locate the package's shipped FFL via the importable package, so the test
# tracks whatever is actually installed/checked out.
# ---------------------------------------------------------------------------
try:
    import save_earth

    _PKG_DIR = Path(save_earth.__file__).resolve().parent
except Exception:  # pragma: no cover - package must be importable to test it
    _PKG_DIR = None


def _discover_ffl() -> list[Path]:
    """All shipped ``*.ffl`` files under the package (test fixtures excluded)."""
    if _PKG_DIR is None or not _PKG_DIR.is_dir():
        return []
    return sorted(p for p in _PKG_DIR.rglob("*.ffl") if "/tests/" not in str(p))


_FFL_FILES = _discover_ffl()
_FFL_IDS = [str(p.relative_to(_PKG_DIR)) for p in _FFL_FILES] if _PKG_DIR else []


def _compile_with_closure(primary: Path, library: list[Path]) -> dict:
    """Parse + validate + emit ``primary`` with ``library`` as supporting sources.

    Raises ``AssertionError`` with the first validation error if validation
    fails; returns the emitted program dict on success.
    """
    primary_entry = SourceEntry(
        text=primary.read_text(encoding="utf-8"),
        origin=FileOrigin(path=str(primary)),
        is_library=False,
    )
    lib_entries = [
        SourceEntry(
            text=p.read_text(encoding="utf-8"),
            origin=FileOrigin(path=str(p)),
            is_library=True,
        )
        for p in library
    ]

    compiler_input = CompilerInput(
        primary_sources=[primary_entry],
        library_sources=lib_entries,
    )

    parser = FFLParser()
    program_ast, _registry = parser.parse_sources(compiler_input)

    result = validate(program_ast)
    assert not result.errors, "validation errors: " + "; ".join(
        str(e) for e in result.errors
    )

    emitted = emit_dict(program_ast, include_locations=False)
    assert emitted.get("type") == "Program"
    return emitted


def test_ffl_files_discovered():
    """Sanity: we found the package and a non-trivial set of FFL files."""
    assert _PKG_DIR is not None, "save_earth package is not importable"
    assert _FFL_FILES, f"no *.ffl files discovered under {_PKG_DIR}"


@pytest.mark.parametrize("ffl_path", _FFL_FILES, ids=_FFL_IDS)
def test_ffl_compiles_clean(ffl_path: Path):
    """The given FFL file parses, validates, and emits against the full library."""
    library = [p for p in _FFL_FILES if p != ffl_path]
    _compile_with_closure(ffl_path, library)
