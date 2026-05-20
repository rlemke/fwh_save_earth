"""EPA cleanup sites — Superfund and Brownfields.

The EPA publishes authoritative remediation-site data via ArcGIS REST
MapServer endpoints under ``geopub.epa.gov``. Each site carries a name,
address, state, EPA region, and a ``facility_url`` back to the source
system.

This module wraps the fetch + cache pattern for each dataset; the
caller picks which dataset to download (``superfund`` or
``brownfields``). Each lands as its own cache entry:

    cache/save-earth/epa-cleanups/superfund.geojson     + .meta.json
    cache/save-earth/epa-cleanups/brownfields.geojson   + .meta.json

The MapServer caps single-query results at 10,000 features and signals
``exceededTransferLimit`` when there's more. This module transparently
paginates via ``resultOffset`` until the server reports no more data.

URLs are overridable per-dataset via ``--url``.  RCRA corrective-action
data is intentionally not included in the defaults — layer 4 on the
same server (Hazardous Waste / RCRAInfo) is millions of facilities and
does not cleanly distinguish corrective-action sites. Users who want
that can pass a suitable ``--url``.
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

logger = logging.getLogger("save-earth.epa-cleanups")

NAMESPACE = "save-earth"
CACHE_TYPE = "epa-cleanups"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
DEFAULT_MAX_AGE_HOURS = 24.0 * 7  # cleanup status moves slowly — a week is fine

# EPA's EnviroFacts map service — verified layers on ``EMEF/efpoints``:
#   Layer 0 = Superfund (NPL), Layer 5 = Brownfields (ACRES).
# MapServer ``query`` endpoints return valid GeoJSON when ``f=geojson``.
# Single queries cap at 10,000 features; we paginate via resultOffset.
_EMEF_BASE = "https://geopub.epa.gov/arcgis/rest/services/EMEF/efpoints/MapServer"

DEFAULT_URLS: dict[str, str] = {
    "superfund": (f"{_EMEF_BASE}/0/query?where=1%3D1&outFields=*&returnGeometry=true&f=geojson"),
    "brownfields": (f"{_EMEF_BASE}/5/query?where=1%3D1&outFields=*&returnGeometry=true&f=geojson"),
}

DATASET_CHOICES = tuple(DEFAULT_URLS)

# Page size when walking past exceededTransferLimit. 2000 is the largest
# commonly-honoured page size across EPA services; servers can cap lower.
PAGE_SIZE = 2000
MAX_PAGES = 200  # hard safety ceiling — 400k features

_lock = threading.Lock()


@dataclass
class FetchResult:
    dataset: str
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    source_url: str
    was_cached: bool
    generated_at: str
    used_mock: bool = False


def download(
    dataset: str,
    *,
    url: str | None = None,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> FetchResult:
    """Fetch one EPA dataset and cache it."""
    if dataset not in DEFAULT_URLS:
        raise ValueError(f"dataset must be one of {list(DEFAULT_URLS)}, got {dataset!r}")
    effective_url = url or DEFAULT_URLS[dataset]
    relative_path = f"{dataset}.geojson"
    s = storage or LocalStorage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, relative_path, s)
            if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, relative_path, s):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info("epa-cleanups/%s cache hit (%.1fh old)", dataset, age or -1.0)
                    return FetchResult(
                        dataset=dataset,
                        absolute_path=art_path,
                        relative_path=relative_path,
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        feature_count=int(side.get("extra", {}).get("feature_count", 0)),
                        source_url=effective_url,
                        was_cached=True,
                        generated_at=side.get("generated_at", ""),
                    )

        if use_mock:
            body = json.dumps(_mock_geojson(dataset)).encode("utf-8")
            return _persist(dataset, body, url=effective_url, storage=s, used_mock=True)

        if requests is None:
            raise RuntimeError(
                "requests library is not installed. Install it, run via the .sh "
                "wrapper (activates .venv), or pass --use-mock."
            )

        logger.info("downloading epa-cleanups/%s from %s", dataset, effective_url)
        data = _fetch_all_pages(effective_url, dataset=dataset)
        body_bytes = json.dumps(data).encode("utf-8")
        return _persist(dataset, body_bytes, url=effective_url, storage=s, used_mock=False)


def _persist(
    dataset: str,
    body: bytes,
    *,
    url: str,
    storage: Storage,
    used_mock: bool,
) -> FetchResult:
    relative_path = f"{dataset}.geojson"
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{dataset}.geojson.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)
    digest = hashlib.sha256(body).hexdigest()

    try:
        parsed = json.loads(body)
        feature_count = len(parsed.get("features") or [])
    except Exception:
        feature_count = 0

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body),
            sha256=digest,
            source={
                "publisher": "US EPA",
                "url": url,
                "dataset": dataset,
                "used_mock": used_mock,
            },
            tool={"name": "epa_cleanups", "version": "1.0"},
            extra={"feature_count": feature_count, "dataset": dataset},
            storage=storage,
        )
    return FetchResult(
        dataset=dataset,
        absolute_path=final_path,
        relative_path=relative_path,
        size_bytes=len(body),
        sha256=digest,
        feature_count=feature_count,
        source_url=url,
        was_cached=False,
        generated_at=side["generated_at"],
        used_mock=used_mock,
    )


def _fetch_all_pages(base_url: str, *, dataset: str) -> dict[str, Any]:
    """Walk ``resultOffset`` pages until the server stops signalling
    ``exceededTransferLimit``. Concatenates all features into one
    FeatureCollection. Raises if the server returns an unrecognised shape.
    """
    separator = "&" if "?" in base_url else "?"
    all_features: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        url = f"{base_url}{separator}resultOffset={offset}&resultRecordCount={PAGE_SIZE}"
        resp = requests.get(
            url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        )
        resp.raise_for_status()
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"EPA endpoint returned non-JSON for {dataset} at offset {offset}: {exc}"
            ) from exc

        if isinstance(parsed, dict) and "error" in parsed:
            # ArcGIS MapServer error envelope — surface the message.
            err = parsed["error"]
            raise RuntimeError(
                f"EPA server error for {dataset} at offset {offset}: {err.get('message', err)}"
            )

        if not (isinstance(parsed, dict) and isinstance(parsed.get("features"), list)):
            raise ValueError(
                f"EPA endpoint returned an unexpected shape for {dataset} "
                f"at offset {offset} (no 'features' array)"
            )

        page_features = parsed["features"]
        all_features.extend(page_features)
        logger.info(
            "epa-cleanups/%s page %d: %d features (total %d)",
            dataset,
            page,
            len(page_features),
            len(all_features),
        )

        # Stop when server signals no more pages, or the current page
        # underflowed PAGE_SIZE (last page), or it's empty.
        more = bool(parsed.get("exceededTransferLimit"))
        if not more or len(page_features) < PAGE_SIZE:
            break
    else:
        logger.warning(
            "epa-cleanups/%s: hit MAX_PAGES=%d — truncating at %d features",
            dataset,
            MAX_PAGES,
            len(all_features),
        )

    return {"type": "FeatureCollection", "features": all_features}


def _age_hours(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    from datetime import datetime

    try:
        ts = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _mock_geojson(dataset: str) -> dict[str, Any]:
    """Small hand-crafted feature set for offline tests.

    Field names match the real EPA EMEF/efpoints schema so downstream
    code (map popups, tests) exercises the same paths in mock + live.
    """
    if dataset == "superfund":
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-73.7549, 42.6511]},
                "properties": {
                    "registry_id": "110000313541",
                    "primary_name": "GENERAL ELECTRIC HUDSON RIVER PCBS",
                    "location_address": "RIVER RD",
                    "city_name": "HUDSON FALLS",
                    "county_name": "WASHINGTON",
                    "state_code": "NY",
                    "epa_region": "02",
                    "pgm_sys_acrnm": "SEMS",
                    "pgm_sys_id": "0202229",
                    "facility_url": "https://enviro.epa.gov/enviro/fii_query_dtl.disp_program_facility?pgm_sys_id_in=0202229",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-122.2711, 37.8044]},
                "properties": {
                    "registry_id": "110000456789",
                    "primary_name": "ALAMEDA NAVAL AIR STATION",
                    "location_address": "950 W MALL SQUARE",
                    "city_name": "ALAMEDA",
                    "county_name": "ALAMEDA",
                    "state_code": "CA",
                    "epa_region": "09",
                    "pgm_sys_acrnm": "SEMS",
                    "pgm_sys_id": "0904231",
                    "facility_url": "https://enviro.epa.gov/enviro/fii_query_dtl.disp_program_facility?pgm_sys_id_in=0904231",
                },
            },
        ]
    elif dataset == "brownfields":
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-71.0589, 42.3601]},
                "properties": {
                    "registry_id": "110070123456",
                    "primary_name": "CHARLESTOWN NAVY YARD",
                    "location_address": "13TH ST",
                    "city_name": "BOSTON",
                    "county_name": "SUFFOLK",
                    "state_code": "MA",
                    "epa_region": "01",
                    "pgm_sys_acrnm": "ACRES",
                    "pgm_sys_id": "B01234567",
                    "facility_url": "https://cumulis.epa.gov/supercpad/cursites/csitinfo.cfm?id=B01234567",
                },
            },
        ]
    else:  # pragma: no cover
        features = []

    return {"type": "FeatureCollection", "features": features}
