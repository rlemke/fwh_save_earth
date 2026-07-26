# Seismic — earthquakes over the fault lines

**Namespace:** `save_earth.sources` (+ `save_earth.maps` for the map) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`DownloadEarthquakes`, `DownloadFaults`, workflow `BuildSeismicMap`) ·
**Handlers:** `src/save_earth/handlers/sources/source_handlers.py`
(`handle_download_earthquakes`, `handle_download_faults`) ·
**Tool:** `src/save_earth/tools/_save_earth_tools/seismic.py` ·
**Tests:** `tests/test_seismic_map.py`

## Overview

The seismic feature answers "where do earthquakes happen, and how does that line up
with the tectonic plate boundaries?". It downloads two independent layers —
**recent significant earthquakes** (USGS real-time feed) and the **global plate
boundaries / fault lines** (Peter Bird 2002 `PB2002`) — and `BuildSeismicMap`
renders both on one map: quakes as **circles sized and coloured by magnitude**,
faults as **lines**. Visually, the quakes cluster along the boundaries.

This is the exemplar for two of the renderer's non-point capabilities: **LineString
geometry** (`geometry="line"`) and **magnitude-scaled circles**
(`magnitude_field="mag"`). See [map-rendering](map-rendering.md).

## How it works

Two `save_earth.sources` event facets, each a thin handler over a `seismic.py`
function:

- **`DownloadEarthquakes`** → `seismic.download_earthquakes` fetches the USGS
  GeoJSON feed (`4.5_month`: M4.5+, past 30 days) with `requests`, keeps each
  feature as a Point with the USGS `properties` verbatim, and adds a derived
  `depth_km`. Cache is time-boxed (`EARTHQUAKES_MAX_AGE_HOURS = 6.0`) since the feed
  updates continuously.
- **`DownloadFaults`** → `seismic.download_faults` fetches the `PB2002_boundaries`
  GeoJSON (LineString features) from the `fraxen/tectonicplates` mirror. Plate
  boundaries are effectively static, so the cache holds a long time
  (`FAULTS_MAX_AGE_HOURS = 24*90`).

`BuildSeismicMap` downloads both (each in a `catch`), then calls `BuildMap` with
`only_layers="faults,earthquakes"` and `dependency_signal = faults.feature_count +
quakes.feature_count`.

Data shape: `USGS/PB2002 GeoJSON → cached FeatureCollection → inlined MapLibre
line + magnitude-circle layers`.

## Fan-out

**Single-task per source — no fan-out.** Each dataset is a single small download
(one USGS feed, one PB2002 file); there is nothing to parallelize.

## Data & fields

- **Earthquakes** (`cache_type` `earthquakes`, `earthquakes.geojson`): USGS
  properties verbatim — `mag`, `place`, `time`, `magType`, `tsunami`, … — plus a
  derived `depth_km`. The map's `_EARTHQUAKE_LAYER` sets `magnitude_field="mag"`, so
  the circle radius + colour interpolate over magnitude (4 → small/`#fee08b`,
  8 → large/`#a50026`); `description_fields=None` shows every USGS property in the
  popup.
- **Faults** (`cache_type` `faults`, `faults.geojson`): PB2002 boundary LineStrings
  with their raw properties. The map's `_FAULTS_LAYER` sets `geometry="line"`,
  `weight=1.6`; faults render *before* earthquakes so the quakes draw on top.

No tag filtering — both are pre-curated feeds taken whole.

## External libraries / binaries

- **`requests`** (pip) — the only dependency, for both fetches. GeoJSON is parsed
  with the stdlib `json`. No binary/geospatial libraries.

## Facets & workflows

| Facet / workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `DownloadEarthquakes(force, use_mock)` | event | external / moderate | Recent M4.5+ quakes (USGS feed), verbatim props + `depth_km`. |
| `DownloadFaults(force, use_mock)` | event | external / moderate | PB2002 plate-boundary LineStrings (Bird 2002). |
| `BuildSeismicMap(center_lat, center_lon, zoom, use_mock)` | workflow | — | Download both → render faults (lines) + quakes (magnitude circles). |

Both downloads carry `with RetryPolicy() with Effect(kind="external") with
Cost(tier="moderate")` and return the standard `SourceFetchResult` shape.

## Cache / output

- Cache namespace `save-earth`; `cache_type`s `earthquakes` and `faults`; artifacts
  `earthquakes.geojson` / `faults.geojson` (+ `.meta.json` sidecars).
- The rendered map lands at `save-earth/maps/seismic/index.html`.
- Backend follows `FW_STORAGE` (local / hdfs / s3-MinIO).

## Gotchas & notes

- **Faults must draw under earthquakes.** The candidate-list order in
  `map_handlers.py` places `_FAULTS_LAYER` before `_EARTHQUAKE_LAYER` on purpose;
  the FFL comment and the renderer both rely on it.
- **The earthquake feed is a rolling 30-day window** — the map shows recent
  seismicity, not a historical catalog. Re-running refreshes (6-hour cache).
- **Magnitude styling only kicks in because the layer declares `magnitude_field`.**
  A source that omitted `mag` would render as flat dots.

## Related specs

- [map-rendering](map-rendering.md) — the line + magnitude-circle rendering this
  feature exercises.
- [workflows](workflows.md) — `BuildSeismicMap` in the workflow catalog.
- [osm-overpass-sources](osm-overpass-sources.md) — sibling point sources.
