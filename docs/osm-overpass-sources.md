# OSM Overpass point sources

**Namespace:** `save_earth.sources` (+ `save_earth.maps`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` ·
**Handlers:** `src/save_earth/handlers/sources/source_handlers.py` ·
**Tools:** `src/save_earth/tools/_save_earth_tools/{nuclear,alpr,datacenters,nuclear_sites,volcanoes,lgbtq,telescope,tesla,aquifers}.py` ·
**Tests:** `tests/test_alpr.py`, `tests/test_alpr_map_render.py`, `tests/test_nuclear_sites.py`, `tests/test_datacenters_aquifers.py`

## Overview

A family of single-query OpenStreetMap sources, each fetching one category of
feature from the **Overpass API**, centroiding any way/relation polygon to a Point,
and caching a GeoJSON with **every OSM tag kept verbatim** in `properties` — so the
map popup can surface whatever the community has mapped. Each is one
`save_earth.sources.Download*` event facet and one `Build*Map` workflow rendering a
single focused layer (or a filtered split of one). They share the same shape as the
seismic and enclave sources but are grouped here because they are structurally
identical: OSM → Overpass → verbatim-tag points.

The USGS **aquifers** source is included here as the polygon backdrop the
data-center map overlays (it is an ArcGIS FeatureServer fetch, not Overpass, but
composes with the data-center OSM source).

## How it works

Each tool defines an `OVERPASS_QUERY` for its tag selector, POSTs it to a mirror
list (`overpass-api.de`, `overpass.kumi.systems`) with a descriptive `User-Agent`,
converts `elements` to GeoJSON Points (ways/relations via `out center`), keeps all
tags, adds any derived fields, and persists via the shared staging→finalize→sidecar
protocol. The matching handler (`handle_download_*`) is a thin
parameter-coercion + step-log wrapper. `Build*Map` downloads the one source
(`catch` on failure) and renders it with `only_layers` scoped to that layer family.

## Fan-out

**Single-task per source — no fan-out.** Each is one global Overpass query. This is
deliberate: Overpass rate-limits ~2 concurrent queries per IP and the fleet shares
one egress IP, so a wide fan-out only thrashes. (The one OSM source that *does* fan
out per country, semiconductor fabs, does so for query-size reasons, not throughput
— see [semiconductor](semiconductor.md).)

## Data & fields

| Source | `cache_type` / file | OSM selector | Derived fields / notes |
|---|---|---|---|
| Nuclear reactors (`nuclear.py`) | `nuclear` / `reactors.geojson` | `generator:source=nuclear` (a reactor) or `plant:source=nuclear` (a station) | Full tags; `_NUCLEAR_LAYER` popup shows all. |
| ALPR cameras (`alpr.py`) | `alpr` / `cameras.geojson` | `man_made=surveillance` + `surveillance:type=ALPR` (DeFlock) | derived `camera_vendor` (flock/motorola/other); ~145k nodes (counted 2026-08-21). Camera **locations only** — no video/plate data. |
| Data centers (`datacenters.py`) | `datacenters` / `datacenters.geojson` | `man_made=data_center` / telecom, continental-US bbox | footprints centroided; existing/mapped facilities only. |
| Nuclear sites (`nuclear_sites.py`) | `nuclear-sites` / `nuclear_sites.geojson` | `military=nuclear_explosion_site` + `bunker_type=missile_silo` | derived `site_type` (test_site / missile_silo) → two toggleable layers from one file. |
| Volcanoes (`volcanoes.py`) | `volcanoes` / `volcanoes.geojson` | `natural=volcano` **with** a `wikidata`/`wikipedia` tag (notability proxy) | ~1–2k worldwide. |
| LGBTQ+ venues (`lgbtq.py`) | `lgbtq` / `lgbtq_venues.geojson` | food/drink amenities tagged `lgbtq=*` (≠ `no`) or legacy `gay=yes` | full tags. |
| Telescopes (`telescope.py`) | `telescopes` / `telescopes.geojson` | named `man_made=telescope` (optical + radio) | `feature_kind` = `telescope:type`. |
| Tesla chargers (`tesla.py`) | `tesla` / `tesla_chargers.geojson` | `amenity=charging_station` branded/networked/socket-Tesla | `feature_kind` = supercharger / charger. |
| Aquifers (`aquifers.py`) | `aquifers` / `aquifers.geojson` | USGS ArcGIS FeatureServer (principal-aquifer polygons, rock-only excluded) | polygon `fill` backdrop for data centers; fields `AQ_NAME`, `ROCK_TYPE`. |

The **ALPR** and **nuclear-sites** layers use the renderer's `filter_field` /
`filter_value` to split one cached file into several styled layers without
duplicating features (`camera_vendor`, `site_type`); ALPR additionally has a
`geometry="heatmap"` density layer drawn under the vendor dots. See
[map-rendering](map-rendering.md).

## External libraries / binaries

- **`requests`** (pip) — every fetch (Overpass POST, ArcGIS GET). GeoJSON assembled
  with the stdlib. **No** pyosmium/osmium-tool/shapely — geometry is only centroid
  arithmetic on Overpass `center` coordinates, so there is no binary dependency.

## Facets & workflows

All event facets carry `with RetryPolicy() with Effect(kind="external") with
Cost(tier="moderate")` and return the standard `SourceFetchResult`:
`DownloadNuclearReactors`, `DownloadALPRCameras`, `DownloadDataCenters`,
`DownloadNuclearSites`, `DownloadScenicHistoricRoads`, `DownloadAquifers`, `DownloadVolcanoes`,
`DownloadLgbtqVenues`, `DownloadTelescopes`, `DownloadTeslaChargers` — each with a
`force` + `use_mock` param. Workflows: `BuildNuclearReactorMap`,
`BuildALPRCameraMap`, `BuildNuclearSitesMap`, `BuildDataCenterWaterMap`,
`BuildVolcanoMap`, `BuildLgbtqVenueMap`, `BuildTelescopeMap`,
`BuildTeslaChargerMap`, `BuildScenicHistoricRoadsMap`.

## Cache / output

- Cache namespace `save-earth`; one `cache_type` per source (table above); each a
  single GeoJSON + `.meta.json` sidecar under `$FW_CACHE_ROOT/save-earth/<type>/`.
- Rendered maps at `save-earth/maps/<region>/index.html` (region = `nuclear`,
  `alpr`, `data-centers`, `nuclear-sites`, `volcanoes`, `lgbtq`, `telescopes`,
  `tesla`).
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **Coverage is community-driven and incomplete.** OSM is not authoritative;
  nuclear-site silos, data centers, and telescopes are especially patchy. The FFL
  docstrings say so per source — keep that caveat in the map `description`.
- **Sensitivity was clarified up front** for nuclear test sites / missile silos and
  ALPR cameras: these are publicly-tagged, geographically-fixed *locations* (the
  same DeFlock/arms-control transparency data), not private operational data.
- **ALPR needs a raised inline cap** (`max_inline_features=130000`) because the
  vendor split + heatmap share ~120k+ features in one source.
- **Aquifers is the odd one out** — ArcGIS, not Overpass, and polygons, not points;
  it exists to give the data-center map a groundwater backdrop.

## Related specs

- [map-rendering](map-rendering.md) — shared-source `filter_field`, heatmap, fill.
- [workflows](workflows.md) — the `Build*Map` workflow catalog.
- [seismic](seismic.md), [enclaves](enclaves.md) — sibling source families.
- [semiconductor](semiconductor.md) — the OSM source that *does* fan out per country.
