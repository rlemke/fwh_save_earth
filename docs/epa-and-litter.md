# EPA remediation, TRI & OpenLitterMap

**Namespace:** `save_earth.sources` (+ `save_earth.maps`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`DownloadOpenLitterMap`, `DownloadEpaCleanups`, `DownloadTri`) ·
**Handler:** `src/save_earth/handlers/sources/source_handlers.py` ·
**Tools:** `src/save_earth/tools/_save_earth_tools/{openlittermap,epa_cleanups,tri}.py`

## Overview

The founding environmental-action sources — the ones the repo was named for. Three
US/global datasets about pollution and cleanup:

- **OpenLitterMap** — crowd-sourced geotagged litter observations.
- **EPA remediation sites** — authoritative Superfund (NPL) and Brownfields (ACRES)
  cleanup registries.
- **EPA Toxic Release Inventory (TRI)** — facility-level toxic-release points.

These feed the combined `BuildGlobalMap` / `BuildRegionalMap` (OpenLitterMap + EPA
cleanups) — the only workflows that render *multiple* environmental layers together
and auto-discover every cached layer.

## How it works

Each is one `save_earth.sources.Download*` event facet over a `_save_earth_tools`
function that fetches upstream, normalizes to GeoJSON Points, and persists via the
shared staging→finalize→sidecar protocol:

- **`DownloadOpenLitterMap`** → `openlittermap.download` hits `openlittermap.com/api`
  in `clusters` (default, any zoom) or `points` mode (server-enforced `zoom >= 15` +
  a bounded bbox). The filename encodes the mode + zoom (+ bbox), and each cached OLM
  file becomes its own auto-discovered map layer.
- **`DownloadEpaCleanups`** → `epa_cleanups.download` fetches an EPA ArcGIS EMEF
  FeatureServer layer — `superfund` (NPL, layer 0) or `brownfields` (ACRES, layer 5)
  — auto-paginating past the server's 10,000-record cap.
- **`DownloadTri`** → `tri.download` fetches `TRI_FACILITY` from
  `data.epa.gov/efservice/` (~65k rows, paginated 10k/request), normalizes longitude
  sign for western-hemisphere state codes, and (with `active_only=true`, the default)
  drops facilities whose `fac_closed_ind` is `"1"`.

## Fan-out

**Single-task per source — no fan-out.** OpenLitterMap and TRI paginate *within* one
task; EPA cleanups paginate the FeatureServer within one task. No `foreach`.

## Data & fields

- **OpenLitterMap** (`cache_type` `openlittermap`, `<mode>-zoom<N>[_<bbox>].geojson`):
  cluster/point properties — the map's `_OLM_DESCRIPTION_FIELDS` surface
  `point_count`, `datetime`, `verified`, `picked_up`, `username`, `id`.
- **EPA cleanups** (`cache_type` `epa-cleanups`, `superfund.geojson` /
  `brownfields.geojson`): the map's `_EPA_LAYERS` surface `primary_name`,
  `location_address`, `city_name`, `state_code`, `epa_region`, `pgm_sys_id`,
  `facility_url`.
- **TRI** (`cache_type` `tri`, `facilities.geojson`): facility points with the TRI
  facility fields verbatim; `active_only` filters closed facilities.

`DownloadEpaCleanups` validates `dataset` against `epa_cleanups.DEFAULT_URLS`
(`{superfund, brownfields}`) and raises on an unknown dataset.

## External libraries / binaries

- **`requests`** (pip) — all three fetches. CSV/JSON/GeoJSON parsed with the stdlib.
  No binary/geospatial dependency.

## Facets & workflows

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `DownloadOpenLitterMap(mode, zoom, bbox, force, use_mock)` | event | external / moderate | Crowd-sourced litter observations (clusters/points). |
| `DownloadEpaCleanups(dataset, force, use_mock)` | event | external / moderate | Superfund NPL or Brownfields ACRES remediation sites. |
| `DownloadTri(active_only, force, use_mock)` | event | external / moderate | EPA Toxic Release Inventory facility points. |

All carry `with RetryPolicy() with Effect(kind="external") with
Cost(tier="moderate")` and return `SourceFetchResult`. Rendered by `BuildGlobalMap`
/ `BuildRegionalMap` (OLM + EPA cleanups auto-discovered). TRI has a facet + handler
but is not yet wired into a dedicated `Build*Map` workflow — it is available as a
layer/source and via the CLI.

## Cache / output

- Cache namespace `save-earth`; `cache_type`s `openlittermap`, `epa-cleanups`, `tri`;
  each artifact + `.meta.json` sidecar under `$FW_CACHE_ROOT/save-earth/<type>/`.
- The combined map lands at `save-earth/maps/global/` (or `<region_key>/`).
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **OpenLitterMap auto-discovery is local-only.** `_openlittermap_layers` uses
  `os.path.isdir`, which is False for an `s3://` prefix — OLM layers enumerate on
  `local` deployments only (see [map-rendering](map-rendering.md)).
- **OpenLitterMap `points` mode is server-constrained** to `zoom >= 15` + a bbox;
  the global feed is `clusters` at zoom 4.
- **TRI longitude sign** is normalized for western-hemisphere states — upstream rows
  occasionally carry a positive longitude that would otherwise plot in the wrong
  hemisphere.
- **`bbox` is passed as a String** in FFL (for schema simplicity) and parsed by
  `parse_bbox` in the handler shim.

## Related specs

- [map-rendering](map-rendering.md) — the multi-layer combined map + OLM auto-discovery.
- [workflows](workflows.md) — `BuildGlobalMap` / `BuildRegionalMap`.
- [cache-and-storage](cache-and-storage.md) — the sidecar cache these share.
