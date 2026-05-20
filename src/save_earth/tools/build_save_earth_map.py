"""Stitch cached save-earth GeoJSON layers into a single MapLibre HTML map.

Reads from the per-source caches the downloaders populate and produces
one ``index.html`` at
``$AFL_CACHE_ROOT/save-earth/maps/<region>/index.html`` with per-source
layer toggles and click popups that surface each feature's upstream
``properties`` (name, description, status, etc.).

Usage::

    # Default: include every cached layer, region key 'global'
    python build_save_earth_map.py

    # Write somewhere other than the cache
    python build_save_earth_map.py --output-dir /tmp/save-earth

    # Custom region key (affects the output subdirectory)
    python build_save_earth_map.py --region us

    # Customize the map view
    python build_save_earth_map.py --center 40.0,-100.0 --zoom 3
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import epa_cleanups, map_render, openlittermap, sidecar, tri  # noqa: E402
from _lib.storage import LocalStorage  # noqa: E402

# EPA layers have fixed filenames. OpenLitterMap layers are auto-
# discovered at render time (see _openlittermap_layers) because the
# filename depends on mode / zoom / bbox and we want every cached
# pull to show up as its own toggleable layer.
#
# Keep OpenLitterMap's description fields list stable so the popup
# content is consistent across auto-discovered entries.
_OLM_DESCRIPTION_FIELDS = [
    "point_count",
    "point_count_abbreviated",
    "datetime",
    "verified",
    "picked_up",
    "username",
    "id",
]
_OLM_COLORS = [
    "#d9534f",  # red — default/first cached zoom
    "#e57373",
    "#f06292",
    "#ba68c8",
    "#7986cb",
]

DEFAULT_LAYERS: list[map_render.LayerSpec] = [
    map_render.LayerSpec(
        name="epa-superfund",
        title="EPA Superfund (NPL) sites",
        source_cache_type=epa_cleanups.CACHE_TYPE,
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
        source_cache_type=epa_cleanups.CACHE_TYPE,
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
    map_render.LayerSpec(
        name="epa-tri",
        title="EPA TRI reporters (Toxic Release Inventory)",
        source_cache_type=tri.CACHE_TYPE,
        source_relative_path=tri.RELATIVE_PATH,
        color="#b30000",
        radius=5,
        description_fields=[
            "facility_name",
            "parent_co_name",
            "standardized_parent_company",
            "city_name",
            "state_abbr",
            "county_name",
            "region",
            "tri_facility_id",
            "closed",
        ],
    ),
]


def _openlittermap_layers(storage: LocalStorage) -> list[map_render.LayerSpec]:
    """Auto-discover every cached OpenLitterMap GeoJSON file and expose each
    as its own toggleable layer.

    Users commonly pull multiple (mode, zoom, bbox) combinations — e.g. a
    global ``clusters-zoom4`` overview plus a city-level
    ``points-zoom15_<bbox>`` detail feed. Without auto-discovery they'd
    only see the default clusters-zoom4 layer on the map.
    """
    olm_dir = sidecar.cache_path(map_render.NAMESPACE, openlittermap.CACHE_TYPE, "", storage)
    if not os.path.isdir(olm_dir):
        return []
    layers: list[map_render.LayerSpec] = []
    names = sorted(
        fn
        for fn in os.listdir(olm_dir)
        if fn.endswith(".geojson") and not fn.endswith(".meta.json")
    )
    for i, fn in enumerate(names):
        # Radius scales with zoom: a clusters-zoom4 overview paints bigger
        # dots because each cluster represents many reports; points-zoomN
        # entries are single photos, so small dots are fine.
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


def _parse_center(s: str) -> tuple[float, float]:
    try:
        parts = [float(p) for p in s.split(",")]
    except ValueError as exc:
        raise SystemExit(f"error: --center needs 2 comma-separated numbers: {exc}") from exc
    if len(parts) != 2:
        raise SystemExit("error: --center needs exactly 2 values (lat,lon)")
    return tuple(parts)  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--region",
        default="global",
        help="Output sub-directory name under maps/ (default: global).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write index.html + sidecar here instead of the cache.",
    )
    parser.add_argument(
        "--center",
        default="39.8283,-98.5795",
        help="Initial map center 'lat,lon' (default: USA centroid).",
    )
    parser.add_argument("--zoom", type=float, default=4.0)
    parser.add_argument(
        "--include",
        action="append",
        default=None,
        help=(
            "Only include these layer names (repeatable). Default: every "
            "cached layer from the standard pipeline."
        ),
    )
    parser.add_argument(
        "--basemap-url",
        default=map_render.DEFAULT_BASEMAP_URL,
        help=(
            "Raster tile URL template (supports {z}/{x}/{y} and optional {s} "
            "for subdomain). Default: CARTO Voyager, which is free, no-key, "
            "and works from file:// — unlike tile.openstreetmap.org, which "
            "blocks requests without a Referer header."
        ),
    )
    parser.add_argument(
        "--basemap-attribution",
        default=map_render.DEFAULT_BASEMAP_ATTRIBUTION,
        help="Attribution HTML to display. Default cites OpenStreetMap + CARTO.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    center = _parse_center(args.center)
    storage = LocalStorage()

    # Pick the layers with actual cached data present. Users with only
    # OpenLitterMap cached shouldn't be forced to download every EPA
    # dataset to render a map.
    candidates = DEFAULT_LAYERS + _openlittermap_layers(storage)
    present: list[map_render.LayerSpec] = []
    for layer in candidates:
        if args.include and layer.name not in args.include:
            continue
        geojson_path = sidecar.cache_path(
            map_render.NAMESPACE,
            layer.source_cache_type,
            layer.source_relative_path,
            storage,
        )
        if os.path.exists(geojson_path):
            present.append(layer)
        else:
            logging.info("skipping layer %s — no cache at %s", layer.name, geojson_path)

    if not present:
        print(
            "error: no cached layers found. Run download-openlittermap.sh and/or "
            "download-epa-cleanups.sh first.",
            file=sys.stderr,
        )
        return 1

    try:
        bundle = map_render.render_map(
            region_key=args.region,
            layers=present,
            center=center,
            zoom=args.zoom,
            output_dir=args.output_dir,
            storage=storage,
            basemap_url=args.basemap_url,
            basemap_attribution=args.basemap_attribution,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    counts_str = ", ".join(f"{name}={count:,}" for name, count in bundle.layer_counts.items())
    print(f"[map] {bundle.html_path}\n      layers: {counts_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
