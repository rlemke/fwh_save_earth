"""Nuclear military sites — worldwide GeoJSON points from OSM (public tags).

Queries OpenStreetMap for two publicly-tagged, geographically-fixed feature sets
and caches them as one GeoJSON FeatureCollection so the map can show them as two
toggleable layers:

- ``military=nuclear_explosion_site`` — nuclear **test** sites (Nevada,
  Semipalatinsk, Lop Nur, Bikini, …), historical / decommissioned.
- ``bunker_type=missile_silo``        — **missile silos** / launch facilities.

These are already-public, crowd-sourced OSM tags (visible on openstreetmap.org and
published for transparency by arms-control groups); this visualises them, it does
not generate new information. Each Feature keeps all OSM tags verbatim plus a
derived ``site_type`` (``test_site`` / ``missile_silo``) so the two can be split
into separate toggleable map layers. Coverage is community-driven and incomplete
(especially for silos); this is the open set, not a comprehensive census.

Pass ``use_mock=True`` for a small offline set.
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

logger = logging.getLogger("save-earth.nuclear_sites")

NAMESPACE = "save-earth"
CACHE_TYPE = "nuclear-sites"
RELATIVE_PATH = "nuclear_sites.geojson"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30

OVERPASS_QUERY = (
    "[out:json][timeout:180];"
    "("
    'nwr["military"="nuclear_explosion_site"];'
    'nwr["bunker_type"="missile_silo"];'
    ");"
    "out center tags;"
)

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
    """Fetch nuclear test sites + missile silos and cache as GeoJSON points."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("nuclear-sites cache hit (%.1fh old)", age or -1.0)
                    return DownloadResult(art_path, RELATIVE_PATH, side.get("size_bytes", 0),
                                          side.get("sha256", ""),
                                          int((side.get("extra") or {}).get("feature_count", 0)),
                                          True, OVERPASS_ENDPOINTS[0])
        if use_mock:
            features, source_url, used_mock = _mock_features(), "mock://nuclear-sites", True
        else:
            if requests is None:
                raise RuntimeError("requests is not installed; run via the .sh wrapper or --use-mock.")
            features, source_url = _fetch_overpass()
            used_mock = False
        body = json.dumps({"type": "FeatureCollection", "features": features},
                          separators=(",", ":")).encode("utf-8")
        return _persist(body, s, source_url=source_url, used_mock=used_mock)


def _fetch_overpass() -> tuple[list[dict[str, Any]], str]:
    last_exc: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            logger.info("querying Overpass %s", endpoint)
            resp = requests.post(endpoint, data={"data": OVERPASS_QUERY},
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Overpass %s failed: %s", endpoint, exc)
            last_exc = exc
            continue
        elements = payload.get("elements")
        if not elements:
            remark = payload.get("remark") or "empty (throttled?)"
            logger.warning("Overpass %s: %s", endpoint, remark)
            last_exc = RuntimeError(f"Overpass {endpoint}: {remark}")
            continue
        features = [f for f in (_to_feature(el) for el in elements) if f]
        if not features:
            last_exc = RuntimeError(f"Overpass {endpoint}: elements had no coordinates")
            continue
        logger.info("Overpass %s -> %d nuclear-site features", endpoint, len(features))
        return features, endpoint
    raise RuntimeError(f"all Overpass mirrors failed/throttled (shared-IP cool-down); last: {last_exc}")


def _classify(tags: dict[str, Any]) -> str | None:
    if tags.get("military") == "nuclear_explosion_site":
        return "test_site"
    if tags.get("bunker_type") == "missile_silo":
        return "missile_silo"
    return None


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    tags = el.get("tags") or {}
    site_type = _classify(tags)
    if site_type is None:
        return None
    if el.get("lat") is not None and el.get("lon") is not None:
        lat, lon = el["lat"], el["lon"]
    elif isinstance(el.get("center"), dict):
        lat, lon = el["center"].get("lat"), el["center"].get("lon")
    else:
        return None
    if lat is None or lon is None:
        return None
    osm_type, osm_id = el.get("type", "node"), el.get("id")
    props: dict[str, Any] = dict(tags)
    props["site_type"] = site_type
    props["osm_type"], props["osm_id"] = osm_type, osm_id
    if osm_id is not None:
        props["osm_url"] = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": props}


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
                              source={"publisher": "OpenStreetMap contributors — Overpass API",
                                      "url": source_url, "license": "ODbL 1.0", "used_mock": used_mock},
                              tool={"name": "nuclear_sites", "version": "1.0"},
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
    base = [
        {"name": "Nevada Test Site", "military": "nuclear_explosion_site", "lat": 37.10, "lon": -116.05},
        {"name": "Semipalatinsk Test Site", "military": "nuclear_explosion_site", "lat": 50.12, "lon": 78.43},
        {"name": "Decommissioned Silo (museum)", "military": "bunker",
         "bunker_type": "missile_silo", "lat": 31.94, "lon": -110.76},
        {"name": "Silo field marker", "military": "bunker",
         "bunker_type": "missile_silo", "lat": 47.50, "lon": -101.30},
    ]
    out = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon")}
        tags["site_type"] = _classify(tags)
        tags["osm_type"], tags["osm_id"] = "node", 300000 + i
        tags["osm_url"] = f"https://www.openstreetmap.org/node/{300000 + i}"
        out.append({"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
                    "properties": tags})
    return out
