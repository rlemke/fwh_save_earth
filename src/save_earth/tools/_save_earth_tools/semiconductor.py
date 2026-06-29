"""Semiconductor fabrication plants — worldwide GeoJSON from OSM, by country.

Country-fan-out source for a world map of semiconductor manufacturing plants
(fabs). Unlike the global ``Download*`` sources, this fetches **one country at a
time** (an Overpass ``area`` query keyed on ``ISO3166-1``) so the workflow can
fan out per country across the runner fleet — the same per-country Overpass
shape osm-mapping uses for its facility counts, but here returning the full
point features.

Three artefacts under ``cache/save-earth/semiconductor/``:

- ``by-country/<ISO2>.geojson`` (+ sidecar) — one country's fabs, cached so a
  re-run never re-queries Overpass for an unchanged country.
- ``fabs.geojson`` — the merged world layer the map renders (LayerSpec
  ``semiconductor-fabs``).

OSM fab tagging is sparse and inconsistent (the honest limitation of an open
crowd-sourced source): we collect ``industrial=semiconductor``,
``industry=semiconductor``, and ``man_made=works`` / ``product`` whose value
mentions semiconductor / integrated-circuit / wafer / microchip. Many real fabs
are simply not tagged this way in OSM, so coverage skews to well-mapped regions.
Every feature keeps **all** of its OSM tags verbatim in ``properties`` so the map
popup can surface everything the dataset knows (operator, name, start_date,
``product``, …), plus derived ``osm_type``/``osm_id``/``osm_url``.

``list_fab_countries`` enumerates countries from the Natural Earth admin-0
dataset (the canonical country set osm-mapping already uses) — not a hardcoded
list — so the fan-out covers every country with an ISO3166-1 code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _save_earth_tools import sidecar  # noqa: E402
from _save_earth_tools.storage import (  # noqa: E402
    Storage,
    get_storage,
    local_staging_subdir,
)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("save-earth.semiconductor")

NAMESPACE = "save-earth"
CACHE_TYPE = "semiconductor"
MERGED_RELATIVE_PATH = "fabs.geojson"
WIKIDATA_RELATIVE_PATH = "wikidata.geojson"

# Wikidata Query Service: every item that is (a subclass of) a semiconductor
# fabrication plant (Q4168959) with coordinates, plus the rich structured fields
# OSM rarely has (operator, country, inception, owner, wikipedia). A single global
# SPARQL gets them all, so this source does NOT fan out (unlike the OSM source) —
# there is no per-area query-size limit to work around. Coverage is sparse and
# skews historical (Wikidata's fab modelling is incomplete), which is exactly why
# we UNION it with the OSM source for fuller coverage rather than replace it.
WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_QUERY = """
SELECT ?item ?itemLabel ?coord ?operatorLabel ?countryLabel ?inception ?ownerLabel ?article WHERE {
  ?item wdt:P31/wdt:P279* wd:Q4168959 ;
        wdt:P625 ?coord .
  OPTIONAL { ?item wdt:P137 ?operator. }
  OPTIONAL { ?item wdt:P17 ?country. }
  OPTIONAL { ?item wdt:P571 ?inception. }
  OPTIONAL { ?item wdt:P127 ?owner. }
  OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Natural Earth admin-0 (same source osm-mapping enumerates) — name + ISO2.
NATURAL_EARTH_COUNTRIES = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson"
)

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 180
PER_COUNTRY_TIMEOUT = 120
# Cache-first: a present per-country cache is ALWAYS reused (no age-based
# auto-refresh) — only force=True re-queries Overpass. The fab fleet changes
# slowly and Overpass is rate-limited, so we never re-fetch on our own.
DEFAULT_MAX_AGE_HOURS = float("inf")
MAX_ATTEMPTS = 3

# Per-country Overpass query: every plausible semiconductor-fab tagging, polygon
# centroids included (`out center`). {iso2} is substituted per country.
_QUERY_TMPL = (
    "[out:json][timeout:{t}];"
    'area["ISO3166-1"="{iso2}"][admin_level=2]->.a;'
    "("
    'nwr["industrial"="semiconductor"](area.a);'
    'nwr["industry"="semiconductor"](area.a);'
    'nwr["man_made"="works"]["product"~"semiconductor|integrated.circuit|microchip|wafer|chip",i](area.a);'
    'nwr["product"~"semiconductor|integrated.circuit|wafer",i](area.a);'
    ");"
    "out center tags;"
)


@dataclass
class CountryResult:
    iso2: str
    name: str
    relative_path: str
    feature_count: int
    was_cached: bool


@dataclass
class MergeResult:
    relative_path: str
    feature_count: int
    country_count: int


# ---------------------------------------------------------------------------
# Country enumeration (Natural Earth admin-0).
# ---------------------------------------------------------------------------


def list_fab_countries() -> list[dict[str, str]]:
    """Return ``[{"iso2": .., "name": ..}, ...]`` for every admin-0 country.

    Read from Natural Earth (cached on disk by the platform's HTTP layer); not a
    hardcoded list. Countries without an ISO3166-1 alpha-2 are skipped (can't be
    area-queried)."""
    if requests is None:
        raise RuntimeError("requests is required to enumerate countries")
    resp = requests.get(
        NATURAL_EARTH_COUNTRIES,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    out: list[dict[str, str]] = []
    for feat in resp.json().get("features", []):
        props = feat.get("properties") or {}
        iso2 = None
        for k in ("ISO_A2_EH", "ISO_A2", "WB_A2"):
            v = props.get(k)
            if v and v not in ("-99", "-1"):
                iso2 = v
                break
        name = props.get("NAME") or props.get("NAME_LONG") or iso2
        if iso2 and name:
            out.append({"iso2": iso2, "name": name})
    out.sort(key=lambda c: c["name"])
    logger.info("enumerated %d fab-candidate countries", len(out))
    return out


# ---------------------------------------------------------------------------
# Per-country Overpass fetch (the fan-out leaf).
# ---------------------------------------------------------------------------


def download_fabs_for_country(
    iso2: str,
    name: str = "",
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
) -> CountryResult:
    """Fetch one country's semiconductor fabs from Overpass → cached GeoJSON.

    Tolerant: an empty country (no fabs / no OSM coverage) caches an empty
    FeatureCollection so it is not re-queried. Network/Overpass failure after
    retries raises (the workflow tolerates per-country failures)."""
    s = storage or get_storage()
    iso2 = iso2.upper()
    rel = f"by-country/{iso2}.geojson"

    if not force:
        side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
        if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, rel, s):
            age = _age_hours(side.get("generated_at"))
            if age is None or age < max_age_hours:
                return CountryResult(
                    iso2=iso2,
                    name=name,
                    relative_path=rel,
                    feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                    was_cached=True,
                )

    features = _fetch_country(iso2)
    body = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    ).encode("utf-8")
    _persist(
        rel,
        body,
        s,
        source_url=OVERPASS_ENDPOINTS[0],
        extra={"feature_count": len(features), "iso2": iso2, "country": name},
    )
    logger.info("%s (%s): %d fab feature(s)", iso2, name, len(features))
    return CountryResult(
        iso2=iso2, name=name, relative_path=rel, feature_count=len(features), was_cached=False
    )


def _fetch_country(iso2: str) -> list[dict[str, Any]]:
    """POST the per-country query, mirror fallback + 429 backoff."""
    if requests is None:
        raise RuntimeError("requests is required to query Overpass")
    query = _QUERY_TMPL.format(t=PER_COUNTRY_TIMEOUT, iso2=iso2)
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        for ep in OVERPASS_ENDPOINTS:
            try:
                resp = requests.post(
                    ep,
                    data={"data": query},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                if resp.status_code == 429:  # rate-limited — back off, try next
                    time.sleep(5 + attempt * 5)
                    last_exc = RuntimeError("Overpass 429")
                    continue
                resp.raise_for_status()
                elements = resp.json().get("elements")
                if not isinstance(elements, list):
                    last_exc = RuntimeError("unexpected Overpass shape")
                    continue
                feats = [f for f in (_to_feature(el) for el in elements) if f]
                return feats
            except Exception as exc:  # noqa: BLE001 — next mirror/attempt
                last_exc = exc
                logger.debug("overpass %s %s failed: %s", iso2, ep, exc)
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"Overpass failed for {iso2} after {MAX_ATTEMPTS} attempts: {last_exc}")


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """Overpass element → GeoJSON Point Feature keeping ALL tags, or None."""
    tags = dict(el.get("tags") or {})
    if not tags:
        return None
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
    props = dict(tags)  # every OSM tag verbatim → popup shows it all
    props["osm_type"] = osm_type
    props["osm_id"] = osm_id
    if osm_id is not None:
        props["osm_url"] = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


# ---------------------------------------------------------------------------
# Wikidata source (single global SPARQL — fuller, structured, no fan-out).
# ---------------------------------------------------------------------------


def download_fabs_wikidata(*, force: bool = False, storage: Storage | None = None) -> CountryResult:
    """Fetch every geocoded semiconductor fab from Wikidata → cached GeoJSON.

    Cache-first like the OSM source: a present cache is reused unless force."""
    s = storage or get_storage()
    rel = WIKIDATA_RELATIVE_PATH
    if not force and sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s):
        if sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, rel, s):
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
            return CountryResult(
                iso2="",
                name="wikidata",
                relative_path=rel,
                feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                was_cached=True,
            )
    if requests is None:
        raise RuntimeError("requests is required to query Wikidata")
    resp = requests.post(
        WIKIDATA_ENDPOINT,
        data={"query": WIKIDATA_QUERY, "format": "json"},
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    resp.raise_for_status()
    features = _wikidata_to_features(resp.json().get("results", {}).get("bindings", []))
    body = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    ).encode("utf-8")
    _persist(rel, body, s, source_url=WIKIDATA_ENDPOINT, extra={"feature_count": len(features)})
    logger.info("wikidata: %d fab feature(s)", len(features))
    return CountryResult(
        iso2="", name="wikidata", relative_path=rel, feature_count=len(features), was_cached=False
    )


def _wikidata_to_features(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SPARQL bindings → GeoJSON Points with structured popup fields.

    One row per (item, optional-value) tuple, so an item with two operators
    yields two rows; de-dupe by item URI (keep the first), so each fab is one
    point."""
    feats: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for b in bindings:
        coord = (b.get("coord") or {}).get("value", "")  # "Point(lon lat)"
        if not coord.startswith("Point("):
            continue
        try:
            lon, lat = (float(x) for x in coord[6:-1].split())
        except (ValueError, IndexError):
            continue
        item = (b.get("item") or {}).get("value", "")  # full entity URI
        if item and item in seen_items:
            continue
        if item:
            seen_items.add(item)
        props: dict[str, Any] = {"source": "wikidata"}
        for fld, key in (
            ("itemLabel", "name"),
            ("operatorLabel", "operator"),
            ("countryLabel", "country"),
            ("inception", "inception"),
            ("ownerLabel", "owner"),
            ("article", "wikipedia"),
        ):
            v = (b.get(fld) or {}).get("value")
            if v:
                props[key] = v
        if item:
            props["wikidata_url"] = item
            props["wikidata_id"] = item.rsplit("/", 1)[-1]
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return feats


# ---------------------------------------------------------------------------
# Merge per-country GeoJSON (+ optional Wikidata) → the rendered world layer.
# ---------------------------------------------------------------------------


def _spatial_key(feat: dict[str, Any]) -> tuple[float, float]:
    """~1km bucket (coords rounded to 2dp) for cross-source de-dup of one fab
    tagged in both OSM and Wikidata at slightly different coordinates."""
    lon, lat = feat["geometry"]["coordinates"]
    return (round(lat, 2), round(lon, 2))


def merge_fabs(
    parts: list[str], wikidata_path: str = "", *, storage: Storage | None = None
) -> MergeResult:
    """Merge per-country OSM FeatureCollections (+ optional Wikidata) into the
    rendered ``fabs.geojson`` world layer.

    Each OSM feature is tagged ``source="osm"``; Wikidata features carry
    ``source="wikidata"`` already. De-dup within OSM by (osm_type, osm_id), then
    UNION with Wikidata and drop cross-source duplicates by ~1km spatial bucket,
    preferring the Wikidata feature (richer structured attributes) on a collision.
    ``country_count`` counts OSM countries contributing features."""
    s = storage or get_storage()
    osm_seen: set[tuple[Any, Any]] = set()
    countries = 0
    osm_feats: list[dict[str, Any]] = []
    for rel in parts:
        try:
            raw = s.read_text(sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s))
        except Exception as exc:  # noqa: BLE001 — tolerate a missing/failed leaf
            logger.warning("merge: skipping %s (%s)", rel, exc)
            continue
        country_feats = json.loads(raw).get("features") or []
        if country_feats:
            countries += 1
        for f in country_feats:
            p = f.get("properties") or {}
            key = (p.get("osm_type"), p.get("osm_id"))
            if key in osm_seen:
                continue
            osm_seen.add(key)
            p.setdefault("source", "osm")
            osm_feats.append(f)

    wd_feats: list[dict[str, Any]] = []
    if wikidata_path:
        try:
            raw = s.read_text(sidecar.cache_path(NAMESPACE, CACHE_TYPE, wikidata_path, s))
            wd_feats = json.loads(raw).get("features") or []
        except Exception as exc:  # noqa: BLE001 — Wikidata optional, tolerate
            logger.warning("merge: skipping wikidata %s (%s)", wikidata_path, exc)

    # Wikidata first (preferred on a spatial collision), then OSM fills the gaps.
    by_bucket: dict[tuple[float, float], dict[str, Any]] = {}
    for f in wd_feats:
        by_bucket[_spatial_key(f)] = f
    osm_kept = 0
    for f in osm_feats:
        k = _spatial_key(f)
        if k not in by_bucket:
            by_bucket[k] = f
            osm_kept += 1
    features = list(by_bucket.values())

    body = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    ).encode("utf-8")
    _persist(
        MERGED_RELATIVE_PATH,
        body,
        s,
        source_url=OVERPASS_ENDPOINTS[0],
        extra={
            "feature_count": len(features),
            "country_count": countries,
            "osm_count": osm_kept,
            "wikidata_count": len(wd_feats),
        },
    )
    logger.info(
        "merged %d fabs (%d wikidata + %d osm, %d osm countries)",
        len(features),
        len(wd_feats),
        osm_kept,
        countries,
    )
    return MergeResult(MERGED_RELATIVE_PATH, len(features), countries)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _persist(rel: str, body: bytes, s: Storage, *, source_url: str, extra: dict[str, Any]) -> None:
    """Stage locally, finalize into the object store, then write the sidecar
    (artifact-first / sidecar-second, the order readers depend on)."""
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{rel.replace('/', '_')}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)
    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, rel, storage=s):
        s.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            rel,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "publisher": "OpenStreetMap contributors - Overpass API",
                "url": source_url,
                "license": "ODbL 1.0",
            },
            tool={"name": "semiconductor", "version": "1.0"},
            extra=extra,
            storage=s,
        )


def _age_hours(generated_at: Any) -> float | None:
    from datetime import UTC, datetime

    if not generated_at:
        return None
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        return (datetime.now(UTC) - ts).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return None
