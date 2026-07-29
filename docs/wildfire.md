# Active fires / thermal anomalies (NASA FIRMS)

World map of satellite-detected thermal anomalies over the past 24 hours, split
into three confidence bands with each circle scaled by fire radiative power.

    save_earth.workflows.BuildWildfireMap
      -> save_earth.sources.DownloadActiveFire   (VIIRS + MODIS -> one cached GeoJSON)
      -> save_earth.maps.BuildMap                (only_layers="fire-*")  after fires

## What this data is — and is not

FIRMS reports **thermal anomalies**: pixels whose infrared signature is far
hotter than their surroundings. Most are vegetation fires, but **gas flares,
industrial heat, active lava and agricultural burning register identically**, and
nothing in the feed distinguishes them. This pipeline therefore does *not*
classify detections as wildfires — the layers are labelled "thermal anomalies"
and the map says so.

That distinction is not pedantry. On a typical day the single strongest detection
worldwide is a volcano: during development the top hit was 3,999 MW at
`19.40 N, 155.29 W` — Kīlauea. Persistent gas flares over oil-producing regions
light up the map the same way a fire front does.

**Absence means "not detected", never "not burning."** Detections are limited by
satellite overpass and cloud cover; a fire under thick cloud or between passes
simply is not in the feed.

## Sources

Two keyless global 24-hour feeds, merged:

| Feed | Instrument | Resolution | Confidence in source |
|---|---|---|---|
| `viirs_snpp` | VIIRS on Suomi-NPP | 375 m | `low` / `nominal` / `high` |
| `modis` | MODIS on Terra + Aqua | 1 km | integer percentage 0-100 |

The `/api/area/` endpoints need a per-user `MAP_KEY`; these bulk CSVs do not,
which is what lets the pipeline run unattended on the fleet.

MODIS percentages are banded onto the VIIRS vocabulary (`<=30` low, `<=80`
nominal, else high — the split FIRMS itself documents) so **one**
`confidence_band` property works across both sensors. Without that, a band filter
would silently drop every MODIS detection.

## Cache

    cache/save-earth/active_fire/active_fire.geojson + .meta.json

One `FeatureCollection`, ~35 MB, ~130k detections on a normal day. Per-feature
properties are deliberately minimal — `sensor`, `confidence_band`,
`confidence_raw`, `frp`, `brightness_k`, `acquired_utc`, `daynight`, `satellite`.
Per-sensor constants (platform, resolution, feed URL) live once in the
collection's `properties.sensors`; repeating them per feature cost ~12 MB.

`max_age_hours` defaults to **1.0**, not days. This is the one near-real-time
layer in the package: a stale fire map is actively misleading in a way that a
stale plate-boundary map is not. The sidecar records `acquired_from` /
`acquired_to`, and the workflow's `detail` states that window so a map can never
silently imply it is live.

**Features are sorted by FRP descending.** This is load-bearing, not cosmetic —
see the cap below.

## The inline cap (read this before changing `max_inline_features`)

The renderer inlines GeoJSON into the HTML and caps each source with a plain
slice. At ~130k detections the cap *always* engages: the workflow defaults to
`max_inline_features = 40000`, producing a ~10 MB page.

Because the cached collection is FRP-sorted, the cap keeps the **most energetic**
detections rather than an arbitrary chunk of the file. That preserves the signal
that matters: in a representative run, 9,449 of 11,291 high-confidence detections
(84%) survived a cap that dropped 70% of all rows.

`layer_counts` reports what is **drawn**, not what is cached, and the renderer
logs a warning naming how many features were dropped. It did not always: the
counts were computed pre-cap, so this map reported 131,574 while drawing 40,000 —
overstating itself 3.3x with nothing in the output to reveal it.

## Layers

Three `LayerSpec`s over **one** cached file, selected with `filter_field` —
duplicating a 35 MB source per band would triple the payload for nothing.

| Layer | Band | Colour |
|---|---|---|
| `fire-low` | low confidence | amber `#ffd54f` |
| `fire-nominal` | nominal | orange `#fb8c00` |
| `fire-high` | high | red `#d50000` |

Drawn low → high so the strongest read on top; `magnitude_field="frp"` scales
each circle, so a 4,000 MW lava lake looks nothing like a 2 MW field burn.

⚠️ **The circle scale must use FRP stops, not the defaults.** `LayerSpec`'s
magnitude defaults are earthquake-tuned (Richter 4/6/8 → 3/9/22 px). FRP runs
0–4000 MW, so on those defaults every detection above 8 MW pinned at the 22 px
cap — measured at **100% of drawn dots**, median radius 22 px, rendering the map
as one solid blob. The fire layers set `magnitude_stops=(5, 100, 1000)` /
`magnitude_radii=(2, 5, 10)`, which gives median 2.5 px, p90 4.2 px and lets a
genuinely large fire stand out. They also set `magnitude_color=False`: colour
already encodes confidence here, and the magnitude ramp was overriding it so the
legend disagreed with the dots.

## US perimeters (NIFC / WFIGS)

Satellite detections have no incident identity — a point is a hot pixel, not
"the Park Fire". `DownloadFirePerimeters` adds the other half: **incident
polygons** with name, acreage, containment, cause and discovery date, from the
NIFC/WFIGS interagency feed.

    cache/save-earth/fire_perimeters/fire_perimeters.geojson

**United States only.** No equivalent global perimeter feed exists, so outside the
US an absent polygon means "not published", not "not burning". The layer titles
and the collection's own `note` say so.

**"Current" includes contained fires** — 60 of 231 observed perimeters were 100%
contained, 65 reported no containment figure at all. Drawing those like an active
fire would overstate the situation, so each feature carries a derived `status`
and the map splits it into three `fill` layers:

| Layer | Status | Colour |
|---|---|---|
| `fire-perimeter-active` | containment 0–99% | red `#e53935` |
| `fire-perimeter-unreported` | no containment figure | amber `#ffb74d` |
| `fire-perimeter-contained` | 100% | grey `#78909c` |

`unreported` is deliberately its own layer rather than folded into `active`:
absent data is not evidence of containment, and the reader should see which it is.
They are listed **before** the point layers so polygons draw underneath the dots
(pinned by a test).

Two gotchas worth knowing:

* **Geometry is generalised server-side to ~111 m** (`maxAllowableOffset=0.001`).
  The raw 231-perimeter response is **26.8 MB**; generalised it is **0.78 MB** — a
  36× reduction with every feature retained, and below one screen pixel until deep
  zoom. Perimeters are aircraft/GPS-mapped with their own error, so this changes
  nothing a reader could act on — but for operational use go to WFIGS directly.
* **The endpoint returns HTTP 200 with a 429 *inside the JSON body*** when the
  shared ArcGIS quota is exhausted. A client checking only the status code caches
  an error document as data. `_fetch_page` inspects the body and backs off.

## Basemap: why not CalTopo

CalTopo's layers are a **subscription product**; pointing this map at their tile
servers would be freeloading on their bandwidth and almost certainly against
their terms — there is no published third-party tile permission, and absence of a
prohibition is not permission. So we do not.

What CalTopo largely curates *is* public domain, and available directly. `BuildMap`
already takes `basemap_url` / `basemap_attribution`, so no code change is needed:

```
basemap_url = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
basemap_attribution = "Basemap: USGS The National Map (public domain)"
```

(`USGSImageryTopo` for imagery+topo.) Note the ArcGIS path order is `{z}/{y}/{x}`,
not the usual `{z}/{x}/{y}` — MapLibre substitutes the tokens wherever they appear,
so the template works as written. 24 zoom levels, no key, US coverage.

## Run it

```bash
# CLI (cache only)
python src/save_earth/tools/download_wildfires.py            # live fetch
python src/save_earth/tools/download_wildfires.py --force    # ignore cache
python src/save_earth/tools/download_wildfires.py --feeds viirs_snpp
python src/save_earth/tools/download_wildfires.py --use-mock # offline

# the whole map
fw ffl run --primary src/save_earth/ffl/save_earth.ffl \
  --workflow save_earth.workflows.BuildWildfireMap
```

⚠️ **A runner on an older image will render an empty map, not an error.** The
`DownloadActiveFire` facet is name-filtered so only a current runner claims it,
but `BuildMap` exists in both old and new code — an old runner wins the race,
matches no `fire-*` layer, and yields `status="completed"` with an empty
`html_path`. Roll the fleet (or stop the stale runner) before running this
workflow against a mixed-version fleet.

## Tests

`tests/test_wildfire_map.py` — fully offline via `use_mock=True`. Covers the
cross-sensor band normalisation, the FRP sort, the acquisition window, the
three-bands-from-one-source split, and a regression test pinning that
`layer_counts` never exceeds the inline cap.
