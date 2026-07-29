# Renewable-energy siting

**Namespace:** `save_earth.sources` (+ `save_earth.maps`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`AnnotateRenewableSiting`, workflow `BuildRenewableSitingMap`) ·
**Handler:** `src/save_earth/handlers/sources/source_handlers.py`
(`handle_annotate_renewable_siting`) + `_siting_layers` in `map_handlers.py` ·
**Tool:** `src/save_earth/tools/_save_earth_tools/siting.py` ·
**Tests:** `tests/test_siting.py`

## Overview

An analysis layer on top of the power plants: it answers **"is this solar/wind plant
actually where the sun/wind is good?"**. It reads the cached WRI solar & wind
layers, samples each plant's location against **NASA POWER's 20-year climatology**
(GHI for solar, 50 m wind speed for wind), and writes one annotated GeoJSON per
source in which each plant carries its raw resource value plus a `siting_score`
(4..8) that the renderer's magnitude ramp maps onto a yellow→dark-red gradient —
dark-red/large = excellent resource (well sited), yellow/small = poor.

## How it works

`siting.annotate` (`siting.py`):

1. Reads the cached `solar.geojson` / `wind.geojson` from the `power` cache (so it
   depends on `DownloadPowerPlants` having run — the FFL orders it with
   `after plants`).
2. For each of the two `RESOURCES` — `("solar", "siting_solar.geojson",
   "ALLSKY_SFC_SW_DWN", "ghi_kwh_m2_day", (2.5, 6.5))` and `("wind",
   "siting_wind.geojson", "WS50M", "wind_speed_ms", (3.0, 11.0))` — it de-duplicates
   plant locations onto a coarse 1° grid, queries NASA POWER's **regional**
   climatology endpoint (max 10°×10° bbox), and back-fills each plant's raw resource
   value.
3. Maps the utility-scale resource domain onto the renderer's `[4, 8]` score ramp
   and writes `siting_score` per plant, one annotated GeoJSON per source.

`_siting_layers` builds `siting-solar` / `siting-wind` `LayerSpec`s with
`magnitude_field="siting_score"`. `BuildRenewableSitingMap` downloads plants →
annotates → renders `only_layers="siting-*"`.

## Fan-out

**Single-task, deliberately sequential — no fan-out.** The FFL docstring is
explicit: even though it makes a few hundred NASA POWER calls, they run
**sequentially** (de-duplicated onto the coarse grid), NOT fanned out, because the
fleet shares one egress IP. A per-thread `requests.Session` (`_session`) gives
connection reuse without concurrency.

## Data & fields

- **`RESOURCES`**: `(slug, output filename, NASA POWER parameter, raw-property name,
  (domain_lo, domain_hi))`. `_FILL = -999.0` is the NASA POWER no-data sentinel;
  `_TILE = 10` is the regional endpoint's max bbox; `_SCORE_LO/_HI = 4.0/8.0` are the
  renderer ramp endpoints.
- **Derived properties:** each plant keeps its full WRI record plus `ghi_kwh_m2_day`
  or `wind_speed_ms` (raw resource) and `siting_score` (4..8). Popups
  (`description_fields=None`) show the full WRI record + the sampled value.
- **Colours:** solar `#f9a825`, wind `#2e7d32` (base swatch); magnitude ramp drives
  the per-plant radius + colour.

## External libraries / binaries

- **`requests`** (pip) — NASA POWER regional climatology API. No binary/geospatial
  dependency; grid math is stdlib.

## Facets & workflows

| Facet / workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `AnnotateRenewableSiting(force)` | event | external / expensive, `Timeout(30m)` | Sample NASA POWER resource at each solar/wind plant, write siting-scored layers. |
| `BuildRenewableSitingMap(force, center_lat, center_lon, zoom)` | workflow | — | Download plants → annotate → render `siting-*`. |

Returns `(cache_type, feature_count, cells_sampled, was_cached, source_url)`.
Callers order it with `after` — it reads what `DownloadPowerPlants` cached and takes
no value from it.

## Cache / output

- Cache namespace `save-earth`, `cache_type` `siting`; artifacts
  `siting_solar.geojson` / `siting_wind.geojson` (+ sidecars).
- Rendered map at `save-earth/maps/siting/index.html`.
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **Use the regional NASA POWER endpoint, not the point endpoint.** The code targets
  `.../climatology/regional` and grids requests onto 10°×10° tiles — the point
  endpoint throttles under the volume.
- **It depends on the power cache.** Run/sequence `DownloadPowerPlants` first;
  `siting.annotate` reads `solar.geojson`/`wind.geojson` from the `power` cache.
- **Score, not raw value, drives the colour.** The `(domain_lo, domain_hi)` → `[4,8]`
  mapping is what makes "well-sited" read as dark-red regardless of absolute units.

## Related specs

- [power-transmission](power-transmission.md) — the WRI plants this annotates.
- [map-rendering](map-rendering.md) — the `magnitude_field` ramp it reuses.
- [workflows](workflows.md) — `BuildRenewableSitingMap`.
