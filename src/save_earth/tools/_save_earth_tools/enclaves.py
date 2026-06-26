"""Ethnic / cultural enclave neighbourhoods — worldwide GeoJSON from OSM.

Queries the OpenStreetMap Overpass API for named *places* (``place=
neighbourhood`` / ``quarter`` / ``suburb`` / ``city_block`` / ``locality``)
whose **name** marks them as a heritage enclave — "Chinatown", "Japantown",
"Little Italy", "Koreatown", "Little Saigon", "Greektown", "Little Havana", …
OSM has no structured ethnicity/heritage attribute (demographics are not
on-the-ground verifiable, so the project deliberately avoids them); the *name*
is the signal, and many enclaves also carry a ``wikidata``/``wikipedia`` link to
the encyclopedic article that does describe the heritage.

Each matching element is classified into a **heritage** bucket from its name and
written to **one GeoJSON FeatureCollection per heritage** under::

    cache/save-earth/enclaves/<slug>.geojson + .meta.json   (e.g. chinese.geojson)

so the map renderer turns each heritage into its own coloured, toggleable layer
with a legend count. Every OSM tag is kept verbatim as the feature's
``properties`` (so the popup shows everything OSM knows), plus derived
``heritage`` / ``osm_type`` / ``osm_id`` / ``osm_url`` fields. Coverage is
OSM-community-driven and uneven — rich in US/European metros, sparse elsewhere,
and name-matching admits the odd false positive (e.g. a historic "Germantown").
Pass ``use_mock=True`` for a small offline set when Overpass is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
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

logger = logging.getLogger("save-earth.enclaves")

NAMESPACE = "save-earth"
CACHE_TYPE = "enclaves"

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
)
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 30  # enclave place names change very slowly


@dataclass(frozen=True)
class Heritage:
    slug: str          # cache file + layer id ("chinese" → chinese.geojson, layer "enclave-chinese")
    label: str         # legend/popup label ("Chinese (Chinatown)")
    color: str         # CSS colour for the layer
    pattern: str       # case-insensitive name regex that marks this heritage


# Order matters — first match wins (more specific patterns earlier). Colours are
# distinct and roughly culturally evocative; presence-filtering drops any heritage
# with no features in the current OSM extract, so this list can be generous.
HERITAGES: tuple[Heritage, ...] = (
    Heritage("chinese", "Chinese (Chinatown)", "#d32f2f", r"china ?town"),
    Heritage("japanese", "Japanese (Japantown / Little Tokyo)", "#ec407a",
             r"japan ?town|little tokyo|nihonmachi|\bj-town\b"),
    Heritage("korean", "Korean (Koreatown)", "#5c6bc0",
             r"korea ?town|\bk-?town\b"),  # \b so "Yorktown"/"Blacktown"/"Cooktown" don't match
    Heritage("vietnamese", "Vietnamese (Little Saigon)", "#00897b",
             r"little saigon|little vietnam|vietnam ?town"),
    Heritage("thai", "Thai (Thaitown)", "#f9a825", r"thai ?town"),
    Heritage("filipino", "Filipino (Little Manila)", "#1565c0",
             r"little manila|little quiapo|manila ?town"),
    Heritage("south-asian", "South Asian (Little India)", "#f57c00",
             r"little india|punjabi market|little bangladesh|little pakistan|little lahore"),
    Heritage("italian", "Italian (Little Italy)", "#2e7d32",
             r"little italy|italian quarter|petite italie"),
    Heritage("irish", "Irish (Corktown / Little Ireland)", "#7cb342",
             r"irishtown|little ireland|corktown|irish quarter"),
    Heritage("jewish", "Jewish quarter", "#283593",
             r"jewish quarter|little jerusalem|judengasse|juder[íi]a|jewishtown"),
    Heritage("greek", "Greek (Greektown)", "#0277bd", r"greek ?town|little greece"),
    Heritage("portuguese", "Portuguese (Little Portugal)", "#6a1b9a",
             r"little portugal|little lisbon|portuguese quarter"),
    Heritage("german", "German (Germantown / Little Germany)", "#455a64",
             r"germantown|little germany|kleindeutschland|german ?town"),
    Heritage("latino", "Mexican / Latino", "#c2185b",
             r"little mexico|mexican quarter|barrio latino|little colombia"),
    Heritage("cuban", "Cuban (Little Havana)", "#00acc1", r"little havana|little cuba"),
    Heritage("ethiopian", "Ethiopian / East African", "#795548",
             r"little ethiopia|little addis|little somalia|little mogadishu"),
    Heritage("armenian", "Armenian (Little Armenia)", "#d84315", r"little armenia"),
    Heritage("arab", "Arab / Maghrebi", "#558b2f",
             r"little arabia|little maghreb|arab quarter"),
    Heritage("persian", "Persian (Tehrangeles)", "#8e24aa",
             r"little tehran|tehrangeles|little persia|persian square"),
    Heritage("polish", "Polish (Little Poland)", "#c62828",
             r"little poland|polish ?town|jackowo"),
    Heritage("eastern-european", "Eastern European / Slavic", "#6d4c41",
             r"little odessa|little russia|little ukraine|little serbia"),
    Heritage("caribbean", "Caribbean", "#43a047",
             r"little jamaica|little caribbean|little haiti"),
    Heritage("brazilian", "Brazilian", "#9e9d24", r"little brazil|little brasil"),
    Heritage("scandinavian", "Scandinavian / Nordic", "#1e88e5",
             r"little sweden|little norway|little denmark|little finland|swedetown|finntown|swede ?town"),
)

# Overpass: named places that look like enclaves. The name regex is the union of
# every heritage pattern, so one query covers them all; classification happens in
# Python. nw (nodes + ways) with `out center` so every feature is a Point; skip
# relations (rare for these, and they make the global name-scan much heavier).
_NAME_RE = "|".join(h.pattern for h in HERITAGES)
OVERPASS_QUERY = (
    "[out:json][timeout:240];"
    'nw["place"~"^(neighbourhood|quarter|suburb|city_block|locality)$"]'
    f'["name"~"{_NAME_RE}",i];'
    "out center tags;"
)

_lock = threading.Lock()
_compiled = [(re.compile(h.pattern, re.I), h) for h in HERITAGES]


def _classify(name: str) -> Heritage | None:
    for rx, h in _compiled:
        if rx.search(name):
            return h
    return None


@dataclass
class DownloadResult:
    cache_type: str
    feature_count: int
    heritage_count: int
    per_heritage: dict[str, int]
    relative_paths: list[str]
    was_cached: bool
    source_url: str
    used_mock: bool = False
    files: list[str] = field(default_factory=list)


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
    """Fetch worldwide heritage-enclave places and cache one GeoJSON per heritage."""
    s = storage or get_storage()

    with _lock:
        # Cache hit: keyed on a manifest sidecar that records the heritages written.
        if not force:
            manifest = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, "_manifest.json", s)
            if manifest and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, "_manifest.json", s):
                age = _age_hours(manifest.get("generated_at"))
                if age is None or age < max_age_hours:
                    extra = manifest.get("extra") or {}
                    per = extra.get("per_heritage") or {}
                    logger.info("enclaves cache hit (%.1fh old)", age or -1.0)
                    return DownloadResult(
                        cache_type=CACHE_TYPE,
                        feature_count=int(extra.get("feature_count", sum(per.values()))),
                        heritage_count=len(per),
                        per_heritage=per,
                        relative_paths=[f"{slug}.geojson" for slug in per],
                        was_cached=True,
                        source_url=OVERPASS_ENDPOINTS[0],
                    )

        if use_mock:
            buckets = _mock_buckets()
            used_mock = True
            source_url = "mock://enclaves"
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via the "
                    ".sh wrapper (activates .venv), or pass --use-mock."
                )
            buckets, source_url = _fetch_overpass()
            used_mock = False

        return _persist(buckets, s, source_url=source_url, used_mock=used_mock)


# ---------------------------------------------------------------------------
# Overpass fetch + classification.
# ---------------------------------------------------------------------------


def _fetch_overpass() -> tuple[dict[str, list[dict[str, Any]]], str]:
    """POST the Overpass query, trying each mirror, and bucket features by heritage."""
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

        buckets: dict[str, list[dict[str, Any]]] = {}
        for el in elements:
            feat = _to_feature(el)
            if feat is not None:
                buckets.setdefault(feat["properties"]["heritage_slug"], []).append(feat)
        logger.info(
            "Overpass %s → %d enclave features across %d heritages",
            endpoint, sum(len(v) for v in buckets.values()), len(buckets),
        )
        return buckets, endpoint

    raise RuntimeError(f"all Overpass mirrors failed; last error: {last_exc}")


def _to_feature(el: dict[str, Any]) -> dict[str, Any] | None:
    """One Overpass element → a GeoJSON Point Feature classified by heritage,
    or ``None`` if it has no coordinate or no recognised heritage name."""
    tags = el.get("tags") or {}
    name = tags.get("name") or ""
    heritage = _classify(name)
    if heritage is None:
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
    props: dict[str, Any] = dict(tags)  # every OSM tag verbatim for the popup
    props["heritage"] = heritage.label
    props["heritage_slug"] = heritage.slug
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
# Cache write — one GeoJSON FeatureCollection per heritage + a manifest sidecar.
# ---------------------------------------------------------------------------


def _persist(
    buckets: dict[str, list[dict[str, Any]]],
    storage: Storage,
    *,
    source_url: str,
    used_mock: bool,
) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)

    per_heritage: dict[str, int] = {}
    rel_paths: list[str] = []
    files: list[str] = []
    # Stable order = HERITAGES order, so layer draw order / legend is deterministic.
    for h in HERITAGES:
        feats = buckets.get(h.slug)
        if not feats:
            continue
        rel = f"{h.slug}.geojson"
        body = json.dumps(
            {"type": "FeatureCollection", "features": feats}, separators=(",", ":")
        ).encode("utf-8")
        stage_path = os.path.join(staging, f"{h.slug}.stage-{os.getpid()}")
        with open(stage_path, "wb") as f:
            f.write(body)
        final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, storage)
        with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, rel, storage=storage):
            storage.finalize_from_local(stage_path, final_path)
            sidecar.write_sidecar(
                NAMESPACE, CACHE_TYPE, rel,
                kind="file",
                size_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                source={
                    "publisher": "OpenStreetMap contributors — Overpass API",
                    "url": source_url,
                    "license": "ODbL 1.0",
                    "used_mock": used_mock,
                },
                tool={"name": "enclaves", "version": "1.0"},
                extra={"heritage": h.label, "feature_count": len(feats)},
                storage=storage,
            )
        per_heritage[h.slug] = len(feats)
        rel_paths.append(rel)
        files.append(final_path)

    total = sum(per_heritage.values())
    # Manifest sidecar (no body — a 1-byte marker) records what was written so a
    # later run can report a cache hit without re-listing the object store.
    marker = b"\n"
    stage_m = os.path.join(staging, f"_manifest.stage-{os.getpid()}")
    with open(stage_m, "wb") as f:
        f.write(marker)
    man_final = sidecar.cache_path(NAMESPACE, CACHE_TYPE, "_manifest.json", storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, "_manifest.json", storage=storage):
        storage.finalize_from_local(stage_m, man_final)
        sidecar.write_sidecar(
            NAMESPACE, CACHE_TYPE, "_manifest.json",
            kind="file",
            size_bytes=len(marker),
            sha256=hashlib.sha256(marker).hexdigest(),
            source={"publisher": "OpenStreetMap contributors — Overpass API",
                    "url": source_url, "license": "ODbL 1.0", "used_mock": used_mock},
            tool={"name": "enclaves", "version": "1.0"},
            extra={"feature_count": total, "per_heritage": per_heritage},
            storage=storage,
        )

    return DownloadResult(
        cache_type=CACHE_TYPE,
        feature_count=total,
        heritage_count=len(per_heritage),
        per_heritage=per_heritage,
        relative_paths=rel_paths,
        was_cached=False,
        source_url=source_url,
        used_mock=used_mock,
        files=files,
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


def _mock_buckets() -> dict[str, list[dict[str, Any]]]:
    """Small offline set — famous enclaves with rich tags so the map renders
    without Overpass. One or two per heritage, enough to exercise every layer
    path that has a mock entry."""
    rows = [
        ("Chinatown", "neighbourhood", 40.7158, -73.9970, "San Francisco/NYC", "Q204184"),
        ("Chinatown", "neighbourhood", 51.5126, -0.1316, "London", "Q1063184"),
        ("Japantown", "neighbourhood", 37.7849, -122.4294, "San Francisco", "Q2734517"),
        ("Little Tokyo", "neighbourhood", 34.0500, -118.2400, "Los Angeles", "Q1632791"),
        ("Koreatown", "neighbourhood", 34.0578, -118.3009, "Los Angeles", "Q1786381"),
        ("Little Italy", "neighbourhood", 40.7191, -73.9973, "New York", "Q1064212"),
        ("Little India", "neighbourhood", 1.3066, 103.8518, "Singapore", "Q1051142"),
        ("Little Saigon", "neighbourhood", 33.7487, -117.9870, "Westminster CA", "Q1772140"),
        ("Greektown", "neighbourhood", 42.3390, -83.0440, "Detroit", "Q5604171"),
        ("Little Havana", "neighbourhood", 25.7656, -80.2197, "Miami", "Q1361433"),
        ("Thaitown", "neighbourhood", 34.1016, -118.3090, "Los Angeles", "Q2433300"),
        ("Little Portugal", "neighbourhood", 43.6490, -79.4230, "Toronto", "Q3257686"),
    ]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for i, (name, place, lat, lon, where, wd) in enumerate(rows):
        h = _classify(name)
        if h is None:
            continue
        props = {
            "name": name, "place": place, "is_in": where, "wikidata": wd,
            "heritage": h.label, "heritage_slug": h.slug,
            "osm_type": "node", "osm_id": 900000 + i,
            "osm_url": f"https://www.openstreetmap.org/node/{900000 + i}",
        }
        buckets.setdefault(h.slug, []).append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    return buckets
