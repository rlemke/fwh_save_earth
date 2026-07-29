# save-earth

A standalone [Facetwork](https://github.com/rlemke/facetwork) example package
providing FFL workflows and handlers that turn open environmental-action
datasets into an interactive map:

- **Source adapters** — one event facet per data source, each caching a
  GeoJSON file + `.meta.json` sidecar:
  - **OpenLitterMap** — crowd-sourced geotagged litter observations
  - **EPA Superfund (NPL)** + **Brownfields (ACRES)** — authoritative
    remediation sites
  - **EPA Toxic Release Inventory (TRI)** — facility-level toxic release points
  - **OSM nuclear** — worldwide nuclear reactors + plants from the OpenStreetMap
    Overpass API (every OSM tag kept verbatim)
  - **OSM volcanoes** — major (notable) volcanoes from OSM
  - **OSM LGBTQ+ venues** — LGBTQ+ bars, pubs, clubs & restaurants from OSM
  - **OSM research telescopes** + **Tesla charging stations** — from OSM
  - **OSM ethnic & cultural enclaves** — heritage-named neighbourhoods
    (Chinatown, Japantown, Little Italy, Koreatown, …; 22 heritages, each a
    word-anchored name pattern over OSM `place`/`neighbourhood` features)
  - **USGS earthquakes** — recent significant quakes (M4.5+, past 30 days) from
    the USGS real-time GeoJSON feed (properties verbatim + derived `depth_km`)
  - **Fault lines** — tectonic plate boundaries (Peter Bird 2002 `PB2002`),
    LineString geometry — the world-scale fault systems
  - **Power infrastructure** — power plants by primary fuel (hydro / coal /
    natural gas / solar / wind / nuclear) from the WRI Global Power Plant DB, plus the
    **≥500 kV transmission lines** from OSM. The transmission fetch is bounded
    and **cache-aware** (it never re-downloads when the layer is already cached)
    and is fetched **sequentially**, not fanned out — a documented "when *not*
    to fan out" case, because Overpass rate-limits ~2 concurrent queries per IP
    and the whole fleet shares one egress IP, so wide fan-out only thrashes.
- **Map build** — `save_earth.maps.BuildMap` auto-discovers every cached layer
  and renders a self-contained MapLibre HTML bundle (CARTO Voyager basemap,
  no API key, works from `file://`). Layers render as points (circles) or
  **lines** (faults/boundaries); a point layer may scale its radius + colour by
  a numeric property (e.g. earthquake **magnitude**). Click popups surface a
  feature's full `properties` (a `name` property is listed first); a layer with
  no curated field list shows **every** property. A name search + per-layer
  toggles are built in.
- **Workflows** — `BuildGlobalMap` / `BuildRegionalMap` / `BuildNuclearReactorMap`
  / `BuildVolcanoMap` / `BuildLgbtqVenueMap` / `BuildSeismicMap` (earthquakes
  over the fault lines) / `BuildEnclaveMap` (heritage-named neighbourhoods) /
  `BuildPowerMap` (plants by fuel + ≥500 kV transmission) download in parallel
  (with `catch` blocks for graceful partial failure) and chain into the map
  build.
- **Storage** — caches + map outputs follow `FW_STORAGE`: `local`, `hdfs`, or
  `s3` (the fleet MinIO). Downloads stage locally and finalize onto the active
  backend, so an object store needs no shared filesystem.

The CLI tools in `src/save_earth/tools/` and the FFL handlers share one
`_save_earth_tools/` implementation and one on-disk cache (`$FW_DATA_ROOT/cache/save-earth/`)
— the terminal and the runtime are two surfaces onto the same data.

Discovered by the Facetwork runner via the `facetwork.domains` entry point
declared in `pyproject.toml`. After `pip install -e .`, Facetwork's
`fw runner start --domain save-earth` and `fw ffl seed`
pick this package up automatically.

## FFL at a glance

The domain is driven from [FFL](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md),
Facetwork's workflow language. A step is `name = Facet(args)`; independent fetches
run in parallel, `after` orders the render behind them, and `only_layers` points the
generic renderer at what they cached:

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.maps

    /** Three fetches at once, then one map over all three layers. */
    workflow HazardMap() => (html_path: String) andThen {

        quakes = save_earth.sources.DownloadEarthquakes()
        faults = save_earth.sources.DownloadFaults()
        volc = save_earth.sources.DownloadVolcanoes()

        map = save_earth.maps.BuildMap(
            region = "global",
            zoom = 2.0,
            only_layers = "earthquakes,faults,volcanoes"
            ) after quakes

        yield HazardMap(html_path = map.html_path)
    }
}
```

```bash
fw ffl run --primary my.ffl --library src/save_earth/ffl/save_earth.ffl \
  --workflow my.save_earth.HazardMap
```

📖 **[docs/ffl-examples.md](docs/ffl-examples.md)** — the full example gallery:
per-source `catch` (the domain's signature pattern), tiled fan-out + merge,
per-country fan-out with Json loop variables and result collection, overriding the
domain's own `RetryPolicy` mixin at a call site, and `when` guards. Every snippet
there is compile-checked.

## Feature specifications

Per-feature specs live under [`docs/`](docs/README.md) — one document per feature,
each covering how it works, whether it fans out, the upstream data/fields, external
libraries, its facets & workflows, and its cache/output. Start with the flagship
[map-rendering](docs/map-rendering.md).

| Spec | What it covers |
|------|----------------|
| [map-rendering](docs/map-rendering.md) | **Flagship.** MapLibre renderer + `BuildMap`: circle/line/fill/heatmap geometry, magnitude circles, shared-source splits, popups/search/legend. |
| [workflows](docs/workflows.md) | Download-then-build workflows: parallel downloads, `catch`, `after` ordering, `only_layers` — the full `Build*Map` catalog. |
| [cache-and-storage](docs/cache-and-storage.md) | Sidecar cache, `FW_STORAGE` backends (local/hdfs/s3-MinIO), CLI↔handler shim, packaging. |
| [epa-and-litter](docs/epa-and-litter.md) | OpenLitterMap + EPA Superfund/Brownfields + EPA TRI (the founding sources). |
| [seismic](docs/seismic.md) | USGS earthquakes (magnitude circles) over Bird-2002 fault lines. |
| [wildfire](docs/wildfire.md) | NASA FIRMS thermal anomalies (past 24h), three confidence bands scaled by fire radiative power. |
| [osm-overpass-sources](docs/osm-overpass-sources.md) | The single-query OSM point family (nuclear/ALPR/data-centers/volcanoes/…) + USGS aquifers. |
| [enclaves](docs/enclaves.md) | Heritage-named neighbourhoods → one coloured layer per heritage. |
| [semiconductor](docs/semiconductor.md) | Per-country OSM fan-out + Wikidata SPARQL union (the do-fan-out case). |
| [power-transmission](docs/power-transmission.md) | WRI plants by fuel + ≥500 kV transmission (the "when *not* to fan out" case). |
| [renewable-siting](docs/renewable-siting.md) | Solar/wind plants coloured by NASA POWER resource (`siting_score`). |

Full index: [`docs/README.md`](docs/README.md).

## Install

```bash
git clone https://github.com/rlemke/fwh_save_earth.git
cd fwh_save_earth
pip install -e .
```

This registers the package under the `facetwork.domains` entry-point group,
making it discoverable by any Facetwork installation in the same environment.

Or, from a Facetwork checkout, use the registry-driven helper:

```bash
scripts/install-example save-earth --check
```

## Run from a Facetwork checkout

```bash
# Seed the workflows, then start a runner that advertises save_earth.* facets:
fw ffl seed --include save-earth
fw runner start --domain save-earth -- --log-format text
```

Then open the dashboard (http://localhost:8080), create a run of
`save_earth.workflows.BuildGlobalMap`, and watch the downloads + map build.
Pass `use_mock = true` to exercise the pipeline without hitting the upstream
APIs.

## Layout

```
fwh_save_earth/
├── pyproject.toml                       # declares facetwork.domains entry point
├── agent.py                             # standalone RegistryRunner entrypoint
├── agent-spec/                          # tools-pattern + cache-layout contracts
├── src/save_earth/
│   ├── __init__.py                      # exports `domain: DomainPackage`
│   ├── ffl/save_earth.ffl               # schemas, mixins, facets, workflows
│   ├── handlers/
│   │   ├── __init__.py                  # register_all_registry_handlers(runner)
│   │   ├── sources/source_handlers.py   # Download{OpenLitterMap,EpaCleanups,Tri,NuclearReactors,Volcanoes,LgbtqVenues,Earthquakes,Faults,EthnicEnclaves,PowerPlants,Transmission}
│   │   ├── maps/map_handlers.py         # BuildMap
│   │   └── shared/save_earth_utils.py   # sys.path shim re-exporting tools/_save_earth_tools
│   └── tools/                           # CLIs + shell wrappers, backed by tools/_save_earth_tools/
└── tests/
```

## Tools

Each source has a Python CLI + shell wrapper under `src/save_earth/tools/`:

```bash
src/save_earth/tools/download-openlittermap.sh
src/save_earth/tools/download-epa-cleanups.sh
src/save_earth/tools/download-tri.sh
src/save_earth/tools/build-save-earth-map.sh
```

They write the same cache the FFL handlers read, so you can pre-warm the cache
from the terminal and then run a map build through the runtime.
