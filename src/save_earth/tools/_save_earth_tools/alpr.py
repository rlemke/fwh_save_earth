"""ALPR surveillance cameras — worldwide GeoJSON from OSM (DeFlock data).

Queries the OpenStreetMap Overpass API for every node tagged as an
Automatic License Plate Reader and caches them as a single GeoJSON
FeatureCollection under::

    cache/save-earth/alpr/cameras.geojson + .meta.json

This is the crowd-sourced surveillance-camera INVENTORY that the DeFlock
project (https://deflock.me) builds directly in OpenStreetMap — it records
*where* ALPR cameras are, NOT any video, image, or plate-read data (those
are private to the camera operators). The tag convention is::

    man_made=surveillance + surveillance:type=ALPR

ALPRs are point features, so — unlike the nuclear source — no ``out
center`` centroiding is needed. Each cached Feature keeps **all** of the
element's OSM tags verbatim as its ``properties`` (so the map popup can
surface everything the dataset knows — ``manufacturer``, ``operator``,
``direction`` the camera faces, ``surveillance:zone``, ``camera:mount``,
``start_date``, …), plus derived fields: ``osm_type``/``osm_id``
(provenance), ``osm_url`` (deep link), and ``camera_vendor``
(``flock`` / ``motorola`` / ``other``, classified from manufacturer/brand
so downstream layers/filters can split by vendor).

By early 2026 the OSM ALPR registry had passed ~336,000 nodes worldwide,
all on one shared per-IP Overpass rate limit — so this is a SINGLE
worldwide cached query (7-day freshness window), never a per-region
fan-out. Coverage is crowd-driven and varies by region — the honest
limitation of an open source. Pass ``use_mock=True`` for a small offline
set when the network or Overpass is unavailable.
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

logger = logging.getLogger("save-earth.alpr")

NAMESPACE = "save-earth"
CACHE_TYPE = "alpr"
RELATIVE_PATH = "cameras.geojson"

# Overpass mirrors, tried in order. All share the caller's public IP, so a
# throttled network trips every one — the real remedy is a cool-down, not more
# mirrors, but a wider list still helps route around a single overloaded host.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 7  # crowd map moves faster than the reactor fleet

# Every ALPR-tagged surveillance node. Filter on the SELECTIVE tag only
# (`surveillance:type=ALPR`) — every ALPR carries it, and intersecting with the
# very common `man_made=surveillance` makes Overpass scan that huge set first and
# time out. node-only — ALPRs are points, so no `out center` is required.
OVERPASS_QUERY = (
    "[out:json][timeout:240];"
    'node["surveillance:type"="ALPR"];'
    "out body;"  # `body` = id + COORDINATES + tags. NOT `out body tags` — a
                 # trailing `tags` means tags-only (no lat/lon), which silently
                 # drops every node in _to_feature.
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
    """Fetch worldwide ALPR camera locations and cache them as GeoJSON."""
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("alpr cache hit (%.1fh old)", age or -1.0)
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
            source_url = "mock://alpr"
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

        # An EMPTY result is never legitimate for a worldwide ALPR query — it
        # means a server-side timeout / throttle (Overpass signals a timeout as
        # HTTP 200 with empty `elements` + a `remark`, but a throttled host can
        # also return empty with no remark). Do NOT accept it as "0 cameras"
        # (that silently produced a blank map once): try the next mirror, and if
        # every mirror is empty, raise so the caller never caches an empty set.
        if not elements:
            remark = payload.get("remark") or "empty result (no remark — likely throttled)"
            logger.warning("Overpass %s returned no elements: %s", endpoint, remark)
            last_exc = RuntimeError(f"Overpass {endpoint}: {remark}")
            continue

        features: list[dict[str, Any]] = []
        for el in elements:
            feat = _to_feature(el)
            if feat is not None:
                features.append(feat)
        # Elements present but NONE convertible → they came back without
        # coordinates (e.g. a tags-only `out` mode); a valid map needs points,
        # so try the next mirror rather than returning a coordinate-less set.
        if not features:
            logger.warning("Overpass %s: %d elements but 0 had coordinates", endpoint, len(elements))
            last_exc = RuntimeError(f"Overpass {endpoint}: elements had no coordinates")
            continue
        logger.info("Overpass %s → %d ALPR features", endpoint, len(features))
        return features, endpoint

    raise RuntimeError(
        f"all {len(OVERPASS_ENDPOINTS)} Overpass mirrors failed or were throttled "
        f"(shared public IP rate-limit needs a cool-down); last error: {last_exc}"
    )


def _classify_vendor(tags: dict[str, Any]) -> str:
    """Bucket a camera by manufacturer/brand so layers/filters can split by vendor."""
    mfr = str(tags.get("manufacturer") or tags.get("brand") or "").lower()
    if "flock" in mfr:
        return "flock"
    if "motorola" in mfr or "vigilant" in mfr or "leonardo" in mfr:
        return "motorola"
    return "other"


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one Overpass node into a GeoJSON Point Feature, or ``None`` if it
    carries no usable coordinate."""
    lat, lon = el.get("lat"), el.get("lon")
    if lat is None or lon is None:
        return None
    tags = el.get("tags") or {}
    osm_type = el.get("type", "node")
    osm_id = el.get("id")

    # Keep every OSM tag verbatim so the popup shows all available
    # information (manufacturer, operator, direction, surveillance:zone, …),
    # then layer the derived provenance/vendor fields on top.
    props: dict[str, Any] = dict(tags)
    props["camera_vendor"] = _classify_vendor(tags)
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
                "publisher": "OpenStreetMap contributors via DeFlock — Overpass API",
                "url": source_url,
                "license": "ODbL 1.0",
                "used_mock": used_mock,
            },
            tool={"name": "alpr", "version": "1.0"},
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
    """Small offline set — a few representative ALPR nodes with rich tags so the
    'click → show all info' popup and the vendor classifier have something to
    display without Overpass."""
    base = [
        {
            "man_made": "surveillance",
            "surveillance:type": "ALPR",
            "manufacturer": "Flock Safety",
            "operator": "City of Example PD",
            "direction": "90",
            "surveillance:zone": "traffic",
            "camera:mount": "pole",
            "lat": 34.0522,
            "lon": -118.2437,
        },
        {
            "man_made": "surveillance",
            "surveillance:type": "ALPR",
            "manufacturer": "Motorola Solutions",
            "brand": "Vigilant",
            "direction": "270",
            "surveillance:zone": "traffic",
            "lat": 40.7128,
            "lon": -74.0060,
        },
        {
            "man_made": "surveillance",
            "surveillance:type": "ALPR",
            "manufacturer": "Flock Safety",
            "operator": "Example HOA",
            "direction": "180",
            "camera:mount": "pole",
            "lat": 33.4484,
            "lon": -112.0740,
        },
        {
            "man_made": "surveillance",
            "surveillance:type": "ALPR",
            # no manufacturer → classifies as "other"
            "direction": "0",
            "lat": 51.5074,
            "lon": -0.1278,
        },
    ]
    features: list[dict[str, Any]] = []
    for i, row in enumerate(base):
        tags = {k: v for k, v in row.items() if k not in ("lat", "lon")}
        tags["camera_vendor"] = _classify_vendor(tags)
        tags["osm_type"] = "node"
        tags["osm_id"] = 900000 + i
        tags["osm_url"] = f"https://www.openstreetmap.org/node/{900000 + i}"
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
                "properties": tags,
            }
        )
    return features
