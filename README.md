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
- **Map build** — `save_earth.maps.BuildMap` auto-discovers every cached layer
  and renders a self-contained MapLibre HTML bundle (CARTO Voyager basemap,
  no API key, works from `file://`).
- **Workflows** — `BuildGlobalMap` / `BuildRegionalMap` download in parallel
  (with `catch` blocks for graceful partial failure) and chain into the map build.

The CLI tools in `src/save_earth/tools/` and the FFL handlers share one
`_lib/` implementation and one on-disk cache (`$AFL_DATA_ROOT/cache/save-earth/`)
— the terminal and the runtime are two surfaces onto the same data.

Discovered by the Facetwork runner via the `facetwork.examples` entry point
declared in `pyproject.toml`. After `pip install -e .`, Facetwork's
`scripts/start-runner --example save-earth` and `scripts/seed-examples`
pick this package up automatically.

## Install

```bash
git clone https://github.com/rlemke/fwh_save_earth.git
cd fwh_save_earth
pip install -e .
```

This registers the package under the `facetwork.examples` entry-point group,
making it discoverable by any Facetwork installation in the same environment.

Or, from a Facetwork checkout, use the registry-driven helper:

```bash
scripts/install-example save-earth --check
```

## Run from a Facetwork checkout

```bash
# Seed the workflows, then start a runner that advertises save_earth.* facets:
scripts/seed-examples --include save-earth
scripts/start-runner --example save-earth -- --log-format text
```

Then open the dashboard (http://localhost:8080), create a run of
`save_earth.workflows.BuildGlobalMap`, and watch the downloads + map build.
Pass `use_mock = true` to exercise the pipeline without hitting the upstream
APIs.

## Layout

```
fwh_save_earth/
├── pyproject.toml                       # declares facetwork.examples entry point
├── agent.py                             # standalone RegistryRunner entrypoint
├── src/save_earth/
│   ├── __init__.py                      # exports `example: ExamplePackage`
│   ├── ffl/save_earth.ffl               # schemas, mixins, facets, workflows
│   ├── handlers/
│   │   ├── __init__.py                  # register_all_registry_handlers(runner)
│   │   ├── sources/source_handlers.py   # DownloadOpenLitterMap / DownloadEpaCleanups / DownloadTri
│   │   ├── maps/map_handlers.py         # BuildMap
│   │   └── shared/save_earth_utils.py   # sys.path shim re-exporting tools/_lib
│   └── tools/                           # CLIs + shell wrappers, backed by tools/_lib/
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
