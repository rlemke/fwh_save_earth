"""Accessor for the composability manifest (``catalog.yaml``).

The manifest is a machine-readable index of this package's reusable
**workflows** (entry points, with intent summaries + tags for reuse-first
catalog matching, à la the platform's ``fw_catalog_match``) and **facets** (a
capability index with effect/cost, à la ``fw_capabilities``). An LLM can load
it to discover and reuse these capabilities by intent instead of grepping FFL.

Example::

    from save_earth.catalog import load_manifest, workflows, facets

    m = load_manifest()
    for wf in workflows():
        print(wf["qualified_name"], "-", wf["summary"])
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

MANIFEST_PATH = Path(__file__).with_name("catalog.yaml")


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """Load and cache the ``catalog.yaml`` manifest as a dict."""
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"catalog.yaml must be a mapping, got {type(data).__name__}")
    return data


def workflows() -> list[dict[str, Any]]:
    """Return the list of indexed entry-point workflows."""
    return list(load_manifest().get("workflows", []))


def facets() -> list[dict[str, Any]]:
    """Return the capability index of composable facets."""
    return list(load_manifest().get("facets", []))
