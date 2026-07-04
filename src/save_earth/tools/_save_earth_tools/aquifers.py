"""Principal aquifers — US groundwater polygons as GeoJSON from USGS.

Queries the USGS "Principal Aquifers of the United States" ArcGIS FeatureServer
for the aquifer polygons, server-side simplified, and caches them as one GeoJSON
FeatureCollection. These are the underground water bodies the US relies on; the
map overlays data-center locations on them to show where high-water-use compute
sits over major groundwater. Rock-only regions (``ROCK_TYPE`` 999 = "not a
principal aquifer") are excluded so the layer shows real aquifers.

Each Feature keeps ``AQ_NAME`` (aquifer name) + ``ROCK_TYPE``. Pass
``use_mock=True`` for a tiny offline polygon set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _save_earth_tools import sidecar  # noqa: E402
from _save_earth_tools.storage import Storage, get_storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("save-earth.aquifers")

NAMESPACE = "save-earth"
CACHE_TYPE = "aquifers"
RELATIVE_PATH = "aquifers.geojson"

AQUIFER_LAYER = ("https://services1.arcgis.com/RQG3sksSXcoDoIfj/arcgis/rest/services/"
                 "Principal_Aquifers_of_the_United_States/FeatureServer/0")
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
PAGE = 1000                 # server maxRecordCount is 2000; page conservatively
SIMPLIFY_OFFSET = 0.02      # degrees (~2 km) server-side generalisation
DEFAULT_MAX_AGE_HOURS = 24.0 * 90  # aquifer geology is essentially static

_lock = threading.Lock()


@dataclass
class DownloadResult:
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    was_cached: bool
    source_url: str
    used_mock: bool = False


def download(*, force: bool = False, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
             storage: Storage | None = None, use_mock: bool = False) -> DownloadResult:
    """Fetch simplified US principal-aquifer polygons and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("aquifers cache hit (%.1fh old)", age or -1.0)
                    return DownloadResult(art_path, RELATIVE_PATH, side.get("size_bytes", 0),
                                          side.get("sha256", ""),
                                          int((side.get("extra") or {}).get("feature_count", 0)),
                                          True, AQUIFER_LAYER)
        if use_mock:
            features, source_url, used_mock = _mock_features(), "mock://aquifers", True
        else:
            if requests is None:
                raise RuntimeError("requests is not installed; run via the .sh wrapper or --use-mock.")
            features, source_url = _fetch_arcgis()
            used_mock = False
        body = json.dumps({"type": "FeatureCollection", "features": features},
                          separators=(",", ":")).encode("utf-8")
        return _persist(body, s, source_url=source_url, used_mock=used_mock)


def _fetch_arcgis() -> tuple[list[dict[str, Any]], str]:
    """Paginate the FeatureServer, requesting simplified GeoJSON, real aquifers only."""
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "where": "ROCK_TYPE<>999",   # exclude "Other rocks" (not a principal aquifer)
            "outFields": "AQ_NAME,ROCK_TYPE",
            "f": "geojson", "returnGeometry": "true",
            "maxAllowableOffset": SIMPLIFY_OFFSET, "geometryPrecision": 4,
            "resultOffset": offset, "resultRecordCount": PAGE,
        }
        resp = requests.get(f"{AQUIFER_LAYER}/query", params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=(30, 180))
        resp.raise_for_status()
        fc = resp.json()
        page = fc.get("features") or []
        for f in page:
            geom = f.get("geometry")
            props = f.get("properties") or {}
            if geom and props.get("AQ_NAME"):
                features.append({"type": "Feature", "geometry": geom,
                                 "properties": {"AQ_NAME": props.get("AQ_NAME"),
                                                "ROCK_TYPE": props.get("ROCK_TYPE")}})
        logger.info("aquifers: +%d (offset %d, total %d)", len(page), offset, len(features))
        if len(page) < PAGE or not fc.get("exceededTransferLimit", len(page) == PAGE):
            if len(page) < PAGE:
                break
        offset += PAGE
        if offset > 20000:  # safety stop
            break
    if not features:
        raise RuntimeError("USGS aquifer FeatureServer returned no polygons")
    return features, AQUIFER_LAYER


def _persist(body: bytes, storage: Storage, *, source_url: str, used_mock: bool) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{RELATIVE_PATH}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)
    try:
        feature_count = len(json.loads(body).get("features") or [])
    except Exception:
        feature_count = 0
    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, kind="file",
                              size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                              source={"publisher": "USGS — Principal Aquifers of the United States",
                                      "url": source_url, "license": "US Government public domain",
                                      "used_mock": used_mock},
                              tool={"name": "aquifers", "version": "1.0"},
                              extra={"feature_count": feature_count}, storage=storage)
    return DownloadResult(final_path, RELATIVE_PATH, len(body), hashlib.sha256(body).hexdigest(),
                          feature_count, False, source_url, used_mock)


def _age_hours(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    from datetime import datetime
    try:
        ts = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _mock_features() -> list[dict[str, Any]]:
    """Two coarse rectangles standing in for aquifer polygons (offline)."""
    def rect(name, w, s, e, n):
        return {"type": "Feature",
                "geometry": {"type": "Polygon",
                             "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
                "properties": {"AQ_NAME": name, "ROCK_TYPE": 100}}
    return [rect("High Plains aquifer (Ogallala)", -104, 32, -97, 43),
            rect("Basin and Range aquifers", -117, 32, -110, 42)]
