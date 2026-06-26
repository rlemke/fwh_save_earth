"""Research telescopes — worldwide large telescope GeoJSON from OSM.

Queries the OpenStreetMap Overpass API for every **named** ``man_made=telescope``
(optical + radio) and caches them as a single GeoJSON FeatureCollection under::

    cache/save-earth/telescopes/telescopes.geojson + .meta.json

The ``name`` requirement is the "research instrument" proxy — observatories name
their telescopes, while amateur/backyard scopes are rarely named in OSM (so the
~4.5k total man_made=telescope drops to ~1.9k named research instruments). Each
cached Feature keeps **all** OSM tags verbatim as its ``properties`` (so the popup
shows everything — ``telescope:type``, ``telescope:diameter``, ``operator``,
``start_date``, ``wikipedia``, …), plus a derived ``feature_kind`` (the
``telescope:type`` when present — optical / radio / … — else ``"telescope"``),
``osm_type``/``osm_id`` provenance and an ``osm_url`` deep link. Way/relation
footprints are centroided to a Point via Overpass ``out center``.

Coverage is OSM-community-driven. Pass ``use_mock=True`` for a small offline set
when Overpass is unavailable.
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

logger = logging.getLogger("save-earth.telescope")

NAMESPACE = "save-earth"
CACHE_TYPE = "telescopes"
RELATIVE_PATH = "telescopes.geojson"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30

# Named telescopes (research instruments — observatories name them; amateur
# scopes are rarely named in OSM). Centroids for ways/relations.
OVERPASS_QUERY = (
    "[out:json][timeout:240];"
    "("
    'nwr["man_made"="telescope"]["name"];'
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
    """Fetch worldwide named research telescopes and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("telescopes cache hit (%.1fh old)", age or -1.0)
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
            source_url = "mock://telescopes"
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
        logger.info("Overpass %s → %d telescopes", endpoint, len(features))
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
    kind = tags.get("telescope:type") or "telescope"

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
            tool={"name": "telescope", "version": "1.0"},
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
        {"name": "Very Large Telescope (VLT)", "man_made": "telescope",
         "telescope:type": "optical", "telescope:diameter": "8.2",
         "operator": "ESO", "country": "CL", "lat": -24.6275, "lon": -70.4044},
        {"name": "Arecibo Telescope", "man_made": "telescope",
         "telescope:type": "radio", "telescope:diameter": "305",
         "country": "PR", "lat": 18.3442, "lon": -66.7528},
        {"name": "Green Bank Telescope", "man_made": "telescope",
         "telescope:type": "radio", "telescope:diameter": "100",
         "country": "US", "lat": 38.4331, "lon": -79.8398},
        {"name": "Gran Telescopio Canarias", "man_made": "telescope",
         "telescope:type": "optical", "telescope:diameter": "10.4",
         "country": "ES", "lat": 28.7567, "lon": -17.8917},
    ]
    features: list[dict[str, Any]] = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon")}
        tags["feature_kind"] = row.get("telescope:type", "telescope")
        tags["osm_type"] = "node"
        tags["osm_id"] = 500000 + i
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
            "properties": tags,
        })
    return features
