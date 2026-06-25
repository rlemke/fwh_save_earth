"""Tesla charging stations — worldwide Supercharger / charger GeoJSON from OSM.

Queries the OpenStreetMap Overpass API for every ``amenity=charging_station``
branded / operated / networked as **Tesla** (or carrying a
``socket:tesla_supercharger`` outlet) and caches them as a single GeoJSON
FeatureCollection under::

    cache/save-earth/tesla/tesla_chargers.geojson + .meta.json

Each cached Feature keeps **all** of the element's OSM tags verbatim as its
``properties`` (so the map popup can surface everything OSM knows — name,
operator, ``capacity``, ``socket:*``, ``opening_hours``, ``fee``, …), plus the
derived ``feature_kind`` (``"supercharger"`` when a Tesla-supercharger socket is
present, else ``"charger"``), ``osm_type``/``osm_id`` provenance and an
``osm_url`` deep link. Way/relation polygons are centroided to a Point via
Overpass ``out center``.

Coverage is OSM-community-driven (good in the US/EU, patchier elsewhere). Pass
``use_mock=True`` for a small offline set when Overpass is unavailable.
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

logger = logging.getLogger("save-earth.tesla")

NAMESPACE = "save-earth"
CACHE_TYPE = "tesla"
RELATIVE_PATH = "tesla_chargers.geojson"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30

# Tesla charging stations: branded/networked/operated Tesla, or carrying a Tesla
# supercharger socket. Case-insensitive on the free-text brand/network/operator.
OVERPASS_QUERY = (
    "[out:json][timeout:240];"
    "("
    'nwr["amenity"="charging_station"]["brand"~"Tesla",i];'
    'nwr["amenity"="charging_station"]["network"~"Tesla",i];'
    'nwr["amenity"="charging_station"]["operator"~"Tesla",i];'
    'nwr["amenity"="charging_station"]["socket:tesla_supercharger"];'
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


def download(
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch worldwide Tesla charging stations and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("tesla cache hit (%.1fh old)", age or -1.0)
                    return DownloadResult(
                        absolute_path=art_path,
                        relative_path=RELATIVE_PATH,
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                        was_cached=True,
                        source_url=OVERPASS_ENDPOINTS[0],
                    )

        if use_mock:
            features = _mock_features()
            used_mock = True
            source_url = "mock://tesla"
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock."
                )
            features, source_url = _fetch_overpass()
            used_mock = False

        body = json.dumps(
            {"type": "FeatureCollection", "features": features},
            separators=(",", ":"),
        ).encode("utf-8")
        return _persist(body, s, source_url=source_url, used_mock=used_mock)


def _fetch_overpass() -> tuple[list[dict[str, Any]], str]:
    last_exc: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            logger.info("querying Overpass %s", endpoint)
            resp = requests.post(
                endpoint,
                data={"data": OVERPASS_QUERY},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning("Overpass %s failed: %s", endpoint, exc)
            last_exc = exc
            continue

        elements = payload.get("elements")
        if not isinstance(elements, list):
            last_exc = RuntimeError(f"Overpass {endpoint} returned an unexpected shape")
            continue

        features: list[dict[str, Any]] = []
        for el in elements:
            feat = _to_feature(el)
            if feat is not None:
                features.append(feat)
        logger.info("Overpass %s → %d Tesla chargers", endpoint, len(features))
        return features, endpoint

    raise RuntimeError(f"all Overpass mirrors failed; last error: {last_exc}")


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    tags = el.get("tags") or {}
    if el.get("lat") is not None and el.get("lon") is not None:
        lat, lon = el["lat"], el["lon"]
    elif isinstance(el.get("center"), dict):
        lat, lon = el["center"].get("lat"), el["center"].get("lon")
    else:
        return None
    if lat is None or lon is None:
        return None

    osm_type = el.get("type", "node")
    osm_id = el.get("id")
    kind = "supercharger" if tags.get("socket:tesla_supercharger") else "charger"

    props: dict[str, Any] = dict(tags)
    props["feature_kind"] = kind
    props["osm_type"] = osm_type
    props["osm_id"] = osm_id
    if osm_id is not None:
        props["osm_url"] = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "properties": props,
    }


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
        sidecar.write_sidecar(
            NAMESPACE, CACHE_TYPE, RELATIVE_PATH,
            kind="file", size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
            source={
                "publisher": "OpenStreetMap contributors — Overpass API",
                "url": source_url, "license": "ODbL 1.0", "used_mock": used_mock,
            },
            tool={"name": "tesla", "version": "1.0"},
            extra={"feature_count": feature_count}, storage=storage,
        )
    return DownloadResult(
        absolute_path=final_path, relative_path=RELATIVE_PATH, size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(), feature_count=feature_count,
        was_cached=False, source_url=source_url, used_mock=used_mock,
    )


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
        {"name": "Tesla Supercharger - Mountain View", "amenity": "charging_station",
         "brand": "Tesla", "socket:tesla_supercharger": "8", "capacity": "8",
         "country": "US", "lat": 37.3947, "lon": -122.0787, "kind": "supercharger"},
        {"name": "Tesla Supercharger - Berlin", "amenity": "charging_station",
         "network": "Tesla Supercharger", "socket:tesla_supercharger": "12",
         "country": "DE", "lat": 52.5200, "lon": 13.4050, "kind": "supercharger"},
        {"name": "Tesla Destination Charger", "amenity": "charging_station",
         "operator": "Tesla", "socket:tesla_destination": "4",
         "country": "GB", "lat": 51.5074, "lon": -0.1278, "kind": "charger"},
    ]
    features: list[dict[str, Any]] = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon", "kind")}
        tags["feature_kind"] = row["kind"]
        tags["osm_type"] = "node"
        tags["osm_id"] = 400000 + i
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": tags,
        })
    return features
