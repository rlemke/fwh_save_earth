"""Active fire / thermal anomaly detections (NASA FIRMS) → one cached GeoJSON.

    cache/save-earth/active_fire/active_fire.geojson + .meta.json

**What this data actually is.** FIRMS reports *thermal anomalies* — pixels whose
infrared signature is far hotter than their surroundings. Most are vegetation
fires, but gas flares, industrial heat, active lava and agricultural burning all
register identically. Nothing in the feed says which is which, so this module
does **not** claim to classify them: every detection is kept and labelled by the
sensor's own confidence, and the map calls them thermal anomalies. Treating the
whole feed as "wildfires" would badly overstate fire activity over oil-producing
regions, where flares burn continuously.

**What absence means.** Detections are limited by satellite overpass and cloud
cover. A fire under thick cloud, or burning between passes, is simply not in the
feed. An empty area means "nothing detected", never "nothing burning".

Two keyless global 24-hour feeds are merged:

* **VIIRS S-NPP** (375 m) — the higher-resolution instrument; ``confidence`` is
  categorical (``low``/``nominal``/``high``).
* **MODIS Terra/Aqua** (1 km) — longer record; ``confidence`` is an integer
  percentage, banded here to the same three levels so one property works for
  both.

Both are normalised to a common property set (with the raw fields kept) and
merged into a single FeatureCollection **sorted by FRP descending**. That order
is load-bearing: the renderer caps inlined features with a plain slice, so
sorting by fire radiative power means a capped map keeps the most energetic
detections rather than an arbitrary chunk of the file.

``max_age_hours`` defaults to 1.0 — this is near-real-time data whose whole value
is currency, unlike the effectively static layers elsewhere in this package.
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
from dataclasses import dataclass
from datetime import UTC, datetime
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

logger = logging.getLogger("save-earth.wildfire")

NAMESPACE = "save-earth"
USER_AGENT = "facetwork-save-earth/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

CACHE_TYPE = "active_fire"
RELATIVE_PATH = "active_fire.geojson"
# NRT data: the whole point is currency. The upstream feeds refresh roughly
# hourly as overpasses are processed.
MAX_AGE_HOURS = 1.0

_FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"

# The keyless bulk feeds. (The /api/area/ endpoints need a per-user MAP_KEY;
# these do not, which is what lets this run unattended on the fleet.)
FEEDS: dict[str, dict[str, str]] = {
    "viirs_snpp": {
        "url": f"{_FIRMS_BASE}/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv",
        "sensor": "VIIRS",
        "platform": "Suomi-NPP",
        "resolution_m": "375",
        "brightness_field": "bright_ti4",
    },
    "modis": {
        "url": f"{_FIRMS_BASE}/modis-c6.1/csv/MODIS_C6_1_Global_24h.csv",
        "sensor": "MODIS",
        "platform": "Terra/Aqua",
        "resolution_m": "1000",
        "brightness_field": "brightness",
    },
}
DEFAULT_FEEDS = ("viirs_snpp", "modis")

# MODIS confidence is 0-100; VIIRS is low/nominal/high. Band both onto one
# property so a single LayerSpec filter works across sensors. Thresholds follow
# the FIRMS FAQ's own low/nominal/high split for MODIS.
_MODIS_LOW_MAX = 30
_MODIS_NOMINAL_MAX = 80

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
    # Per-band counts, so a caller can report "12 high / 900 nominal" without
    # re-reading and re-parsing a multi-megabyte FeatureCollection.
    band_counts: dict[str, int] | None = None
    sensor_counts: dict[str, int] | None = None
    acquired_from: str = ""
    acquired_to: str = ""


def download_active_fire(
    *,
    force: bool = False,
    max_age_hours: float = MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
    feeds: tuple[str, ...] = DEFAULT_FEEDS,
) -> DownloadResult:
    """Fetch the FIRMS 24h global feeds → one normalised, cached GeoJSON."""
    s = storage or get_storage()
    urls = ",".join(FEEDS[f]["url"] for f in feeds if f in FEEDS)
    cached = _cache_hit(max_age_hours, force, s, urls)
    if cached is not None:
        return cached

    features: list[dict[str, Any]] = []
    if use_mock:
        features = _mock_features()
        source_url, used_mock = "mock://firms", True
    else:
        for key in feeds:
            meta = FEEDS.get(key)
            if meta is None:
                raise ValueError(f"unknown FIRMS feed {key!r}; known: {sorted(FEEDS)}")
            text = _fetch_csv(meta["url"])
            got = _to_features(text, meta)
            logger.info("%s: %d detections", key, len(got))
            features.extend(got)
        source_url, used_mock = urls, False

    # Sort by FRP descending — see the module docstring: the renderer's inline
    # cap is a plain slice, so this decides WHICH detections survive a capped
    # map. Strongest-first is the only defensible choice for a fire map.
    features.sort(key=lambda f: f["properties"].get("frp") or 0.0, reverse=True)

    fc = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "generated_at": datetime.now(UTC).isoformat(),
            # Per-sensor constants, recorded once rather than on every feature.
            "sensors": {
                FEEDS[k]["sensor"]: {
                    "platform": FEEDS[k]["platform"],
                    "resolution_m": int(FEEDS[k]["resolution_m"]),
                    "feed_url": FEEDS[k]["url"],
                }
                for k in feeds if k in FEEDS
            },
            "note": (
                "NASA FIRMS active fire / thermal anomaly detections, past 24h. "
                "Detections include gas flares, industrial heat and agricultural "
                "burning as well as wildfires; absence of a detection does not "
                "mean absence of fire (cloud cover and overpass gaps)."
            ),
        },
    }
    body = json.dumps(fc, separators=(",", ":")).encode("utf-8")
    return _persist(
        body, s,
        source={
            "publisher": "NASA FIRMS (Fire Information for Resource Management System)",
            "url": source_url,
            "license": "public domain (US Government); cite NASA FIRMS/LANCE",
            "used_mock": used_mock,
        },
        source_url=source_url,
        used_mock=used_mock,
        features=features,
    )


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------


def _fetch_csv(url: str) -> str:
    if requests is None:  # pragma: no cover
        raise RuntimeError("requests is required for FIRMS downloads")
    logger.info("GET %s", url)
    resp = requests.get(
        url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), headers={"User-Agent": USER_AGENT}
    )
    resp.raise_for_status()
    return resp.text


def _band(raw: str, sensor: str) -> str:
    """Normalise per-sensor confidence onto low/nominal/high."""
    v = (raw or "").strip().lower()
    if sensor == "MODIS":
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            return "nominal"
        if n <= _MODIS_LOW_MAX:
            return "low"
        return "nominal" if n <= _MODIS_NOMINAL_MAX else "high"
    if v in ("low", "nominal", "high"):
        return v
    return "nominal"


def _acq_iso(date_s: str, time_s: str) -> str:
    """FIRMS splits acquisition into acq_date + acq_time ('HHMM', UTC)."""
    t = (time_s or "").strip().zfill(4)
    try:
        return f"{date_s}T{t[:2]}:{t[2:]}:00Z"
    except Exception:  # pragma: no cover - defensive
        return date_s


def _to_features(csv_text: str, meta: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sensor = meta["sensor"]
    bright_field = meta["brightness_field"]
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            lon = float(row["longitude"])
            lat = float(row["latitude"])
        except (TypeError, ValueError, KeyError):
            continue  # a malformed row must not sink the whole feed
        try:
            frp = float(row.get("frp") or 0.0)
        except ValueError:
            frp = 0.0
        raw_conf = row.get("confidence", "")
        # Only per-detection facts live on the feature. `platform`,
        # `resolution_m` and `version` are constant per sensor, so repeating
        # them 131k times cost ~12 MB of inlined payload for nothing — they are
        # recorded once in the collection's `sensors` metadata instead.
        props = {
            "sensor": sensor,
            "confidence_band": _band(raw_conf, sensor),
            "confidence_raw": raw_conf,
            "frp": round(frp, 2),
            "brightness_k": _float_or_none(row.get(bright_field)),
            "acquired_utc": _acq_iso(row.get("acq_date", ""), row.get("acq_time", "")),
            "daynight": "day" if (row.get("daynight") or "").upper() == "D" else "night",
            "satellite": row.get("satellite", ""),
        }
        out.append({
            "type": "Feature",
            # 5 dp ~ 1 m; the sensors resolve 375-1000 m, so more digits are
            # noise that only inflates the inlined payload.
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": props,
        })
    return out


def _float_or_none(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cache plumbing (mirrors seismic.py)
# ---------------------------------------------------------------------------


def _cache_hit(
    max_age_hours: float, force: bool, storage: Storage, source_url: str
) -> DownloadResult | None:
    if force:
        return None
    with _lock:
        side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
        if side and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage):
            age = _age_hours(side.get("generated_at"))
            if age is None or age < max_age_hours:
                extra = side.get("extra") or {}
                logger.info("active_fire cache hit (%.2fh old)", age if age is not None else -1.0)
                return DownloadResult(
                    absolute_path=sidecar.cache_path(
                        NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage
                    ),
                    relative_path=RELATIVE_PATH,
                    size_bytes=side.get("size_bytes", 0),
                    sha256=side.get("sha256", ""),
                    feature_count=int(extra.get("feature_count", 0)),
                    was_cached=True,
                    source_url=source_url,
                    band_counts=extra.get("band_counts") or {},
                    sensor_counts=extra.get("sensor_counts") or {},
                    acquired_from=extra.get("acquired_from", ""),
                    acquired_to=extra.get("acquired_to", ""),
                )
    return None


def _persist(
    body: bytes, storage: Storage, *, source: dict[str, Any], source_url: str,
    used_mock: bool, features: list[dict[str, Any]],
) -> DownloadResult:
    staging = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging, exist_ok=True)
    stage_path = os.path.join(staging, f"{RELATIVE_PATH}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    bands: dict[str, int] = {}
    sensors: dict[str, int] = {}
    times: list[str] = []
    for feat in features:
        p = feat["properties"]
        bands[p["confidence_band"]] = bands.get(p["confidence_band"], 0) + 1
        sensors[p["sensor"]] = sensors.get(p["sensor"], 0) + 1
        if p.get("acquired_utc"):
            times.append(p["acquired_utc"])
    acquired_from = min(times) if times else ""
    acquired_to = max(times) if times else ""

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage)
    digest = hashlib.sha256(body).hexdigest()
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE, CACHE_TYPE, RELATIVE_PATH,
            kind="file", size_bytes=len(body), sha256=digest,
            source=source,
            tool={"name": "wildfire.active_fire", "version": "1.0"},
            extra={
                "feature_count": len(features),
                "band_counts": bands,
                "sensor_counts": sensors,
                "acquired_from": acquired_from,
                "acquired_to": acquired_to,
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
        source_url=source_url,
        used_mock=used_mock,
        band_counts=bands,
        sensor_counts=sensors,
        acquired_from=acquired_from,
        acquired_to=acquired_to,
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
    """Small offline set — three detections spanning all three bands."""
    rows = [
        (-120.51, 38.72, "high", 152.4, "VIIRS", "Suomi-NPP", 375, "day", 341.2),
        (133.88, -23.41, "nominal", 18.6, "VIIRS", "Suomi-NPP", 375, "night", 320.8),
        (25.14, -12.07, "low", 4.1, "MODIS", "Terra/Aqua", 1000, "day", 311.5),
    ]
    return [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "sensor": sensor,
                "confidence_band": band, "confidence_raw": band,
                "frp": frp, "brightness_k": bright,
                "acquired_utc": "2026-01-01T00:00:00Z",
                "daynight": dn, "satellite": "N",
            },
        }
        for lon, lat, band, frp, sensor, _platform, _res, dn, bright in rows
    ]
