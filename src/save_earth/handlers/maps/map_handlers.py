"""Map handlers — stitch cached GeoJSON into a MapLibre HTML bundle."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..shared.save_earth_utils import (
    alpr,
    aquifers,
    datacenters,
    enclaves,
    get_storage,
    power,
    lgbtq,
    map_render,
    nuclear,
    openlittermap,
    seismic,
    semiconductor,
    siting,
    telescope,
    tesla,
    volcanoes,
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

# Nuclear power features (OSM via Overpass). description_fields is left
# None on purpose: with no curated list the renderer falls back to
# Object.keys(props), so clicking a reactor pops up EVERY OSM tag the
# dataset carries — the "show all available information" requirement.
_NUCLEAR_LAYER = map_render.LayerSpec(
    name="nuclear-reactors",
    title="Nuclear power reactors & plants (OSM)",
    source_cache_type=nuclear.CACHE_TYPE,
    source_relative_path=nuclear.RELATIVE_PATH,
    color="#2e7d32",
    radius=8,
    description_fields=None,
)

# ALPR surveillance cameras (OSM via DeFlock). One cached file → a density heatmap
# + a per-vendor split (Flock / Motorola / other), all sharing the same inlined
# source (the renderer dedupes it). description_fields=None → the popup shows EVERY
# OSM tag (manufacturer, operator, direction, surveillance:zone, …). The heatmap is
# listed FIRST so it draws UNDER the coloured vendor dots.
_ALPR_LAYERS = [
    map_render.LayerSpec(
        name="alpr-heatmap",
        title="ALPR camera density (heatmap)",
        source_cache_type=alpr.CACHE_TYPE,
        source_relative_path=alpr.RELATIVE_PATH,
        color="#ff922b",  # legend swatch (heatmap has no single dot colour)
        geometry="heatmap",
        description_fields=None,
    ),
    map_render.LayerSpec(
        name="alpr-flock",
        title="Flock Safety",
        source_cache_type=alpr.CACHE_TYPE,
        source_relative_path=alpr.RELATIVE_PATH,
        color="#c62828",
        radius=4,
        filter_field="camera_vendor",
        filter_value="flock",
        description_fields=None,
    ),
    map_render.LayerSpec(
        name="alpr-motorola",
        title="Motorola / Vigilant",
        source_cache_type=alpr.CACHE_TYPE,
        source_relative_path=alpr.RELATIVE_PATH,
        color="#f59f00",
        radius=4,
        filter_field="camera_vendor",
        filter_value="motorola",
        description_fields=None,
    ),
    map_render.LayerSpec(
        name="alpr-other",
        title="Other / unspecified ALPR",
        source_cache_type=alpr.CACHE_TYPE,
        source_relative_path=alpr.RELATIVE_PATH,
        color="#868e96",
        radius=4,
        filter_field="camera_vendor",
        filter_value="other",
        description_fields=None,
    ),
]

# US principal-aquifer polygons (USGS) — the groundwater backdrop. Registered
# BEFORE the data-center points so it draws underneath them.
_AQUIFER_LAYER = map_render.LayerSpec(
    name="aquifers",
    title="Principal aquifers (USGS)",
    source_cache_type=aquifers.CACHE_TYPE,
    source_relative_path=aquifers.RELATIVE_PATH,
    color="#1f78b4",
    geometry="fill",
    description_fields=["AQ_NAME", "ROCK_TYPE"],
)

# US data centers (OSM) — high-water-use compute over the aquifers.
_DATACENTER_LAYER = map_render.LayerSpec(
    name="data-centers",
    title="Data centers (OSM)",
    source_cache_type=datacenters.CACHE_TYPE,
    source_relative_path=datacenters.RELATIVE_PATH,
    color="#e31a1c",
    radius=5,
    description_fields=None,
)

# Semiconductor fabrication plants (OSM via per-country Overpass). description_fields
# =None so the popup shows EVERY OSM tag (operator, name, product, start_date, …).
_SEMICONDUCTOR_LAYER = map_render.LayerSpec(
    name="semiconductor-fabs",
    title="Semiconductor fabrication plants (OSM)",
    source_cache_type=semiconductor.CACHE_TYPE,
    source_relative_path=semiconductor.MERGED_RELATIVE_PATH,
    color="#6a1b9a",
    radius=7,
    description_fields=None,
)

# Major (notable) volcanoes (OSM via Overpass). description_fields=None so the
# popup shows EVERY OSM tag (name, ele, volcano:type, volcano:status, …).
_VOLCANO_LAYER = map_render.LayerSpec(
    name="volcanoes",
    title="Major volcanoes (OSM)",
    source_cache_type=volcanoes.CACHE_TYPE,
    source_relative_path=volcanoes.RELATIVE_PATH,
    color="#d84315",
    radius=8,
    description_fields=None,
)

# LGBTQ+ bars & restaurants (OSM). description_fields=None → popup table shows
# every OSM tag the venue carries.
_LGBTQ_LAYER = map_render.LayerSpec(
    name="lgbtq",
    title="LGBTQ+ bars & restaurants (OSM)",
    source_cache_type=lgbtq.CACHE_TYPE,
    source_relative_path=lgbtq.RELATIVE_PATH,
    color="#d500f9",
    radius=6,
    description_fields=None,
)

# Tesla charging stations (OSM). description_fields=None → popup shows every OSM
# tag (operator, capacity, socket:*, opening_hours, fee, …).
_TESLA_LAYER = map_render.LayerSpec(
    name="tesla",
    title="Tesla charging stations (OSM)",
    source_cache_type=tesla.CACHE_TYPE,
    source_relative_path=tesla.RELATIVE_PATH,
    color="#e82127",
    radius=6,
    description_fields=None,
)

# Research telescopes (OSM). description_fields=None -> popup shows every OSM tag
# (telescope:type, telescope:diameter, operator, start_date, wikipedia, ...).
_TELESCOPE_LAYER = map_render.LayerSpec(
    name="telescopes",
    title="Research telescopes (OSM)",
    source_cache_type=telescope.CACHE_TYPE,
    source_relative_path=telescope.RELATIVE_PATH,
    color="#3949ab",
    radius=6,
    description_fields=None,
)

# Tectonic plate boundaries (fault lines) — LineString geometry, drawn as
# lines. description_fields=None → click a boundary to see its raw properties.
_FAULTS_LAYER = map_render.LayerSpec(
    name="faults",
    title="Fault lines / plate boundaries (Bird 2002)",
    source_cache_type=seismic.FAULTS_CACHE_TYPE,
    source_relative_path=seismic.FAULTS_RELATIVE_PATH,
    color="#ff7043",
    geometry="line",
    weight=1.6,
    description_fields=None,
)

# Recent earthquakes (USGS feed). magnitude_field="mag" → circles sized + coloured
# by magnitude. description_fields=None → popup shows every USGS property.
_EARTHQUAKE_LAYER = map_render.LayerSpec(
    name="earthquakes",
    title="Recent earthquakes M4.5+ (USGS, past 30 days)",
    source_cache_type=seismic.EARTHQUAKES_CACHE_TYPE,
    source_relative_path=seismic.EARTHQUAKES_RELATIVE_PATH,
    color="#d73027",
    radius=6,
    magnitude_field="mag",
    description_fields=None,
)

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


def _openlittermap_layers(storage) -> list[map_render.LayerSpec]:
    olm_dir = sidecar_lib.cache_path(map_render.NAMESPACE, openlittermap.CACHE_TYPE, "", storage)
    # os.path.isdir is False for an s3:// prefix, so OLM auto-discovery is a
    # no-op on object storage (not needed for the nuclear workflow). Local
    # deployments still enumerate the cached OLM files here.
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


def _enclave_layers(storage) -> list[map_render.LayerSpec]:
    """One coloured layer per heritage (chinese, japanese, italian, …).

    Built from the fixed ``enclaves.HERITAGES`` constant — NOT a directory
    listing — so it works identically on local disk and the s3/MinIO object
    store (``storage.exists`` is the presence test; a heritage with no cached
    GeoJSON is simply dropped). Layer ids are ``enclave-<slug>`` so the workflow
    selects them all with ``only_layers="enclave-*"``. description_fields=None →
    the popup shows every OSM tag (name first), plus the derived ``heritage`` /
    ``osm_url`` fields."""
    layers: list[map_render.LayerSpec] = []
    for h in enclaves.HERITAGES:
        layers.append(
            map_render.LayerSpec(
                name=f"enclave-{h.slug}",
                title=h.label,
                source_cache_type=enclaves.CACHE_TYPE,
                source_relative_path=f"{h.slug}.geojson",
                color=h.color,
                radius=6,
            )
        )
    return layers


def _power_layers(storage) -> list[map_render.LayerSpec]:
    """One coloured point layer per power source (hydro/coal/solar/wind/nuclear,
    from the WRI Global Power Plant Database) + a ≥500 kV transmission-line layer
    (OSM). Layer ids are ``power-<slug>`` so the workflow selects them with
    ``only_layers="power-*"``. description_fields=None → the popup shows the full
    WRI record / OSM tags."""
    layers: list[map_render.LayerSpec] = []
    for slug, _wri, label, color in power.FUELS:
        layers.append(
            map_render.LayerSpec(
                name=f"power-{slug}",
                title=f"{label} power plants",
                source_cache_type=power.CACHE_TYPE,
                source_relative_path=f"{slug}.geojson",
                color=color,
                radius=4,
            )
        )
    layers.append(
        map_render.LayerSpec(
            name="power-transmission",
            title="Transmission lines ≥500 kV (OSM)",
            source_cache_type=power.CACHE_TYPE,
            source_relative_path="transmission.geojson",
            color="#e91e63",
            geometry="line",
            weight=0.7,
            description_fields=None,
        )
    )
    return layers


def _siting_layers(storage) -> list[map_render.LayerSpec]:
    """Renewable-siting layers: solar & wind plants coloured by the local
    resource (``siting_score``, 4..8) so the renderer's magnitude ramp shows how
    well each plant is sited. Layer ids are ``siting-<slug>`` (selected with
    ``only_layers="siting-*"``). description_fields=None → the popup shows the
    full WRI record plus the sampled resource value."""
    layers: list[map_render.LayerSpec] = []
    for slug, rel, _param, _prop, _dom in siting.RESOURCES:
        label = "Solar" if slug == "solar" else "Wind"
        color = "#f9a825" if slug == "solar" else "#2e7d32"
        layers.append(
            map_render.LayerSpec(
                name=f"siting-{slug}",
                title=f"{label} plants by local resource (NASA POWER)",
                source_cache_type=siting.CACHE_TYPE,
                source_relative_path=rel,
                color=color,
                radius=4,
                magnitude_field="siting_score",
                description_fields=None,
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
    # only_layers: comma-separated layer-name allowlist (empty = auto-discover
    # every cached layer, the global-map behaviour). Lets the nuclear/volcano
    # workflows render a single focused layer instead of every cached source.
    only = {s.strip() for s in (params.get("only_layers", "") or "").split(",") if s.strip()}
    attribution_workflow = params.get("attribution_workflow", "") or ""
    attribution_ffl_url = params.get("attribution_ffl_url", "") or ""
    description = params.get("description", "") or ""
    # Per-source inline cap. Default 50k; raise for a dense single-source map
    # (e.g. ~120k ALPR cameras behind a vendor split + heatmap).
    max_inline_features = int(params.get("max_inline_features", 0) or 50_000)
    step_log = params.get("_step_log")

    storage = get_storage()
    # Faults (lines) before earthquakes (points) so quakes draw on top.
    candidates = (
        [
            _NUCLEAR_LAYER,
            *_ALPR_LAYERS,
            _AQUIFER_LAYER,
            _DATACENTER_LAYER,
            _SEMICONDUCTOR_LAYER,
            _VOLCANO_LAYER,
            _LGBTQ_LAYER,
            _TESLA_LAYER,
            _TELESCOPE_LAYER,
            _FAULTS_LAYER,
            _EARTHQUAKE_LAYER,
        ]
        + _EPA_LAYERS
        + _openlittermap_layers(storage)
        + _enclave_layers(storage)
        + _power_layers(storage)
        + _siting_layers(storage)
    )
    if only:
        # Exact name match, plus a trailing-"*" prefix wildcard so a workflow can
        # select a whole family of layers (e.g. only_layers="enclave-*").
        prefixes = tuple(o[:-1] for o in only if o.endswith("*"))
        exact = {o for o in only if not o.endswith("*")}
        candidates = [
            layer for layer in candidates if layer.name in exact or layer.name.startswith(prefixes)
        ]
    present: list[map_render.LayerSpec] = []
    for layer in candidates:
        geojson_path = sidecar_lib.cache_path(
            map_render.NAMESPACE,
            layer.source_cache_type,
            layer.source_relative_path,
            storage,
        )
        if storage.exists(geojson_path):
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
        attribution_workflow=attribution_workflow,
        attribution_ffl_url=attribution_ffl_url,
        description=description,
        max_inline_features=max_inline_features,
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
