"""Hydroelectric power plants — worldwide GeoJSON from OSM.

Queries the OpenStreetMap Overpass API for every power station tagged
``plant:source=hydro`` and caches them as one GeoJSON FeatureCollection::

    cache/save-earth/hydro/plants.geojson + .meta.json

A hydro PLANT is the station (usually a way or relation polygon at the dam
or powerhouse); its centroid comes from Overpass ``out center``. Individual
turbines (``generator:source=hydro``) are deliberately not collected: they sit
inside the plant polygons, so they would draw the same dam several times.

Each Feature keeps every OSM tag verbatim, plus derived fields the map uses:

- ``plant_type`` — a readable label for ``hydro_type`` (not ``type``, which
  OSM relations already use for ``multipolygon``);
- ``hydro_type`` — ``storage`` (a dam with a reservoir), ``run_of_river``,
  ``pumped_storage``, or ``unspecified``, from ``plant:method``;
- ``capacity_mw`` — ``plant:output:electricity`` normalised to megawatts
  (``"2256 MW"``, ``"500 kW"``, ``"1,2 GW"``), absent when the tag is missing
  or is not a quantity (``"yes"``);
- ``osm_type``/``osm_id``/``osm_url`` — provenance.

Measured 2026-10-19: 16,224 plants; 13,181 carry an output tag and 7,842 a
method, so about half are drawn as ``unspecified``. That is OSM's coverage,
reported as such rather than guessed from the name or the capacity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

logger = logging.getLogger("save-earth.hydro")

NAMESPACE = "save-earth"
CACHE_TYPE = "hydro"
RELATIVE_PATH = "plants.geojson"
TOOL_VERSION = "1.0"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30  # dams change slowly

OVERPASS_QUERY = '[out:json][timeout:240];nwr["plant:source"="hydro"];out center tags;'

#: plant:method value → hydro_type. Anything else (absent, misspelt, "dam")
#: is ``unspecified``: a wrong class is worse than an honest unknown.
METHODS = {
    "water-storage": "storage",
    "run-of-the-river": "run_of_river",
    "water-pumped-storage": "pumped_storage",
}
TYPE_LABELS = {
    "storage": "Reservoir dam (storage)",
    "run_of_river": "Run-of-river",
    "pumped_storage": "Pumped storage",
    "unspecified": "Type not recorded",
}

_lock = threading.Lock()
_QTY = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(gw|mw|kw|w)\s*$", re.IGNORECASE)
_UNIT_MW = {"gw": 1000.0, "mw": 1.0, "kw": 0.001, "w": 0.000001}


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


def parse_capacity_mw(value: Any) -> float | None:
    """``plant:output:electricity`` → MW, or None when it is not a quantity.

    OSM writes a decimal comma about as often as a point (``"1,2 GW"``), so a
    comma is read as a decimal separator — a thousands separator would be
    ``"1,200 MW"``, which has three digits after it and is handled below.
    """
    if value is None:
        return None
    text = str(value).strip()
    m = re.match(r"^\s*([0-9]{1,3}(?:,[0-9]{3})+)\s*(gw|mw|kw|w)\s*$", text, re.IGNORECASE)
    if m:  # "1,200 MW" — thousands separators
        return round(float(m.group(1).replace(",", "")) * _UNIT_MW[m.group(2).lower()], 3)
    m = _QTY.match(text)
    if not m:
        return None
    return round(float(m.group(1).replace(",", ".")) * _UNIT_MW[m.group(2).lower()], 3)


def hydro_type(tags: dict[str, Any]) -> str:
    return METHODS.get(str(tags.get("plant:method") or "").strip().lower(), "unspecified")


def download(
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch worldwide hydroelectric plants and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if (
                side
                and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
                and (side.get("tool") or {}).get("version") == TOOL_VERSION
            ):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("hydro cache hit (%.1fh old)", age or -1.0)
                    return DownloadResult(
                        absolute_path=art_path,
                        relative_path=RELATIVE_PATH,
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                        was_cached=True,
                        source_url=OVERPASS_ENDPOINTS[0],
                        used_mock=bool((side.get("source") or {}).get("used_mock")),
                    )

        if use_mock:
            features = [f for f in (to_feature(el) for el in _MOCK_ELEMENTS) if f]
            source_url = "mock://hydro"
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock."
                )
            features, source_url = _fetch_overpass()

        body = json.dumps(
            {"type": "FeatureCollection", "features": features}, separators=(",", ":")
        ).encode("utf-8")
        return _persist(body, s, source_url=source_url, used_mock=use_mock)


def _fetch_overpass() -> tuple[list[dict[str, Any]], str]:
    """POST the Overpass query, trying each mirror until one answers."""
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
        except Exception as exc:  # network / rate-limit / non-JSON → next mirror
            logger.warning("Overpass %s failed: %s", endpoint, exc)
            last_exc = exc
            continue
        elements = payload.get("elements")
        if not isinstance(elements, list):
            last_exc = RuntimeError(f"Overpass {endpoint} returned an unexpected shape")
            continue
        if payload.get("remark"):
            # Overpass reports a timeout or memory abort as a 200 with a remark
            # and a TRUNCATED element list — a partial world must not be cached.
            last_exc = RuntimeError(f"Overpass {endpoint}: {payload['remark']}")
            continue
        features = [f for f in (to_feature(el) for el in elements) if f]
        logger.info("Overpass %s → %d hydro plants", endpoint, len(features))
        return features, endpoint
    raise RuntimeError(f"all Overpass mirrors failed; last error: {last_exc}")


def to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """One Overpass element → a GeoJSON Point Feature, or None without a coordinate."""
    tags = el.get("tags") or {}
    if el.get("lat") is not None and el.get("lon") is not None:
        lat, lon = el["lat"], el["lon"]
    elif isinstance(el.get("center"), dict):
        lat, lon = el["center"].get("lat"), el["center"].get("lon")
    else:
        return None
    if lat is None or lon is None:
        return None

    props: dict[str, Any] = dict(tags)
    kind = hydro_type(tags)
    props["hydro_type"] = kind
    props["plant_type"] = TYPE_LABELS[kind]  # not "type": relations carry type=multipolygon
    mw = parse_capacity_mw(tags.get("plant:output:electricity"))
    if mw is not None:
        props["capacity_mw"] = mw
    osm_type, osm_id = el.get("type", "node"), el.get("id")
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
    feats = json.loads(body).get("features") or []
    by_type: dict[str, int] = {}
    for f in feats:
        t = f["properties"]["hydro_type"]
        by_type[t] = by_type.get(t, 0) + 1
    digest = hashlib.sha256(body).hexdigest()

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            RELATIVE_PATH,
            kind="file",
            size_bytes=len(body),
            sha256=digest,
            source={
                "publisher": "OpenStreetMap contributors — Overpass API",
                "url": source_url,
                "query": OVERPASS_QUERY,
                "license": "ODbL 1.0",
                "used_mock": used_mock,
            },
            tool={"name": "hydro", "version": TOOL_VERSION},
            extra={"feature_count": len(feats), "by_type": by_type},
            storage=storage,
        )
    return DownloadResult(
        absolute_path=final_path,
        relative_path=RELATIVE_PATH,
        size_bytes=len(body),
        sha256=digest,
        feature_count=len(feats),
        was_cached=False,
        source_url=source_url,
        used_mock=used_mock,
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


# Offline set — Overpass-shaped elements, so the mock exercises to_feature too.
_MOCK_ELEMENTS: list[dict[str, Any]] = [
    {"type": "way", "id": 1, "center": {"lat": 30.823, "lon": 111.003},
     "tags": {"name": "Three Gorges Dam", "operator": "China Yangtze Power",
              "plant:source": "hydro", "plant:method": "water-storage",
              "plant:output:electricity": "22500 MW", "start_date": "2003"}},
    {"type": "way", "id": 2, "center": {"lat": -25.408, "lon": -54.589},
     "tags": {"name": "Itaipu", "operator": "Itaipu Binacional", "plant:source": "hydro",
              "plant:method": "water-storage", "plant:output:electricity": "14 GW",
              "start_date": "1984"}},
    {"type": "way", "id": 3, "center": {"lat": 36.016, "lon": -114.737},
     "tags": {"name": "Hoover Dam", "operator": "Bureau of Reclamation",
              "plant:source": "hydro", "plant:method": "water-storage",
              "plant:output:electricity": "2,08 GW", "start_date": "1936"}},
    {"type": "relation", "id": 4, "center": {"lat": 47.567, "lon": 7.664},
     "tags": {"name": "Kraftwerk Rheinfelden", "plant:source": "hydro",
              "plant:method": "run-of-the-river", "plant:output:electricity": "100 MW"}},
    {"type": "way", "id": 5, "center": {"lat": 53.135, "lon": -4.115},
     "tags": {"name": "Dinorwig Power Station", "plant:source": "hydro",
              "plant:method": "water-pumped-storage", "plant:output:electricity": "1728 MW"}},
    {"type": "node", "id": 6, "lat": 46.2, "lon": 7.3,
     "tags": {"plant:source": "hydro", "plant:output:electricity": "yes"}},
]
