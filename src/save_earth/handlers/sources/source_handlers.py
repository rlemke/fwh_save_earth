"""Source handlers — download per-source GeoJSON + sidecar.

Each handler is a thin parameter-coercion layer over one
``_save_earth_tools`` function. MongoDB / state side-effects stay out — these handlers
write only to the filesystem cache the tools manage.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..shared.save_earth_utils import (
    enclaves,
    power,
    epa_cleanups,
    lgbtq,
    nuclear,
    openlittermap,
    parse_bbox,
    seismic,
    semiconductor,
    siting,
    telescope,
    tesla,
    tri,
    volcanoes,
)

logger = logging.getLogger("save-earth.sources")
NAMESPACE = "save_earth.sources"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def _result_payload(res: Any) -> dict[str, Any]:
    """Project a dataclass FetchResult into the FFL-visible return shape.

    ``cache_type`` is intentionally not included — each handler spreads
    this dict **after** setting its own cache_type, so setting it here
    would shadow the explicit value via Python's dict-merge semantics
    (later keys win). Callers: ``return {"cache_type": <X>.CACHE_TYPE,
    **_result_payload(res)}``.
    """
    return {
        "relative_path": res.relative_path,
        "feature_count": res.feature_count,
        "size_bytes": res.size_bytes,
        "sha256": res.sha256,
        "source_url": res.source_url,
        "was_cached": res.was_cached,
        "used_mock": res.used_mock,
    }


def handle_download_openlittermap(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadOpenLitterMap."""
    mode = params.get("mode", "clusters")
    zoom = int(params.get("zoom", 4))
    bbox_str = params.get("bbox", "") or ""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    bbox = parse_bbox(bbox_str)
    _step_log(
        step_log,
        f"DownloadOpenLitterMap mode={mode} zoom={zoom} bbox={bbox_str or '(none)'}",
    )

    res = openlittermap.download(
        mode=mode,
        zoom=zoom,
        bbox=bbox,
        force=force,
        use_mock=use_mock,
    )
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] openlittermap/{res.relative_path}  {res.feature_count:,} features",
        "success",
    )
    return {"cache_type": openlittermap.CACHE_TYPE, **_result_payload(res)}


def handle_download_tri(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadTri — EPA Toxic Release Inventory facility points."""
    active_only = bool(params.get("active_only", True))
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadTri active_only={active_only}")
    res = tri.download(active_only=active_only, force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] tri/{res.absolute_path}  {res.feature_count:,} facilities",
        "success",
    )
    return {"cache_type": tri.CACHE_TYPE, **_result_payload(res)}


def handle_download_epa_cleanups(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadEpaCleanups."""
    dataset = params.get("dataset", "superfund")
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    if dataset not in epa_cleanups.DEFAULT_URLS:
        raise ValueError(
            f"unknown EPA dataset {dataset!r}; choices: {sorted(epa_cleanups.DEFAULT_URLS)}"
        )

    _step_log(step_log, f"DownloadEpaCleanups dataset={dataset}")
    res = epa_cleanups.download(dataset, force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] epa-cleanups/{res.relative_path}  {res.feature_count:,} features",
        "success",
    )
    return {"cache_type": epa_cleanups.CACHE_TYPE, **_result_payload(res)}


def handle_download_nuclear_reactors(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadNuclearReactors — worldwide nuclear power features from OSM."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadNuclearReactors force={force} use_mock={use_mock}")
    res = nuclear.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] nuclear/{res.relative_path}  {res.feature_count:,} reactors/plants",
        "success",
    )
    return {"cache_type": nuclear.CACHE_TYPE, **_result_payload(res)}


def handle_download_ethnic_enclaves(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadEthnicEnclaves — heritage-named enclave neighbourhoods from OSM.

    Writes one GeoJSON per heritage (chinese/japanese/italian/…); returns the
    total feature count + heritage count so the map build can sequence after it.
    """
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadEthnicEnclaves force={force} use_mock={use_mock}")
    res = enclaves.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    top = ", ".join(
        f"{k}={v}" for k, v in sorted(res.per_heritage.items(), key=lambda x: -x[1])[:5]
    )
    _step_log(
        step_log,
        f"[{status}] enclaves  {res.feature_count:,} places across {res.heritage_count} heritages ({top})",
        "success",
    )
    return {
        "cache_type": enclaves.CACHE_TYPE,
        "feature_count": res.feature_count,
        "heritage_count": res.heritage_count,
        "source_url": res.source_url,
        "was_cached": res.was_cached,
        "used_mock": res.used_mock,
    }


def handle_download_power_plants(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadPowerPlants — world power plants by source (WRI database)."""
    step_log = params.get("_step_log")
    _step_log(step_log, "DownloadPowerPlants (WRI Global Power Plant Database)")
    res = power.download_plants(force=bool(params.get("force", False)))
    top = ", ".join(f"{k}={v}" for k, v in sorted(res.per_layer.items(), key=lambda x: -x[1]))
    _step_log(step_log, f"[download] power plants  {res.feature_count:,} ({top})", "success")
    return {
        "cache_type": power.CACHE_TYPE,
        "feature_count": res.feature_count,
        "layer_count": len(res.per_layer),
        "source_url": res.source_url,
    }


def handle_annotate_renewable_siting(params: dict[str, Any]) -> dict[str, Any]:
    """Handle AnnotateRenewableSiting — sample NASA POWER resource at each
    solar/wind plant and write the siting-scored layers."""
    step_log = params.get("_step_log")
    force = bool(params.get("force", False))
    _step_log(step_log, f"AnnotateRenewableSiting force={force} (NASA POWER climatology)")
    res = siting.annotate(force=force)
    per = ", ".join(f"{k}={v}" for k, v in sorted(res.per_layer.items()))
    _step_log(
        step_log,
        f"[siting] {res.feature_count:,} plants, {res.cells_sampled} cells sampled "
        f"({per}) cached={res.was_cached}",
        "success",
    )
    return {
        "cache_type": siting.CACHE_TYPE,
        "feature_count": res.feature_count,
        "cells_sampled": res.cells_sampled,
        "was_cached": res.was_cached,
        "source_url": res.source_url,
    }


def handle_list_transmission_tiles(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ListTransmissionTiles — emit the tile list (Json) for the fan-out."""
    tiles = power.list_tiles()
    sl = params.get("_step_log")
    _step_log(sl, f"ListTransmissionTiles: {len(tiles)} tiles")
    return {"tiles": tiles, "count": len(tiles)}


def handle_download_transmission(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadTransmission — ≥500 kV lines worldwide (bounded, cache-aware).

    Fetches the tiles one at a time (Overpass rate-limits ~2/IP, so a wide fan-out
    just thrashes), and only when ``transmission.geojson`` isn't already cached."""
    step_log = params.get("_step_log")
    force = bool(params.get("force", False))
    _step_log(step_log, f"DownloadTransmission force={force}")
    res = power.download_transmission(force=force)
    status = "cache" if res.was_cached else "download"
    _step_log(step_log, f"[{status}] {res.feature_count:,} transmission lines ≥500 kV", "success")
    return {
        "cache_type": power.CACHE_TYPE,
        "feature_count": res.feature_count,
        "was_cached": res.was_cached,
    }


def handle_download_transmission_tile(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadTransmissionTile — ≥500 kV lines for ONE bbox (fan-out leaf)."""
    step_log = params.get("_step_log")
    bbox = params.get("bbox", "")
    _step_log(step_log, f"DownloadTransmissionTile bbox={bbox}")
    n = power.download_transmission_tile(bbox, force=bool(params.get("force", False)))
    _step_log(step_log, f"[tile {bbox}] {n:,} lines ≥500 kV", "success")
    return {"bbox": bbox, "feature_count": n}


def handle_merge_transmission(params: dict[str, Any]) -> dict[str, Any]:
    """Handle MergeTransmission — merge the per-tile line GeoJSONs (dedupe by way id)."""
    step_log = params.get("_step_log")
    tiles = params.get("tiles") or power.list_tiles()
    _step_log(step_log, f"MergeTransmission ({len(tiles)} tiles)")
    n = power.merge_transmission(list(tiles))
    _step_log(step_log, f"[merge] {n:,} transmission lines ≥500 kV", "success")
    return {"cache_type": power.CACHE_TYPE, "feature_count": n}


def handle_download_volcanoes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadVolcanoes — worldwide major (notable) volcanoes from OSM."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadVolcanoes force={force} use_mock={use_mock}")
    res = volcanoes.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] volcanoes/{res.relative_path}  {res.feature_count:,} volcanoes",
        "success",
    )
    return {"cache_type": volcanoes.CACHE_TYPE, **_result_payload(res)}


def handle_download_lgbtq_venues(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadLgbtqVenues — worldwide LGBTQ+ bars & restaurants from OSM."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadLgbtqVenues force={force} use_mock={use_mock}")
    res = lgbtq.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] lgbtq/{res.relative_path}  {res.feature_count:,} venues",
        "success",
    )
    return {"cache_type": lgbtq.CACHE_TYPE, **_result_payload(res)}


def handle_download_telescopes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadTelescopes — worldwide named research telescopes from OSM."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadTelescopes force={force} use_mock={use_mock}")
    res = telescope.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] telescopes/{res.relative_path}  {res.feature_count:,} telescopes",
        "success",
    )
    return {"cache_type": telescope.CACHE_TYPE, **_result_payload(res)}


def handle_download_tesla_chargers(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadTeslaChargers — worldwide Tesla charging stations from OSM."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadTeslaChargers force={force} use_mock={use_mock}")
    res = tesla.download(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] tesla/{res.relative_path}  {res.feature_count:,} chargers",
        "success",
    )
    return {"cache_type": tesla.CACHE_TYPE, **_result_payload(res)}


def handle_download_earthquakes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadEarthquakes — recent significant quakes from the USGS feed."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadEarthquakes force={force} use_mock={use_mock}")
    res = seismic.download_earthquakes(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] earthquakes/{res.relative_path}  {res.feature_count:,} quakes",
        "success",
    )
    return {"cache_type": seismic.EARTHQUAKES_CACHE_TYPE, **_result_payload(res)}


def handle_download_faults(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadFaults — tectonic plate boundaries (Bird 2002 PB2002)."""
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadFaults force={force} use_mock={use_mock}")
    res = seismic.download_faults(force=force, use_mock=use_mock)
    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    _step_log(
        step_log,
        f"[{status}] faults/{res.relative_path}  {res.feature_count:,} boundary lines",
        "success",
    )
    return {"cache_type": seismic.FAULTS_CACHE_TYPE, **_result_payload(res)}


def handle_list_fab_countries(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ListFabCountries — admin-0 countries (Natural Earth) for the fan-out."""
    step_log = params.get("_step_log")
    countries = semiconductor.list_fab_countries()
    _step_log(step_log, f"ListFabCountries → {len(countries)} countries", "success")
    return {"countries": countries, "country_count": len(countries)}


def handle_download_fabs_for_country(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadFabsForCountry — one country's semiconductor fabs from OSM."""
    iso2 = params.get("country_iso2", "") or ""
    name = params.get("country_name", "") or ""
    force = bool(params.get("force", False))
    step_log = params.get("_step_log")
    res = semiconductor.download_fabs_for_country(iso2, name, force=force)
    status = "cache" if res.was_cached else "download"
    _step_log(step_log, f"[{status}] {iso2} ({name}) → {res.feature_count} fab(s)", "success")
    return {
        "relative_path": res.relative_path,
        "feature_count": res.feature_count,
        "was_cached": res.was_cached,
    }


def handle_download_fabs_wikidata(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DownloadFabsWikidata — all geocoded fabs from Wikidata (one SPARQL)."""
    force = bool(params.get("force", False))
    step_log = params.get("_step_log")
    res = semiconductor.download_fabs_wikidata(force=force)
    status = "cache" if res.was_cached else "download"
    _step_log(step_log, f"[{status}] wikidata → {res.feature_count} fab(s)", "success")
    return {
        "relative_path": res.relative_path,
        "feature_count": res.feature_count,
        "was_cached": res.was_cached,
    }


def handle_merge_fabs(params: dict[str, Any]) -> dict[str, Any]:
    """Handle MergeFabs — merge per-country OSM fabs (+ optional Wikidata) into the layer."""
    parts = params.get("parts") or []
    wikidata_path = params.get("wikidata_path", "") or ""
    step_log = params.get("_step_log")
    res = semiconductor.merge_fabs(list(parts), wikidata_path)
    _step_log(
        step_log,
        f"MergeFabs → {res.feature_count} fabs / {res.country_count} OSM countries",
        "success",
    )
    return {
        "relative_path": res.relative_path,
        "feature_count": res.feature_count,
        "country_count": res.country_count,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.ListFabCountries": handle_list_fab_countries,
    f"{NAMESPACE}.DownloadFabsForCountry": handle_download_fabs_for_country,
    f"{NAMESPACE}.DownloadFabsWikidata": handle_download_fabs_wikidata,
    f"{NAMESPACE}.MergeFabs": handle_merge_fabs,
    f"{NAMESPACE}.DownloadOpenLitterMap": handle_download_openlittermap,
    f"{NAMESPACE}.DownloadEpaCleanups": handle_download_epa_cleanups,
    f"{NAMESPACE}.DownloadTri": handle_download_tri,
    f"{NAMESPACE}.DownloadNuclearReactors": handle_download_nuclear_reactors,
    f"{NAMESPACE}.DownloadEthnicEnclaves": handle_download_ethnic_enclaves,
    f"{NAMESPACE}.DownloadPowerPlants": handle_download_power_plants,
    f"{NAMESPACE}.AnnotateRenewableSiting": handle_annotate_renewable_siting,
    f"{NAMESPACE}.DownloadTransmission": handle_download_transmission,
    f"{NAMESPACE}.ListTransmissionTiles": handle_list_transmission_tiles,
    f"{NAMESPACE}.DownloadTransmissionTile": handle_download_transmission_tile,
    f"{NAMESPACE}.MergeTransmission": handle_merge_transmission,
    f"{NAMESPACE}.DownloadVolcanoes": handle_download_volcanoes,
    f"{NAMESPACE}.DownloadLgbtqVenues": handle_download_lgbtq_venues,
    f"{NAMESPACE}.DownloadTeslaChargers": handle_download_tesla_chargers,
    f"{NAMESPACE}.DownloadTelescopes": handle_download_telescopes,
    f"{NAMESPACE}.DownloadEarthquakes": handle_download_earthquakes,
    f"{NAMESPACE}.DownloadFaults": handle_download_faults,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH[facet]
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_source_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
