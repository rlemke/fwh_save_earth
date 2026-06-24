"""Major volcanoes — worldwide notable-volcano GeoJSON from OpenStreetMap.

Queries the OpenStreetMap Overpass API for every feature tagged
``natural=volcano`` that also carries a ``wikidata`` or ``wikipedia`` tag,
and caches them as a single GeoJSON FeatureCollection under::

    cache/save-earth/volcanoes/volcanoes.geojson + .meta.json

**Why the Wikipedia/Wikidata filter = "major".** OSM tags tens of thousands of
``natural=volcano`` nodes, most of them minor cinder cones / vents with no
attributes. There is no reliable global prominence/VEI tag in OSM, so we use
encyclopedic notability as the "major" proxy: a volcano that has a Wikipedia
article or a Wikidata entity is one the world considers significant (Fuji,
Vesuvius, Kilauea, Etna, St. Helens, …). This yields ~1–2k worldwide. (For a
stricter, eruption-history-ranked set, the Smithsonian Global Volcanism Program
Holocene list is the authoritative alternative — a future source.)

Each cached Feature keeps **all** of the element's OSM tags verbatim as its
``properties`` (so the popup can surface everything OSM knows — name, ele,
volcano:type, volcano:status, wikipedia, …), plus derived ``osm_type``/``osm_id``
(provenance), ``osm_url`` (deep link), and ``feature_kind="volcano"``.

Coverage and tag completeness are OSM-community-driven, so they vary by region —
the honest limitation of an open crowd-sourced source. Pass ``use_mock=True`` for
a small offline set (well-known volcanoes) when Overpass is unavailable.
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

logger = logging.getLogger("save-earth.volcanoes")

NAMESPACE = "save-earth"
CACHE_TYPE = "volcanoes"
RELATIVE_PATH = "volcanoes.geojson"

# Overpass mirrors, tried in order (same set the nuclear source uses).
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30  # the volcano set changes very slowly

# "Major" = natural=volcano with an encyclopedic entity (wikidata or wikipedia).
# Overpass unions de-duplicate by element id, so a feature with both tags is
# returned once.
OVERPASS_QUERY = (
    "[out:json][timeout:180];"
    "("
    'nwr["natural"="volcano"]["wikidata"];'
    'nwr["natural"="volcano"]["wikipedia"];'
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
    """Fetch worldwide major (notable) volcanoes and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("volcanoes cache hit (%.1fh old)", age or -1.0)
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
            source_url = "mock://volcanoes"
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
        logger.info("Overpass %s → %d volcano features", endpoint, len(features))
        return features, endpoint

    raise RuntimeError(f"all Overpass mirrors failed; last error: {last_exc}")


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Overpass element into a GeoJSON Point Feature, or ``None`` if it
    carries no usable coordinate."""
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

    # Keep every OSM tag verbatim so the popup shows all available information,
    # then layer the derived provenance fields on top.
    props: dict[str, Any] = dict(tags)
    props["feature_kind"] = "volcano"
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
            tool={"name": "volcanoes", "version": "1.0"},
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
    """Small offline set — well-known volcanoes with rich tags so the
    'click → show all info' popup has something to display without Overpass."""
    base = [
        {
            "name": "Mount Fuji", "ele": "3776", "volcano:type": "stratovolcano",
            "volcano:status": "dormant", "wikipedia": "en:Mount Fuji",
            "wikidata": "Q39231", "country": "JP", "lat": 35.3606, "lon": 138.7274,
        },
        {
            "name": "Mount Vesuvius", "ele": "1281", "volcano:type": "stratovolcano",
            "volcano:status": "active", "wikipedia": "en:Mount Vesuvius",
            "wikidata": "Q4102", "country": "IT", "lat": 40.8217, "lon": 14.4289,
        },
        {
            "name": "Kīlauea", "ele": "1247", "volcano:type": "shield",
            "volcano:status": "active", "wikipedia": "en:Kīlauea",
            "wikidata": "Q156673", "country": "US", "lat": 19.4069, "lon": -155.2834,
        },
        {
            "name": "Mount Etna", "ele": "3357", "volcano:type": "stratovolcano",
            "volcano:status": "active", "wikipedia": "en:Mount Etna",
            "wikidata": "Q13452", "country": "IT", "lat": 37.7510, "lon": 14.9934,
        },
        {
            "name": "Mount St. Helens", "ele": "2549", "volcano:type": "stratovolcano",
            "volcano:status": "active", "wikipedia": "en:Mount St. Helens",
            "wikidata": "Q4675", "country": "US", "lat": 46.1912, "lon": -122.1944,
        },
    ]
    features: list[dict[str, Any]] = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon")}
        tags["natural"] = "volcano"
        tags["feature_kind"] = "volcano"
        tags["osm_type"] = "node"
        tags["osm_id"] = 200000 + i
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
                "properties": tags,
            }
        )
    return features
