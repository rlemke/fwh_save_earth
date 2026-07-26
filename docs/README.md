# save-earth — Feature Specifications

This directory holds one **spec per save-earth feature**. Each document follows a
common shape ([`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md)) and states, for that feature:
how it works, whether and how it **fans out** across the fleet, what upstream
**data & fields** it pulls, the **external libraries** it relies on, its **facets &
workflows**, and its **cache/output**. Claims are grounded in the FFL `/** … */`
docstrings, the handler code, and the `_save_earth_tools` implementation — the
source of truth for each facet remains its FFL docstring; these specs are the
feature-level narrative over them.

**Start here:** [**Map rendering**](map-rendering.md) — the flagship. Every
workflow ends in one `save_earth.maps.BuildMap`, and this MapLibre renderer is
where the domain's geometry breadth lives (point circles, lines, polygon fills,
density heatmaps, magnitude-scaled circles, shared-source filters).

## Cross-cutting

| Spec | What it covers |
|------|----------------|
| [map-rendering.md](map-rendering.md) | **Flagship.** The MapLibre HTML renderer + `BuildMap`: geometry types (circle/line/fill/heatmap), magnitude circles, shared-source `filter_field` splits, popups/search/legend, basemap + provenance. |
| [workflows.md](workflows.md) | The download-then-build workflow layer: parallel downloads, per-download `catch`, `dependency_signal` sequencing, `only_layers` scoping — the full `Build*Map` catalog. |
| [cache-and-storage.md](cache-and-storage.md) | The sidecar cache, `FW_STORAGE` backends (local/hdfs/s3-MinIO), the CLI↔handler shim, staging→finalize, and packaging (`facetwork.domains` + `catalog.yaml`). |

## Sources

| Spec | What it covers |
|------|----------------|
| [epa-and-litter.md](epa-and-litter.md) | The founding environmental sources: OpenLitterMap, EPA Superfund/Brownfields (ArcGIS), EPA TRI — feed the combined `BuildGlobalMap`. |
| [seismic.md](seismic.md) | Earthquakes (USGS M4.5+ feed, magnitude circles) over the fault lines (Bird 2002 `PB2002`, LineString) — `BuildSeismicMap`. |
| [osm-overpass-sources.md](osm-overpass-sources.md) | The single-query OSM point family (nuclear reactors, ALPR/DeFlock, data centers, nuclear sites, volcanoes, LGBTQ+, telescopes, Tesla) + USGS aquifers — verbatim-tag popups, shared-source splits. |
| [enclaves.md](enclaves.md) | Heritage-named neighbourhoods (24 heritages, name-regex classification) → one coloured GeoJSON + layer per heritage. |
| [semiconductor.md](semiconductor.md) | Semiconductor fabs: per-country OSM fan-out (`foreach`) + a single Wikidata SPARQL, unioned — the domain's **do-fan-out** case. |

## Power & energy

| Spec | What it covers |
|------|----------------|
| [power-transmission.md](power-transmission.md) | WRI power plants by fuel + ≥500 kV OSM transmission (bounded/sequential/cache-aware) — the **"when *not* to fan out"** case, with a kept fan-out demo. |
| [renewable-siting.md](renewable-siting.md) | Solar/wind plants annotated with NASA POWER 20-year resource → `siting_score` on the magnitude ramp. |

---

*See also the machine-readable capability index at
[`src/save_earth/catalog.yaml`](../src/save_earth/catalog.yaml) (workflows + facets
by intent), the repo [`README.md`](../README.md), and the tools contract in
[`agent-spec/tools-pattern.agent-spec.yaml`](../agent-spec/tools-pattern.agent-spec.yaml)
/ [`agent-spec/cache-layout.agent-spec.yaml`](../agent-spec/cache-layout.agent-spec.yaml).
The live/queryable interface is the MCP `fw_capabilities` / `fw_catalog_search` /
`fw_describe_handler` tools.*
