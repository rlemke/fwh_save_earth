# FFL Examples — `save-earth`

Every numbered scenario is a **complete, compilable FFL file**. Copy one into
`my.ffl` and run it:

```bash
fw ffl run --primary my.ffl \
  --library ~/fw_handlers/fwh_save_earth/src/save_earth/ffl/save_earth.ffl \
  --workflow my.save_earth.<WorkflowName>
```

A runner serving the `save_earth` namespace must be up
(`fw runner start --domain save-earth`). Every block below is compile-checked
against the domain's FFL. Pass `use_mock = true` to exercise the pipeline offline.

New to the language? Start with the
[FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md)
and the [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical).

---

## The shape of this domain

Many small **source** facets that each fetch-and-cache one open dataset, and one
generic **`BuildMap`** that renders whatever is in the cache. Adding a map means
adding a source and naming its layer — not writing a new renderer.

| Declaration | Role |
|---|---|
| `save_earth.mixins.RetryPolicy(max_retries = 3, backoff_ms = 2000)` | Custom mixin, attached to every network facet (plus an `implicit` default) |
| `save_earth.sources.Download*` | One per dataset: OpenLitterMap, TRI, EPA cleanups, nuclear reactors/sites, ALPR cameras, data centers, aquifers, power plants, volcanoes, earthquakes, faults, telescopes, … |
| `save_earth.sources.ListTransmissionTiles` / `DownloadTransmissionTile` / `ScanTransmissionTiles` / `MergeTransmission` | A tiled fan-out + merge, for a source too big for one query |
| `save_earth.sources.ListFabCountries` / `DownloadFabsForCountry` / `MergeFabs` + `save_earth.workflows.FetchFabsByCountry` | Per-country fan-out + merge |
| `save_earth.maps.BuildMap(region, center_lat, center_lon, zoom, only_layers, …)` | The generic renderer |
| `save_earth.workflows.Build*Map` | The shipped end-to-end map workflows |

---

## 1. Run what ships — no FFL to write

```bash
fw ffl seed --include save-earth

fw ffl run --workflow save_earth.workflows.BuildGlobalMap
fw ffl run --workflow save_earth.workflows.BuildNuclearReactorMap
fw ffl run --workflow save_earth.workflows.BuildALPRCameraMap
```

Write FFL when you want a different *shape* — your own combination of layers, a
regional view, extra error handling, or a new dataset dropped into the same
renderer.

## 2. One source, one map

Every FFL workflow needs a `namespace`, a `use` per namespace it calls into, and a
`yield` back to itself. `only_layers` picks which cached layers the generic
renderer draws; `after` sequences the render behind the fetch.

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.maps

    /** Fetch volcanoes, render just that layer. */
    workflow VolcanoMap() => (html_path: String, layers: Int) andThen {

        volc = save_earth.sources.DownloadVolcanoes(force = false)

        map = save_earth.maps.BuildMap(
            region = "global",
            zoom = 2.0,
            only_layers = "volcanoes"
            ) after volc

        yield VolcanoMap(html_path = map.html_path, layers = map.layer_count)
    }
}
```

Rules visible above: `=>` sits on the **same line** as the closing `)`; references
are always `step.field`; `$.x` reads the container's parameters.

## 3. Several sources in parallel, one map

Steps that reference nothing from each other are dispatched **concurrently**. Only
the render waits, because it references one of them.

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

> Note: without an `after` clause naming every fetch, only a step the map actually
> references is *guaranteed* to finish first. The shipped workflows name them all —
> `after a, b, c` — for exactly this reason; see `BuildGlobalMap`.

## 4. A dead source shouldn't sink the map — `catch`

This is the domain's signature pattern: every external download is wrapped so one
unreachable open-data endpoint degrades the run instead of failing it.

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.maps

    /** Best-effort: report which source failed, still finish cleanly. */
    workflow BestEffortHazardMap() => (status: String, html_path: String, detail: String) andThen {

        quakes = save_earth.sources.DownloadEarthquakes() catch {
            yield BestEffortHazardMap(
                status = "partial_failure", html_path = "", detail = "usgs earthquakes failed")
        }

        volc = save_earth.sources.DownloadVolcanoes() catch {
            yield BestEffortHazardMap(
                status = "partial_failure", html_path = "", detail = "volcanoes failed")
        }

        map = save_earth.maps.BuildMap(
            region = "global",
            only_layers = "earthquakes,volcanoes"
            ) after volc

        yield BestEffortHazardMap(
            status = "completed", html_path = map.html_path, detail = "")
    }
}
```

## 5. Tiled fan-out + merge — when one query is too big

`ListTransmissionTiles` emits bbox strings; `ScanTransmissionTiles` fans a fetch
out over them (one runtime step per tile, in parallel); `MergeTransmission` fans
them back in, ordered with `after` — naming a `foreach` step waits for every
iteration.

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.maps

    /** Tile the world, fetch tiles in parallel, merge, then render. */
    workflow TransmissionMap() => (html_path: String, features: Int) andThen {

        tiles = save_earth.sources.ListTransmissionTiles()

        scanned = save_earth.sources.ScanTransmissionTiles(tiles = tiles.tiles)

        merged = save_earth.sources.MergeTransmission(
            tiles = tiles.tiles
            ) after scanned

        map = save_earth.maps.BuildMap(
            region = "global",
            only_layers = "transmission"
            ) after merged

        yield TransmissionMap(html_path = map.html_path, features = merged.feature_count)
    }
}
```

A region that times out retries on its own instead of sinking the whole fetch —
that is the point of tiling.

## 6. Fan out per country and collect the results

`FetchFabsByCountry` shows two idioms worth stealing: a loop variable that is a
**Json object** (`$.c.iso2`, `$.c.name`), and a `yield` of a one-element array
that accumulates into a list across iterations.

```ffl
namespace my.save_earth {

    use save_earth.sources

    /** One Overpass query per country, in parallel; collect the cache paths. */
    workflow MyFabScan(countries: Json, force: Boolean = false) => (fab_paths: [String]) andThen foreach c in $.countries {

        fab = save_earth.sources.DownloadFabsForCountry(
            country_iso2 = $.c.iso2,
            country_name = $.c.name,
            force = $.force)

        yield MyFabScan(fab_paths = [fab.relative_path])
    }
}
```

Then merge the collected paths:

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.workflows

    /** Enumerate → fan out → merge, the full per-country pattern. */
    workflow MyFabMerge(force: Boolean = false) => (path: String, countries: Int) andThen {

        countries = save_earth.sources.ListFabCountries()

        fetched = save_earth.workflows.FetchFabsByCountry(
            countries = countries.countries, force = $.force)

        wd = save_earth.sources.DownloadFabsWikidata(force = $.force)

        merged = save_earth.sources.MergeFabs(
            parts = fetched.fab_paths,
            wikidata_path = wd.relative_path)

        yield MyFabMerge(path = merged.relative_path, countries = merged.country_count)
    }
}
```

## 7. Call-time mixins — including this domain's own

`RetryPolicy` is declared in `save_earth.mixins` and attached to every network
facet. At a **call site** you can add or override any mixin for one use:

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.mixins

    /** A throttled endpoint: more retries, longer backoff, bigger timeout. */
    workflow StubbornFetch() => (features: Int) andThen {

        dc = save_earth.sources.DownloadDataCenters(force = true) with RetryPolicy(max_retries = 8, backoff_ms = 10000) with Timeout(minutes = 45)

        yield StubbornFetch(features = dc.feature_count)
    }
}
```

## 8. Branch on a result — `when`

A `when` block hangs off the step it inspects: inside a case `$` is that step and
`$$` reaches the workflow. Every `when` needs a default case, last, and conditions
must be real `Boolean`s (no truthy coercion).

```ffl
namespace my.save_earth {

    use save_earth.sources
    use save_earth.maps

    /** Don't render a map off a suspiciously empty fetch. */
    workflow GuardedQuakeMap(min_features: Int = 100) => (status: String, html_path: String) andThen {

        quakes = save_earth.sources.DownloadEarthquakes() andThen when {
            case $.feature_count >= $$.min_features => {
                map = save_earth.maps.BuildMap(
                    region = "global", only_layers = "earthquakes")
                yield GuardedQuakeMap(status = "completed", html_path = map.html_path)
            }
            case _ => {
                yield GuardedQuakeMap(status = "empty_fetch", html_path = "")
            }
        }
    }
}
```

## 9. Reuse the shipped workflows

```ffl
namespace my.save_earth {

    use save_earth.workflows

    /** Wrap the shipped global map and reshape its result. */
    workflow GlobalWithHeadline() => (headline: String, html_path: String) andThen {

        built = save_earth.workflows.BuildGlobalMap()

        yield GlobalWithHeadline(
            headline = built.status ++ ": " ++ built.detail,
            html_path = built.html_path)
    }
}
```

---

## Cheat sheet

| You want to… | Write |
|---|---|
| Read a workflow/step parameter | `$.name` (`$$.name` one level out) |
| Read a field of a Json loop variable | `$.c.iso2` |
| Read a previous step's result | `stepname.field` |
| Run steps in parallel | write them with no reference between them |
| Order a render after a fetch | `step = Facet(…) after fetch` (only when no value flows) |
| Fan out over a list | `workflow W(items: Json) … andThen foreach i in $.items { … }` |
| Collect fan-out results | `yield W(paths = [step.relative_path])` — the arrays accumulate |
| Override a mixin for one call | `… with RetryPolicy(max_retries = 8) with Timeout(minutes = 45)` |
| Handle a step failure | `step = Facet(…) catch { yield … }` |
| Branch | `step = Facet(…) andThen when { case <bool> => { … } case _ => { … } }` |

**Validate before you run:** `afl my.ffl --check` or MCP `fw_validate`. Every error
carries a `rule_id` — fetch `fw://docs/rules/{rule_id}` for a wrong/right pair.

## See also

- [`docs/README.md`](README.md) — per-feature specs, one per dataset/map
- [FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md) ·
  [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical) ·
  [relative `$`-scoping](https://github.com/rlemke/facetwork/blob/main/docs/architecture/ffl-relative-scoping.md)
- `src/save_earth/ffl/save_earth.ffl` — the source of truth for every signature above
