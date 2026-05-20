"""Map handlers — stitch cached GeoJSON into a MapLibre HTML bundle."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..shared.save_earth_utils import (
    LocalStorage,
    map_render,
    openlittermap,
)
from ..shared.save_earth_utils import (
    sidecar as sidecar_lib,
)

logger = logging.getLogger("save-earth.maps")
NAMESPACE = "save_earth.maps"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


# Keep these layer defaults in lockstep with DEFAULT_LAYERS in the CLI
# (``tools/build_save_earth_map.py``). The handler uses the same
# auto-discovery rule for OpenLitterMap — every cached OLM file becomes
# its own toggleable layer.
_EPA_LAYERS: list[map_render.LayerSpec] = [
    map_render.LayerSpec(
        name="epa-superfund",
        title="EPA Superfund (NPL) sites",
        source_cache_type="epa-cleanups",
        source_relative_path="superfund.geojson",
        color="#5e3c99",
        radius=7,
        description_fields=[
            "primary_name",
            "location_address",
            "city_name",
            "state_code",
            "epa_region",
            "pgm_sys_id",
            "facility_url",
        ],
    ),
    map_render.LayerSpec(
        name="epa-brownfields",
        title="EPA Brownfield sites (ACRES)",
        source_cache_type="epa-cleanups",
        source_relative_path="brownfields.geojson",
        color="#c66a00",
        radius=6,
        description_fields=[
            "primary_name",
            "location_address",
            "city_name",
            "state_code",
            "epa_region",
            "pgm_sys_id",
            "facility_url",
        ],
    ),
]

_OLM_DESCRIPTION_FIELDS = [
    "point_count",
    "point_count_abbreviated",
    "datetime",
    "verified",
    "picked_up",
    "username",
    "id",
]
_OLM_COLORS = ["#d9534f", "#e57373", "#f06292", "#ba68c8", "#7986cb"]


def _openlittermap_layers(storage: LocalStorage) -> list[map_render.LayerSpec]:
    olm_dir = sidecar_lib.cache_path(map_render.NAMESPACE, openlittermap.CACHE_TYPE, "", storage)
    if not os.path.isdir(olm_dir):
        return []
    layers: list[map_render.LayerSpec] = []
    names = sorted(
        fn
        for fn in os.listdir(olm_dir)
        if fn.endswith(".geojson") and not fn.endswith(".meta.json")
    )
    for i, fn in enumerate(names):
        base_radius = 9 if fn.startswith("clusters-") else 5
        layers.append(
            map_render.LayerSpec(
                name=f"olm-{fn[: -len('.geojson')]}",
                title=f"OpenLitterMap — {fn[: -len('.geojson')]}",
                source_cache_type=openlittermap.CACHE_TYPE,
                source_relative_path=fn,
                color=_OLM_COLORS[i % len(_OLM_COLORS)],
                radius=base_radius,
                description_fields=_OLM_DESCRIPTION_FIELDS,
            )
        )
    return layers


def handle_build_map(params: dict[str, Any]) -> dict[str, Any]:
    """Handle BuildMap — auto-discover every cached layer and render HTML."""
    region = params.get("region", "global") or "global"
    center_lat = float(params.get("center_lat", 39.8283))
    center_lon = float(params.get("center_lon", -98.5795))
    zoom = float(params.get("zoom", 4.0))
    # Empty-string overrides fall back to the library defaults (CARTO
    # Voyager + OSM/CARTO attribution) so FFL callers can leave them
    # unset without breaking rendering.
    basemap_url = params.get("basemap_url", "") or map_render.DEFAULT_BASEMAP_URL
    basemap_attr = params.get("basemap_attribution", "") or map_render.DEFAULT_BASEMAP_ATTRIBUTION
    step_log = params.get("_step_log")

    storage = LocalStorage()
    candidates = _EPA_LAYERS + _openlittermap_layers(storage)
    present: list[map_render.LayerSpec] = []
    for layer in candidates:
        geojson_path = sidecar_lib.cache_path(
            map_render.NAMESPACE,
            layer.source_cache_type,
            layer.source_relative_path,
            storage,
        )
        if os.path.exists(geojson_path):
            present.append(layer)
        else:
            logger.info("skipping layer %s — no cache at %s", layer.name, geojson_path)

    if not present:
        _step_log(
            step_log,
            "No cached layers — run DownloadOpenLitterMap / DownloadEpaCleanups first",
            "warning",
        )
        return {
            "region_key": region,
            "output_dir": "",
            "html_path": "",
            "layer_count": 0,
            "layer_counts": json.dumps({}),
        }

    _step_log(
        step_log,
        f"BuildMap region={region} layers={len(present)} "
        f"names=[{', '.join(layer.name for layer in present)}]",
    )

    bundle = map_render.render_map(
        region_key=region,
        layers=present,
        center=(center_lat, center_lon),
        zoom=zoom,
        storage=storage,
        basemap_url=basemap_url,
        basemap_attribution=basemap_attr,
    )
    _step_log(
        step_log,
        f"[map] {bundle.html_path}",
        "success",
    )
    return {
        "region_key": bundle.region_key,
        "output_dir": str(bundle.output_dir),
        "html_path": str(bundle.html_path),
        "layer_count": len(bundle.layer_counts),
        "layer_counts": json.dumps(bundle.layer_counts),
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.BuildMap": handle_build_map,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH[facet]
    return handler(payload)


def register_handlers(runner) -> None:
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_map_handlers(poller) -> None:
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
