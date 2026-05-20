"""MapLibre HTML renderer — inline-GeoJSON, multi-layer map.

Reads the cached GeoJSON artifacts for each configured source and emits
a self-contained MapLibre GL JS page with per-source layer toggles,
click popups that surface the source's ``properties``, and a permissive
raster basemap.

Output::

    cache/save-earth/maps/<region>/index.html              (+ .meta.json sibling)

Basemap: CARTO Voyager raster tiles by default — free, no API key,
works from ``file://`` origins (OSM's direct tile server rejects
no-Referer requests per its volunteer-tile usage policy, so opening
the HTML locally against osm.org 403s). Callers can swap via
``basemap_url`` / ``basemap_attribution`` on :func:`render_map` if
they have their own tile provider.

For the first version we inline every source's GeoJSON directly into
the HTML as JS constants. This avoids needing tippecanoe / PMTiles or
a static file server, at the cost of bigger HTML when datasets get
large. For the current 2-source mix (OpenLitterMap + EPA) it's fine;
we can swap to PMTiles-backed sources later without changing the
outer API.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _lib import sidecar  # noqa: E402
from _lib.storage import LocalStorage, Storage  # noqa: E402

NAMESPACE = "save-earth"
CACHE_TYPE = "maps"

# Default basemap — CARTO Voyager. Free, no key required, permissive
# terms (attribute CARTO + OSM). Unlike tile.openstreetmap.org, CARTO
# does not require a Referer header, so the generated HTML opens
# correctly from file:// without tripping the OSM tile usage policy.
DEFAULT_BASEMAP_URL = (
    "https://cartodb-basemaps-{s}.global.ssl.fastly.net/rastertiles/voyager/{z}/{x}/{y}.png"
)
DEFAULT_BASEMAP_SUBDOMAINS = ["a", "b", "c", "d"]
DEFAULT_BASEMAP_ATTRIBUTION = (
    '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    'contributors © <a href="https://carto.com/attributions">CARTO</a>'
)


@dataclass
class LayerSpec:
    """One point/polygon source to render on the map."""

    name: str  # short id — lowercase slug ("openlittermap")
    title: str  # human-readable title ("Litter observations")
    source_cache_type: str  # cache_type inside NAMESPACE
    source_relative_path: str  # relative path of the cached GeoJSON
    color: str  # CSS colour for the circles
    radius: int = 5  # circle radius in px
    description_fields: list[str] | None = None  # property names to show in popups


@dataclass
class MapBundle:
    output_dir: Path
    html_path: Path
    layer_counts: dict[str, int]  # layer name → feature count included
    region_key: str


def render_map(
    *,
    region_key: str,
    layers: list[LayerSpec],
    center: tuple[float, float] = (39.8283, -98.5795),  # roughly USA centroid
    zoom: float = 4.0,
    output_dir: Path | None = None,
    storage: Storage | None = None,
    basemap_url: str = DEFAULT_BASEMAP_URL,
    basemap_attribution: str = DEFAULT_BASEMAP_ATTRIBUTION,
) -> MapBundle:
    """Stitch cached GeoJSON from each LayerSpec into a single HTML map.

    The ``region_key`` is the on-disk sub-directory name (e.g. ``us``,
    ``europe__germany``). It only affects the output path, not the
    data actually included — that is governed by the LayerSpec's
    ``source_relative_path``.
    """
    s = storage or LocalStorage()

    loaded_layers: list[tuple[LayerSpec, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for layer in layers:
        geojson_path = sidecar.cache_path(
            NAMESPACE, layer.source_cache_type, layer.source_relative_path, s
        )
        if not os.path.exists(geojson_path):
            raise FileNotFoundError(
                f"layer {layer.name!r} expects cached GeoJSON at {geojson_path} — "
                f"run the matching download-* tool first"
            )
        with open(geojson_path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("type") != "FeatureCollection":
            raise ValueError(f"{geojson_path} is not a FeatureCollection — aborting")
        loaded_layers.append((layer, data))
        counts[layer.name] = len(data.get("features") or [])

    html = _render_html(
        region_key,
        loaded_layers,
        center=center,
        zoom=zoom,
        basemap_url=basemap_url,
        basemap_attribution=basemap_attribution,
    )

    out_dir = _resolve_output_dir(region_key, output_dir=output_dir, storage=s)
    html_path = out_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")

    body_bytes = html.encode("utf-8")
    rel = f"{region_key}/index.html"
    sidecar.write_sidecar(
        NAMESPACE,
        CACHE_TYPE,
        rel,
        kind="file",
        size_bytes=len(body_bytes),
        sha256=hashlib.sha256(body_bytes).hexdigest(),
        tool={"name": "map_render", "version": "1.0"},
        extra={
            "region_key": region_key,
            "layer_counts": counts,
            "layers": [_layer_meta(layer) for layer, _ in loaded_layers],
        },
        storage=s,
    )

    return MapBundle(
        output_dir=out_dir,
        html_path=html_path,
        layer_counts=counts,
        region_key=region_key,
    )


def _resolve_output_dir(
    region_key: str,
    *,
    output_dir: Path | None,
    storage: Storage,
) -> Path:
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir
    abs_dir = sidecar.cache_path(NAMESPACE, CACHE_TYPE, region_key, storage)
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    return Path(abs_dir)


def _layer_meta(layer: LayerSpec) -> dict[str, Any]:
    return {
        "name": layer.name,
        "title": layer.title,
        "source_cache_type": layer.source_cache_type,
        "source_relative_path": layer.source_relative_path,
        "color": layer.color,
    }


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------


def _render_html(
    region_key: str,
    loaded_layers: list[tuple[LayerSpec, dict[str, Any]]],
    *,
    center: tuple[float, float],
    zoom: float,
    basemap_url: str,
    basemap_attribution: str,
) -> str:
    # Expand {s} → list of subdomains MapLibre understands. CARTO's
    # default URL uses {s} but Fastly's subdomains are a/b/c/d.
    if "{s}" in basemap_url:
        tile_urls_js = json.dumps(
            [basemap_url.replace("{s}", d) for d in DEFAULT_BASEMAP_SUBDOMAINS]
        )
    else:
        tile_urls_js = json.dumps([basemap_url])
    # Inlined GeoJSON as a single JS dict keyed by layer id. Putting each
    # layer's FeatureCollection in its own top-level `const` wouldn't be
    # visible on `window` in modern JS (block-scoped), so the map's
    # `map.addSource(..., { data })` call would see undefined and
    # MapLibre would 422 the source. One dict sidesteps that.
    layer_data_map = {}
    for layer, data in loaded_layers:
        # Cap at 50k features per layer — callers who need more should
        # upgrade to PMTiles.
        features = (data.get("features") or [])[:50_000]
        layer_data_map[_safe_js_id(layer.name)] = {
            "type": "FeatureCollection",
            "features": features,
        }
    inline_data = "const LAYER_DATA = " + json.dumps(layer_data_map, separators=(",", ":")) + ";"

    layer_specs_js = json.dumps(
        [
            {
                "id": _safe_js_id(layer.name),
                "name": layer.name,
                "title": layer.title,
                "color": layer.color,
                "radius": layer.radius,
                "description_fields": layer.description_fields or [],
                "feature_count": len(data.get("features") or []),
            }
            for layer, data in loaded_layers
        ],
        indent=2,
    )

    center_lat, center_lon = center
    title = html_mod.escape(f"save-earth map — {region_key}")
    style = textwrap.dedent(
        """\
        html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system,
          BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        #map { position: absolute; top: 0; bottom: 0; left: 0; right: 0; }
        .panel {
          position: absolute; top: 10px; right: 10px; z-index: 5;
          background: rgba(255,255,255,0.95); border-radius: 6px;
          padding: 10px 14px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          max-width: 260px; font-size: 13px;
        }
        .panel h3 { margin: 0 0 6px; font-size: 14px; }
        .panel label { display: block; margin: 4px 0; cursor: pointer; }
        .panel .swatch {
          display: inline-block; width: 10px; height: 10px; border-radius: 50%;
          vertical-align: middle; margin-right: 6px;
        }
        .maplibregl-popup-content { max-width: 320px; font-size: 12px; }
        .maplibregl-popup-content h4 { margin: 0 0 4px; font-size: 13px; }
        .maplibregl-popup-content dl { margin: 4px 0 0; }
        .maplibregl-popup-content dt { font-weight: 600; margin-top: 4px; color: #555; }
        .maplibregl-popup-content dd { margin-left: 0; margin-bottom: 2px; }
        """
    )

    script = textwrap.dedent(
        f"""\
        const LAYER_SPECS = {layer_specs_js};

        const BASEMAP_TILES = {tile_urls_js};
        const BASEMAP_ATTRIBUTION = {json.dumps(basemap_attribution)};

        const map = new maplibregl.Map({{
          container: 'map',
          style: {{
            version: 8,
            sources: {{
              basemap: {{
                type: 'raster',
                tiles: BASEMAP_TILES,
                tileSize: 256,
                attribution: BASEMAP_ATTRIBUTION
              }}
            }},
            layers: [{{ id: 'basemap', type: 'raster', source: 'basemap' }}]
          }},
          center: [{center_lon}, {center_lat}],
          zoom: {zoom},
          hash: true
        }});

        // Normal paint: radius stays at spec.radius; clusters with
        // point_count auto-scale a little so dense areas read bigger.
        function normalRadius(spec) {{
          return [
            'case',
            ['has', 'point_count'],
            [
              'interpolate', ['linear'], ['get', 'point_count'],
              1, spec.radius,
              10, spec.radius + 3,
              100, spec.radius + 7,
              1000, spec.radius + 12
            ],
            spec.radius
          ];
        }}
        // "Big dots" paint: inflate everything so it's obvious on a world map.
        function bigRadius(spec) {{
          const r = Math.max(spec.radius * 2.5, 14);
          return [
            'case',
            ['has', 'point_count'],
            [
              'interpolate', ['linear'], ['get', 'point_count'],
              1, r,
              10, r + 4,
              100, r + 10,
              1000, r + 16
            ],
            r
          ];
        }}

        let bigDotsOn = false;

        map.on('load', () => {{
          for (const spec of LAYER_SPECS) {{
            const data = LAYER_DATA[spec.id];
            if (!data) {{
              console.warn('save-earth: missing inlined data for', spec.id);
              continue;
            }}
            map.addSource(spec.id, {{ type: 'geojson', data }});
            map.addLayer({{
              id: spec.id,
              type: 'circle',
              source: spec.id,
              paint: {{
                'circle-radius': normalRadius(spec),
                'circle-color': spec.color,
                'circle-stroke-width': 1.5,
                'circle-stroke-color': '#fff',
                'circle-opacity': 0.9
              }}
            }});
            map.on('click', spec.id, (e) => {{
              const props = e.features[0].properties || {{}};
              const fields = spec.description_fields.length
                ? spec.description_fields
                : Object.keys(props);
              const rows = fields
                .filter(k => props[k] !== undefined && props[k] !== null && props[k] !== '')
                .map(k => `<dt>${{k}}</dt><dd>${{String(props[k])
                    .replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</dd>`)
                .join('');
              const title = props.primary_name || props.SITE_NAME
                || props.NAME || props.FACILITY_NAME
                || props.title || props.description || spec.title;
              new maplibregl.Popup({{ closeButton: true }})
                .setLngLat(e.lngLat)
                .setHTML(`<h4>${{title}}</h4><dl>${{rows}}</dl>`)
                .addTo(map);
            }});
            map.on('mouseenter', spec.id, () => map.getCanvas().style.cursor = 'pointer');
            map.on('mouseleave', spec.id, () => map.getCanvas().style.cursor = '');
          }}

          function applyRadius() {{
            for (const spec of LAYER_SPECS) {{
              map.setPaintProperty(
                spec.id,
                'circle-radius',
                bigDotsOn ? bigRadius(spec) : normalRadius(spec)
              );
            }}
          }}

          const panel = document.getElementById('panel');

          // "Big dots" global toggle — applies to every layer, overrides radius.
          const bigLabel = document.createElement('label');
          bigLabel.style.borderBottom = '1px solid #ddd';
          bigLabel.style.paddingBottom = '6px';
          bigLabel.style.marginBottom = '4px';
          bigLabel.style.fontWeight = '600';
          const bigCb = document.createElement('input');
          bigCb.type = 'checkbox';
          bigCb.id = 'big-dots';
          bigCb.addEventListener('change', () => {{
            bigDotsOn = bigCb.checked;
            applyRadius();
          }});
          bigLabel.appendChild(bigCb);
          bigLabel.appendChild(document.createTextNode(' Big dots (any zoom)'));
          panel.appendChild(bigLabel);

          // Per-layer visibility toggles.
          for (const spec of LAYER_SPECS) {{
            const label = document.createElement('label');
            const cb = document.createElement('input');
            cb.type = 'checkbox'; cb.checked = true;
            cb.addEventListener('change', () => {{
              map.setLayoutProperty(
                spec.id, 'visibility', cb.checked ? 'visible' : 'none'
              );
            }});
            const swatch = document.createElement('span');
            swatch.className = 'swatch';
            swatch.style.background = spec.color;
            label.appendChild(cb);
            label.appendChild(swatch);
            label.appendChild(document.createTextNode(
              `${{spec.title}} (${{spec.feature_count.toLocaleString()}})`
            ));
            panel.appendChild(label);
          }}
        }});
        """
    )

    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>{title}</title>
          <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
          <style>{style}</style>
        </head>
        <body>
        <div id="map"></div>
        <div class="panel" id="panel">
          <h3>Layers</h3>
        </div>
        <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
        <script>
        {inline_data}
        </script>
        <script>
        {script}
        </script>
        </body>
        </html>
        """
    )


def _safe_js_id(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
