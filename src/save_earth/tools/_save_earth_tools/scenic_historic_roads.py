"""Scenic and historic roads — OSM ways/routes cached as one GeoJSON of LINES.

Two toggleable map layers off ONE cached file, split by a derived ``road_kind``
(``scenic`` / ``historic``) via the shared-source filter mechanism, exactly as
the nuclear-sites map splits ``site_type``.

What each layer actually is (measured against Overpass 2026-08-27, so the map's
claims are the data's claims):

- **scenic** — ``scenic=yes`` on a ``highway``. This is overwhelmingly a NORTH
  AMERICAN mapping convention: 8,611 of the world's ~11,400 highway-tagged
  ``scenic=yes`` ways are in North America, which is why that is the default
  region. It marks a road a mapper considered scenic; it is NOT the official
  National Scenic Byway designation, which OSM does not carry as a distinct
  network (checked: no ``network=*`` value naming a byway appears in the top 200).
- **historic** — three sources unioned, because no single tag covers it:
  ``historic=yes`` on a highway (1,498 in NA), the road-shaped ``historic=*``
  values (``road``/``hollow_way``/``roman_road``/``ancient_road`` — 189 in NA but
  the bulk of the EUROPEAN set: ~10k worldwide), and ``route=historic`` RELATIONS
  (49 in NA, 1,127 worldwide) whose members carry the route name — this is the
  layer that holds Route 66 and the National Historic Trails.

⚠️ Query the SELECTIVE tag, never a bare key. ``way[highway][historic]`` over a
continent makes Overpass scan every historic object (all the buildings, ruins and
tombs) before intersecting — measured: it blew a 120 s client timeout, while the
value-specified form answers in seconds. Same trap the ALPR module documents.

Coverage is crowd-sourced and uneven: absence of a line means nobody tagged it,
not that the road is neither scenic nor historic.
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

logger = logging.getLogger("save-earth.scenic_historic_roads")

NAMESPACE = "save-earth"
CACHE_TYPE = "scenic-historic-roads"
DEFAULT_REGION = "north-america"

# Endpoints and the region table are configuration, not code: override the
# mirrors with FW_OVERPASS_ENDPOINTS (comma-separated) and the region/bbox table
# with FW_ROAD_REGIONS_FILE, so a deployment can repoint either without an edit.
_DEFAULT_OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
_REGIONS_ENV = "FW_ROAD_REGIONS_FILE"
_DEFAULT_REGIONS_FILE = Path(__file__).resolve().parents[2] / "data" / "road_regions.json"

USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
DEFAULT_MAX_AGE_HOURS = 24.0 * 30

# Road-shaped values of `historic`. Bare `historic` is deliberately NOT used —
# see the module docstring's warning about scanning every historic object.
_HISTORIC_WAY_VALUES = ("road", "hollow_way", "roman_road", "ancient_road")

# ~1.1 m at the equator. Road geometries dominate the file size (thousands of
# ways x tens of nodes); full float repr roughly doubles it for precision the
# map cannot render.
_COORD_PRECISION = 5

_lock = threading.Lock()


def overpass_endpoints() -> tuple[str, ...]:
    raw = os.environ.get("FW_OVERPASS_ENDPOINTS", "").strip()
    if raw:
        eps = tuple(e.strip() for e in raw.split(",") if e.strip())
        if eps:
            return eps
    return _DEFAULT_OVERPASS_ENDPOINTS


def regions() -> dict[str, dict[str, Any]]:
    """The region table (label/bbox/center/zoom), from JSON config."""
    regs = _config().get("regions")
    if not isinstance(regs, dict) or not regs:
        raise RuntimeError("road regions config has no 'regions' mapping")
    return regs


def _config() -> dict[str, Any]:
    path = Path(os.environ.get(_REGIONS_ENV) or _DEFAULT_REGIONS_FILE)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def historic_route_exclusions() -> tuple[str, ...]:
    """Lower-cased patterns marking a `route=historic` relation as NOT a road.

    See the config file's own comment: route=historic is a weak tag and a few
    relations carrying it are transit or interurban rail. Empty tuple disables
    the cleanup entirely.
    """
    block = _config().get("historic_route_exclusions") or {}
    pats = block.get("operator_or_network_patterns") or []
    return tuple(str(p).lower() for p in pats if str(p).strip())


def region_config(region: str) -> dict[str, Any]:
    regs = regions()
    if region not in regs:
        raise KeyError(f"unknown region {region!r}; known: {sorted(regs)}")
    return regs[region]


def relative_path(region: str) -> str:
    """Cache path is region-scoped so several regions coexist."""
    return f"scenic_historic_roads_{region.replace('/', '_')}.geojson"


def _bbox_clause(cfg: dict[str, Any]) -> str:
    bbox = cfg.get("bbox")
    if not bbox:
        return ""  # worldwide
    s, w, n, e = (float(v) for v in bbox)
    return f"({s},{w},{n},{e})"


def _queries(region: str) -> list[tuple[str, str]]:
    """(road_kind, Overpass QL) for each selective sub-query."""
    b = _bbox_clause(region_config(region))
    hist_vals = "|".join(_HISTORIC_WAY_VALUES)
    return [
        # Scenic: one selective tag, intersected with highway.
        ("scenic", f'[out:json][timeout:600];way["scenic"="yes"]["highway"]{b};out geom;'),
        # Historic ways: historic=yes on a road, plus the road-shaped values.
        ("historic",
         f'[out:json][timeout:600];('
         f'way["historic"="yes"]["highway"]{b};'
         f'way["historic"~"^({hist_vals})$"]{b};'
         f');out geom;'),
    ]


def _route_queries(region: str) -> tuple[str, str]:
    """(relations-with-geometry, highway-member-ids) for historic route relations.

    TWO queries joined on way id, because `route=historic` also carries mistagged
    historic RAILWAYS (Chicago Junction Railway, Rock Island and Pacific, …) whose
    proper tag is `route=historic_railway`. Their names give them away but
    name-matching is brittle — a road may legitimately be called "Old Railroad
    Road". Instead Overpass proves road-ness: the second query returns only those
    members that are actually `[highway]`, and members outside that id set are
    dropped. The relation is still fetched whole because only IT carries the route
    name (a member way of Route 66 is an unremarkable highway on its own).
    """
    b = _bbox_clause(region_config(region))
    return (
        f'[out:json][timeout:600];relation["route"="historic"]{b};out geom;',
        f'[out:json][timeout:600];relation["route"="historic"]{b}->.r;'
        f'way(r.r)["highway"];out ids;',
    )


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


def download(*, region: str = DEFAULT_REGION, force: bool = False,
             max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
             storage: Storage | None = None, use_mock: bool = False) -> DownloadResult:
    """Fetch scenic + historic roads for ``region`` and cache as one GeoJSON."""
    s = storage or get_storage()
    rel = relative_path(region)
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)
    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, rel, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("scenic/historic roads cache hit for %s (%.1fh old)", region, age or -1.0)
                    return DownloadResult(art_path, rel, side.get("size_bytes", 0),
                                          side.get("sha256", ""),
                                          int((side.get("extra") or {}).get("feature_count", 0)),
                                          True, overpass_endpoints()[0])
        if use_mock:
            features, source_url, used_mock = _mock_features(), "mock://scenic-historic-roads", True
        else:
            if requests is None:
                raise RuntimeError("requests is not installed; run via the .sh wrapper or --use-mock.")
            features, source_url = _fetch_all(region)
            used_mock = False
        body = json.dumps({"type": "FeatureCollection", "features": features},
                          separators=(",", ":")).encode("utf-8")
        return _persist(body, s, region=region, source_url=source_url, used_mock=used_mock)


def _fetch_all(region: str) -> tuple[list[dict[str, Any]], str]:
    """Run each sub-query, tag its features with road_kind, and merge.

    Deduplicated by (kind, osm id): a way can match both `historic=yes` and a
    road-shaped `historic=*` value in the union, and a way can belong to more
    than one historic route relation.
    """
    features: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    used_endpoint = ""
    work: list[tuple[str, list[dict[str, Any]], set[int]]] = []
    for kind, query in _queries(region):
        elements, endpoint = _fetch_one(query, kind)
        used_endpoint = used_endpoint or endpoint
        work.append((kind, elements, set()))

    rel_q, id_q = _route_queries(region)
    rel_els, endpoint = _fetch_one(rel_q, "historic-routes")
    used_endpoint = used_endpoint or endpoint
    id_els, _ = _fetch_one(id_q, "historic-route-road-members")
    road_ids = {int(e["id"]) for e in id_els if e.get("type") == "way" and e.get("id") is not None}
    logger.info("historic route relations: %d, road members: %d", len(rel_els), len(road_ids))
    work.append(("historic", rel_els, road_ids))

    for kind, elements, road_ids_ in work:
        for el in elements:
            for feat in _to_features(el, kind, road_ids_):
                key = (kind, feat["properties"]["osm_id"])
                if key in seen:
                    continue
                seen.add(key)
                features.append(feat)
    if not features:
        raise RuntimeError(f"no scenic/historic road features returned for region {region!r}")
    counts: dict[str, int] = {}
    for f in features:
        k = f["properties"]["road_kind"]
        counts[k] = counts.get(k, 0) + 1
    logger.info("scenic/historic roads %s -> %s", region, counts)
    return features, used_endpoint


def _fetch_one(query: str, kind: str) -> tuple[list[dict[str, Any]], str]:
    last_exc: Exception | None = None
    for endpoint in overpass_endpoints():
        try:
            logger.info("querying Overpass %s (%s)", endpoint, kind)
            resp = requests.post(endpoint, data={"data": query},
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Overpass %s failed: %s", endpoint, exc)
            last_exc = exc
            continue
        elements = payload.get("elements")
        if elements is None:
            remark = payload.get("remark") or "no elements key (throttled?)"
            logger.warning("Overpass %s: %s", endpoint, remark)
            last_exc = RuntimeError(f"Overpass {endpoint}: {remark}")
            continue
        # An EMPTY list is a legitimate answer here (e.g. no Roman roads in North
        # America), so it is returned, not treated as a throttle.
        return elements, endpoint
    raise RuntimeError(f"all Overpass mirrors failed/throttled; last: {last_exc}")


def _line(geometry: list[dict[str, Any]] | None) -> list[list[float]] | None:
    if not geometry:
        return None
    coords = [[round(float(p["lon"]), _COORD_PRECISION), round(float(p["lat"]), _COORD_PRECISION)]
              for p in geometry if p.get("lat") is not None and p.get("lon") is not None]
    return coords if len(coords) >= 2 else None


def _props(tags: dict[str, Any], kind: str, osm_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    props = {
        "osm_id": osm_id,
        "road_kind": kind,
        "name": tags.get("name") or tags.get("ref") or "",
        "highway": tags.get("highway", ""),
        "historic": tags.get("historic", ""),
        "scenic": tags.get("scenic", ""),
        "surface": tags.get("surface", ""),
        "operator": tags.get("operator", ""),
        "wikipedia": tags.get("wikipedia", ""),
    }
    props.update(extra)
    return props


def _to_features(el: dict[str, Any], kind: str,
                 road_member_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """One OSM element -> zero or more LineString features."""
    tags = el.get("tags") or {}
    etype, eid = el.get("type"), el.get("id")
    if etype == "way":
        coords = _line(el.get("geometry"))
        if not coords:
            return []
        return [{"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": coords},
                 "properties": _props(tags, kind, f"way/{eid}", {"route_name": ""})}]
    if etype == "relation":
        # Drop the transit/rail mistags before they become "historic roads".
        haystack = f"{tags.get('operator', '')} {tags.get('network', '')}".lower()
        if any(p in haystack for p in historic_route_exclusions()):
            logger.info("skipping non-road route=historic relation %s (%s)",
                        eid, tags.get("name") or tags.get("network") or "?")
            return []
        # Members carry the geometry; the RELATION carries the route name, which
        # is the whole point of this sub-query (a member way of Route 66 is
        # usually just an unremarkable highway on its own).
        route_name = tags.get("name") or tags.get("ref") or ""
        out = []
        for i, m in enumerate(el.get("members") or []):
            if m.get("type") != "way":
                continue
            # Only members Overpass confirmed are highways (drops rail members).
            if road_member_ids is not None and int(m.get("ref", -1)) not in road_member_ids:
                continue
            coords = _line(m.get("geometry"))
            if not coords:
                continue
            out.append({"type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": coords},
                        "properties": _props(tags, kind, f"relation/{eid}/{m.get('ref', i)}",
                                             {"route_name": route_name,
                                              "name": route_name})})
        return out
    return []


def _persist(body: bytes, storage: Storage, *, region: str, source_url: str,
             used_mock: bool) -> DownloadResult:
    rel = relative_path(region)
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{rel}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)
    try:
        feats = json.loads(body).get("features") or []
    except Exception:
        feats = []
    counts: dict[str, int] = {}
    for f_ in feats:
        k = (f_.get("properties") or {}).get("road_kind", "?")
        counts[k] = counts.get(k, 0) + 1
    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, rel, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(NAMESPACE, CACHE_TYPE, rel, kind="file",
                              size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                              source={"publisher": "OpenStreetMap contributors — Overpass API",
                                      "url": source_url, "license": "ODbL 1.0",
                                      "used_mock": used_mock},
                              tool={"name": "scenic_historic_roads", "version": "1.0"},
                              extra={"feature_count": len(feats), "region": region,
                                     "counts_by_kind": counts}, storage=storage)
    return DownloadResult(final_path, rel, len(body), hashlib.sha256(body).hexdigest(),
                          len(feats), False, source_url, used_mock)


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
    """Tiny offline set: one scenic road, one historic route member."""
    return [
        {"type": "Feature",
         "geometry": {"type": "LineString",
                      "coordinates": [[-116.9, 36.5], [-116.8, 36.6], [-116.7, 36.7]]},
         "properties": _props({"name": "Mock Scenic Drive", "highway": "secondary",
                               "scenic": "yes"}, "scenic", "way/1", {"route_name": ""})},
        {"type": "Feature",
         "geometry": {"type": "LineString",
                      "coordinates": [[-97.5, 35.4], [-97.4, 35.45], [-97.3, 35.5]]},
         "properties": _props({"name": "Mock Historic Route", "highway": "trunk",
                               "historic": "yes"}, "historic", "relation/2/9",
                              {"route_name": "Mock Historic Route"})},
    ]
