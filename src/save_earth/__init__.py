"""save-earth example package — Facetwork workflows + handlers that pull
open environmental-action datasets (OpenLitterMap litter observations, EPA
Superfund/Brownfields remediation sites, EPA Toxic Release Inventory) into a
shared GeoJSON cache and stitch them into a MapLibre HTML map.

Discovered by the Facetwork runner via the ``facetwork.examples`` entry
point declared in ``pyproject.toml``::

    [project.entry-points."facetwork.examples"]
    save-earth = "save_earth:example"

Once ``pip install -e .`` has been run from this repository, Facetwork's
``scripts/start-runner --example save-earth`` and ``scripts/seed-examples``
will pick this package up automatically — no edits to the Facetwork
repository required.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.examples import ExamplePackage

from .handlers import register_all_registry_handlers

example = ExamplePackage(
    name="save-earth",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
