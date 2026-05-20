"""OpenLitterMap downloader — geotagged litter observations.

OpenLitterMap (https://openlittermap.com) is a crowd-sourced,
CC-BY-SA licensed database of geotagged litter photos. The public API
has two GeoJSON endpoints relevant to a global map:

- ``/api/clusters?zoom=N`` — aggregated clusters, works at any zoom.
  Best for a global overview; each feature has ``point_count``.
- ``/api/points?bbox=...&zoom=N`` — individual photos with full
  per-observation metadata (tags, materials, datetime, picked-up
  status). Requires ``zoom >= 15``, so it only works for small
  bboxes (neighbourhood-scale).

This module wraps both endpoints behind a single ``download()`` call.
The default mode is ``clusters`` at zoom 4 — a world-scale overview
that fits every other save-earth layer. Pass ``mode="points"`` with
``bbox`` + ``zoom >= 15`` for detail maps.

Cache layout::

    cache/save-earth/openlittermap/<mode>-zoom<N>[_<bbox>].geojson + .meta.json

Each (mode, zoom, bbox) combination gets its own cache entry so you
can have both a global clusters map and a per-city points map without
stepping on each other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from datetime import UTC

from _lib import sidecar  # noqa: E402
from _lib.storage import LocalStorage, Storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("save-earth.openlittermap")

NAMESPACE = "save-earth"
CACHE_TYPE = "openlittermap"

# API base — the mode suffix is appended at fetch time.
DEFAULT_BASE_URL = "https://openlittermap.com/api"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 180
DEFAULT_MAX_AGE_HOURS = 24.0

Mode = Literal["clusters", "points"]
DEFAULT_MODE: Mode = "clusters"
DEFAULT_ZOOM = 4
MIN_POINTS_ZOOM = 15  # server-enforced on /api/points

_lock = threading.Lock()


@dataclass
class FetchResult:
    mode: Mode
    zoom: int
    bbox: tuple[float, float, float, float] | None
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    source_url: str
    was_cached: bool
    generated_at: str
    used_mock: bool = False


def download(
    *,
    mode: Mode = DEFAULT_MODE,
    zoom: int = DEFAULT_ZOOM,
    bbox: tuple[float, float, float, float] | None = None,
    url: str = DEFAULT_BASE_URL,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> FetchResult:
    """Fetch an OpenLitterMap GeoJSON feed and cache it.

    ``mode``:
      - ``"clusters"`` (default): aggregate clusters; any zoom; bbox optional.
      - ``"points"``: individual photos; requires ``zoom >= 15`` AND a bbox.

    ``bbox`` uses ``(min_lon, min_lat, max_lon, max_lat)`` order — the
    same (left, bottom, right, top) OpenLitterMap expects.
    """
    if mode == "points":
        if zoom < MIN_POINTS_ZOOM:
            raise ValueError(
                f"mode=points requires zoom>={MIN_POINTS_ZOOM} "
                f"(server enforces this); got zoom={zoom}. Use mode=clusters "
                f"for lower zooms / global maps."
            )
        if bbox is None:
            raise ValueError(
                "mode=points requires a --bbox (min_lon,min_lat,max_lon,max_lat). "
                "The /api/points endpoint rejects unbounded queries."
            )

    s = storage or LocalStorage()
    relative_path = _relative_path(mode, zoom, bbox)
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, relative_path, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, relative_path, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info(
                        "openlittermap/%s cache hit (%.1fh old)",
                        relative_path,
                        age or -1.0,
                    )
                    return FetchResult(
                        mode=mode,
                        zoom=zoom,
                        bbox=bbox,
                        absolute_path=art_path,
                        relative_path=relative_path,
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        feature_count=int(side.get("extra", {}).get("feature_count", 0)),
                        source_url=_build_url(url, mode, zoom, bbox),
                        was_cached=True,
                        generated_at=side.get("generated_at", ""),
                    )

        if use_mock:
            body = json.dumps(_mock_geojson(mode)).encode("utf-8")
            return _persist(
                mode=mode,
                zoom=zoom,
                bbox=bbox,
                body=body,
                url=_build_url(url, mode, zoom, bbox),
                storage=s,
                used_mock=True,
                relative_path=relative_path,
            )

        if requests is None:
            raise RuntimeError(
                "requests library is not installed. Install it, run via the .sh "
                "wrapper (activates .venv), or pass --use-mock."
            )

        full_url = _build_url(url, mode, zoom, bbox)
        logger.info("downloading %s", full_url)
        resp = requests.get(
            full_url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        )
        if resp.status_code == 422:
            # Validation error from the Laravel API; surface it cleanly.
            try:
                err = resp.json()
                msg = err.get("message") or json.dumps(err)
            except Exception:
                msg = resp.text[:200]
            raise RuntimeError(f"OpenLitterMap rejected request: {msg}")
        resp.raise_for_status()
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"OpenLitterMap returned non-JSON ({resp.status_code}): {resp.text[:200]!r}"
            ) from exc
        data = _normalize(parsed)
        body_bytes = json.dumps(data).encode("utf-8")
        return _persist(
            mode=mode,
            zoom=zoom,
            bbox=bbox,
            body=body_bytes,
            url=full_url,
            storage=s,
            used_mock=False,
            relative_path=relative_path,
        )


# ---------------------------------------------------------------------------
# URL + path helpers.
# ---------------------------------------------------------------------------


def _build_url(
    base: str,
    mode: Mode,
    zoom: int,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Compose the full API URL for a (mode, zoom, bbox) tuple."""
    base = base.rstrip("/")
    params = [f"zoom={zoom}"]
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        params.extend(
            [
                f"bbox%5Bleft%5D={min_lon}",
                f"bbox%5Bbottom%5D={min_lat}",
                f"bbox%5Bright%5D={max_lon}",
                f"bbox%5Btop%5D={max_lat}",
            ]
        )
    query = "&".join(params)
    return f"{base}/{mode}?{query}"


def _relative_path(
    mode: Mode,
    zoom: int,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """Cache entry path — one per distinct (mode, zoom, bbox) combination."""
    if bbox is None:
        return f"{mode}-zoom{zoom}.geojson"
    # Stable bbox suffix — floats with 4 decimal places, separators safe for FS.
    return f"{mode}-zoom{zoom}_{bbox[0]:.4f},{bbox[1]:.4f},{bbox[2]:.4f},{bbox[3]:.4f}.geojson"


def _persist(
    *,
    mode: Mode,
    zoom: int,
    bbox: tuple[float, float, float, float] | None,
    body: bytes,
    url: str,
    storage: Storage,
    used_mock: bool,
    relative_path: str,
) -> FetchResult:
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{relative_path.replace('/', '_')}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)
    digest = hashlib.sha256(body).hexdigest()

    try:
        parsed = json.loads(body)
        feature_count = len(parsed.get("features") or [])
    except Exception:
        feature_count = 0

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body),
            sha256=digest,
            source={
                "publisher": "OpenLitterMap",
                "url": url,
                "mode": mode,
                "zoom": zoom,
                "bbox": list(bbox) if bbox else None,
                "used_mock": used_mock,
            },
            tool={"name": "openlittermap", "version": "1.0"},
            extra={
                "mode": mode,
                "zoom": zoom,
                "feature_count": feature_count,
            },
            storage=storage,
        )
    return FetchResult(
        mode=mode,
        zoom=zoom,
        bbox=bbox,
        absolute_path=final_path,
        relative_path=relative_path,
        size_bytes=len(body),
        sha256=digest,
        feature_count=feature_count,
        source_url=url,
        was_cached=False,
        generated_at=side["generated_at"],
        used_mock=used_mock,
    )


def _normalize(data: Any) -> dict[str, Any]:
    """Coerce whatever the upstream returned into a stable FeatureCollection.

    Both /api/clusters and /api/points return a FeatureCollection; the
    guard handles odd wrapping if that ever changes.
    """
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        return {
            "type": "FeatureCollection",
            "features": data.get("features") or [],
        }
    if isinstance(data, dict) and isinstance(data.get("features"), list):
        return {"type": "FeatureCollection", "features": data["features"]}
    raise ValueError("OpenLitterMap response is not a recognizable GeoJSON shape")


def _age_hours(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    from datetime import datetime

    try:
        ts = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _mock_geojson(mode: Mode) -> dict[str, Any]:
    """Deterministic offline data matching the real-endpoint shape for each mode."""
    if mode == "clusters":
        return {
            "type": "FeatureCollection",
            "name": "clusters",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "lon": -73.9857,
                        "lat": 40.7484,
                        "cluster": True,
                        "point_count": 42,
                        "point_count_abbreviated": "42",
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [-73.9857, 40.7484],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "lon": -122.4194,
                        "lat": 37.7749,
                        "cluster": True,
                        "point_count": 18,
                        "point_count_abbreviated": "18",
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [-122.4194, 37.7749],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {
                        "lon": 2.3522,
                        "lat": 48.8566,
                        "cluster": True,
                        "point_count": 7,
                        "point_count_abbreviated": "7",
                    },
                    "geometry": {"type": "Point", "coordinates": [2.3522, 48.8566]},
                },
            ],
        }
    # points mode
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [-73.9857, 40.7484],
                },
                "properties": {
                    "id": 1,
                    "datetime": "2026-04-10T12:30:00Z",
                    "verified": 2,
                    "picked_up": True,
                    "summary": {
                        "tags": [{"category_id": 1, "object_id": 10}],
                        "totals": {"litter": 1},
                        "keys": {"categories": {"1": "smoking"}},
                    },
                },
            },
        ],
    }
