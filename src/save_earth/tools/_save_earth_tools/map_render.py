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

from _save_earth_tools import sidecar  # noqa: E402
from _save_earth_tools.storage import Storage, get_storage  # noqa: E402

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
    color: str  # CSS colour for the circles / lines
    radius: int = 5  # circle radius in px
    description_fields: list[str] | None = None  # property names to show in popups
    geometry: str = "circle"  # "circle" pts | "line" | "fill" (polygons) | "heatmap" (density)
    weight: float = 1.5  # line width in px (geometry="line")
    magnitude_field: str = ""  # circle layer: scale radius + colour by this numeric
    #                            property (e.g. earthquake "mag") instead of a flat dot
    filter_field: str = ""  # render only features where properties[filter_field]==filter_value.
    filter_value: str = ""  # Lets several LayerSpecs (e.g. a vendor split) share ONE cached
    #                         file — the GeoJSON is inlined once and each layer filters it,
    #                         instead of duplicating the features per layer.


@dataclass
class MapBundle:
    output_dir: str  # may be a local path or an s3:// URI
    html_path: str  # may be a local path or an s3:// URI
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
    attribution_workflow: str = "",
    attribution_ffl_url: str = "",
    description: str = "",
    max_inline_features: int = 50_000,
) -> MapBundle:
    """Stitch cached GeoJSON from each LayerSpec into a single HTML map.

    The ``region_key`` is the on-disk sub-directory name (e.g. ``us``,
    ``europe__germany``). It only affects the output path, not the
    data actually included — that is governed by the LayerSpec's
    ``source_relative_path``.
    """
    s = storage or get_storage()

    loaded_layers: list[tuple[LayerSpec, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for layer in layers:
        geojson_path = sidecar.cache_path(
            NAMESPACE, layer.source_cache_type, layer.source_relative_path, s
        )
        if not s.exists(geojson_path):
            raise FileNotFoundError(
                f"layer {layer.name!r} expects cached GeoJSON at {geojson_path} — "
                f"run the matching download-* tool first"
            )
        # read_text resolves local paths and s3:// URIs uniformly
        data = json.loads(s.read_text(geojson_path))
        if data.get("type") != "FeatureCollection":
            raise ValueError(f"{geojson_path} is not a FeatureCollection — aborting")
        loaded_layers.append((layer, data))
        # Filtered count when the layer renders only part of a shared source.
        feats = data.get("features") or []
        counts[layer.name] = (
            len(feats)
            if not layer.filter_field
            else sum(
                1
                for f in feats
                if str((f.get("properties") or {}).get(layer.filter_field, "")) == layer.filter_value
            )
        )

    html = _render_html(
        region_key,
        loaded_layers,
        center=center,
        zoom=zoom,
        basemap_url=basemap_url,
        basemap_attribution=basemap_attribution,
        attribution_workflow=attribution_workflow,
        attribution_ffl_url=attribution_ffl_url,
        description=description,
        max_inline_features=max_inline_features,
    )

    out_dir = _resolve_output_dir(region_key, output_dir=output_dir, storage=s)
    # write_text_atomic finalizes to local disk or the s3 object store
    # depending on the active backend (no Path() on an s3:// URI).
    html_path = s.join(out_dir, "index.html")
    s.write_text_atomic(html_path, html)

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
) -> str:
    """Return the output directory as a string (a local path or s3:// URI).

    ``storage.mkdir_p`` makes parent dirs on local/hdfs and is a no-op on s3
    (key prefixes are implicit), so this never does ``Path()`` on an s3 URI.
    """
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return str(output_dir)
    abs_dir = sidecar.cache_path(NAMESPACE, CACHE_TYPE, region_key, storage)
    storage.mkdir_p(abs_dir)
    return abs_dir


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
    attribution_workflow: str = "",
    attribution_ffl_url: str = "",
    description: str = "",
    max_inline_features: int = 50_000,
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
    def _src_key(layer: LayerSpec) -> str:
        return _safe_js_id(f"{layer.source_cache_type}__{layer.source_relative_path}")

    def _matches(feat: dict[str, Any], layer: LayerSpec) -> bool:
        if not layer.filter_field:
            return True
        return str((feat.get("properties") or {}).get(layer.filter_field, "")) == layer.filter_value

    def _count(layer: LayerSpec, data: dict[str, Any]) -> int:
        feats = data.get("features") or []
        return len(feats) if not layer.filter_field else sum(1 for f in feats if _matches(f, layer))

    # Inline each UNIQUE source's FeatureCollection ONCE (keyed by source, not by
    # layer), so several LayerSpecs that share a cached file — a vendor split +
    # a heatmap over the same points — don't duplicate ~N features per layer.
    # Cap per source (callers who need more pass a higher max_inline_features).
    layer_data_map: dict[str, Any] = {}
    for layer, data in loaded_layers:
        sk = _src_key(layer)
        if sk not in layer_data_map:
            features = (data.get("features") or [])[:max_inline_features]
            layer_data_map[sk] = {"type": "FeatureCollection", "features": features}
    inline_data = "const LAYER_DATA = " + json.dumps(layer_data_map, separators=(",", ":")) + ";"

    layer_specs_js = json.dumps(
        [
            {
                "id": _safe_js_id(layer.name),
                "source_id": _src_key(layer),
                "name": layer.name,
                "title": layer.title,
                "color": layer.color,
                "radius": layer.radius,
                "description_fields": layer.description_fields or [],
                "feature_count": _count(layer, data),
                "geometry": layer.geometry,
                "weight": layer.weight,
                "magnitude_field": layer.magnitude_field,
                "filter_field": layer.filter_field,
                "filter_value": layer.filter_value,
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
        .info {
          position: absolute; top: 10px; left: 10px; z-index: 5;
          background: rgba(255,255,255,0.95); border-radius: 6px;
          padding: 10px 14px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          max-width: 300px; font-size: 12px; line-height: 1.4;
        }
        .info h3 { margin: 0 0 5px; font-size: 14px; }
        .info p { margin: 0; color: #444; }
        .panel label { display: block; margin: 4px 0; cursor: pointer; }
        .panel .swatch {
          display: inline-block; width: 10px; height: 10px; border-radius: 50%;
          vertical-align: middle; margin-right: 6px;
        }
        .ptlegend {
          position: absolute; bottom: 10px; right: 10px; z-index: 5;
          background: rgba(255,255,255,0.95); border-radius: 6px;
          padding: 8px 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.2); font-size: 12px;
        }
        .ptlegend b { display: block; margin-bottom: 4px; font-size: 13px; }
        .ptlegend .row { display: flex; align-items: center; margin: 2px 0; }
        .ptlegend .dot {
          display: inline-block; width: 12px; height: 12px; border-radius: 50%;
          margin-right: 7px; border: 1.5px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,0.25);
        }
        .search { position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
          z-index: 6; width: 290px; max-width: 70%; }
        .search input { width: 100%; box-sizing: border-box; padding: 7px 11px;
          border: 1px solid #aaa; border-radius: 6px; font-size: 13px;
          box-shadow: 0 2px 6px rgba(0,0,0,0.2); }
        .search .results { background: #fff; border-radius: 0 0 6px 6px;
          box-shadow: 0 2px 6px rgba(0,0,0,0.2); max-height: 240px; overflow: auto; }
        .search .results div { padding: 6px 11px; cursor: pointer; font-size: 12px;
          border-top: 1px solid #f0f0f0; }
        .search .results div:hover { background: #f3f3f3; }
        .search .results .sub { color: #888; }
        .maplibregl-popup-content { max-width: 340px; font-size: 12px; }
        .maplibregl-popup-content h4 { margin: 0 0 4px; font-size: 13px; }
        table.attrs { border-collapse: collapse; margin-top: 4px; }
        table.attrs td { padding: 1px 8px 1px 0; vertical-align: top; }
        table.attrs td.k { font-weight: 600; color: #555; white-space: nowrap; }
        .attribution {
          position: absolute; bottom: 10px; left: 10px; z-index: 5;
          background: rgba(255,255,255,0.92); border-radius: 6px;
          padding: 6px 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.2);
          font-size: 11px; color: #444; max-width: 360px;
        }
        .attribution a { color: #1565c0; text-decoration: none; }
        .attribution a:hover { text-decoration: underline; }
        .attribution code { background: #f0f0f0; padding: 0 3px; border-radius: 3px; }
        .infobtn { margin-top: 8px; width: 100%; padding: 7px; font-size: 13px;
          cursor: pointer; background: #fff8e1; border: 1px solid #f6c343;
          border-radius: 4px; color: #5d4b00; }
        .infobtn:hover { background: #fff2c4; }
        .modal { position: absolute; inset: 0; z-index: 10;
          background: rgba(0,0,0,.45); display: flex; align-items: center;
          justify-content: center; }
        .modalcard { background: #fff; max-width: 460px; margin: 16px;
          padding: 18px 20px 20px; border-radius: 10px;
          box-shadow: 0 4px 24px rgba(0,0,0,.4); position: relative;
          font: 13px/1.5 system-ui, sans-serif; max-height: 80vh; overflow: auto; }
        .modalcard h2 { margin: 0 0 8px; font-size: 16px; }
        .modalbody { color: #333; }
        .modalbody b { color: #111; }
        .modalbody p { margin: 0 0 10px; }
        .modalbody a { color: #1565c0; text-decoration: none; }
        .modalbody a:hover { text-decoration: underline; }
        .modalbody code { background: #f0f0f0; padding: 0 3px; border-radius: 3px; }
        .modalclose { position: absolute; top: 6px; right: 10px; border: none;
          background: none; font-size: 24px; line-height: 1; cursor: pointer;
          color: #999; }
        .modalclose:hover { color: #444; }
        """
    )

    # Provenance footer: which Facetwork workflow produced this map + a link to
    # its FFL source. Falls back to a generic Facetwork credit when the caller
    # passes no workflow identity.
    from datetime import UTC, datetime
    _ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if attribution_workflow:
        wf = html_mod.escape(attribution_workflow)
        if attribution_ffl_url:
            repo_url = attribution_ffl_url.split("/blob/")[0]
            ffl_link = (
                f' &middot; <a href="{html_mod.escape(attribution_ffl_url)}" '
                f'target="_blank" rel="noopener">view FFL</a>'
                f' &middot; <a href="{html_mod.escape(repo_url)}" '
                f'target="_blank" rel="noopener">source repo</a>'
            )
        else:
            ffl_link = ""
        attribution_inner = (
            f"Generated by Facetwork workflow <code>{wf}</code>{ffl_link} "
            f"&middot; generated {_ts}"
        )
    else:
        attribution_inner = (
            'Generated with <a href="https://github.com/rlemke/facetwork" '
            f'target="_blank" rel="noopener">Facetwork</a> &middot; generated {_ts}'
        )
    attribution_html = f'<div class="attribution">{attribution_inner}</div>'

    # Top-left description box explaining what the map shows.
    info_html = (
        f'<div class="info"><p>{html_mod.escape(description)}</p></div>' if description else ""
    )

    # "About this data" modal — assembled from this map's OWN description/notes
    # and its source/provenance credit (same identity shown in the footer), so
    # each map's popup is specific to it. Matches the rest of the gallery.
    about_bits: list[str] = []
    if description:
        about_bits.append(f"<p>{html_mod.escape(description)}</p>")
    about_bits.append(f"<p><b>Source &amp; provenance:</b><br>{attribution_inner}</p>")
    about_bits.append(
        f'<p style="font-size:11px;color:#777">Basemap: {basemap_attribution}</p>'
    )
    about_html = "".join(about_bits)

    # Bottom-right legend: a coloured dot per layer with its feature count.
    legend_rows = "".join(
        f'<div class="row"><span class="dot" style="background:{html_mod.escape(layer.color)}">'
        f"</span>{html_mod.escape(layer.title)} ({_count(layer, data):,})</div>"
        for layer, data in loaded_layers
    )
    ptlegend_html = f'<div class="ptlegend"><b>Legend</b>{legend_rows}</div>' if loaded_layers else ""

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
        // Order property keys so a name-like field is listed first in popups.
        function nameFirst(fields) {{
          const NK = ['name','primary_name','official_name','NAME','SITE_NAME',
                      'FACILITY_NAME','title'];
          const lead = NK.filter(k => fields.includes(k));
          return [...lead, ...fields.filter(k => !lead.includes(k))];
        }}

        // Earthquake styling: scale circle radius + colour by magnitude when a
        // layer declares a magnitude_field; otherwise a flat dot.
        function magExpr(spec, lo, mid, hi) {{
          const m = ['coalesce', ['to-number', ['get', spec.magnitude_field]], 0];
          return ['interpolate', ['linear'], m, 4, lo, 6, mid, 8, hi];
        }}
        function radiusExpr(spec) {{
          return spec.magnitude_field ? magExpr(spec, 3, 9, 22) : normalRadius(spec);
        }}
        function colorExpr(spec) {{
          if (!spec.magnitude_field) return spec.color;
          const m = ['coalesce', ['to-number', ['get', spec.magnitude_field]], 0];
          return ['interpolate', ['linear'], m,
            4, '#fee08b', 5, '#fdae61', 6, '#f46d43', 7, '#d73027', 8, '#a50026'];
        }}

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

        // MapLibre '==' filter for a spec that renders only part of a shared source.
        function filterOf(spec) {{
          return spec.filter_field
            ? ['==', ['to-string', ['get', spec.filter_field]], spec.filter_value]
            : null;
        }}

        map.on('load', () => {{
          // Each unique inlined source is added ONCE; several layers (a vendor
          // split + a heatmap over the same points) then reference it.
          for (const sid of Object.keys(LAYER_DATA)) {{
            if (!map.getSource(sid)) map.addSource(sid, {{ type: 'geojson', data: LAYER_DATA[sid] }});
          }}
          for (const spec of LAYER_SPECS) {{
            if (!LAYER_DATA[spec.source_id]) {{
              console.warn('save-earth: missing inlined data for', spec.source_id);
              continue;
            }}
            const flt = filterOf(spec);
            if (spec.geometry === 'heatmap') {{
              const lyr = {{
                id: spec.id, type: 'heatmap', source: spec.source_id,
                paint: {{
                  'heatmap-weight': 1,
                  'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 1, 9, 3],
                  'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 2, 6, 12, 12, 30],
                  'heatmap-opacity': 0.85,
                  'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
                    0, 'rgba(0,0,255,0)', 0.2, '#4dabf7', 0.4, '#69db7c',
                    0.6, '#ffd43b', 0.8, '#ff922b', 1, '#f03e3e']
                }}
              }};
              if (flt) lyr.filter = flt;
              map.addLayer(lyr);
              continue;  // a density heatmap has no per-feature popup
            }} else if (spec.geometry === 'fill') {{
              const lyr = {{
                id: spec.id, type: 'fill', source: spec.source_id,
                paint: {{
                  'fill-color': spec.color,
                  'fill-opacity': 0.35,
                  'fill-outline-color': spec.color
                }}
              }};
              if (flt) lyr.filter = flt;
              map.addLayer(lyr);
            }} else if (spec.geometry === 'line') {{
              const lyr = {{
                id: spec.id, type: 'line', source: spec.source_id,
                layout: {{ 'line-join': 'round', 'line-cap': 'round' }},
                paint: {{
                  'line-color': spec.color,
                  'line-width': spec.weight || 1.5,
                  'line-opacity': 0.85
                }}
              }};
              if (flt) lyr.filter = flt;
              map.addLayer(lyr);
            }} else {{
              const lyr = {{
                id: spec.id, type: 'circle', source: spec.source_id,
                paint: {{
                  'circle-radius': radiusExpr(spec),
                  'circle-color': colorExpr(spec),
                  'circle-stroke-width': 1,
                  'circle-stroke-color': '#fff',
                  'circle-opacity': 0.85
                }}
              }};
              if (flt) lyr.filter = flt;
              map.addLayer(lyr);
            }}
            map.on('click', spec.id, (e) => {{
              const props = e.features[0].properties || {{}};
              const fields = nameFirst(spec.description_fields.length
                ? spec.description_fields
                : Object.keys(props));
              const rows = fields
                .filter(k => props[k] !== undefined && props[k] !== null && props[k] !== '')
                .map(k => `<tr><td class="k">${{k}}</td><td>${{String(props[k])
                    .replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</td></tr>`)
                .join('');
              const title = props.name || props.primary_name || props.SITE_NAME
                || props.NAME || props.Name || props.FACILITY_NAME
                || props.title || props.place || props.description || spec.title;
              new maplibregl.Popup({{ closeButton: true }})
                .setLngLat(e.lngLat)
                .setHTML(`<h4>${{title}}</h4><table class="attrs">${{rows}}</table>`)
                .addTo(map);
            }});
            map.on('mouseenter', spec.id, () => map.getCanvas().style.cursor = 'pointer');
            map.on('mouseleave', spec.id, () => map.getCanvas().style.cursor = '');
          }}

          function applyRadius() {{
            for (const spec of LAYER_SPECS) {{
              if (spec.geometry !== 'circle') continue;  // radius only applies to circle layers
              map.setPaintProperty(
                spec.id,
                'circle-radius',
                bigDotsOn ? bigRadius(spec) : radiusExpr(spec)
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

          // "About this data" button — appended under the toggles so it reads
          // as the panel footer; opens the on-load modal (matches the gallery).
          const _ib = document.createElement('button');
          _ib.id = 'infobtn';
          _ib.className = 'infobtn';
          _ib.innerHTML = '&#8505;&#65039; About this data';
          panel.appendChild(_ib);
          const _im = document.getElementById('infomodal');
          if (_im) {{
            const _ic = document.getElementById('infoclose');
            _ib.onclick = () => {{ _im.style.display = 'flex'; }};
            if (_ic) _ic.onclick = () => {{ _im.style.display = 'none'; }};
            _im.onclick = (e) => {{ if (e.target === _im) _im.style.display = 'none'; }};
          }}

          // --- Name search: type to fly to a matching feature + open its popup.
          // Index each unique source once (layers sharing a source would
          // otherwise index the same features several times).
          const sidx = [];
          const _searchedSrc = new Set();
          for (const spec of LAYER_SPECS) {{
            if (_searchedSrc.has(spec.source_id)) continue;
            _searchedSrc.add(spec.source_id);
            const data = LAYER_DATA[spec.source_id]; if (!data) continue;
            for (const f of data.features) {{
              const p = f.properties || {{}};
              const nm = p.name || p.primary_name || p.NAME || p.Name
                || p.FACILITY_NAME || p.title || p.place || '';
              // Lines (faults) have no single coordinate — anchor the popup at the
              // geometry's first vertex so search still flies to it.
              const g = f.geometry || {{}};
              const c = g.type === 'Point' ? g.coordinates
                : (g.type === 'LineString' ? g.coordinates[0]
                : (g.type === 'MultiLineString' ? g.coordinates[0][0] : null));
              if (nm && c) sidx.push({{ name: nm,
                sub: p.amenity || p['addr:city'] || p.operator || p.mag || '',
                coords: c, props: p, spec }});
            }}
          }}
          function popupFor(o) {{
            const fields = nameFirst(o.spec.description_fields.length ? o.spec.description_fields : Object.keys(o.props));
            const rows = fields
              .filter(k => o.props[k] !== undefined && o.props[k] !== null && o.props[k] !== '')
              .map(k => `<tr><td class="k">${{k}}</td><td>${{String(o.props[k])
                  .replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</td></tr>`).join('');
            new maplibregl.Popup({{ closeButton: true }}).setLngLat(o.coords)
              .setHTML(`<h4>${{o.name}}</h4><table class="attrs">${{rows}}</table>`).addTo(map);
          }}
          const sbox = document.getElementById('vsearch'), sres = document.getElementById('vresults');
          sbox.addEventListener('input', () => {{
            const q = sbox.value.trim().toLowerCase(); sres.innerHTML = '';
            if (q.length < 2) return;
            const hits = sidx.filter(x => x.name.toLowerCase().includes(q)).slice(0, 12);
            for (const h of hits) {{
              const d = document.createElement('div');
              d.innerHTML = `${{h.name}}` + (h.sub ? ` <span class="sub">${{h.sub}}</span>` : '');
              d.addEventListener('click', () => {{
                map.flyTo({{ center: h.coords, zoom: 13 }});
                sres.innerHTML = ''; sbox.value = h.name; popupFor(h);
              }});
              sres.appendChild(d);
            }}
          }});
          document.addEventListener('click', (e) => {{
            if (!e.target.closest('.search')) sres.innerHTML = '';
          }});
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
        <div class="search"><input id="vsearch" placeholder="Find by name…" autocomplete="off">
          <div class="results" id="vresults"></div></div>
        {info_html}
        <div class="panel" id="panel">
          <h3>Layers</h3>
        </div>
        {ptlegend_html}
        {attribution_html}
        <div id="infomodal" class="modal"><div class="modalcard">
          <button id="infoclose" class="modalclose">&times;</button>
          <h2>About this data</h2>
          <div class="modalbody">{about_html}</div>
        </div></div>
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
