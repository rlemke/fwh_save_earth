#!/usr/bin/env python3
"""save-earth RegistryRunner entry point.

Starts a RegistryRunner that advertises every save-earth event facet
(``save_earth.sources.*`` downloads + ``save_earth.maps.BuildMap``) and
dispatches them to the handlers in this package.

Usage::

    # From a Facetwork checkout (preferred — handles env + seeding):
    fw runner start --domain save-earth

    # Or directly, once `pip install -e .` has registered the package:
    python agent.py

Requires (set for Docker/MongoDB mode)::

    FW_MONGODB_URL=mongodb://localhost:27017
    FW_MONGODB_DATABASE=facetwork
"""

from __future__ import annotations

from save_earth.handlers import register_all_registry_handlers

from facetwork.runtime.registry_runner import create_registry_runner


def main() -> None:
    runner = create_registry_runner("save-earth", topics=["save_earth.*"])
    register_all_registry_handlers(runner)
    print(f"save-earth RegistryRunner started with {len(runner.registered_names())} handlers")
    runner.start()


if __name__ == "__main__":
    main()
