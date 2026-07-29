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
