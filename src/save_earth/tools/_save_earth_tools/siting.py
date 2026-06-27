"""Renewable-energy siting — annotate solar/wind plants with local resource.

Answers the *siting* question that the bare power-infrastructure map can't:
**is each renewable plant actually where the sun/wind is good?** It reads the
WRI ``solar.geojson`` / ``wind.geojson`` plant layers produced by
:func:`power.download_plants`, looks up the local resource for each plant, and
writes two annotated layers under ``cache/save-earth/siting/``:

  ``siting_solar.geojson`` — every solar plant + ``ghi_kwh_m2_day`` + ``siting_score``
  ``siting_wind.geojson``  — every wind  plant + ``wind_speed_ms``  + ``siting_score``

``siting_score`` is the raw resource value linearly mapped into ``[4, 8]`` over a
sensible utility-scale domain, so the *shared* MapLibre renderer's fixed
magnitude ramp (yellow = poor → dark-red = excellent) sizes + colours each plant
by how well it is sited — with **no renderer change**. The raw value is kept for
the popup.

Data source — **NASA POWER** (https://power.larc.nasa.gov), free, no key,
global. ``ALLSKY_SFC_SW_DWN`` is the all-sky surface solar irradiance (GHI,
kWh/m²/day); ``WS50M`` is the mean wind speed at 50 m (m/s).

**Why the REGIONAL endpoint, not per-point.** NASA POWER throttles sustained
point-query volume (a per-point sweep of ~2k cells trips HTTP 429 after a few
hundred calls and then crawls in backoff). The *regional* endpoint instead
returns a whole 1° grid for a bounding box in ONE call (max 10°×10° = 100
points, one parameter per call). So we fetch only the ~10° tiles that actually
contain plants — ~250 calls cover all ~16k plants worldwide at 1° resolution —
build an in-memory grid, and sample every plant locally. Far fewer calls (stays
under the throttle), bounded wall-time, and the grid is cached so re-runs are
free. Calls run at a small in-process concurrency with 429/5xx retry+backoff —
bounded concurrency, NOT a fleet fan-out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _save_earth_tools import power, sidecar  # noqa: E402
from _save_earth_tools.storage import Storage, get_storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("save-earth.siting")

NAMESPACE = "save-earth"
CACHE_TYPE = "siting"

REGIONAL_URL = "https://power.larc.nasa.gov/api/temporal/climatology/regional"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
_FILL = -999.0  # NASA POWER no-data sentinel
_TILE = 10      # regional endpoint max bounding box = 10° × 10° (a 1° grid)

# One annotated layer per renewable source: (source slug in the WRI/power cache,
# output filename, NASA POWER parameter, raw-property name, (domain_lo, domain_hi)).
# The domain is the utility-scale range mapped onto the renderer's [4,8] ramp.
RESOURCES = [
    ("solar", "siting_solar.geojson", "ALLSKY_SFC_SW_DWN", "ghi_kwh_m2_day", (2.5, 6.5)),
    ("wind",  "siting_wind.geojson",  "WS50M",             "wind_speed_ms",  (3.0, 11.0)),
]

_SCORE_LO, _SCORE_HI = 4.0, 8.0  # renderer magnitude ramp endpoints

_lock = threading.Lock()
_tls = threading.local()


def _session():
    """A per-thread requests.Session (connection reuse; thread-safe by isolation)."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
    return s


@dataclass
class SitingResult:
    cache_type: str
    feature_count: int
    per_layer: dict[str, int]
    cells_sampled: int  # number of 1° grid cells populated from NASA POWER
    was_cached: bool
    source_url: str
    files: list[str] = field(default_factory=list)


def _siting_score(value: float, lo: float, hi: float) -> float:
    """Map a raw resource value onto the renderer's [4,8] magnitude ramp."""
    frac = (value - lo) / (hi - lo) if hi > lo else 0.0
    frac = min(1.0, max(0.0, frac))
    return round(_SCORE_LO + (_SCORE_HI - _SCORE_LO) * frac, 3)


def _cell(lon: float, lat: float) -> tuple[int, int]:
    """1° grid cell key — floor maps both a plant and the NASA grid-point centre
    (X.5) to the same integer cell."""
    return (math.floor(lon), math.floor(lat))


def _tile(lon: float, lat: float) -> tuple[int, int]:
    """The 10° regional tile origin (SW corner) containing a point."""
    return (math.floor(lon / _TILE) * _TILE, math.floor(lat / _TILE) * _TILE)


def _regional(param: str, tlon: int, tlat: int, *, max_retries: int = 5) -> list[tuple[float, float, float]]:
    """Fetch one 10° tile's 1° grid for ``param`` → [(lon, lat, ANN), …].

    Retries 429 / 5xx with exponential backoff (honouring ``Retry-After``).
    """
    if requests is None:
        raise RuntimeError("requests not installed")
    sess = _session()
    delay = 1.5
    for attempt in range(max_retries + 1):
        r = sess.get(REGIONAL_URL, params={
            "parameters": param, "community": "RE",
            "latitude-min": tlat, "latitude-max": min(tlat + _TILE, 90),
            "longitude-min": tlon, "longitude-max": min(tlon + _TILE, 180),
            "format": "JSON",
        }, timeout=(30, 180))
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            wait = r.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait else delay, 30.0))
            delay = min(delay * 2, 30.0)
            continue
        r.raise_for_status()
        out: list[tuple[float, float, float]] = []
        for f in r.json().get("features", []):
            c = f["geometry"]["coordinates"]
            ann = (f["properties"].get("parameter", {}).get(param) or {}).get("ANN")
            if ann is not None and float(ann) > _FILL:
                out.append((float(c[0]), float(c[1]), float(ann)))
        return out
    return []


def _read_layer(rel: str, s: Storage) -> list[dict]:
    """Read a cached power layer's features (empty if absent)."""
    path = sidecar.cache_path(NAMESPACE, power.CACHE_TYPE, rel, s)
    if not s.exists(path):
        return []
    return json.loads(s.read_text(path)).get("features", [])


def _persist_layer(rel: str, features: list[dict], s: Storage, *, source: dict) -> str:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    body = json.dumps({"type": "FeatureCollection", "features": features},
                      separators=(",", ":")).encode("utf-8")
    stage = os.path.join(staging, f"{rel}.stage-{os.getpid()}")
    with open(stage, "wb") as f:
        f.write(body)
    final = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, rel, storage=s):
        s.finalize_from_local(stage, final)
        sidecar.write_sidecar(NAMESPACE, CACHE_TYPE, rel, kind="file",
                              size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                              source=source, tool={"name": "siting", "version": "1.0"},
                              extra={"feature_count": len(features)}, storage=s)
    return final


def _layer_count(rel: str, s: Storage) -> int:
    side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
    return int((side.get("extra") or {}).get("feature_count", 0)) if side else 0


def _fetch_grid(param: str, tiles: set[tuple[int, int]], max_workers: int) -> dict[tuple[int, int], float]:
    """Fetch every needed 10° tile for ``param`` and assemble a 1° grid lookup."""
    grid: dict[tuple[int, int], float] = {}

    def fetch(tile: tuple[int, int]):
        try:
            return _regional(param, tile[0], tile[1])
        except Exception as exc:  # noqa: BLE001
            logger.warning("NASA POWER regional %s tile %s failed: %s", param, tile, exc)
            return []

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for i, pts in enumerate(pool.map(fetch, sorted(tiles))):
            for lon, lat, val in pts:
                grid[_cell(lon, lat)] = val
            if (i + 1) % 25 == 0:
                logger.info("%s: fetched %d/%d tiles (%d cells)", param, i + 1, len(tiles), len(grid))
    return grid


def annotate(*, force: bool = False, max_workers: int = 3,
             storage: Storage | None = None) -> SitingResult:
    """Annotate cached solar/wind plants with NASA POWER resource + siting score.

    Cache-aware: returns immediately if both annotated layers already exist
    (unless ``force``). Requires the WRI plant layers to be downloaded first
    (``power.download_plants``) — the workflow sequences that.
    """
    s = storage or get_storage()
    outs = [rel for _, rel, *_ in RESOURCES]
    if not force and all(s.exists(sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)) for rel in outs):
        per = {rel: _layer_count(rel, s) for rel in outs}
        return SitingResult(CACHE_TYPE, sum(per.values()), per, 0, True, REGIONAL_URL, [])

    loaded: dict[str, list[dict]] = {slug: _read_layer(f"{slug}.geojson", s) for slug, *_ in RESOURCES}
    if not any(loaded.values()):
        raise RuntimeError("no solar/wind plants cached — run DownloadPowerPlants first")

    # One resource grid per parameter, fetched only over the 10° tiles that hold
    # that resource's plants.
    grids: dict[str, dict[tuple[int, int], float]] = {}
    with _lock:
        for slug, _rel, param, _prop, _dom in RESOURCES:
            if param in grids:
                continue
            tiles = {_tile(*ft["geometry"]["coordinates"][:2])
                     for ft in loaded[slug] if ft.get("geometry", {}).get("coordinates")}
            logger.info("%s: %d plants over %d tiles", param, len(loaded[slug]), len(tiles))
            grids[param] = _fetch_grid(param, tiles, max_workers)

    per, files = {}, []
    src = {"publisher": "NASA POWER (climatology, regional) + WRI Global Power Plant Database",
           "url": REGIONAL_URL, "license": "NASA POWER: free / WRI: CC BY 4.0"}
    for slug, rel, param, prop, (lo, hi) in RESOURCES:
        grid = grids.get(param, {})
        annotated: list[dict] = []
        for ft in loaded.get(slug, []):
            try:
                lon, lat = ft["geometry"]["coordinates"][:2]
            except (KeyError, TypeError, ValueError):
                continue
            val = grid.get(_cell(float(lon), float(lat)))
            props = dict(ft.get("properties") or {})
            if val is not None:
                props[prop] = round(val, 3)
                props["siting_score"] = _siting_score(val, lo, hi)
            annotated.append({"type": "Feature", "geometry": ft["geometry"], "properties": props})
        files.append(_persist_layer(rel, annotated, s, source=src))
        per[rel] = len(annotated)

    cells = sum(len(g) for g in grids.values())
    return SitingResult(CACHE_TYPE, sum(per.values()), per, cells, False, REGIONAL_URL, files)
