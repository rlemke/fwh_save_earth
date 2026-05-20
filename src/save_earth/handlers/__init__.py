"""Handler registration for the save-earth example.

Imports are deferred to function bodies to avoid import-lock deadlocks
when the RegistryRunner concurrently imports handler modules from
separate threads (mirrors the noaa-weather pattern).
"""

from __future__ import annotations


def register_all_handlers(poller) -> None:
    """Register all save-earth handlers with an AgentPoller."""
    from .maps.map_handlers import register_map_handlers
    from .sources.source_handlers import register_source_handlers

    register_source_handlers(poller)
    register_map_handlers(poller)


def register_all_registry_handlers(runner) -> None:
    """Register all save-earth handlers with a RegistryRunner."""
    from .maps.map_handlers import register_handlers as reg_maps
    from .sources.source_handlers import register_handlers as reg_sources

    reg_sources(runner)
    reg_maps(runner)
