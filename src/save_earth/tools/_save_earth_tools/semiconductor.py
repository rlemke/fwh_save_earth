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
# Merge per-country GeoJSON → the single rendered world layer.
# ---------------------------------------------------------------------------


def merge_fabs(parts: list[str], *, storage: Storage | None = None) -> MergeResult:
    """Concatenate per-country FeatureCollections into ``fabs.geojson``.

    ``parts`` are the per-country relative paths (``by-country/<ISO2>.geojson``)
    accumulated by the fan-out. De-dupes by (osm_type, osm_id)."""
    s = storage or get_storage()
    seen: set[tuple[Any, Any]] = set()
    countries = 0
    features: list[dict[str, Any]] = []
    for rel in parts:
        try:
            raw = s.read_text(sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel, s))
        except Exception as exc:  # noqa: BLE001 — tolerate a missing/failed leaf
            logger.warning("merge: skipping %s (%s)", rel, exc)
            continue
        fc = json.loads(raw)
        country_feats = fc.get("features") or []
        if country_feats:
            countries += 1
        for f in country_feats:
            p = f.get("properties") or {}
            key = (p.get("osm_type"), p.get("osm_id"))
            if key in seen:
                continue
            seen.add(key)
            features.append(f)
    body = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    ).encode("utf-8")
    _persist(
        MERGED_RELATIVE_PATH,
        body,
        s,
        source_url=OVERPASS_ENDPOINTS[0],
        extra={"feature_count": len(features), "country_count": countries},
    )
    logger.info("merged %d fab feature(s) across %d country(ies)", len(features), countries)
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
