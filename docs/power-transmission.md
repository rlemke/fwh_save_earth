# Power plants & ≥500 kV transmission

**Namespace:** `save_earth.sources` (+ `save_earth.maps`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`DownloadPowerPlants`, `DownloadTransmission`, `ListTransmissionTiles`, `DownloadTransmissionTile`, `ScanTransmissionTiles`, `MergeTransmission`, workflow `BuildPowerMap`) ·
**Handler:** `src/save_earth/handlers/sources/source_handlers.py` ·
**Tool:** `src/save_earth/tools/_save_earth_tools/power.py` ·
**Tests:** (rendering covered indirectly; siting split in `tests/test_siting.py`)

## Overview

The world power-infrastructure map: **power plants by primary fuel** from the WRI
Global Power Plant Database (hydro/coal/gas/solar/wind/nuclear, ~35k plants, one
GeoJSON per fuel) plus the **≥500 kV transmission backbone** from OpenStreetMap.
`BuildPowerMap` renders the six fuel point-layers + one transmission line-layer.
This feature is also the domain's canonical **"when *not* to fan out"** case: the
transmission fetch is deliberately bounded and sequential, and a fan-out variant is
kept in the FFL purely as a demonstration.

## How it works

- **Plants** — `power.download_plants` fetches the WRI CSV, buckets rows by
  `primary_fuel` into six fuel slugs (`_FUEL_BY_WRI`), keeps the important popup
  columns (name, capacity_mw, primary_fuel, commissioning_year, country_long,
  owner, …; skips WRI's bulky per-year generation columns), and writes one
  `<slug>.geojson` per fuel. **Cache-aware:** if all six layers already exist and
  `force=false`, it returns the cached counts without re-fetching.
- **Transmission (production path)** — `power.download_transmission` fetches the
  ≥500 kV lines **one tile at a time** over a ~16-tile world grid
  (`TRANSMISSION_BBOXES`), only when `transmission.geojson` isn't already cached
  (`force=true` re-fetches). A voltage regex (`_VOLT_RE`, matching 500000–999999 or
  7+ digits, incl. multi-value `500000;230000` tags) selects the lines. Endpoints
  rotate across `overpass-api.de` / `overpass.kumi.systems`.

`BuildPowerMap` downloads plants + transmission (each `catch`ed) and renders with
`only_layers="power-*"`. `_power_layers` builds one `power-<slug>` point layer per
fuel plus a `power-transmission` line layer (`geometry="line"`, `weight=0.7`).

## Fan-out

**Deliberately sequential — the documented "when *not* to fan out" case.** A single
global `out geom` power query 504s, so the world is tiled; but Overpass rate-limits
~2 concurrent queries per IP and the fleet shares one egress IP, so a wide fan-out
is throttled by Overpass regardless of width — finer tiles are about *reliability*
(avoiding giant-query timeouts), not throughput. The production `DownloadTransmission`
therefore walks the tiles **in one task, sequentially, cache-aware**.

The FFL still declares a fan-out variant — `ListTransmissionTiles` (pure) →
`ScanTransmissionTiles` (a `foreach tile` workflow, one `DownloadTransmissionTile`
per tile) → `MergeTransmission` — as a **kept demonstration**, not the path
`BuildPowerMap` uses. `DownloadTransmissionTile` fetches one bbox; `MergeTransmission`
dedupes per-tile GeoJSONs by OSM way id.

## Data & fields

- **`FUELS`**: `(slug, WRI primary_fuel, label, colour)` — hydro `#1565c0`, coal
  `#37474f`, gas `#8e24aa`, solar `#f9a825`, wind `#2e7d32`, nuclear `#d84315`.
- **Plant properties** (from WRI, `CC BY 4.0`): name, capacity_mw, primary_fuel,
  other_fuel1, commissioning_year, country_long, owner, source, url,
  geolocation_source, gppd_idnr.
- **Transmission**: OSM `power=line` ways ≥500 kV, full OSM tags; rendered as lines.
- Layers use `description_fields=None`, so popups show the full WRI record / OSM tags.

## External libraries / binaries

- **`requests`** (pip) — WRI CSV GET + Overpass POST. CSV parsed with stdlib `csv`;
  GeoJSON with stdlib `json`. No binary/geospatial dependency. Writes go through the
  shared staging→finalize→sidecar helper.

## Facets & workflows

| Facet / workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `DownloadPowerPlants(force)` | event | external / moderate | WRI plants → one GeoJSON per fuel (cache-aware). |
| `DownloadTransmission(force)` | event | external / expensive, `Timeout(30m)` | ≥500 kV lines, bounded + sequential + cache-aware (the path `BuildPowerMap` uses). |
| `ListTransmissionTiles()` | event | pure / free | Emit the tile bbox list for the fan-out demo. |
| `DownloadTransmissionTile(bbox, force)` | event | external / moderate, `Timeout(15m)` | One tile's ≥500 kV lines (fan-out leaf). |
| `ScanTransmissionTiles(tiles)` | workflow | — | `foreach tile` fan-out demo (not used by `BuildPowerMap`). |
| `MergeTransmission(tiles, dependency_signal)` | event | io / cheap | Merge per-tile GeoJSONs, dedupe by way id. |
| `BuildPowerMap(force, center_lat, center_lon, zoom)` | workflow | — | Plants + transmission → `power-*` map. |

## Cache / output

- Cache namespace `save-earth`, `cache_type` `power`; artifacts `<fuel>.geojson`
  (six) + `transmission.geojson` (+ per-tile files for the fan-out demo), each with
  a `.meta.json` sidecar.
- Rendered map at `save-earth/maps/power/index.html`.
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **Re-runs reuse the cache unless `force=true`.** Both plants and transmission are
  cache-aware; `BuildPowerMap`'s `detail` reports `lines.was_cached`.
- **The map `description` explains the "line ends in mid-air" artifact** — only
  ≥500 kV is shown, so a line visibly stops where it steps down to 400/230 kV lines
  that aren't in the layer (and OSM has no plant-to-substation linkage at world
  scale).
- **Prefer `DownloadTransmission`, not `ScanTransmissionTiles`.** The fan-out is a
  teaching artifact; using it fires a retry-storm at Overpass's 2-slot limit.

## Related specs

- [renewable-siting](renewable-siting.md) — annotates the same WRI solar/wind
  plants with NASA POWER resource.
- [map-rendering](map-rendering.md) — the `power-*` layers + line geometry.
- [workflows](workflows.md) — `BuildPowerMap`; the fan-out reasoning.
- [semiconductor](semiconductor.md) — the *do*-fan-out counterpart (per country).
