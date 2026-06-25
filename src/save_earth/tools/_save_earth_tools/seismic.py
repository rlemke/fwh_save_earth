"""Seismic sources — recent earthquakes (USGS) + fault / plate-boundary lines.

Two world-scale GeoJSON layers for a seismic-hazard map, cached under::

    cache/save-earth/earthquakes/earthquakes.geojson + .meta.json
    cache/save-earth/faults/faults.geojson           + .meta.json

**Earthquakes** come from the USGS real-time GeoJSON feed (the canonical
authoritative source for global seismicity). The default feed is M4.5+ over
the past 30 days — a clean global signal (a few hundred events) rather than
the tens of thousands of micro-quakes in the all-quakes feed. Each feature is
a Point with the USGS properties verbatim (``mag``, ``place``, ``time``,
``url``, ``magType``, ``tsunami``, …), plus a derived ``depth_km`` (the third
coordinate) so a popup shows depth.

**Faults** are the tectonic plate boundaries from the Peter Bird (2002)
``PB2002`` dataset — the major global fault systems along which earthquakes
cluster. LineString features, properties kept verbatim. (Plate boundaries are
the world-scale "fault lines"; the GEM Global Active Faults DB is the finer
regional alternative.)

Both keep every source property verbatim so the popup surfaces all available
information. Pass ``use_mock=True`` for a small offline set when the network is
unavailable (tests).
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

logger = logging.getLogger("save-earth.seismic")

NAMESPACE = "save-earth"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 180

# --- earthquakes (USGS real-time feed) -------------------------------------
EARTHQUAKES_CACHE_TYPE = "earthquakes"
EARTHQUAKES_RELATIVE_PATH = "earthquakes.geojson"
# M4.5+ over the past 30 days — a clean global picture. Other feeds:
# significant_month, 2.5_month, all_month, 4.5_week …
USGS_FEED_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_month.geojson"
)
EARTHQUAKES_MAX_AGE_HOURS = 6.0  # the feed updates continuously; refresh often

# --- faults (Peter Bird 2002 plate boundaries) -----------------------------
FAULTS_CACHE_TYPE = "faults"
FAULTS_RELATIVE_PATH = "faults.geojson"
PLATES_URL = (
    "https://raw.githubusercontent.com/fraxen/tectonicplates/master/"
    "GeoJSON/PB2002_boundaries.json"
)
FAULTS_MAX_AGE_HOURS = 24.0 * 90  # plate boundaries are effectively static

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


def download_earthquakes(
    *,
    force: bool = False,
    max_age_hours: float = EARTHQUAKES_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch recent significant earthquakes (USGS feed) → cached GeoJSON."""
    s = storage or get_storage()
    cached = _cache_hit(
        EARTHQUAKES_CACHE_TYPE, EARTHQUAKES_RELATIVE_PATH, max_age_hours, force, s,
        USGS_FEED_URL,
    )
    if cached is not None:
        return cached

    if use_mock:
        fc = _mock_earthquakes()
        source_url, used_mock = "mock://earthquakes", True
    else:
        fc = _augment_quakes(_fetch_json(USGS_FEED_URL))
        source_url, used_mock = USGS_FEED_URL, False

    body = json.dumps(fc, separators=(",", ":")).encode("utf-8")
    return _persist(
        body, s,
        cache_type=EARTHQUAKES_CACHE_TYPE, relative_path=EARTHQUAKES_RELATIVE_PATH,
        source={
            "publisher": "U.S. Geological Survey (USGS) Earthquake Hazards Program",
            "url": source_url, "license": "public domain (US Government)",
            "used_mock": used_mock,
        },
        tool_name="seismic.earthquakes", source_url=source_url, used_mock=used_mock,
    )


def download_faults(
    *,
    force: bool = False,
    max_age_hours: float = FAULTS_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch tectonic plate boundaries (Bird 2002) → cached GeoJSON lines."""
    s = storage or get_storage()
    cached = _cache_hit(
        FAULTS_CACHE_TYPE, FAULTS_RELATIVE_PATH, max_age_hours, force, s, PLATES_URL,
    )
    if cached is not None:
        return cached

    if use_mock:
        fc = _mock_faults()
        source_url, used_mock = "mock://faults", True
    else:
        fc = _fetch_json(PLATES_URL)
        if fc.get("type") != "FeatureCollection":
            raise RuntimeError(f"plate-boundary source returned unexpected shape from {PLATES_URL}")
        source_url, used_mock = PLATES_URL, False

    body = json.dumps(fc, separators=(",", ":")).encode("utf-8")
    return _persist(
        body, s,
        cache_type=FAULTS_CACHE_TYPE, relative_path=FAULTS_RELATIVE_PATH,
        source={
            "publisher": "Peter Bird (2002) PB2002 plate boundaries — via fraxen/tectonicplates",
            "url": source_url, "license": "public domain", "used_mock": used_mock,
        },
        tool_name="seismic.faults", source_url=source_url, used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Fetch + normalise.
# ---------------------------------------------------------------------------


def _fetch_json(url: str) -> dict[str, Any]:
    if requests is None:
        raise RuntimeError(
            "requests library is not installed. Install it, run via the .sh "
            "wrapper (activates .venv), or pass --use-mock."
        )
    logger.info("fetching %s", url)
    resp = requests.get(
        url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def _augment_quakes(fc: dict[str, Any]) -> dict[str, Any]:
    """Keep the USGS feed verbatim, adding a derived ``depth_km`` (3rd coord)
    so the popup shows depth, and dropping any feature without coordinates."""
    out: list[dict[str, Any]] = []
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") != "Point" or len(coords) < 2:
            continue
        props = dict(feat.get("properties") or {})
        if len(coords) >= 3 and coords[2] is not None:
            props["depth_km"] = round(float(coords[2]), 1)
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": out}


# ---------------------------------------------------------------------------
# Cache read/write.
# ---------------------------------------------------------------------------


def _cache_hit(
    cache_type: str, relative_path: str, max_age_hours: float, force: bool,
    storage: Storage, source_url: str,
) -> DownloadResult | None:
    if force:
        return None
    with _lock:
        side = sidecar.read_sidecar(NAMESPACE, cache_type, relative_path, storage)
        if side and sidecar.exists_and_valid(NAMESPACE, cache_type, relative_path, storage):
            age = _age_hours(side.get("generated_at"))
            if age is None or age < max_age_hours:
                logger.info("%s cache hit (%.1fh old)", cache_type, age or -1.0)
                return DownloadResult(
                    absolute_path=sidecar.cache_path(NAMESPACE, cache_type, relative_path, storage),
                    relative_path=relative_path,
                    size_bytes=side.get("size_bytes", 0),
                    sha256=side.get("sha256", ""),
                    feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                    was_cached=True,
                    source_url=source_url,
                )
    return None


def _persist(
    body: bytes, storage: Storage, *, cache_type: str, relative_path: str,
    source: dict[str, Any], tool_name: str, source_url: str, used_mock: bool,
) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{cache_type}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{relative_path}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    try:
        feature_count = len(json.loads(body).get("features") or [])
    except Exception:
        feature_count = 0

    final_path = sidecar.cache_path(NAMESPACE, cache_type, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, cache_type, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE, cache_type, relative_path,
            kind="file", size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source=source,
            tool={"name": tool_name, "version": "1.0"},
            extra={"feature_count": feature_count},
            storage=storage,
        )

    return DownloadResult(
        absolute_path=final_path,
        relative_path=relative_path,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        feature_count=feature_count,
        was_cached=False,
        source_url=source_url,
        used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Helpers + mock data.
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


def _mock_earthquakes() -> dict[str, Any]:
    rows = [
        ("M 6.4 - 12km SW of Example City", 6.4, -118.2, 34.0, 10.2, "us6000abcd"),
        ("M 5.1 - Mid-Atlantic Ridge", 5.1, -30.0, 0.5, 12.0, "us6000efgh"),
        ("M 7.2 - off the coast of Honshu", 7.2, 142.4, 38.3, 24.5, "us6000ijkl"),
        ("M 4.8 - near Athens, Greece", 4.8, 23.7, 38.0, 8.0, "us6000mnop"),
    ]
    feats = []
    for title, mag, lon, lat, depth, idc in rows:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "mag": mag, "place": title.split(" - ", 1)[-1], "title": title,
                "magType": "mww", "depth_km": depth, "tsunami": 0,
                "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{idc}",
                "code": idc, "type": "earthquake",
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def _mock_faults() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [
                    [-125.0, 40.0], [-120.0, 36.0], [-116.0, 33.0]]},
                "properties": {"Name": "PA/NA (San Andreas, mock)", "LAYER": "boundary"},
            },
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [
                    [140.0, 40.0], [142.0, 36.0], [143.0, 32.0]]},
                "properties": {"Name": "PA/OK (Japan Trench, mock)", "LAYER": "boundary"},
            },
        ],
    }
