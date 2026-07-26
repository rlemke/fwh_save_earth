# Map rendering — the MapLibre HTML renderer

**Namespace:** `save_earth.maps` ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (namespace `save_earth.maps`) ·
**Handler:** `src/save_earth/handlers/maps/map_handlers.py` ·
**Renderer:** `src/save_earth/tools/_save_earth_tools/map_render.py` ·
**Tests:** `tests/test_alpr_map_render.py`, `tests/test_seismic_map.py`

## Overview

This is the **flagship** feature: every workflow in the repo ends in one
`save_earth.maps.BuildMap` step. `BuildMap` auto-discovers the cached GeoJSON
layers, reads each one, and stitches them into a **single self-contained MapLibre
GL JS HTML page** — CARTO Voyager basemap (no API key, works from `file://`),
per-layer visibility toggles, a name search, a bottom-right legend with per-layer
feature counts, click popups over a feature's `properties`, and an "About this
data" modal. The HTML inlines every layer's GeoJSON directly as a JS constant, so
the output is one portable file with no tile server, no PMTiles, and no external
data fetch (only the basemap raster tiles + the maplibre-gl CDN asset).

Every source feature (nuclear, ALPR, faults, power, …) is just a `LayerSpec`
pointed at a cached file; the renderer is the shared substrate they all compose
onto. It is where the domain's geometry breadth lives: **point circles, lines,
polygon fills, density heatmaps, and magnitude-scaled circles** all come out of
this one module.

## How it works

`handle_build_map` (`map_handlers.py`) builds a fixed list of candidate
`LayerSpec`s (module-level constants `_NUCLEAR_LAYER`, `_ALPR_LAYERS`,
`_AQUIFER_LAYER`, `_FAULTS_LAYER`, `_EARTHQUAKE_LAYER`, …) plus dynamically
generated families (`_openlittermap_layers`, `_enclave_layers`, `_power_layers`,
`_siting_layers`). Then:

1. **`only_layers` selection.** The `only_layers` param is a comma-separated
   allowlist with a trailing-`*` prefix wildcard, so a workflow renders one focused
   family — `only_layers="enclave-*"`, `"power-*"`, `"faults,earthquakes"` — instead
   of every cached source. Empty = auto-discover everything (the global-map default).
2. **Presence filter.** For each candidate, `sidecar.cache_path(...)` is resolved
   on the active `FW_STORAGE` backend and `storage.exists(...)` drops any layer
   with no cached file (logged, not fatal). A build with **no** present layers
   returns `layer_count: 0` and an empty `html_path` rather than erroring.
3. **Render.** `map_render.render_map(...)` loads each present layer's GeoJSON
   (`storage.read_text`, asserting `type == "FeatureCollection"`), inlines each
   **unique source once** (keyed by `source_cache_type__source_relative_path`, so a
   vendor split + heatmap over the same file don't duplicate features), caps each
   source at `max_inline_features` (default 50 000), emits the HTML via
   `_render_html`, writes it atomically, and writes a `.meta.json` sidecar with the
   per-layer counts.

Data shape: `cached GeoJSON → inlined JS const LAYER_DATA → MapLibre sources →
per-LayerSpec map layers`.

## Fan-out

**Single-task — no fan-out.** `BuildMap` is one render pass over already-cached
layers; the parallelism lives upstream in the source downloads (and, for a couple
of sources, in per-country/per-tile fan-out — see
[semiconductor](semiconductor.md) and [power-transmission](power-transmission.md)).
The map build is sequenced *after* its downloads by the `dependency_signal` param
(callers pass the summed `feature_count`s), which the runtime resolves as a
data-flow edge.

## Data & fields

The renderer is source-agnostic — it filters and styles by the `LayerSpec`, not by
any upstream schema. The `LayerSpec` dataclass (`map_render.py`) carries:

- **`geometry`** — one of `"circle"` (points, default), `"line"`, `"fill"`
  (polygons), `"heatmap"` (density). Each maps to a distinct MapLibre layer type in
  `_render_html` (`circle` / `line` / `fill` / `heatmap`). A heatmap has no
  per-feature popup by design.
- **`magnitude_field`** — a numeric property name (e.g. earthquakes' `mag`,
  siting's `siting_score`). When set, a circle layer scales **both radius and
  colour** by that value via a MapLibre `interpolate` expression (`magExpr` maps
  4→small/`#fee08b` … 8→large/`#a50026`); otherwise a flat dot at `radius`.
- **`filter_field` / `filter_value`** — render only features where
  `properties[filter_field] == filter_value`. Lets several `LayerSpec`s share ONE
  cached file: the ALPR vendor split (`camera_vendor` = flock/motorola/other) and
  the nuclear-sites split (`site_type` = test_site/missile_silo) inline the source
  once and each layer filters it.
- **`description_fields`** — the popup field allowlist. **`None` → the popup shows
  every `properties` key** (name-like keys first, via `nameFirst`), which is how the
  "show all available information" requirement is met for the OSM sources.
- **`color`, `radius`, `weight`** — styling (circle radius px, line width px).

Popup titles fall back through `name` / `primary_name` / `SITE_NAME` / `NAME` /
`FACILITY_NAME` / `place` / the layer title. The name search indexes each unique
source once and anchors line features (faults) at their first vertex.

## External libraries / binaries

- **Python:** stdlib only (`json`, `hashlib`, `html`, `textwrap`, `dataclasses`) —
  no `requests` here; the renderer only reads already-cached files.
- **Browser (runtime, via CDN in the emitted HTML):** `maplibre-gl@4.7.1` JS + CSS
  from unpkg, and CARTO Voyager raster tiles. These are the only external fetches
  the page makes. No osmium/shapely/GDAL anywhere in this repo.

## Facets & workflows

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `save_earth.maps.BuildMap(region, center_lat, center_lon, zoom, basemap_url, basemap_attribution, dependency_signal, only_layers, attribution_workflow, attribution_ffl_url, description, max_inline_features)` | event | io / cheap | Auto-discover every cached layer (or the `only_layers` subset) and render one MapLibre HTML bundle. |

Returns `MapBundle` (`region_key, output_dir, html_path, layer_count,
layer_counts: Json`). Provenance: `attribution_workflow` + `attribution_ffl_url`
render a "Generated by Facetwork workflow … view FFL … source repo" footer and
feed the "About this data" modal; `description` populates both the top-left info
box and the modal.

## Cache / output

- **Cache namespace / type:** `save-earth` / `maps`. Output is written to
  `$FW_CACHE_ROOT/save-earth/maps/<region_key>/index.html` with a sibling
  `<region_key>/index.html.meta.json` sidecar (recording size, sha256, per-layer
  counts, and the layer manifest).
- **Format:** a single self-contained HTML file (GeoJSON inlined as JS).
- **Backend:** governed by `FW_STORAGE` (`local` / `hdfs` / `s3`). On the fleet
  MinIO backend the HTML lands at `s3://afl-cache/save-earth/maps/<region>/index.html`;
  `render_map` stages locally and finalizes onto the active backend, so no shared
  filesystem is needed. See [cache-and-storage](cache-and-storage.md).

## Gotchas & notes

- **OpenLitterMap auto-discovery is a no-op on object storage.**
  `_openlittermap_layers` uses `os.path.isdir`, which is False for an `s3://`
  prefix, so OLM layers only enumerate on `local`. The enclave/power/siting layer
  families instead iterate a fixed constant and use `storage.exists`, so they work
  identically on local and s3.
- **Per-source inline cap.** `max_inline_features` defaults to 50 000 per *source*.
  A dense single-source map (e.g. ~120k ALPR cameras behind the vendor split +
  heatmap) must pass a higher cap — `BuildALPRCameraMap` sets `max_inline_features
  = 130000`. Beyond that features are silently truncated (the design note in
  `map_render.py` flags PMTiles as the future path for very large sets).
- **Layer draw order is candidate-list order.** Fills/lines are listed before the
  points that should draw on top (aquifers before data centers; faults before
  earthquakes) — reordering the candidate list changes z-order.
- **`type != "FeatureCollection"` aborts the whole render** (`ValueError`), so a
  corrupt cached layer fails the build rather than silently dropping.

## Related specs

- [workflows](workflows.md) — the workflow layer that sequences downloads → `BuildMap`.
- [seismic](seismic.md) — the line (faults) + magnitude-circle (earthquakes) exemplar.
- [osm-overpass-sources](osm-overpass-sources.md) — the point-source family, incl.
  the ALPR heatmap + vendor split and the nuclear-sites shared-source filter.
- [cache-and-storage](cache-and-storage.md) — the sidecar cache + `FW_STORAGE` backends.
