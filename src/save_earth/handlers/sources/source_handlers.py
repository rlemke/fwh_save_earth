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
    epa_cleanups,
    lgbtq,
    nuclear,
    openlittermap,
    parse_bbox,
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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.DownloadOpenLitterMap": handle_download_openlittermap,
    f"{NAMESPACE}.DownloadEpaCleanups": handle_download_epa_cleanups,
    f"{NAMESPACE}.DownloadTri": handle_download_tri,
    f"{NAMESPACE}.DownloadNuclearReactors": handle_download_nuclear_reactors,
    f"{NAMESPACE}.DownloadVolcanoes": handle_download_volcanoes,
    f"{NAMESPACE}.DownloadLgbtqVenues": handle_download_lgbtq_venues,
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
