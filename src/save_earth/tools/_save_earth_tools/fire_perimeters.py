"""US wildfire perimeters (NIFC / WFIGS) → one cached GeoJSON.

    cache/save-earth/fire_perimeters/fire_perimeters.geojson + .meta.json

The companion to :mod:`wildfire`, and a fundamentally different kind of data.
FIRMS gives satellite *thermal anomalies*: anonymous hot pixels, global, no
identity. This gives **incident polygons** — a named fire with acreage,
containment, cause and discovery date, mapped by the agencies working it.

**United States only.** WFIGS is the interagency (NIFC/IRWIN) feed; there is no
equivalent global perimeter source. On a world map this layer covers the US and
nothing else, which the layer titles and the map description must say — an empty
Siberia here means "not published", not "not burning".

**"Current" includes contained fires.** Of 231 perimeters observed, 60 were 100%
contained and 66 reported no containment figure at all. Drawing those in the same
style as an actively-burning fire would overstate the situation, so each feature
carries a derived ``status``:

* ``active``     — containment 0-99%
* ``contained``  — containment 100%
* ``unreported`` — no containment figure (grouped WITH active on the map, because
  absent data is not evidence of containment)

**The throttling trap.** The ArcGIS endpoint answers **HTTP 200 with a 429 error
inside the JSON body** when the shared quota is exceeded
(``"API calls quota exceeded … Retry after 60 sec"``). A client that checks only
the status code caches an error document as data. :func:`_fetch_page` therefore
inspects the body and backs off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _save_earth_tools import sidecar  # noqa: E402
from _save_earth_tools.storage import Storage, get_storage, local_staging_subdir  # noqa: E402

logger = logging.getLogger("save-earth.fire_perimeters")

NAMESPACE = "save-earth"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"

CACHE_TYPE = "fire_perimeters"
RELATIVE_PATH = "fire_perimeters.geojson"
# Perimeters are re-flown / re-mapped a few times a day, not continuously like a
# satellite overpass — so a longer window than wildfire's 1 h, but still short.
MAX_AGE_HOURS = 3.0

# WFIGS Current Interagency Fire Perimeters (the authoritative NIFC/IRWIN feed).
PERIMETER_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)
PAGE_SIZE = 1000  # server maxRecordCount is 2000; stay well under it
# Server-side geometry generalisation, in degrees. Perimeter polygons carry
# enormous vertex counts: the raw 231-perimeter response is 26.8 MB, which would
# have made the combined map ~37 MB. At 0.001 deg (~111 m) it is 0.74 MB — a 36x
# reduction with all 231 features retained, and below one screen pixel until deep
# zoom. These perimeters are aircraft/GPS-mapped with their own error and are not
# survey-grade, so a 111 m generalisation changes nothing a reader could act on —
# but it IS a generalisation: for operational use go to the WFIGS source.
MAX_ALLOWABLE_OFFSET = 0.001
MAX_PAGES = 40  # backstop: 40k perimeters is far beyond any real fire season
_RETRIES = 5
_RETRY_SLEEP_S = 35.0

# The subset worth carrying. The service returns 119 fields; inlining all of them
# for every polygon would bloat the map for no benefit.
_OUT_FIELDS = ",".join([
    "poly_IncidentName",
    "poly_GISAcres",
    "poly_DateCurrent",
    "attr_IncidentSize",
    "attr_PercentContained",
    "attr_FireCause",
    "attr_FireCauseGeneral",
    "attr_FireDiscoveryDateTime",
    "attr_POOState",
    "attr_IncidentTypeCategory",
])

_lock = threading.Lock()


@dataclass
class DownloadResult:
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    was_cached: bool
    source_url: str = ""
    used_mock: bool = False
    status_counts: dict[str, int] | None = None
    acres_total: float = 0.0


def download_perimeters(
    *,
    force: bool = False,
    max_age_hours: float = MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch current US fire perimeters (WFIGS) → normalised cached GeoJSON."""
    s = storage or get_storage()
    cached = _cache_hit(max_age_hours, force, s)
    if cached is not None:
        return cached

    if use_mock:
        features = _mock_features()
        used_mock = True
    else:
        features = _fetch_all()
        used_mock = False

    fc = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "generated_at": datetime.now(UTC).isoformat(),
            "note": (
                "US wildfire perimeters from the NIFC/WFIGS interagency feed. "
                "UNITED STATES ONLY — no equivalent global perimeter source "
                "exists, so an empty area outside the US means 'not published', "
                "not 'not burning'. Includes contained fires; see each feature's "
                "`status` and `percent_contained`."
            ),
        },
    }
    body = json.dumps(fc, separators=(",", ":")).encode("utf-8")
    return _persist(body, s, features=features, used_mock=used_mock)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def _fetch_page(offset: int) -> dict[str, Any]:
    """One page, retrying on the body-embedded 429.

    The endpoint returns HTTP **200** with ``{"error": {"code": 429, …}}`` when
    the shared ArcGIS quota is exhausted, so status-code-only error handling
    silently caches an error document. Inspect the body.
    """
    params = {
        "where": "1=1",
        "outFields": _OUT_FIELDS,
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "maxAllowableOffset": str(MAX_ALLOWABLE_OFFSET),
        "f": "geojson",
    }
    url = f"{PERIMETER_URL}?{urllib.parse.urlencode(params)}"
    last = ""
    for attempt in range(1, _RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                doc = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            logger.warning("perimeter page %d attempt %d failed: %s", offset, attempt, last)
            time.sleep(_RETRY_SLEEP_S)
            continue
        err = doc.get("error")
        if not err:
            return doc
        last = f"{err.get('code')} {err.get('message')}"
        logger.warning(
            "perimeter page %d attempt %d throttled (HTTP 200 body error): %s",
            offset, attempt, last,
        )
        time.sleep(_RETRY_SLEEP_S)
    raise RuntimeError(f"WFIGS perimeter query failed after {_RETRIES} attempts: {last}")


def _fetch_all() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        doc = _fetch_page(page * PAGE_SIZE)
        feats = doc.get("features") or []
        logger.info("perimeter page %d: %d features", page, len(feats))
        for f in feats:
            norm = _normalise(f)
            if norm is not None:
                out.append(norm)
        if len(feats) < PAGE_SIZE:
            break
    else:
        logger.warning("hit MAX_PAGES=%d — perimeter set may be truncated", MAX_PAGES)
    return out


def _epoch_ms_to_iso(v: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(v) / 1000.0, UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _status_of(pct: Any) -> str:
    """Derive a status. Unreported containment is NOT treated as contained."""
    if pct is None or pct == "":
        return "unreported"
    try:
        return "contained" if float(pct) >= 100.0 else "active"
    except (TypeError, ValueError):
        return "unreported"


def _normalise(feat: dict[str, Any]) -> dict[str, Any] | None:
    geom = feat.get("geometry")
    if not geom or not geom.get("coordinates"):
        return None  # a perimeter with no polygon is not renderable
    p = feat.get("properties") or {}
    pct = p.get("attr_PercentContained")
    acres = p.get("attr_IncidentSize")
    if acres in (None, ""):
        acres = p.get("poly_GISAcres")
    try:
        acres = round(float(acres), 1)
    except (TypeError, ValueError):
        acres = None
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "incident_name": p.get("poly_IncidentName") or "(unnamed)",
            "status": _status_of(pct),
            "percent_contained": pct,
            "acres": acres,
            "cause": p.get("attr_FireCause") or p.get("attr_FireCauseGeneral"),
            "state": p.get("attr_POOState"),
            "incident_type": p.get("attr_IncidentTypeCategory"),
            "discovered_utc": _epoch_ms_to_iso(p.get("attr_FireDiscoveryDateTime")),
            "perimeter_updated_utc": _epoch_ms_to_iso(p.get("poly_DateCurrent")),
        },
    }


# ---------------------------------------------------------------------------
# Cache plumbing
# ---------------------------------------------------------------------------


def _cache_hit(max_age_hours: float, force: bool, storage: Storage) -> DownloadResult | None:
    if force:
        return None
    with _lock:
        side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
        if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage):
            age = _age_hours(side.get("generated_at"))
            if age is None or age < max_age_hours:
                extra = side.get("extra") or {}
                logger.info("fire_perimeters cache hit (%.2fh old)", age if age is not None else -1.0)
                return DownloadResult(
                    absolute_path=sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage),
                    relative_path=RELATIVE_PATH,
                    size_bytes=side.get("size_bytes", 0),
                    sha256=side.get("sha256", ""),
                    feature_count=int(extra.get("feature_count", 0)),
                    was_cached=True,
                    source_url=PERIMETER_URL,
                    status_counts=extra.get("status_counts") or {},
                    acres_total=float(extra.get("acres_total") or 0.0),
                )
    return None


def _persist(
    body: bytes, storage: Storage, *, features: list[dict[str, Any]], used_mock: bool
) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{RELATIVE_PATH}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    statuses: dict[str, int] = {}
    acres_total = 0.0
    for feat in features:
        st = feat["properties"]["status"]
        statuses[st] = statuses.get(st, 0) + 1
        acres_total += feat["properties"].get("acres") or 0.0

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
    digest = hashlib.sha256(body).hexdigest()
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE, CACHE_TYPE, RELATIVE_PATH,
            kind="file", size_bytes=len(body), sha256=digest,
            source={
                "publisher": "NIFC / WFIGS (Wildland Fire Interagency Geospatial Services)",
                "url": PERIMETER_URL,
                "license": "public domain (US Government)",
                "coverage": "United States only",
                "used_mock": used_mock,
            },
            tool={"name": "fire_perimeters", "version": "1.0"},
            extra={
                "feature_count": len(features),
                "status_counts": statuses,
                "acres_total": round(acres_total, 1),
            },
            storage=storage,
        )

    return DownloadResult(
        absolute_path=final_path,
        relative_path=RELATIVE_PATH,
        size_bytes=len(body),
        sha256=digest,
        feature_count=len(features),
        was_cached=False,
        source_url=PERIMETER_URL,
        used_mock=used_mock,
        status_counts=statuses,
        acres_total=round(acres_total, 1),
    )


def _age_hours(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    try:
        ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _mock_features() -> list[dict[str, Any]]:
    """Three perimeters, one per status — offline, no network."""
    rows = [
        ("Mock Ridge Fire", "active", 45, 12000.0, "US-CA"),
        ("Mock Canyon Fire", "contained", 100, 850.0, "US-OR"),
        ("Mock Mesa Fire", "unreported", None, 300.0, "US-NM"),
    ]
    out = []
    for i, (name, status, pct, acres, state) in enumerate(rows):
        x, y = -120.0 + i, 40.0 + i
        out.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [x, y], [x + 0.4, y], [x + 0.4, y + 0.3], [x, y + 0.3], [x, y]
            ]]},
            "properties": {
                "incident_name": name, "status": status, "percent_contained": pct,
                "acres": acres, "cause": "Human", "state": state,
                "incident_type": "WF",
                "discovered_utc": "2026-01-01T00:00:00+00:00",
                "perimeter_updated_utc": "2026-01-02T00:00:00+00:00",
            },
        })
    return out
