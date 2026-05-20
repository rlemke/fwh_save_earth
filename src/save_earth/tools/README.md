# Save Earth — Tools

Standalone CLI utilities for fetching and mapping geolocated data from environmental-action sources: crowd-sourced litter observations, authoritative cleanup-site registries, and (in later PRs) tree-equity scores, 311 cleanup requests, coastal-cleanup chapters, and tree-planting projects. All outputs overlay cleanly on an OSM basemap and follow the same cache + handler pattern as the standalone [osm-geocoder](https://github.com/rlemke/fwh_osm) and [noaa-weather](https://github.com/rlemke/fwh_noaa_weather) repos.

Pattern contract: [`agent-spec/tools-pattern.agent-spec.yaml`](../../../agent-spec/tools-pattern.agent-spec.yaml)
Cache layout contract: [`agent-spec/cache-layout.agent-spec.yaml`](../../../agent-spec/cache-layout.agent-spec.yaml)

## Setup

```bash
./tools/install-tools.sh          # installs requests into the repo .venv
```

## Pipeline

```
   ┌──────────────────────────────┐     ┌──────────────────────────────┐
   │  download-openlittermap      │     │  download-epa-cleanups       │
   │  (crowd-sourced litter)      │     │  Superfund / Brownfields /   │
   │                              │     │  RCRA — authoritative sites  │
   └──────────────┬───────────────┘     └──────────────┬───────────────┘
                  │ openlittermap/                     │ epa-cleanups/
                  ▼                                    ▼
            ┌─────────────────────────────────────────────────┐
            │  build-save-earth-map                           │
            │  (MapLibre HTML with per-source layer toggles,  │
            │  OSM basemap, click popups w/ descriptions)     │
            └──────────────────────┬──────────────────────────┘
                                   ▼
                                maps/<region>/index.html
```

Every arrow is sidecar-mediated: each artifact has a sibling `.meta.json` with size, SHA-256, upstream URL, tool + version, and source-specific `extra` (feature count, dataset name, bbox if any).

## Available tools

| Tool | Input | Output cache | Purpose |
|------|-------|--------------|---------|
| `download-openlittermap` | `--mode {clusters,points}`, `--zoom`, `--bbox` | `openlittermap/<mode>-zoom<N>[_<bbox>].geojson` | Fetch crowd-sourced litter observations. Default: global clusters at zoom 4. Individual photos (`--mode points`) require `--zoom>=15` and a bbox (server-enforced). |
| `download-epa-cleanups` | `--dataset {superfund,brownfields}` (repeatable) | `epa-cleanups/<dataset>.geojson` | Fetch EPA authoritative remediation-site data from `geopub.epa.gov/EMEF/efpoints` (auto-paginates past the 10,000-record server cap) |
| `download-tri` | `--include-closed` / `--force` / `--use-mock` | `tri/facilities.geojson` | Fetch the EPA Toxic Release Inventory facility table (~65k facilities) from `data.epa.gov/efservice/`. Paginates past the 10k-row cap; negates longitude for western-hemisphere state codes (the DB stores longitude unsigned). Default filter drops closed facilities. |
| `build-save-earth-map` | `--region`, `--center`, `--zoom` | `maps/<region>/index.html` | Stitch every cached layer into a single MapLibre HTML page |

Every tool supports:
- `--help` — show flags
- `--force` — re-download even if cached (where applicable)
- `--use-mock` — deterministic offline data (no network required)
- `--log-level` — Python logging level (default: INFO)

Defaults are real endpoints; if an upstream URL rotates, pass `--url` and it'll be recorded in the sidecar.

## Data sources

| Source | Shape | Licence | Notes |
|--------|-------|---------|-------|
| **OpenLitterMap** (openlittermap.com) | Points | CC-BY-SA 4.0 | Crowd-sourced, ~1M+ geotagged litter photos globally |
| **EPA Superfund NPL** — `geopub.epa.gov/EMEF/efpoints` layer 0 | Points | US Government public domain | ~1,400 NPL sites |
| **EPA Brownfields (ACRES)** — `geopub.epa.gov/EMEF/efpoints` layer 5 | Points | US Government public domain | ~40,000+ redevelopment sites |
| **EPA TRI** — `data.epa.gov/efservice/TRI_FACILITY` | Points | US Government public domain | ~65,000 Toxic Release Inventory reporters (all time); ~35,000 currently active |

Feature popups preserve the upstream `properties` so every point carries a real name, status, description, and (where available) a link back to the source system.

## Cache layout

All outputs live at `$AFL_CACHE_ROOT/save-earth/` (default: `/Volumes/afl_data/cache/save-earth/`):

```
cache/save-earth/
├── openlittermap/
│   ├── clusters-zoom<N>.geojson + .meta.json
│   └── points-zoom<N>_<bbox>.geojson + .meta.json   (if --mode points used)
├── epa-cleanups/
│   ├── superfund.geojson + .meta.json
│   └── brownfields.geojson + .meta.json
├── tri/
│   └── facilities.geojson + .meta.json   ← EPA TRI facility points
└── maps/
    └── <region>/
        ├── index.html
        └── .meta.json      (sibling of the file, per cache-layout spec)
```

## Typical workflows

**Bootstrap + render a global map (offline mock data, first-run friendly):**

```bash
./tools/install-tools.sh

./tools/download-openlittermap.sh  --use-mock
./tools/download-epa-cleanups.sh   --use-mock
./tools/build-save-earth-map.sh

open "$AFL_CACHE_ROOT/save-earth/maps/global/index.html"
```

**Real data, global overview:**

```bash
./tools/download-openlittermap.sh                    # clusters @ zoom 4
./tools/download-epa-cleanups.sh
./tools/build-save-earth-map.sh
```

**Real data, US-focused:**

```bash
./tools/download-openlittermap.sh --zoom 6           # finer clusters
./tools/download-epa-cleanups.sh
./tools/build-save-earth-map.sh --region us --center 39.8,-98.6 --zoom 4
```

**Neighbourhood-level detail (individual litter photos):**

```bash
# OpenLitterMap /api/points requires zoom >= 15 and a bbox — use '=' because
# bboxes start with '-' (argparse would read it as a flag otherwise).
./tools/download-openlittermap.sh --mode points --zoom 15 \
    --bbox=-74.02,40.70,-73.97,40.75
```

**Only EPA data (no litter layer):**

```bash
./tools/download-epa-cleanups.sh --dataset superfund --dataset brownfields
./tools/build-save-earth-map.sh --include epa-superfund --include epa-brownfields
```

## Map rendering details

The map builder auto-discovers layers from the cache rather than requiring a fixed list — every OpenLitterMap GeoJSON file in `cache/save-earth/openlittermap/` becomes its own toggleable layer, so pulling `clusters-zoom4` and `clusters-zoom10` and `points-zoom15_<bbox>` gives you three overlaid layers automatically. EPA Superfund and Brownfields layers appear whenever their GeoJSON is present.

The HTML page ships with:

- **"Big dots (any zoom)" checkbox** at the top of the layer panel. Toggles every layer's circle radius to ≥ 14 px (2.5 × the per-layer default) so features stay visible at a zoomed-out world view. Off by default — normal mode auto-scales cluster dots by `point_count` so dense regions already read bigger.
- **Per-layer visibility toggles** with a colour swatch and feature count.
- **Click popups** surfacing the upstream `properties` verbatim (EPA primary_name / facility_url, OpenLitterMap datetime / verified / tags, etc.) — no transformation, so rows match whatever the source ships.
- **CARTO Voyager basemap** by default. The previous OSM-direct default hit OSM's volunteer-tile Referer policy and 403'd when the HTML was opened from `file://`. CARTO is free, no-key, and works in every origin. Override with `--basemap-url` + `--basemap-attribution` (supports `{z}/{x}/{y}` and optional `{s}` for subdomain rotation).

## `_lib/` — shared library

| Module | Role |
|--------|------|
| `sidecar.py` | Per-entry `.meta.json` read/write, per-entry locking |
| `storage.py` | LocalStorage / HdfsStorage abstraction + root-path derivation |
| `openlittermap.py` | OpenLitterMap fetch + cache + GeoJSON normalization (supports `clusters` and `points` modes; modes/zoom/bbox each cache in their own entry) |
| `epa_cleanups.py` | EPA Superfund / Brownfield fetch via `geopub.epa.gov/EMEF/efpoints` MapServer — auto-paginates past the 10 k-record server cap |
| `map_render.py` | MapLibre HTML renderer — inlines each cached GeoJSON as a toggleable layer with click popups and the Big-Dots toggle |

The downloaders are pure fetch-and-cache — no transformation beyond normalizing the wrapper shape to a valid FeatureCollection. All per-feature metadata is preserved verbatim so popups can surface it.

## FFL handlers

Every CLI tool has a matching FFL event facet in `../ffl/save_earth.ffl`:

| Tool | FFL facet |
|------|-----------|
| `download-openlittermap` | `save_earth.sources.DownloadOpenLitterMap(mode, zoom, bbox, force, use_mock)` |
| `download-epa-cleanups` | `save_earth.sources.DownloadEpaCleanups(dataset, force, use_mock)` |
| `build-save-earth-map` | `save_earth.maps.BuildMap(region, center_lat, center_lon, zoom, basemap_url, basemap_attribution, dependency_signal)` |

Workflows:

- `save_earth.workflows.BuildGlobalMap` — OLM clusters + Superfund + Brownfields in parallel, then BuildMap.
- `save_earth.workflows.BuildRegionalMap` — region-scoped OLM zoom + Superfund, then BuildMap.

Handlers are thin dispatchers (`handlers/sources/`, `handlers/maps/`) that import from `tools/_lib/` via `handlers/shared/save_earth_utils.py` — same code path as the CLI.

## Standards + references

- **GeoJSON RFC 7946** — feature layer exchange format.
- **CARTO basemap attribution** — https://carto.com/attributions (also cite OpenStreetMap as the data source).
- **OSM tile usage policy** — https://operations.osmfoundation.org/policies/tiles/ (the reason we don't default to tile.openstreetmap.org).
- **CC-BY-SA 4.0** for OpenLitterMap content — when redistributing the derived HTML, attribute OpenLitterMap contributors.
- **US Government public domain** for EPA datasets.

## Planned additions (later PRs)

- `download-rcra-corrective-action` — RCRA corrective-action sites (filtered from ECHO or pulled from per-region EPA feature services)
- `download-tree-equity` — American Forests Tree Equity Score (per-census-block canopy % + equity score) → `tree-equity/<state>.geojson`
- `download-open311` — municipal 311 "illegal dumping" / "cleanup needed" feeds per city → `open311/<city>/<service_code>.geojson`
- `download-surfrider` — Surfrider chapter directory + beach-cleanup events → `surfrider/{chapters,beach-cleanups}.geojson`

When those ship they'll slot into `build-save-earth-map` automatically via the auto-discovery — new entries just get added to `DEFAULT_LAYERS`.
