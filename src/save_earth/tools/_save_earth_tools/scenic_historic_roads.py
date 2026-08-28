"""Scenic and historic roads - OSM ways and route relations as one GeoJSON of LINES.

Two toggleable map layers off ONE cached file, split by a derived ``road_kind``
(``scenic`` / ``historic``) via the shared-source filter, exactly as the
nuclear-sites map splits ``site_type``.

⚠️ The per-way tags are NOT the main source, though they look like the obvious
one. Measured across North America (2026-08-28):

    source                              scenic          historic
    per-way tags only                 4,915 km         1,981 km
    + designated route relations     51,829 km        61,526 km

``scenic=yes`` is a per-way annotation a mapper adds by hand, and it is both
sparse and geographically biased - 8,611 ways but only 428 distinct names,
dominated by a handful of Florida roads (Overseas Highway, A1A, Tamiami Trail)
plus the Blue Ridge Parkway. Judged on it alone the map says more about where
diligent mappers live than where scenic roads are.

The DESIGNATED routes are modelled the way OSM models any named route: as
relations carrying a ``network`` tag. There are 43 such state byway networks in
North America (``US:CO:Scenic``, ``US:OH:Byway``, ``US:FL:Scenic``, ...) and 22
historic ones, chiefly ``US:NHT`` (National Historic Trail auto-tour routes -
Lewis and Clark, Santa Fe, California) and ``US:auto_trail`` (Lincoln Highway,
Jefferson Highway). No single network is large enough to rank in taginfo's top
200, which is why they are easy to conclude do not exist.

Both are unioned and deduplicated BY WAY ID: a designated byway member is often
also tagged ``scenic=yes``, and a way belongs to several relations (the Lewis
and Clark auto tour is split across many), so keying by relation drew the same
road repeatedly. When the same way arrives twice, the ROUTE name wins over the
street name - a member way of the Santa Fe Trail is an unremarkable highway on
its own.

Everything selectable lives in the JSON config (``road_regions.json``, override
with ``FW_ROAD_REGIONS_FILE``): regions and bboxes, the per-kind Overpass
selectors, the transit/rail exclusion patterns, and the simplify tolerance.

⚠️ Query the SELECTIVE tag, never a bare key. ``way[highway][historic]`` over a
continent makes Overpass scan every historic object (all the ruins, tombs and
buildings) before intersecting - it blew a client timeout in testing. Likewise
``verify_highway_members`` costs a second query recursing to every member and
504s on wide selectors, so it is opt-in: ``route=road`` already means road,
while ``route=historic`` mixes in railways and transit and genuinely needs it.

Geometry is simplified (~33 m, under a pixel at these zooms): unsimplified, the
continental set is 81.6 MB of coordinates, too large to inline into a page.

Coverage is crowd-sourced and uneven: absence of a line means nobody tagged or
mapped that route, not that the road is neither scenic nor historic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
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


def layer_sources() -> dict[str, dict[str, list[str]]]:
    """Per-kind Overpass selectors, from JSON config."""
    block = _config().get("layer_sources") or {}
    out: dict[str, dict[str, list[str]]] = {}
    for kind, cfg in block.items():
        if kind.startswith("_") or not isinstance(cfg, dict):
            continue
        rels = []
        for entry in cfg.get("relation_selectors") or []:
            if isinstance(entry, str):
                rels.append({"selector": entry, "verify_highway_members": False})
            else:
                rels.append({"selector": entry["selector"],
                             "verify_highway_members": bool(entry.get("verify_highway_members"))})
        out[kind] = {
            "way_selectors": list(cfg.get("way_selectors") or []),
            "relation_selectors": rels,
        }
    if not out:
        raise RuntimeError("road config has no 'layer_sources'")
    return out


def _way_query(selectors: list[str], bbox: str) -> str:
    body = "".join(f"{sel}{bbox};" for sel in selectors)
    return f"[out:json][timeout:600];({body});out geom;"


def _relation_queries(selector: str, bbox: str) -> tuple[str, str]:
    """(relations-with-geometry, its highway-member ids) for ONE selector.

    TWO queries joined on way id. The route relations carry mistagged historic
    RAILWAYS and transit lines, and a relation's `out geom` gives members no
    tags of their own, so road-ness cannot be judged from the first response.
    The second query asks Overpass for exactly those members that are
    `[highway]`; members outside that id set are dropped. The relation is still
    fetched whole because only IT carries the route name - a member way of the
    Santa Fe Trail is an unremarkable highway on its own.
    """
    return (
        f'[out:json][timeout:600];{selector}{bbox};out geom;',
        f'[out:json][timeout:600];{selector}{bbox}->.r;way(r.r)["highway"];out ids;',
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
    """Fetch every configured source, tag with road_kind, merge and dedupe.

    Deduplicated by (kind, way id): a way can match several selectors at once
    (a designated byway is often also tagged scenic=yes, and can belong to more
    than one route relation), and without this the same road would be drawn -
    and counted - repeatedly.
    """
    b = _bbox_clause(region_config(region))
    features: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    used_endpoint = ""
    per_kind: dict[str, int] = {}

    index: dict[tuple[str, str], dict[str, Any]] = {}

    def _keep(feat: dict[str, Any], kind: str) -> None:
        key = (kind, feat["properties"]["osm_id"])
        prior = index.get(key)
        if prior is not None:
            # Same road reached twice. Keep the richer label: a way selector
            # gives the street name, a route relation gives the ROUTE name
            # ("Santa Fe National Historic Trail"), which is what a reader wants.
            if feat["properties"].get("route_name") and not prior["properties"].get("route_name"):
                prior["properties"]["route_name"] = feat["properties"]["route_name"]
                prior["properties"]["name"] = feat["properties"]["name"]
            return
        index[key] = feat
        seen.add(key)
        features.append(feat)
        per_kind[kind] = per_kind.get(kind, 0) + 1

    def _keep_route(el: dict[str, Any], kind: str, road_ids: set[int] | None) -> None:
        """One route relation -> ONE MultiLineString feature.

        Emitting a Feature per member way meant ~115k tiny features (2.8
        coordinates each) that repeated the route's properties over and over.
        Collapsing a route into a single multi-part geometry keeps every metre
        of road and every popup field while paying the envelope once.
        """
        tags = el.get("tags") or {}
        if _is_excluded_route(tags):
            logger.info("skipping non-road route relation %s (%s)", el.get("id"),
                        tags.get("name") or tags.get("network") or "?")
            return
        parts, claimed = [], []
        for i, m in enumerate(el.get("members") or []):
            if m.get("type") != "way":
                continue
            ref = int(m.get("ref", -i))
            if road_ids is not None and ref not in road_ids:
                continue
            if (kind, f"way/{ref}") in seen:      # already carried by another route
                continue
            line = _line(m.get("geometry"))
            if not line:
                continue
            parts.append(line)
            claimed.append(ref)
        if not parts:
            return
        for ref in claimed:
            seen.add((kind, f"way/{ref}"))
        route_name = tags.get("name") or tags.get("ref") or ""
        features.append({
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": parts},
            "properties": _props(tags, kind, f"relation/{el.get('id')}",
                                 {"route_name": route_name, "name": route_name,
                                  "segments": len(parts)}),
        })
        per_kind[kind] = per_kind.get(kind, 0) + 1

    # Relations FIRST: they carry the route name, and a way reached through a
    # route should be labelled with the route rather than the street it happens
    # to be. Way selectors then contribute only what no route already claimed.
    for kind, cfg in layer_sources().items():
        for entry in cfg["relation_selectors"]:
            sel = entry["selector"]
            rel_q, id_q = _relation_queries(sel, b)
            rel_els, ep = _fetch_one(rel_q, f"{kind}-relations")
            used_endpoint = used_endpoint or ep
            road_ids: set[int] | None = None
            if entry["verify_highway_members"]:
                id_els, _ = _fetch_one(id_q, f"{kind}-relation-road-members")
                road_ids = {int(e["id"]) for e in id_els
                            if e.get("type") == "way" and e.get("id") is not None}
            logger.info("%s: %d relations, road-member gate=%s", kind, len(rel_els),
                        len(road_ids) if road_ids is not None else "off")
            for el in rel_els:
                _keep_route(el, kind, road_ids)

        if cfg["way_selectors"]:
            els, ep = _fetch_one(_way_query(cfg["way_selectors"], b), f"{kind}-ways")
            used_endpoint = used_endpoint or ep
            for el in els:
                for feat in _to_features(el, kind):
                    _keep(feat, kind)

    if not features:
        raise RuntimeError(f"no scenic/historic road features returned for region {region!r}")
    logger.info("scenic/historic roads %s -> %s", region, per_kind)
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


def simplify_tolerance() -> float:
    """Degrees of RDP tolerance; 0 disables. Config, overridable by env."""
    raw = os.environ.get("FW_ROAD_SIMPLIFY_DEG", "").strip()
    if raw:
        return float(raw)
    return float(_config().get("simplify_tolerance_deg", 0.0) or 0.0)


def _line(geometry: list[dict[str, Any]] | None,
          tol: float | None = None) -> list[list[float]] | None:
    if not geometry:
        return None
    coords = [[round(float(p["lon"]), _COORD_PRECISION), round(float(p["lat"]), _COORD_PRECISION)]
              for p in geometry if p.get("lat") is not None and p.get("lon") is not None]
    if len(coords) < 2:
        return None
    return _simplify(coords, simplify_tolerance() if tol is None else tol)


def _simplify(coords: list[list[float]], tol: float) -> list[list[float]]:
    """Ramer-Douglas-Peucker, iterative (a recursive one blows the stack on a
    10,000-node way). Tolerance is in DEGREES: these maps are viewed between
    zoom ~3 and ~9, where a few tens of metres is well under one pixel, and the
    unsimplified continental set is 81.6 MB of coordinates - too big to inline.
    """
    if tol <= 0 or len(coords) < 3:
        return coords
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        (x1, y1), (x2, y2) = coords[i], coords[j]
        dx, dy = x2 - x1, y2 - y1
        den = math.hypot(dx, dy)
        worst, wi = -1.0, -1
        for k in range(i + 1, j):
            x0, y0 = coords[k]
            d = (abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / den) if den else math.hypot(x0 - x1, y0 - y1)
            if d > worst:
                worst, wi = d, k
        if worst > tol:
            keep[wi] = True
            stack.append((i, wi))
            stack.append((wi, j))
    return [c for c, k in zip(coords, keep) if k]


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
    # Drop empties. Most roads carry few of these tags, and at ~115k features an
    # always-present key set cost 27.8 MB of empty strings against 11.2 MB of
    # actual geometry - the properties, not the roads, were the file.
    return {k: v for k, v in props.items() if v not in ("", None)}


def _is_excluded_route(tags: dict[str, Any]) -> bool:
    """True for a route relation that is transit or rail, not a road."""
    haystack = f"{tags.get('operator', '')} {tags.get('network', '')}".lower()
    return any(p in haystack for p in historic_route_exclusions())


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
        if _is_excluded_route(tags):
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
                        "properties": _props(tags, kind, f"way/{m.get('ref', i)}",
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
