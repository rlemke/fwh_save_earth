"""Renewable-energy siting — annotate solar/wind plants with local resource.

Answers the *siting* question that the bare power-infrastructure map can't:
**is each renewable plant actually where the sun/wind is good?** It reads the
WRI ``solar.geojson`` / ``wind.geojson`` plant layers produced by
:func:`power.download_plants`, samples each plant's location against NASA
POWER's 20-year climatology, and writes two annotated layers under
``cache/save-earth/siting/``:

  ``siting_solar.geojson`` — every solar plant + ``ghi_kwh_m2_day`` + ``siting_score``
  ``siting_wind.geojson``  — every wind  plant + ``wind_speed_ms``  + ``siting_score``

``siting_score`` is the raw resource value linearly mapped into ``[4, 8]`` over a
sensible utility-scale domain, so the *shared* MapLibre renderer's fixed
magnitude ramp (yellow = poor → dark-red = excellent) sizes + colours each plant
by how well it is sited — with **no renderer change**. The raw value is kept for
the popup.

Data source — **NASA POWER** (https://power.larc.nasa.gov): a free, no-key,
global point climatology API. ``ALLSKY_SFC_SW_DWN`` is the all-sky surface solar
irradiance (GHI, kWh/m²/day); ``WS50M`` is the mean wind speed at 50 m (m/s).
Resource varies smoothly in space, so plant locations are de-duplicated onto a
coarse grid (default 0.5°) and ONE climatology call is made per unique cell
(both parameters at once) — a few hundred *sequential* calls inside a single
cached handler, deliberately **not** a fleet fan-out (the transmission/Overpass
lesson: a shared egress IP must not fan out a rate-limited API).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
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

NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
_FILL = -999.0  # NASA POWER no-data sentinel

# One annotated layer per renewable source: (source slug in the WRI/power cache,
# output filename, NASA POWER parameter, raw-property name, (domain_lo, domain_hi)).
# The domain is the utility-scale range mapped onto the renderer's [4,8] ramp.
RESOURCES = [
    ("solar", "siting_solar.geojson", "ALLSKY_SFC_SW_DWN", "ghi_kwh_m2_day", (2.5, 6.5)),
    ("wind",  "siting_wind.geojson",  "WS50M",             "wind_speed_ms",  (3.0, 11.0)),
]

_SCORE_LO, _SCORE_HI = 4.0, 8.0  # renderer magnitude ramp endpoints

_lock = threading.Lock()


@dataclass
class SitingResult:
    cache_type: str
    feature_count: int
    per_layer: dict[str, int]
    cells_sampled: int
    was_cached: bool
    source_url: str
    files: list[str] = field(default_factory=list)


def _siting_score(value: float, lo: float, hi: float) -> float:
    """Map a raw resource value onto the renderer's [4,8] magnitude ramp."""
    frac = (value - lo) / (hi - lo) if hi > lo else 0.0
    frac = min(1.0, max(0.0, frac))
    return round(_SCORE_LO + (_SCORE_HI - _SCORE_LO) * frac, 3)


def _cell(lon: float, lat: float, step: float) -> tuple[int, int]:
    """Coarse-grid key for de-duplicating NASA POWER calls."""
    return (round(lon / step), round(lat / step))


def _cell_center(key: tuple[int, int], step: float) -> tuple[float, float]:
    return (round(key[0] * step, 4), round(key[1] * step, 4))


def _nasa_power(lat: float, lon: float, params: list[str]) -> dict[str, float]:
    """One NASA POWER climatology call → {param: ANN value} (skips no-data)."""
    if requests is None:
        raise RuntimeError("requests not installed")
    r = requests.get(
        NASA_POWER_URL,
        params={"parameters": ",".join(params), "community": "RE",
                "longitude": lon, "latitude": lat, "format": "JSON"},
        headers={"User-Agent": USER_AGENT}, timeout=(30, 120),
    )
    r.raise_for_status()
    block = r.json()["properties"]["parameter"]
    out: dict[str, float] = {}
    for p in params:
        ann = (block.get(p) or {}).get("ANN")
        if ann is not None and float(ann) > _FILL:
            out[p] = float(ann)
    return out


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


def annotate(*, force: bool = False, grid_deg: float = 0.5,
             throttle_s: float = 0.2, storage: Storage | None = None) -> SitingResult:
    """Annotate cached solar/wind plants with NASA POWER resource + siting score.

    Cache-aware: returns immediately if both annotated layers already exist
    (unless ``force``). Requires the WRI plant layers to be downloaded first
    (``power.download_plants``) — the workflow sequences that.
    """
    s = storage or get_storage()
    outs = [rel for _, rel, *_ in RESOURCES]
    if not force and all(s.exists(sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)) for rel in outs):
        per = {rel: _layer_count(rel, s) for rel in outs}
        return SitingResult(CACHE_TYPE, sum(per.values()), per, 0, True, NASA_POWER_URL, [])

    # Load source plants and collect the unique grid cells to sample (across all
    # resources, so a cell shared by a nearby solar+wind plant is sampled once).
    loaded: dict[str, list[dict]] = {}
    cells: set[tuple[int, int]] = set()
    for slug, _rel, _param, _prop, _dom in RESOURCES:
        feats = _read_layer(f"{slug}.geojson", s)
        loaded[slug] = feats
        for ft in feats:
            try:
                lon, lat = ft["geometry"]["coordinates"][:2]
            except (KeyError, TypeError, ValueError):
                continue
            cells.add(_cell(float(lon), float(lat), grid_deg))
    if not any(loaded.values()):
        raise RuntimeError(
            "no solar/wind plants cached — run DownloadPowerPlants first")

    params = [p for _, _, p, _, _ in RESOURCES]
    sampled: dict[tuple[int, int], dict[str, float]] = {}
    with _lock:
        for i, key in enumerate(sorted(cells)):
            clon, clat = _cell_center(key, grid_deg)
            try:
                sampled[key] = _nasa_power(clat, clon, params)
            except Exception as exc:  # noqa: BLE001
                logger.warning("NASA POWER cell (%s,%s) failed: %s", clon, clat, exc)
                sampled[key] = {}
            if (i + 1) % 50 == 0:
                logger.info("sampled %d/%d cells", i + 1, len(cells))
            if throttle_s:
                time.sleep(throttle_s)

    per, files = {}, []
    src = {"publisher": "NASA POWER (climatology) + WRI Global Power Plant Database",
           "url": NASA_POWER_URL, "license": "NASA POWER: free / WRI: CC BY 4.0"}
    for slug, rel, param, prop, (lo, hi) in RESOURCES:
        annotated: list[dict] = []
        for ft in loaded.get(slug, []):
            try:
                lon, lat = ft["geometry"]["coordinates"][:2]
            except (KeyError, TypeError, ValueError):
                continue
            val = sampled.get(_cell(float(lon), float(lat), grid_deg), {}).get(param)
            props = dict(ft.get("properties") or {})
            if val is not None:
                props[prop] = round(val, 3)
                props["siting_score"] = _siting_score(val, lo, hi)
            annotated.append({"type": "Feature", "geometry": ft["geometry"], "properties": props})
        files.append(_persist_layer(rel, annotated, s, source=src))
        per[rel] = len(annotated)

    return SitingResult(CACHE_TYPE, sum(per.values()), per, len(cells), False, NASA_POWER_URL, files)
