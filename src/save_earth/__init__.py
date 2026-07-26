"""save-earth example package — Facetwork workflows + handlers that pull
open environmental-action datasets (OpenLitterMap litter observations, EPA
Superfund/Brownfields remediation sites, EPA Toxic Release Inventory) into a
shared GeoJSON cache and stitch them into a MapLibre HTML map.

Discovered by the Facetwork runner via the ``facetwork.domains`` entry
point declared in ``pyproject.toml``::

    [project.entry-points."facetwork.domains"]
    save-earth = "save_earth:domain"

Once ``pip install -e .`` has been run from this repository, Facetwork's
``fw runner start --domain save-earth`` and ``fw ffl seed``
will pick this package up automatically — no edits to the Facetwork
repository required.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

domain = DomainPackage(
    name="save-earth",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
