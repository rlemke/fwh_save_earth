# Workflows — download-then-build map pipelines

**Namespace:** `save_earth.workflows` ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (namespace `save_earth.workflows`) ·
**Handlers:** none — workflows are pure FFL orchestration run by the runtime.

## Overview

The workflow layer is the user-facing surface: each workflow downloads one or more
sources (in parallel) and chains into a single `save_earth.maps.BuildMap`. These
are the entry points a dashboard run picks from. There is no handler code here —
FFL `andThen` blocks *are* the orchestration, executed by the Facetwork runtime's
step state machine.

## How it works

Every map-build workflow follows the same shape (see `BuildSeismicMap`,
`BuildNuclearReactorMap`, etc.):

1. **Download step(s)** — one or more `save_earth.sources.Download*` calls. FFL
   `andThen` siblings that don't reference each other are scheduled **concurrently**,
   so `BuildGlobalMap`'s OpenLitterMap + Superfund + Brownfields downloads run in
   parallel.
2. **`catch` per download** — each download is wrapped in a `catch { yield … }` that
   yields a terminal `status` (`"partial_failure"` / `"failed"`) with a describing
   `detail`, so one upstream outage degrades gracefully instead of failing the run.
3. **`BuildMap` step** — pinned *after* the downloads by
   `dependency_signal = <download>.feature_count [+ …]`. Referencing a field each
   download produced makes the runtime resolve a data-flow edge, guaranteeing the
   map build sees the cached layers. Most workflows also pass `only_layers` to render
   just their family, plus `attribution_workflow` / `attribution_ffl_url` /
   `description` for the map's provenance footer and "About this data" modal.
4. **Terminal `yield`** — a final `yield <Workflow>(status="completed", html_path=…,
   detail=…)`.

## Fan-out

Most workflows are **single-task per step — no fan-out** (a fixed set of downloads
+ one build). Two exceptions fan out inside a source stage:

- **`FetchFabsByCountry`** — a `workflow` with a `foreach c in $.countries` body,
  one `DownloadFabsForCountry` distributed task per country. See
  [semiconductor](semiconductor.md).
- **`ScanTransmissionTiles`** — a `workflow` with a `foreach tile in $.tiles` body,
  one `DownloadTransmissionTile` per tile. This is a **kept demonstration of when
  *not* to fan out** — the production `BuildPowerMap` uses the bounded, sequential
  `DownloadTransmission` instead. See [power-transmission](power-transmission.md).

## Data & fields

Workflows carry no data of their own beyond parameters (map center/zoom,
`use_mock`, `force`, region key) and the `status` / `html_path` / `detail` they
yield. The layer data belongs to the source specs.

## External libraries / binaries

None — FFL only. The downloads and render they invoke pull in `requests` (sources)
and the browser MapLibre asset (render); see those specs.

## Facets & workflows

The full workflow catalog (all `save_earth.workflows.*`):

| Workflow | Sources → layers | Notes |
|---|---|---|
| `BuildGlobalMap(openlittermap_zoom, include_brownfields, use_mock)` | OLM + EPA Superfund + Brownfields → auto-discover all | The combined environmental map; parallel downloads, per-download `catch`. |
| `BuildRegionalMap(region_key, openlittermap_zoom, center_lat, center_lon, zoom, use_mock)` | OLM (tighter zoom) + Superfund | Country/state framing; chains into the global build. |
| `BuildNuclearReactorMap(...)` | `nuclear-reactors` | Single OSM layer, full-tag popups. |
| `BuildALPRCameraMap(...)` | `alpr-*` | Vendor split + heatmap; `max_inline_features=130000`. |
| `BuildNuclearSitesMap(...)` | `nuclear-test-sites,missile-silos` | Two toggleable layers from one file. |
| `BuildDataCenterWaterMap(...)` | `aquifers,data-centers` | Polygon fill under points. |
| `BuildEnclaveMap(...)` | `enclave-*` | One coloured layer per heritage. |
| `BuildPowerMap(force, ...)` | `power-*` | Plants by fuel + ≥500 kV transmission line layer. |
| `BuildRenewableSitingMap(force, ...)` | `siting-*` | Solar/wind coloured by NASA POWER resource. |
| `BuildVolcanoMap(...)` | `volcanoes` | OSM notable volcanoes. |
| `BuildLgbtqVenueMap(...)` | `lgbtq` | OSM LGBTQ+ venues. |
| `BuildTeslaChargerMap(...)` | `tesla` | OSM Tesla chargers. |
| `BuildTelescopeMap(...)` | `telescopes` | OSM research telescopes. |
| `BuildSeismicMap(...)` | `faults,earthquakes` | Faults (lines) + quakes (magnitude circles). |
| `BuildSemiconductorMap(force)` | `semiconductor-fabs` | Per-country fan-out + Wikidata union. |
| `FetchFabsByCountry(countries, force)` | — | `foreach` fan-out helper (returns `fab_paths`). |
| `ScanTransmissionTiles(tiles)` | — | `foreach` fan-out demo (not used by `BuildPowerMap`). |

All are `workflow`s (runtime-run). `use_mock = true` on the download-bearing ones
exercises the pipeline without hitting upstream APIs.

## Cache / output

Workflows produce no cache of their own; their `BuildMap` step writes the HTML to
`save-earth/maps/<region>/` (see [map-rendering](map-rendering.md)) and the sources
write their per-source caches.

## Gotchas & notes

- **`dependency_signal` is the sequencing mechanism.** Dropping it would let
  `BuildMap` run before the downloads finish and render zero layers. Every workflow
  sums the relevant `feature_count`s into it.
- **`catch` yields a *terminal* status, it does not retry.** Transient retries are
  the `RetryPolicy` mixin on the download facets; `catch` is the last-resort
  graceful-degradation path.
- **`BuildRegionalMap`'s zero-arg cousins parse fine, but a truly zero-parameter
  workflow header does not** — a documented FFL gotcha in the wider project; these
  workflows all carry defaulted params.

## Related specs

- [map-rendering](map-rendering.md) — the `BuildMap` step every workflow ends in.
- [semiconductor](semiconductor.md), [power-transmission](power-transmission.md) —
  the two fan-out workflows.
- [epa-and-litter](epa-and-litter.md), [seismic](seismic.md),
  [osm-overpass-sources](osm-overpass-sources.md) — the sources they compose.
