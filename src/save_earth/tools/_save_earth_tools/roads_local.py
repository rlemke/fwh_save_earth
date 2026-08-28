"""Local-PBF backend for the scenic/historic roads map - no Overpass.

Overpass is a query API over OSM data; it is not a different dataset. We already
host that data: the self-hosted planet split serves every continent as
``<region>-latest.osm.pbf`` (see fwh_osm's replication publishing), refreshed
nightly. What Overpass adds is a TAG INDEX, which is why a selective query
answers in seconds there while a bare extract has to be scanned.

For this map that trade is worth reversing, measured on oceania (1.6 GB):

    download from our own tree   43 s
    osmium tags-filter           6 s   (1.6 GB -> 33 MB, 765% CPU)
    pyosmium extract             3 s
                                ----
                                ~53 s   vs 70-157 s per region via Overpass

and the local path has no rate limit, no 429, and no dependency on three
third-party mirrors that were ALL failing when this was written (main 504,
kumi.systems 502, private.coffee connection refused). It is also reproducible:
the extract carries an ``osmosis_replication_timestamp``, so a rebuild can state
the vintage of its input, which an Overpass answer cannot.

⚠️ Scanning is only cheap because ``osmium tags-filter`` runs FIRST and cuts the
continent to a few tens of MB. Do not point the extractor at a raw continent.

The output is the same schema the Overpass path produces, so the renderer, the
dedupe rules and the map are unchanged - only where the bytes come from differs.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("save-earth.roads_local")

# Region key -> the extract's basename in the self-hosted tree. Kept as a
# mapping, not a format string, because the tree's names need not match our
# region keys (australia-oceania here is "oceania" there).
DEFAULT_REGION_FILES = {
    "north-america": "north-america",
    "united-states": "north-america",
    "europe": "europe",
    "south-america": "south-america",
    "africa": "africa",
    "asia": "asia",
    "australia-oceania": "oceania",
}

_HISTORIC_WAY_VALUES = {"road", "hollow_way", "roman_road", "ancient_road"}


def _compiled(selectors: dict[str, Any]) -> dict[str, Any]:
    """Turn the JSON selector config into the regexes this backend applies.

    The Overpass selectors are strings like
    ``relation["route"="road"]["network"~"scenic|byway",i]``. Rather than parse
    that (a partial Overpass QL parser is a liability), the regex bodies are
    lifted out with a narrow pattern and applied directly.
    """
    out: dict[str, dict[str, re.Pattern | None]] = {}
    for kind, cfg in selectors.items():
        nets, names = [], []
        for entry in cfg.get("relation_selectors", []):
            sel = entry["selector"] if isinstance(entry, dict) else entry
            m = re.search(r'\["network"~"([^"]+)"', sel)
            if m:
                nets.append(m.group(1))
            m = re.search(r'\["name"~"([^"]+)"', sel)
            if m:
                names.append(m.group(1))
        out[kind] = {
            "network": re.compile("|".join(nets), re.I) if nets else None,
            "name": re.compile("|".join(names), re.I) if names else None,
        }
    return out


def extract(pbf_path: str, selectors: dict[str, Any], *,
            exclusions: tuple[str, ...] = (),
            simplify=None) -> list[dict[str, Any]]:
    """Build the road features from a PRE-FILTERED extract.

    Two passes, because a PBF stores ways before relations: pass A learns which
    relations match and which ways they claim, pass B reads geometry for the
    ways that either match a tag rule or are claimed by a route.
    """
    import osmium

    pat = _compiled(selectors)
    scenic, historic = pat.get("scenic", {}), pat.get("historic", {})

    def _route_kind(tags: dict[str, str]) -> tuple[str, bool] | None:
        route = tags.get("route")
        if route == "historic":
            return "historic", True           # mixes in rail/transit: gate members
        if route != "road":
            return None
        net, nm = tags.get("network", ""), tags.get("name", "")
        for kind, p in (("scenic", scenic), ("historic", historic)):
            if (p.get("network") and p["network"].search(net)) or \
               (p.get("name") and p["name"].search(nm)):
                return kind, False            # route=road already means road
        return None

    def _excluded(tags: dict[str, str]) -> bool:
        hay = f"{tags.get('operator', '')} {tags.get('network', '')}".lower()
        return any(p in hay for p in exclusions)

    class _Rels(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.rels: dict[int, tuple] = {}

        def relation(self, r):
            tags = dict(r.tags)
            kind = _route_kind(tags)
            if not kind or _excluded(tags):
                return
            members = [m.ref for m in r.members if m.type == "w"]
            if members:
                self.rels[r.id] = (kind[0], kind[1],
                                   tags.get("name") or tags.get("ref") or "", members, tags)

    rels = _Rels()
    rels.apply_file(pbf_path)
    claimed: set[int] = set()
    for v in rels.rels.values():
        claimed.update(v[3])
    logger.info("local: %d matching route relations, %d member ways", len(rels.rels), len(claimed))

    class _Ways(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.geom: dict[int, list] = {}
            self.tags: dict[int, dict] = {}
            self.own: dict[int, str] = {}

        def way(self, w):
            tags = dict(w.tags)
            hw = "highway" in tags
            own = None
            if tags.get("scenic") == "yes" and hw:
                own = "scenic"
            elif (tags.get("historic") == "yes" and hw) or tags.get("historic") in _HISTORIC_WAY_VALUES:
                own = "historic"
            if own is None and w.id not in claimed:
                return
            try:
                coords = [[round(n.lon, 5), round(n.lat, 5)]
                          for n in w.nodes if n.location.valid()]
            except Exception:  # noqa: BLE001 - a way with unresolved nodes is skipped
                return
            if len(coords) < 2:
                return
            self.geom[w.id] = coords
            self.tags[w.id] = tags
            if own:
                self.own[w.id] = own

    ways = _Ways()
    # locations=True resolves node coordinates; the filtered extract carries the
    # referenced nodes because osmium tags-filter keeps them by default.
    ways.apply_file(pbf_path, locations=True)

    simp = simplify or (lambda c: c)
    feats: list[dict[str, Any]] = []
    # Dedupe PER KIND, not globally. A road can be both scenic and historic and
    # belongs in BOTH layers; a single shared set let whichever kind happened to
    # be visited first claim the way, which silently cost ~8% of the road-km
    # against the Overpass baseline.
    seen: dict[str, set[int]] = {}

    # Routes first: only they carry the route name.
    for rid, (kind, gate, route_name, members, tags) in rels.rels.items():
        claimed_k = seen.setdefault(kind, set())
        parts = []
        for wid in members:
            if wid in claimed_k or wid not in ways.geom:
                continue
            if gate and "highway" not in ways.tags[wid]:
                continue
            parts.append(simp(ways.geom[wid]))
            claimed_k.add(wid)
        if not parts:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": parts},
            "properties": _props(tags, kind, f"relation/{rid}", route_name, len(parts)),
        })

    for wid, kind in ways.own.items():
        claimed_k = seen.setdefault(kind, set())
        if wid in claimed_k:
            continue
        claimed_k.add(wid)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": simp(ways.geom[wid])},
            "properties": _props(ways.tags[wid], kind, f"way/{wid}", "", 1),
        })
    return feats


def _props(tags: dict[str, str], kind: str, osm_id: str,
           route_name: str, segments: int) -> dict[str, Any]:
    p = {
        "osm_id": osm_id,
        "road_kind": kind,
        "name": route_name or tags.get("name") or tags.get("ref") or "",
        "route_name": route_name,
        "segments": segments,
        "highway": tags.get("highway", ""),
        "historic": tags.get("historic", ""),
        "scenic": tags.get("scenic", ""),
        "surface": tags.get("surface", ""),
        "operator": tags.get("operator", ""),
        "network": tags.get("network", ""),
        "wikipedia": tags.get("wikipedia", ""),
    }
    return {k: v for k, v in p.items() if v not in ("", None)}
