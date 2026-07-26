<!-- SPEC TEMPLATE — every docs/<feature>.md follows this shape so the set reads
consistently. Delete this comment in real specs. Keep sections in this order;
omit a section only if it genuinely does not apply (say so in one line rather
than dropping the heading silently). Ground every claim in the actual FFL
docstrings / handler code / tools — do not invent behaviour. -->

# <Feature Name>

**Namespace(s):** `save_earth.<ns>` · **FFL:** `src/save_earth/ffl/save_earth.ffl` ·
**Handlers:** `src/save_earth/handlers/<sources|maps>/*.py` ·
**Tools:** `src/save_earth/tools/_save_earth_tools/<...>.py` (if any)

## Overview
One or two paragraphs: what this feature is for, the request it answers, and where
it sits in the pipeline (source → cache → render). Every source in this repo is
one event facet that fetches an upstream dataset and caches a GeoJSON + `.meta.json`
sidecar; every map is `save_earth.maps.BuildMap` reading those cached layers.

## How it works
The algorithm / data flow, step by step. Name the concrete steps and the shape of
the data at each (upstream API → GeoJSON FeatureCollection → cached file →
inline-in-HTML MapLibre layer). If the source keeps every upstream attribute
verbatim in `properties`, say so.

## Fan-out
Does it fan out across the fleet? If yes: what is the fan-out unit (per-country /
per-tile), which facet drives it (a `foreach` over what list), and why it reduces
wall-clock. If it is single-task, say "single-task — no fan-out" and why (e.g. the
whole dataset is one download, or Overpass rate-limits ~2 concurrent queries per
IP and the fleet shares one egress IP, so wide fan-out only thrashes — a documented
"when *not* to fan out" case).

## Data & fields
What the source returns and on which upstream attributes it filters — be specific
(OSM `generator:source=nuclear` / `plant:source=nuclear`, `man_made=surveillance`
+ `surveillance:type=ALPR`, WRI `primary_fuel`, USGS `mag`, a heritage name regex
over `place`/`neighbourhood`). Name the filter/fetch mechanism (an Overpass query,
an ArcGIS FeatureServer `where=`, a Wikidata SPARQL, a raw GeoJSON feed). Name the
key `properties` the map popup surfaces. If the feature does no filtering, say so.

## External libraries / binaries
Every non-stdlib dependency this feature relies on and what for. In this repo the
only runtime pip dependency is `requests` (all upstream fetches) plus `PyYAML`
(the catalog manifest) — there is **no** osmium/shapely/pyproj/GDAL and no binary
dependency; each source parses GeoJSON/CSV/JSON with the stdlib. Say so explicitly,
and flag any exception.

## Facets & workflows
The key event facets and workflows, with signatures and a one-line purpose taken
from the FFL docstrings. Mark event facets (need a handler) vs pure facets / a
`workflow` foreach body (run by the runtime), and note `Effect`/`Cost`/`Timeout`
mixins where present.

## Cache / output
The cache namespace `save-earth` and the `cache_type` under
`$FW_CACHE_ROOT/save-earth/<cache_type>/`, the artifact filename(s), and the format
(GeoJSON FeatureCollection / HTML map). Note the per-entry `.meta.json` sidecar and
whether outputs go to local disk, MinIO/S3, or HDFS (governed by `FW_STORAGE`).

## Gotchas & notes
Known pitfalls, rate limits, coverage/sensitivity caveats, or non-obvious
constraints (worth capturing anything a future maintainer would trip on).

## Related specs
Links to the specs this feature composes with or depends on.
