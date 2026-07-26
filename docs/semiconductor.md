# Semiconductor fabrication plants

**Namespace:** `save_earth.sources` (+ `save_earth.maps`, workflows `FetchFabsByCountry` / `BuildSemiconductorMap`) ·
**FFL:** `src/save_earth/ffl/save_earth.ffl` (`ListFabCountries`, `DownloadFabsForCountry`, `DownloadFabsWikidata`, `MergeFabs`, workflows `FetchFabsByCountry` / `BuildSemiconductorMap`) ·
**Handler:** `src/save_earth/handlers/sources/source_handlers.py` ·
**Tool:** `src/save_earth/tools/_save_earth_tools/semiconductor.py` ·
**Tests:** `tests/test_semiconductor.py`

## Overview

A world map of semiconductor fabrication plants (fabs), unioning two open sources:
**OpenStreetMap** (fetched **per country**, fanned out across the fleet) and
**Wikidata** (one global SPARQL query). This is the domain's canonical
**do-fan-out** feature — the counterpoint to the transmission "when *not* to fan
out" case — because the OSM fetch is a per-country `area` query, and a country is a
natural, cache-friendly fan-out unit.

## How it works

`BuildSemiconductorMap` composes five facets:

1. **`ListFabCountries`** — enumerates admin-0 countries from the **Natural Earth**
   admin-0 set (`NATURAL_EARTH_COUNTRIES`, not a hardcoded list) → a JSON list of
   `{iso2, name}`.
2. **`FetchFabsByCountry`** — a `workflow` with `foreach c in $.countries`, one
   `DownloadFabsForCountry` distributed task per country, accumulating each
   country's cached relative path. **Cache-first** (`force=false`): a present
   per-country cache is always reused, so a re-run never re-queries Overpass;
   per-country failures are tolerated and simply drop that country.
3. **`DownloadFabsForCountry`** — an Overpass `area` query keyed on `ISO3166-1`,
   collecting `industrial=semiconductor` / `industry=semiconductor` / `man_made=works`
   + `product` matching semiconductor/IC/wafer/microchip terms; keeps all OSM tags
   verbatim plus derived `osm_type`/`osm_id`/`osm_url`. Writes
   `by-country/<ISO2>.geojson`.
4. **`DownloadFabsWikidata`** — one global SPARQL query to the Wikidata Query Service
   for items that are (subclasses of) a semiconductor fabrication plant with
   coordinates, adding structured fields OSM rarely has (operator, country,
   inception, owner, wikipedia). Does **not** fan out — there is no per-area query
   limit to work around.
5. **`MergeFabs`** — merges the per-country OSM parts (dedupe by `osm_type` + `osm_id`)
   and unions the Wikidata features into the rendered `fabs.geojson`.

`BuildMap` renders the merged `semiconductor-fabs` layer.

## Fan-out

**Per-country fan-out (OSM) + single global query (Wikidata).** `FetchFabsByCountry`'s
`foreach` spreads one `DownloadFabsForCountry` task per country across the runner
fleet; being cache-first, most countries return instantly on a re-run and only
uncached ones hit Overpass. The Wikidata source is a single SPARQL — no fan-out, no
per-area limit.

## Data & fields

- **OSM fabs**: all OSM tags verbatim + `osm_type`/`osm_id`/`osm_url`; `source`
  implicitly OSM.
- **Wikidata fabs**: `source="wikidata"`, plus `wikidata_url`/`wikidata_id` and the
  structured operator/country/inception/owner/wikipedia fields.
- Merged layer `fabs.geojson` (`MERGED_RELATIVE_PATH`); Wikidata staged at
  `wikidata.geojson` (`WIKIDATA_RELATIVE_PATH`); per-country parts under
  `by-country/<ISO2>.geojson`. Popups (`description_fields=None`) show each source's
  fields.

## External libraries / binaries

- **`requests`** (pip) — Overpass POST + Wikidata SPARQL GET. GeoJSON/JSON parsed
  with the stdlib. No binary/geospatial dependency.

## Facets & workflows

| Facet / workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `ListFabCountries()` | event | external / cheap | Natural Earth admin-0 countries `{iso2, name}`. |
| `DownloadFabsForCountry(country_iso2, country_name, force)` | event | external / moderate | One country's fabs from Overpass (cache-first). |
| `DownloadFabsWikidata(force)` | event | external / moderate | All geocoded fabs from Wikidata (one SPARQL). |
| `MergeFabs(parts, wikidata_path)` | event | pure / cheap | Merge OSM parts (dedupe) + union Wikidata → `fabs.geojson`. |
| `FetchFabsByCountry(countries, force)` | workflow | — | `foreach` per-country fan-out → `fab_paths`. |
| `BuildSemiconductorMap(force)` | workflow | — | Enumerate → fan out → merge → render `semiconductor-fabs`. |

## Cache / output

- Cache namespace `save-earth`, `cache_type` `semiconductor`; artifacts
  `by-country/<ISO2>.geojson`, `wikidata.geojson`, and the merged `fabs.geojson`
  (each + sidecar).
- Rendered map at `save-earth/maps/semiconductor/index.html`; the docstring notes
  publishing separately via `census.workflows.PublishToSite` (dest `world/semiconductor`).
- Backend per `FW_STORAGE`.

## Gotchas & notes

- **OSM fab tagging is sparse and inconsistent** — many real fabs aren't tagged as
  semiconductor works, so coverage skews to well-mapped regions. The Wikidata union
  is there precisely to fill structured gaps; both sources are honestly incomplete.
- **Cache-first is what makes the fan-out cheap.** Without it, every re-run would
  re-query Overpass for every country and hit the shared-egress rate limit.
- **The country set is Natural Earth, not hardcoded** — the same admin-0 set the
  osm-mapping domain uses.

## Related specs

- [power-transmission](power-transmission.md) — the *don't*-fan-out counterpart.
- [osm-overpass-sources](osm-overpass-sources.md) — the single-query OSM sources.
- [map-rendering](map-rendering.md) — the `semiconductor-fabs` layer.
- [workflows](workflows.md) — `FetchFabsByCountry` / `BuildSemiconductorMap`.
