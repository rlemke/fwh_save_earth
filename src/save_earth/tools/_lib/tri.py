"""EPA Toxic Release Inventory — facility-location GeoJSON.

Fetches the TRI_FACILITY table from EPA's Envirofacts data warehouse
(``data.epa.gov/efservice``) and caches it as a single GeoJSON
FeatureCollection under::

    cache/save-earth/tri/facilities.geojson + .meta.json

Each feature is a point at the facility's reported lat/lon plus the
TRI identifier, facility name, parent company, industry-adjacent
fields, and a ``closed`` boolean.

Three gotchas baked into the downloader:

1. The old ``enviro.epa.gov/enviro/efservice`` URL 301-redirects to
   ``data.epa.gov/efservice``. We just target the new host directly.

2. ``pref_longitude`` in the DB is unsigned — the value is the
   *absolute* distance from Greenwich, so a Puerto Rico facility at
   67.185° W comes back as ``67.185``. We negate for western-
   hemisphere state codes on ingest so the output follows the GeoJSON
   convention (positive = east). The handful of US territories that
   are actually east of Greenwich (Guam, Northern Marianas, American
   Samoa, Micronesia, Palau, Marshalls) stay positive.

3. The table has ~65 k records total, paginated at 10 k rows per
   request. We walk until a page returns less than ``PAGE_SIZE``.

``fac_closed_ind`` distinguishes active from closed facilities —
``"1"`` means closed. Default is active-only; pass
``active_only=False`` to include closures.
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
from typing import Any

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

logger = logging.getLogger("save-earth.tri")

NAMESPACE = "save-earth"
CACHE_TYPE = "tri"
RELATIVE_PATH = "facilities.geojson"

BASE_URL = "https://data.epa.gov/efservice/TRI_FACILITY"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 7  # TRI data refreshes annually-ish
PAGE_SIZE = 10_000  # Envirofacts max per request
MAX_PAGES = 20  # safety ceiling — full TRI is ~7 pages

# US/territory state codes whose facilities are physically east of
# Greenwich, so their stored longitude is already a valid positive
# (east) value. Everything else is assumed western-hemisphere and
# gets the longitude negated.
_EASTERN_HEMISPHERE_US = {
    "GU",  # Guam
    "MP",  # Northern Mariana Islands
    "AS",  # American Samoa
    "FM",  # Micronesia
    "PW",  # Palau
    "MH",  # Marshall Islands
}

# Properties we keep per facility. Everything else gets dropped on
# ingest so the cached GeoJSON stays small.
_KEPT_FIELDS = (
    "tri_facility_id",
    "facility_name",
    "street_address",
    "city_name",
    "county_name",
    "state_abbr",
    "zip_code",
    "region",
    "parent_co_name",
    "standardized_parent_company",
    "frs_id",
    "epa_registry_id",
    "asgn_public_contact",
    "asgn_public_phone",
    "asgn_public_contact_email",
)

_lock = threading.Lock()


@dataclass
class DownloadResult:
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    active_only: bool
    was_cached: bool
    source_url: str
    used_mock: bool = False


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def download(
    *,
    active_only: bool = True,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch the full TRI_FACILITY table and cache it as GeoJSON.

    Sidecar's ``extra`` records whether the active-only filter was
    applied, so a later run that flips the flag invalidates the
    cache appropriately (the sidecar's flag won't match).
    """
    s = storage or LocalStorage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, s):
                cached_active = bool((side.get("extra") or {}).get("active_only", False))
                if cached_active == active_only:
                    age = _age_hours(side.get("generated_at"))
                    if age is None or age < max_age_hours:
                        logger.info(
                            "tri cache hit (%.1fh old, active_only=%s)",
                            age or -1.0,
                            active_only,
                        )
                        return DownloadResult(
                            absolute_path=art_path,
                            relative_path=RELATIVE_PATH,
                            size_bytes=side.get("size_bytes", 0),
                            sha256=side.get("sha256", ""),
                            feature_count=int((side.get("extra") or {}).get("feature_count", 0)),
                            active_only=active_only,
                            was_cached=True,
                            source_url=BASE_URL,
                        )

        if use_mock:
            features = _mock_features(active_only=active_only)
            used_mock = True
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock."
                )
            features = _fetch_all_pages(active_only=active_only)
            used_mock = False

        body = json.dumps(
            {"type": "FeatureCollection", "features": features},
            separators=(",", ":"),
        ).encode("utf-8")

        return _persist(body, s, active_only=active_only, used_mock=used_mock)


# ---------------------------------------------------------------------------
# Pagination + normalisation.
# ---------------------------------------------------------------------------


def _fetch_all_pages(*, active_only: bool) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE - 1
        url = f"{BASE_URL}/ROWS/{start}:{end}/JSON"
        logger.info(
            "fetching TRI_FACILITY rows %d:%d (page %d)",
            start,
            end,
            page,
        )
        resp = requests.get(
            url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        try:
            rows = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"TRI endpoint returned non-JSON at offset {start}: {exc}") from exc
        if not isinstance(rows, list):
            raise RuntimeError(f"TRI endpoint returned an unexpected shape at offset {start}")
        if not rows:
            break

        for row in rows:
            feat = _to_feature(row, active_only=active_only)
            if feat is not None:
                features.append(feat)

        # Less than a full page → we've reached the end.
        if len(rows) < PAGE_SIZE:
            break

    else:  # pragma: no cover — safety valve
        logger.warning("TRI pagination hit MAX_PAGES=%d", MAX_PAGES)
    return features


def _to_feature(row: dict[str, Any], *, active_only: bool) -> dict[str, Any] | None:
    """Turn one TRI_FACILITY row into a GeoJSON Feature, or ``None``
    if it has no usable coordinate or is filtered out."""
    if active_only and str(row.get("fac_closed_ind") or "").strip() == "1":
        return None
    lat = row.get("pref_latitude")
    lon = row.get("pref_longitude")
    if lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (ValueError, TypeError):
        return None
    state = (row.get("state_abbr") or "").upper()
    lon_signed = lon_f if state in _EASTERN_HEMISPHERE_US else -lon_f

    props: dict[str, Any] = {k: row.get(k) for k in _KEPT_FIELDS if row.get(k) is not None}
    props["closed"] = str(row.get("fac_closed_ind") or "").strip() == "1"
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon_signed, lat_f]},
        "properties": props,
    }


# ---------------------------------------------------------------------------
# Cache write.
# ---------------------------------------------------------------------------


def _persist(
    body: bytes,
    storage: Storage,
    *,
    active_only: bool,
    used_mock: bool,
) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{RELATIVE_PATH}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    try:
        feature_count = len(json.loads(body).get("features") or [])
    except Exception:
        feature_count = 0

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            RELATIVE_PATH,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "publisher": "US EPA — Envirofacts TRI_FACILITY",
                "url": BASE_URL,
                "used_mock": used_mock,
            },
            tool={"name": "tri", "version": "1.0"},
            extra={
                "feature_count": feature_count,
                "active_only": active_only,
            },
            storage=storage,
        )

    return DownloadResult(
        absolute_path=final_path,
        relative_path=RELATIVE_PATH,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        feature_count=feature_count,
        active_only=active_only,
        was_cached=False,
        source_url=BASE_URL,
        used_mock=used_mock,
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


def _mock_features(*, active_only: bool) -> list[dict[str, Any]]:
    """Small offline set — one active facility in each of 5 regions,
    plus one closed facility so ``active_only=False`` covers both cases."""
    base = [
        # tri_id, name, city, state, parent, lat, lon_unsigned, closed
        (
            "NYCHEM001",
            "HUDSON CHEMICAL CORP",
            "TROY",
            "NY",
            "HUDSON INDUSTRIES INC",
            42.7284,
            73.6918,
            False,
        ),
        (
            "CAREFIN42",
            "PACIFIC REFINING COMPANY",
            "MARTINEZ",
            "CA",
            "PACIFIC ENERGY CORP",
            38.0194,
            122.1341,
            False,
        ),
        (
            "TXSTEEL07",
            "LONE STAR STEEL",
            "HOUSTON",
            "TX",
            "LONE STAR HOLDINGS",
            29.7604,
            95.3698,
            False,
        ),
        (
            "GUPACKGING",
            "PACIFIC PACKAGING HAGATNA",
            "HAGATNA",
            "GU",
            "PACIFIC PKG INC",
            13.4443,
            144.7937,
            False,  # real east-hemisphere
        ),
        (
            "ILTEXTIL9X",
            "MIDWEST TEXTILES INC",
            "CHICAGO",
            "IL",
            "MIDWEST HOLDINGS LLC",
            41.8781,
            87.6298,
            False,
        ),
        (
            "PAABANDON1",
            "DEFUNCT WIDGETS CO",
            "PHILADELPHIA",
            "PA",
            "DEFUNCT HOLDINGS",
            39.9526,
            75.1652,
            True,  # closed
        ),
    ]
    features: list[dict[str, Any]] = []
    for tri_id, name, city, state, parent, lat, lon_u, closed in base:
        if active_only and closed:
            continue
        lon = lon_u if state in _EASTERN_HEMISPHERE_US else -lon_u
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "tri_facility_id": tri_id,
                    "facility_name": name,
                    "city_name": city,
                    "state_abbr": state,
                    "parent_co_name": parent,
                    "closed": closed,
                },
            }
        )
    return features
