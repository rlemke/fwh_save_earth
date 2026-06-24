"""Nuclear power facilities — worldwide reactor/plant GeoJSON from OSM.

Queries the OpenStreetMap Overpass API for every feature tagged as a
nuclear power generator or plant and caches them as a single GeoJSON
FeatureCollection under::

    cache/save-earth/nuclear/reactors.geojson + .meta.json

Two OSM tagging schemes are collected (both are "all available
information about the reactor", straight from OSM):

- ``generator:source=nuclear`` — an individual generating unit, i.e. a
  reactor. These are the per-reactor points.
- ``plant:source=nuclear``     — a whole power station (often a way or
  relation polygon). We take the polygon centroid via Overpass ``out
  center`` so every feature is a renderable Point.

Each cached Feature keeps **all** of the element's OSM tags verbatim as
its ``properties`` (so the map popup can surface everything the dataset
knows — name, operator, ``plant:output:electricity``, ``start_date``,
``generator:method``, ``construction`` status, etc.), plus three derived
fields: ``osm_type``/``osm_id`` (provenance), ``osm_url`` (deep link),
and ``feature_kind`` (``"reactor"`` for a generator, ``"plant"`` for a
station).

Coverage and tag completeness are OSM-community-driven, so they vary by
country — this is the honest limitation of an open crowd-sourced source.
Pass ``use_mock=True`` for a small offline set (well-known stations) when
the network or Overpass is unavailable.
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

logger = logging.getLogger("save-earth.nuclear")

NAMESPACE = "save-earth"
CACHE_TYPE = "nuclear"
RELATIVE_PATH = "reactors.geojson"

# Overpass mirrors, tried in order. The main instance is rate-limited;
# the others are community mirrors that accept the same query.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30  # the reactor fleet changes slowly

# Both nuclear power tagging schemes, polygon centroids included.
OVERPASS_QUERY = (
    "[out:json][timeout:180];"
    "("
    'nwr["generator:source"="nuclear"];'
    'nwr["plant:source"="nuclear"];'
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


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def download(
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch worldwide nuclear power features and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("nuclear cache hit (%.1fh old)", age or -1.0)
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
            source_url = "mock://nuclear"
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


# ---------------------------------------------------------------------------
# Overpass fetch + normalisation.
# ---------------------------------------------------------------------------


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

        features: list[dict[str, Any]] = []
        for el in elements:
            feat = _to_feature(el)
            if feat is not None:
                features.append(feat)
        logger.info("Overpass %s → %d nuclear features", endpoint, len(features))
        return features, endpoint

    raise RuntimeError(f"all Overpass mirrors failed; last error: {last_exc}")


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Overpass element into a GeoJSON Point Feature, or ``None``
    if it carries no usable coordinate."""
    tags = el.get("tags") or {}
    # Nodes carry lat/lon directly; ways/relations carry a `center`
    # because the query used `out center`.
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
    kind = "reactor" if tags.get("generator:source") == "nuclear" else "plant"

    # Keep every OSM tag verbatim so the popup shows all available
    # information, then layer the derived provenance fields on top.
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


# ---------------------------------------------------------------------------
# Cache write.
# ---------------------------------------------------------------------------


def _persist(
    body: bytes,
    storage: Storage,
    *,
    source_url: str,
    used_mock: bool,
) -> DownloadResult:
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
            NAMESPACE,
            CACHE_TYPE,
            RELATIVE_PATH,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "publisher": "OpenStreetMap contributors — Overpass API",
                "url": source_url,
                "license": "ODbL 1.0",
                "used_mock": used_mock,
            },
            tool={"name": "nuclear", "version": "1.0"},
            extra={"feature_count": feature_count},
            storage=storage,
        )

    return DownloadResult(
        absolute_path=final_path,
        relative_path=RELATIVE_PATH,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        feature_count=feature_count,
        was_cached=False,
        source_url=source_url,
        used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


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
    """Small offline set — five well-known stations with rich tags so the
    'click → show all info' popup has something to display without Overpass."""
    base = [
        {
            "name": "Diablo Canyon Power Plant",
            "operator": "Pacific Gas and Electric Company",
            "plant:source": "nuclear",
            "plant:output:electricity": "2256 MW",
            "start_date": "1985",
            "country": "US",
            "lat": 35.2117,
            "lon": -120.8553,
            "kind": "plant",
        },
        {
            "name": "Cattenom Nuclear Power Plant",
            "operator": "Électricité de France",
            "plant:source": "nuclear",
            "plant:output:electricity": "5200 MW",
            "start_date": "1986",
            "country": "FR",
            "lat": 49.4158,
            "lon": 6.2181,
            "kind": "plant",
        },
        {
            "name": "Bruce Nuclear Generating Station",
            "operator": "Bruce Power",
            "plant:source": "nuclear",
            "plant:output:electricity": "6550 MW",
            "start_date": "1977",
            "country": "CA",
            "lat": 44.3253,
            "lon": -81.5997,
            "kind": "plant",
        },
        {
            "name": "Kashiwazaki-Kariwa Reactor 1",
            "operator": "TEPCO",
            "generator:source": "nuclear",
            "generator:method": "fission",
            "generator:output:electricity": "1100 MW",
            "start_date": "1985",
            "country": "JP",
            "lat": 37.4292,
            "lon": 138.5969,
            "kind": "reactor",
        },
        {
            "name": "Olkiluoto Nuclear Power Plant",
            "operator": "Teollisuuden Voima",
            "plant:source": "nuclear",
            "plant:output:electricity": "3300 MW",
            "start_date": "1979",
            "country": "FI",
            "lat": 61.2369,
            "lon": 21.4406,
            "kind": "plant",
        },
    ]
    features: list[dict[str, Any]] = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon", "kind")}
        tags["feature_kind"] = row["kind"]
        tags["osm_type"] = "way" if row["kind"] == "plant" else "node"
        tags["osm_id"] = 100000 + i
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
                "properties": tags,
            }
        )
    return features
