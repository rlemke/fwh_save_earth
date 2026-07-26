# Ethnic & cultural enclaves

**Namespace:** `save_earth.sources` (+ `save_earth.maps`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`DownloadEthnicEnclaves`, workflow `BuildEnclaveMap`) ·
**Handler:** `src/save_earth/handlers/sources/source_handlers.py`
(`handle_download_ethnic_enclaves`) + `_enclave_layers` in `map_handlers.py` ·
**Tool:** `src/save_earth/tools/_save_earth_tools/enclaves.py` ·
**Tests:** `tests/test_enclaves.py`

## Overview

A world map of **heritage-named neighbourhoods** — Chinatown, Japantown, Little
Italy, Koreatown, Little Saigon, Greektown, Little Havana, and 15 more. OSM has no
structured ethnicity attribute (demographics aren't on-the-ground verifiable), so
the **neighbourhood name is the signal**: this source fetches named places whose
name matches a heritage pattern, classifies each into a heritage bucket, and writes
**one GeoJSON per heritage** so the map renders one coloured, toggleable layer per
heritage.

## How it works

`enclaves.download` (`enclaves.py`):

1. **One Overpass query** for named places (`place=neighbourhood` / `quarter` /
   `suburb` / `city_block` / `locality`) whose `name` matches `_NAME_RE` — the union
   of every heritage's regex pattern, so a single global query covers all 24
   heritages. Uses `nw` (nodes + ways) with `out center` so every feature is a
   Point; relations are skipped (rare here, and expensive on a global name scan).
2. **Python classification** — each match is run against the per-heritage compiled
   regexes (`_compiled`) and tagged with `heritage_slug` + `heritage` label; every
   OSM tag is kept verbatim (many carry a `wikidata`/`wikipedia` link).
3. **Per-heritage GeoJSON** — features are bucketed and written to
   `<slug>.geojson` (e.g. `chinese.geojson`, `italian.geojson`) in the fixed
   `HERITAGES` order, each with its own sidecar.

`_enclave_layers` (map handler) builds one `LayerSpec` per heritage from the
`HERITAGES` constant (not a directory listing, so it works on s3 too), id
`enclave-<slug>`, coloured by `Heritage.color`. `BuildEnclaveMap` renders them all
with `only_layers="enclave-*"`.

## Fan-out

**Single-task — no fan-out.** One global Overpass query covers every heritage
(the name regex is unioned); classification is in-process. Same Overpass
shared-egress reasoning as the other OSM sources.

## Data & fields

- **`Heritage` dataclass** (`slug`, `label`, `color`, `pattern`) — 24 heritages in
  `HERITAGES`, e.g. `chinese` (`china ?town`), `korean`
  (`korea ?town|\bk-?town\b`, `\b`-anchored so "Yorktown"/"Cooktown" don't match),
  `italian` (`little italy|italian quarter|petite italie`), `cuban`
  (`little havana|little cuba`), `persian` (`little tehran|tehrangeles|…`), …
- **Derived properties:** `heritage` (label) and `heritage_slug` added to each
  feature; all original OSM tags kept. `description_fields` is unset, so the popup
  shows every tag (name first) plus the wikidata link.
- **Return shape** differs from the standard source: `DownloadEthnicEnclaves`
  returns `feature_count` + `heritage_count` (not the per-file `relative_path`),
  since it writes many files.

## External libraries / binaries

- **`requests`** (pip) — Overpass fetch. Classification uses stdlib `re`. No binary
  or geospatial dependency.

## Facets & workflows

| Facet / workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `DownloadEthnicEnclaves(force, use_mock)` | event | external / moderate | Fetch named places, classify by heritage regex, write one GeoJSON per heritage. |
| `BuildEnclaveMap(center_lat, center_lon, zoom, use_mock)` | workflow | — | Download enclaves → render `enclave-*` (one coloured layer per heritage). |

`DownloadEthnicEnclaves` returns `(cache_type, feature_count, heritage_count,
source_url, was_cached, used_mock)`.

## Cache / output

- Cache namespace `save-earth`, `cache_type` `enclaves`; one artifact per heritage
  (`<slug>.geojson` + `.meta.json`), e.g. `save-earth/enclaves/chinese.geojson`.
- Rendered map at `save-earth/maps/enclaves/index.html`.
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **Word-boundary anchoring is load-bearing.** Patterns like `\bk-?town\b` and the
  Korean/German rules avoid false positives on unrelated place names — a known
  gotcha called out in the code comments.
- **The name is a proxy, not demographics.** The map `description` is explicit that
  OSM has no ethnicity attribute; coverage is uneven (rich in US/European metros).
- **Draw/legend order is deterministic** because buckets are emitted in `HERITAGES`
  order.
- **s3-safe layer discovery.** `_enclave_layers` iterates the constant + uses
  `storage.exists`, unlike the OpenLitterMap `os.path.isdir` path — a heritage with
  no cached file is simply dropped.

## Related specs

- [map-rendering](map-rendering.md) — the per-heritage coloured layers + `enclave-*`
  wildcard selection.
- [osm-overpass-sources](osm-overpass-sources.md) — sibling OSM sources.
- [workflows](workflows.md) — `BuildEnclaveMap`.
