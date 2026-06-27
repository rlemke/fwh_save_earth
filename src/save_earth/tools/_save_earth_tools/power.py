"""World power infrastructure — power plants by source + major transmission lines.

Two sources, cached under ``cache/save-earth/power/``:

- **Power plants** from the WRI **Global Power Plant Database** (~35k plants
  worldwide). Classified by ``primary_fuel`` into one GeoJSON per source —
  hydro (dams) / coal / solar / wind / nuclear — so the map draws one coloured,
  toggleable layer each. Every WRI field is kept (name, capacity_mw,
  commissioning_year, country, owner, …) so the popup shows the full record.
- **Transmission lines** from OpenStreetMap: ``power=line`` at **≥500 kV** (the
  major / backbone lines), as one LineString layer. Best-effort and standalone —
  OSM has no plant→substation linkage and the full global grid is far too large,
  so this is the highest-voltage subset, simplified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
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
try:
    from shapely.geometry import LineString, mapping
except ImportError:  # pragma: no cover
    LineString = None

logger = logging.getLogger("save-earth.power")

NAMESPACE = "save-earth"
CACHE_TYPE = "power"

WRI_CSV = ("https://raw.githubusercontent.com/wri/global-power-plant-database/"
           "master/output_database/global_power_plant_database.csv")
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"

# WRI primary_fuel -> (slug, label, colour). "Hydro" = dams.
FUELS = [
    ("hydro",   "Hydro", "Hydroelectric (dams)", "#1565c0"),
    ("coal",    "Coal",  "Coal",                 "#37474f"),
    ("solar",   "Solar", "Solar",                "#f9a825"),
    ("wind",    "Wind",  "Wind",                 "#2e7d32"),
    ("nuclear", "Nuclear", "Nuclear",            "#d84315"),
]
_FUEL_BY_WRI = {wri: slug for slug, wri, _, _ in FUELS}

# ≥500 kV: any 6-digit voltage 500000-999999, or 7+ digits (1 MV+). Multi-value
# voltage tags ("500000;230000") are matched if any component qualifies.
_VOLT_RE = "[5-9][0-9]{5}|[0-9]{7}"
# A single global `out geom` query scans every power=line worldwide and 504s, so
# tile by continent (S,W,N,E) — each scan is bounded and merges/dedupes by way id.
TRANSMISSION_BBOXES = [
    (10, -170, 75, -48),    # North + Central America
    (-58, -85, 14, -32),    # South America
    (34, -28, 72, 42),      # Europe
    (-37, -20, 38, 55),     # Africa
    (3, 42, 45, 90),        # West / Central / South Asia
    (3, 90, 55, 150),       # East / SE Asia
    (45, 42, 78, 180),      # North Asia (Russia)
    (-48, 110, 2, 180),     # Oceania
]

_lock = threading.Lock()


@dataclass
class DownloadResult:
    cache_type: str
    feature_count: int
    per_layer: dict[str, int]
    was_cached: bool
    source_url: str
    files: list[str] = field(default_factory=list)


def _rnd(o, nd=3):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, (list, tuple)):
        return [_rnd(x, nd) for x in o]
    return o


def _persist_layer(rel: str, features: list[dict], storage: Storage, *, source: dict, tool: str):
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    body = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")).encode("utf-8")
    stage = os.path.join(staging, f"{rel}.stage-{os.getpid()}")
    with open(stage, "wb") as f:
        f.write(body)
    final = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, rel, storage=storage):
        storage.finalize_from_local(stage, final)
        sidecar.write_sidecar(NAMESPACE, CACHE_TYPE, rel, kind="file",
                              size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                              source=source, tool={"name": tool, "version": "1.0"},
                              extra={"feature_count": len(features)}, storage=storage)
    return final


# ---------------------------------------------------------------------------
# Power plants (WRI Global Power Plant Database)
# ---------------------------------------------------------------------------

def download_plants(*, force: bool = False, storage: Storage | None = None) -> DownloadResult:
    s = storage or get_storage()
    if requests is None:
        raise RuntimeError("requests not installed")
    with _lock:
        text = requests.get(WRI_CSV, headers={"User-Agent": USER_AGENT}, timeout=120).text
        buckets: dict[str, list[dict]] = {slug: [] for slug, *_ in FUELS}
        for row in csv.DictReader(io.StringIO(text)):
            slug = _FUEL_BY_WRI.get(row.get("primary_fuel"))
            if slug is None:
                continue
            try:
                lon, lat = float(row["longitude"]), float(row["latitude"])
            except (KeyError, ValueError, TypeError):
                continue
            # Keep the IMPORTANT fields for the popup (skip WRI's bulky per-year
            # generation_gwh_* / estimated_generation_* columns that just bloat).
            keep = ("name", "capacity_mw", "primary_fuel", "other_fuel1", "commissioning_year",
                    "country_long", "owner", "source", "url", "geolocation_source", "gppd_idnr")
            props = {k: row[k] for k in keep if row.get(k) not in ("", None)}
            buckets[slug].append({"type": "Feature",
                                  "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                                  "properties": props})
        per, files = {}, []
        src = {"publisher": "World Resources Institute — Global Power Plant Database",
               "url": WRI_CSV, "license": "CC BY 4.0"}
        for slug, *_ in FUELS:
            feats = buckets[slug]
            if feats:
                files.append(_persist_layer(f"{slug}.geojson", feats, s, source=src, tool="power"))
                per[slug] = len(feats)
        return DownloadResult(CACHE_TYPE, sum(per.values()), per, False, WRI_CSV, files)


# ---------------------------------------------------------------------------
# Transmission lines (OSM ≥500 kV)
# ---------------------------------------------------------------------------

def _overpass(query: str):
    last = None
    for ep in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(ep, data={"data": query},
                              headers={"User-Agent": USER_AGENT}, timeout=(30, 400))
            r.raise_for_status()
            return r.json()["elements"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Overpass %s failed: %s", ep, exc)
            last = exc
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def download_transmission(*, force: bool = False, storage: Storage | None = None) -> DownloadResult:
    s = storage or get_storage()
    if requests is None or LineString is None:
        raise RuntimeError("requests + shapely required")
    seen: set[int] = set()
    feats: list[dict] = []
    for (south, west, north, east) in TRANSMISSION_BBOXES:
        q = (f'[out:json][timeout:300][bbox:{south},{west},{north},{east}];'
             f'way["power"="line"]["voltage"~"{_VOLT_RE}"];out geom;')
        els = _overpass(q)
        logger.info("bbox %s,%s,%s,%s -> %d ways", south, west, north, east, len(els))
        for el in els:
            oid = el.get("id")
            if oid in seen:
                continue  # lines crossing a tile border appear in >1 bbox
            seen.add(oid)
            g = el.get("geometry")
            if not g or len(g) < 2:
                continue
            line = LineString([(p["lon"], p["lat"]) for p in g]).simplify(0.02, preserve_topology=False)
            if line.is_empty or line.length == 0:
                continue
            gj = mapping(line)
            props = dict(el.get("tags") or {})
            props["osm_id"] = oid
            props["osm_url"] = f"https://www.openstreetmap.org/way/{oid}"
            feats.append({"type": "Feature",
                          "geometry": {"type": gj["type"], "coordinates": _rnd(gj["coordinates"])},
                          "properties": props})
    src = {"publisher": "OpenStreetMap contributors — Overpass API", "url": OVERPASS_ENDPOINTS[0],
           "license": "ODbL 1.0"}
    final = _persist_layer("transmission.geojson", feats, s, source=src, tool="power")
    return DownloadResult(CACHE_TYPE, len(feats), {"transmission": len(feats)}, False, OVERPASS_ENDPOINTS[0], [final])


# ---------------------------------------------------------------------------
# Transmission — fan-out form: one tile per continental bbox + a merge step.
# ---------------------------------------------------------------------------

TILES_SUBDIR = "transmission-tiles"
_SRC_OSM = {"publisher": "OpenStreetMap contributors — Overpass API",
            "url": OVERPASS_ENDPOINTS[0], "license": "ODbL 1.0"}


def _bbox_slug(bbox: str) -> str:
    return bbox.strip().replace(",", "_").replace(" ", "")


def _line_features(els: list[dict]) -> list[dict]:
    feats = []
    for el in els:
        g = el.get("geometry")
        if not g or len(g) < 2:
            continue
        line = LineString([(p["lon"], p["lat"]) for p in g]).simplify(0.02, preserve_topology=False)
        if line.is_empty or line.length == 0:
            continue
        gj = mapping(line)
        props = dict(el.get("tags") or {})
        props["osm_id"] = el.get("id")
        props["osm_url"] = f"https://www.openstreetmap.org/way/{el.get('id')}"
        feats.append({"type": "Feature",
                      "geometry": {"type": gj["type"], "coordinates": _rnd(gj["coordinates"])},
                      "properties": props})
    return feats


def download_transmission_tile(bbox: str, *, storage: Storage | None = None) -> int:
    """Fetch ≥500 kV lines for ONE bbox ("S,W,N,E") and cache as a per-tile file.
    Designed to run in parallel (one task per continental bbox)."""
    s = storage or get_storage()
    if requests is None or LineString is None:
        raise RuntimeError("requests + shapely required")
    south, west, north, east = (p.strip() for p in bbox.split(","))
    q = (f'[out:json][timeout:300][bbox:{south},{west},{north},{east}];'
         f'way["power"="line"]["voltage"~"{_VOLT_RE}"];out geom;')
    feats = _line_features(_overpass(q))
    _persist_layer(f"{TILES_SUBDIR}/{_bbox_slug(bbox)}.geojson", feats, s, source=_SRC_OSM, tool="power")
    return len(feats)


def merge_transmission(bboxes: list[str], *, storage: Storage | None = None) -> int:
    """Merge the per-tile transmission GeoJSONs into one layer, deduping lines
    that cross a tile border by OSM way id. Reads only the named tiles (no object
    listing needed, so it works on s3)."""
    s = storage or get_storage()
    seen: set = set()
    feats: list[dict] = []
    for bbox in bboxes:
        rel = f"{TILES_SUBDIR}/{_bbox_slug(bbox)}.geojson"
        path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)
        if not s.exists(path):
            continue
        for f in json.loads(s.read_text(path)).get("features", []):
            oid = (f.get("properties") or {}).get("osm_id")
            if oid in seen:
                continue
            seen.add(oid)
            feats.append(f)
    _persist_layer("transmission.geojson", feats, s, source=_SRC_OSM, tool="power")
    return len(feats)
